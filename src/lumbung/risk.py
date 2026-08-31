"""Position sizing and the hard risk gates.

Every order path -- backtest, paper, live -- calls into this module. There is no
override flag and no "just this once" branch: if a gate says no, no order is sent.

Money is in IDR throughout. Times are WIB (Asia/Jakarta), which is what the daily
loss limit resets on.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum

from .config import Config
from .strategy.base import Signal

WIB = timezone(timedelta(hours=7))


class Gate(StrEnum):
    OK = "ok"
    HALTED = "halted"
    MAX_DRAWDOWN = "max_drawdown"
    DAILY_LOSS_LIMIT = "daily_loss_limit"
    MAX_POSITIONS = "max_positions"
    MAX_EXPOSURE = "max_exposure"
    NOT_WHITELISTED = "not_whitelisted"
    BELOW_MIN_NOTIONAL = "below_min_notional"
    INSUFFICIENT_CASH = "insufficient_cash"
    BAD_SIGNAL = "bad_signal"


@dataclass(frozen=True)
class GateResult:
    gate: Gate
    detail: str = ""

    @property
    def allowed(self) -> bool:
        return self.gate is Gate.OK

    def __bool__(self) -> bool:  # so `if gate_result:` reads naturally
        return self.allowed


@dataclass(frozen=True)
class Sizing:
    qty: float
    notional: float
    stop: float
    risk_idr: float
    capped_by: str  # "risk" | "position_cap" | "cash"


@dataclass
class RiskState:
    """A snapshot the gates reason about. The engine rebuilds this each loop."""

    equity: float
    peak_equity: float
    day_start_equity: float
    cash_idr: float
    open_positions: int
    exposure_idr: float
    day_key: str  # 'YYYY-MM-DD' in WIB
    halted: bool = False
    halt_reason: str = ""

    @property
    def drawdown_pct(self) -> float:
        if self.peak_equity <= 0:
            return 0.0
        return max(0.0, (self.peak_equity - self.equity) / self.peak_equity)

    @property
    def day_pnl_pct(self) -> float:
        if self.day_start_equity <= 0:
            return 0.0
        return (self.equity - self.day_start_equity) / self.day_start_equity


def resume_after_halt(state: RiskState) -> RiskState:
    """Clear a halt and rebase the equity peak to the CURRENT equity.

    Without the rebase, resuming re-trips the drawdown gate on the very next
    tick -- the peak is still the old high, so the drawdown is still >= the
    limit and the engine deadlocks. Resuming is a human decision (`/resume`),
    and it means "this is my new starting line".
    """
    state.halted = False
    state.halt_reason = ""
    state.peak_equity = state.equity
    state.day_start_equity = state.equity
    return state


def wib_day_key(ts: float | None = None) -> str:
    dt = datetime.fromtimestamp(ts, tz=WIB) if ts else datetime.now(WIB)
    return dt.strftime("%Y-%m-%d")


class RiskManager:
    def __init__(self, cfg: Config, *, enable_dd_halt: bool = True) -> None:
        self.cfg = cfg
        self.risk = cfg.risk
        self.sleeve = cfg.capital.sleeve_idr
        self.whitelist = set(cfg.universe.pairs)
        # Sweeps disable this so a mid-window drawdown does not silently freeze
        # the rest of the run and make two parameter sets incomparable.
        self.enable_dd_halt = enable_dd_halt

    # -- sizing ------------------------------------------------------------
    def size(
        self,
        signal: Signal,
        state: RiskState,
        *,
        amount_precision: int = 8,
        exchange_min_idr: float = 10_000.0,
    ) -> tuple[Sizing | None, GateResult]:
        """Convert a signal into a concrete quantity, or explain why not.

        qty = (sleeve * risk_per_trade) / (entry - stop), then capped by the
        per-position limit and by available cash.
        """
        risk_per_unit = signal.risk_per_unit
        if risk_per_unit <= 0 or signal.price <= 0:
            return None, GateResult(Gate.BAD_SIGNAL, "stop is at or above entry")

        risk_budget = self.sleeve * self.risk.risk_per_trade_pct
        qty = risk_budget / risk_per_unit
        capped_by = "risk"

        position_cap = self.sleeve * self.risk.max_position_pct
        if qty * signal.price > position_cap:
            qty = position_cap / signal.price
            capped_by = "position_cap"

        # Leave headroom for fees so a fill can never overdraw the IDR balance.
        spendable = state.cash_idr * 0.995
        if qty * signal.price > spendable:
            qty = spendable / signal.price
            capped_by = "cash"

        # Round DOWN so we never exceed a cap we just applied.
        factor = 10**amount_precision
        qty = math.floor(qty * factor) / factor

        notional = qty * signal.price
        floor_idr = max(self.risk.min_notional_idr, exchange_min_idr)
        if qty <= 0 or notional < floor_idr:
            return None, GateResult(
                Gate.BELOW_MIN_NOTIONAL,
                f"notional Rp {notional:,.0f} < floor Rp {floor_idr:,.0f}",
            )

        return (
            Sizing(
                qty=qty,
                notional=notional,
                stop=signal.stop or 0.0,
                risk_idr=qty * risk_per_unit,
                capped_by=capped_by,
            ),
            GateResult(Gate.OK),
        )

    # -- gates -------------------------------------------------------------
    def check_halt(self, state: RiskState) -> GateResult:
        """Account-level stops. These also block *management* of new entries."""
        if state.halted:
            return GateResult(Gate.HALTED, state.halt_reason or "engine halted")
        if self.enable_dd_halt and state.drawdown_pct >= self.risk.max_drawdown_pct:
            return GateResult(
                Gate.MAX_DRAWDOWN,
                f"drawdown {state.drawdown_pct * 100:.2f}% >= "
                f"{self.risk.max_drawdown_pct * 100:.2f}% limit",
            )
        return GateResult(Gate.OK)

    def check_entry(self, signal: Signal, state: RiskState) -> GateResult:
        """All pre-trade gates for opening a NEW position, in severity order."""
        halt = self.check_halt(state)
        if not halt:
            return halt

        if signal.pair not in self.whitelist:
            return GateResult(Gate.NOT_WHITELISTED, f"{signal.pair} not in universe")

        if state.day_pnl_pct <= -self.risk.daily_loss_limit_pct:
            return GateResult(
                Gate.DAILY_LOSS_LIMIT,
                f"day P&L {state.day_pnl_pct * 100:.2f}% <= "
                f"-{self.risk.daily_loss_limit_pct * 100:.2f}% (resets 00:00 WIB)",
            )

        if state.open_positions >= self.risk.max_concurrent_positions:
            return GateResult(
                Gate.MAX_POSITIONS,
                f"{state.open_positions}/{self.risk.max_concurrent_positions} positions open",
            )

        max_exposure = self.sleeve * self.risk.max_total_exposure_pct
        if state.exposure_idr >= max_exposure:
            return GateResult(
                Gate.MAX_EXPOSURE,
                f"exposure Rp {state.exposure_idr:,.0f} >= cap Rp {max_exposure:,.0f}",
            )

        if state.cash_idr < max(self.risk.min_notional_idr, 10_000.0):
            return GateResult(
                Gate.INSUFFICIENT_CASH, f"cash Rp {state.cash_idr:,.0f} too low to trade"
            )

        return GateResult(Gate.OK)

    def should_liquidate(self, state: RiskState) -> GateResult:
        """True when the drawdown limit demands we go flat, not merely stop buying."""
        if self.enable_dd_halt and state.drawdown_pct >= self.risk.max_drawdown_pct:
            return GateResult(
                Gate.MAX_DRAWDOWN,
                f"drawdown {state.drawdown_pct * 100:.2f}% -- flatten and halt",
            )
        return GateResult(Gate.OK)
