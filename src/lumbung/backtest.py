"""Event-driven backtester.

Two rules keep the numbers honest:

1. **No look-ahead.** An entry signal fires on bar `i`'s close and fills on bar
   `i+1`'s open. Only stop and take-profit triggers act intrabar, because those
   are resting orders that genuinely would have executed.
2. **Same costs, same gates as live.** Fees, the 0.21% sell tax, slippage and
   every risk gate come from the same config the live engine reads. A backtest
   that skips the gates is describing a bot you are not going to run.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .config import Config
from .risk import RiskManager, RiskState, wib_day_key
from .strategy.donchian_trend import DonchianTrend, PositionState

log = logging.getLogger(__name__)


@dataclass
class Fill:
    time: int
    price: float
    qty: float
    fee: float
    reason: str


@dataclass
class Trade:
    pair: str
    entry_time: int
    entry_price: float
    qty: float
    initial_stop: float
    initial_risk_idr: float
    entry_fee: float
    exits: list[Fill] = field(default_factory=list)

    @property
    def closed(self) -> bool:
        return abs(self.qty_remaining) < 1e-12

    @property
    def qty_remaining(self) -> float:
        return self.qty - sum(f.qty for f in self.exits)

    @property
    def exit_time(self) -> int:
        return self.exits[-1].time if self.exits else 0

    @property
    def realized_pnl(self) -> float:
        """Net of every fee and tax on both sides."""
        proceeds = sum(f.price * f.qty - f.fee for f in self.exits)
        cost_basis = self.entry_price * sum(f.qty for f in self.exits)
        entry_fee_share = (
            self.entry_fee * (sum(f.qty for f in self.exits) / self.qty) if self.qty else 0.0
        )
        return proceeds - cost_basis - entry_fee_share

    @property
    def r_multiple(self) -> float:
        return self.realized_pnl / self.initial_risk_idr if self.initial_risk_idr > 0 else 0.0

    @property
    def exit_reason(self) -> str:
        return self.exits[-1].reason if self.exits else ""

    @property
    def duration_hours(self) -> float:
        return (self.exit_time - self.entry_time) / 3600 if self.exits else 0.0


@dataclass
class BacktestResult:
    equity: pd.Series
    trades: list[Trade]
    config: Config
    halted_at: int | None = None
    halt_reason: str = ""

    # -- headline metrics --------------------------------------------------
    @property
    def start_equity(self) -> float:
        return float(self.equity.iloc[0]) if len(self.equity) else 0.0

    @property
    def end_equity(self) -> float:
        return float(self.equity.iloc[-1]) if len(self.equity) else 0.0

    @property
    def total_return_pct(self) -> float:
        return (self.end_equity / self.start_equity - 1) * 100 if self.start_equity else 0.0

    @property
    def max_drawdown_pct(self) -> float:
        if not len(self.equity):
            return 0.0
        dd = (self.equity / self.equity.cummax() - 1) * 100
        return float(dd.min())

    @property
    def closed_trades(self) -> list[Trade]:
        return [t for t in self.trades if t.exits]

    @property
    def win_rate_pct(self) -> float:
        ct = self.closed_trades
        if not ct:
            return 0.0
        return 100 * sum(1 for t in ct if t.realized_pnl > 0) / len(ct)

    @property
    def profit_factor(self) -> float:
        wins = sum(t.realized_pnl for t in self.closed_trades if t.realized_pnl > 0)
        losses = -sum(t.realized_pnl for t in self.closed_trades if t.realized_pnl < 0)
        if losses <= 0:
            return float("inf") if wins > 0 else 0.0
        return wins / losses

    @property
    def avg_r(self) -> float:
        ct = self.closed_trades
        return float(np.mean([t.r_multiple for t in ct])) if ct else 0.0

    @property
    def months(self) -> float:
        if len(self.equity) < 2:
            return 0.0
        span = (self.equity.index[-1] - self.equity.index[0]).total_seconds()
        return span / (30.44 * 86400)

    @property
    def monthly_return_pct(self) -> float:
        """Geometric mean monthly return -- what the subscription goal needs."""
        m = self.months
        if m <= 0 or self.start_equity <= 0 or self.end_equity <= 0:
            return 0.0
        return ((self.end_equity / self.start_equity) ** (1 / m) - 1) * 100

    def monthly_table(self) -> pd.DataFrame:
        if not len(self.equity):
            return pd.DataFrame()
        m = self.equity.resample("ME").last()
        first = pd.Series([self.start_equity], index=[self.equity.index[0]])
        m = pd.concat([first, m])
        ret = m.pct_change().dropna() * 100
        return pd.DataFrame({"month": ret.index.strftime("%Y-%m"), "return_pct": ret.values})

    def summary(self) -> dict:
        return {
            "months": round(self.months, 1),
            "start_equity": round(self.start_equity),
            "end_equity": round(self.end_equity),
            "total_return_pct": round(self.total_return_pct, 2),
            "monthly_return_pct": round(self.monthly_return_pct, 2),
            "max_drawdown_pct": round(self.max_drawdown_pct, 2),
            "trades": len(self.closed_trades),
            "win_rate_pct": round(self.win_rate_pct, 1),
            "profit_factor": round(self.profit_factor, 2),
            "avg_r": round(self.avg_r, 3),
            "halted_at": self.halted_at,
            "halt_reason": self.halt_reason,
        }


class Backtester:
    def __init__(
        self, cfg: Config, *, halt_is_terminal: bool = True, enable_dd_halt: bool = True
    ) -> None:
        self.cfg = cfg
        self.strategy = DonchianTrend(cfg.strategy)
        self.risk = RiskManager(cfg, enable_dd_halt=enable_dd_halt)
        self.costs = cfg.costs
        self.halt_is_terminal = halt_is_terminal
        # Turn the halt off only for parameter sweeps: a run that stops trading
        # halfway is not comparable to one that ran the full window.
        self.enable_dd_halt = enable_dd_halt

    def run(
        self, data: dict[str, pd.DataFrame], *, ticks: dict[str, float] | None = None
    ) -> BacktestResult:
        """`data` maps pair -> raw OHLCV frame (time-indexed, from candles.load)."""
        ticks = ticks or {}
        prepared = {p: self.strategy.prepare(df) for p, df in data.items() if not df.empty}
        if not prepared:
            raise ValueError("no candle data -- run `lumbung sync` first")

        # Unified bar clock across pairs, plus per-pair row lookup by timestamp.
        all_times = sorted({int(t) for df in prepared.values() for t in df["time"]})
        index_of = {p: {int(t): i for i, t in enumerate(df["time"])} for p, df in prepared.items()}

        sleeve = self.cfg.capital.sleeve_idr
        cash = sleeve
        positions: dict[str, PositionState] = {}
        trades: dict[str, Trade] = {}
        all_trades: list[Trade] = []
        pending_entries: dict[str, tuple] = {}  # pair -> (signal, sizing)

        equity_times: list[int] = []
        equity_vals: list[float] = []
        peak_equity = sleeve
        day_key = wib_day_key(all_times[0])
        day_start_equity = sleeve
        halted_at: int | None = None
        halt_reason = ""

        def mark_price(pair: str, t: int) -> float | None:
            i = index_of[pair].get(t)
            if i is None:
                return None
            return float(prepared[pair]["close"].iloc[i])

        def mark_to_market(t: int) -> float:
            total = cash
            for pair, pos in positions.items():
                px = mark_price(pair, t) or pos.entry_price
                total += pos.qty * px
            return total

        for t in all_times:
            # --- WIB day roll: reset the daily loss budget -------------------
            k = wib_day_key(t)
            if k != day_key:
                day_key = k
                day_start_equity = mark_to_market(t)

            # --- 1. fill entries queued on the previous bar ------------------
            for pair, (_signal, sizing) in list(pending_entries.items()):
                i = index_of[pair].get(t)
                if i is None:
                    continue
                del pending_entries[pair]
                if pair in positions or halted_at:
                    continue
                row = prepared[pair].iloc[i]
                tick = ticks.get(pair, 0.0)
                fill_px = float(row["open"]) + self.costs.slippage_ticks * tick
                qty = sizing.qty
                notional = qty * fill_px
                fee = notional * self.costs.buy_cost_pct(taker=False)
                if notional + fee > cash:  # price gapped up past our budget
                    continue
                cash -= notional + fee
                initial_risk = fill_px - sizing.stop
                if initial_risk <= 0:
                    cash += notional + fee
                    continue
                positions[pair] = PositionState(
                    pair=pair,
                    entry_price=fill_px,
                    qty=qty,
                    stop=sizing.stop,
                    initial_risk=initial_risk,
                    highest_close=fill_px,
                )
                tr = Trade(
                    pair=pair,
                    entry_time=t,
                    entry_price=fill_px,
                    qty=qty,
                    initial_stop=sizing.stop,
                    initial_risk_idr=qty * initial_risk,
                    entry_fee=fee,
                )
                trades[pair] = tr
                all_trades.append(tr)

            # --- 2. manage open positions ------------------------------------
            for pair in list(positions):
                i = index_of[pair].get(t)
                if i is None:
                    continue
                pos = positions[pair]
                df = prepared[pair]

                decision = self.strategy.check_exit(df, i, pos)
                if decision is not None:
                    tick = ticks.get(pair, 0.0)
                    qty_out = pos.qty if decision.fraction >= 1.0 else pos.qty * decision.fraction
                    qty_out = min(qty_out, pos.qty)
                    px = decision.price - self.costs.slippage_ticks * tick
                    proceeds = qty_out * px
                    fee = proceeds * self.costs.sell_cost_pct(taker=decision.urgent)
                    cash += proceeds - fee
                    trades[pair].exits.append(
                        Fill(time=t, price=px, qty=qty_out, fee=fee, reason=decision.kind)
                    )
                    pos.qty -= qty_out
                    if decision.kind == "partial_tp":
                        pos.partial_done = True
                        pos.stop = max(pos.stop, pos.entry_price)  # breakeven
                    if pos.qty <= 1e-12:
                        del positions[pair]
                        del trades[pair]
                        continue

                pos.highest_close = max(pos.highest_close, float(df["close"].iloc[i]))
                pos.stop = self.strategy.update_trail(df, i, pos)

            # --- 3. equity, peak, and the account-level halt -----------------
            eq = mark_to_market(t)
            equity_times.append(t)
            equity_vals.append(eq)
            peak_equity = max(peak_equity, eq)

            state = RiskState(
                equity=eq,
                peak_equity=peak_equity,
                day_start_equity=day_start_equity,
                cash_idr=cash,
                open_positions=len(positions),
                exposure_idr=eq - cash,
                day_key=day_key,
                halted=halted_at is not None,
            )

            if halted_at is None and self.enable_dd_halt:
                liq = self.risk.should_liquidate(state)
                if not liq:
                    halted_at, halt_reason = t, liq.detail
                    for pair in list(positions):
                        pos = positions[pair]
                        px = mark_price(pair, t) or pos.entry_price
                        proceeds = pos.qty * px
                        fee = proceeds * self.costs.sell_cost_pct(taker=True)
                        cash += proceeds - fee
                        trades[pair].exits.append(
                            Fill(t, px, pos.qty, fee, "drawdown_halt")
                        )
                        del positions[pair]
                        del trades[pair]
                    equity_vals[-1] = cash
                    if self.halt_is_terminal:
                        break
                    continue

            if halted_at is not None:
                continue

            # --- 4. look for new entries -------------------------------------
            for pair in prepared:
                if pair in positions or pair in pending_entries:
                    continue
                i = index_of[pair].get(t)
                if i is None or i + 1 >= len(prepared[pair]):
                    continue
                signal = self.strategy.entry_signal(prepared[pair], i, pair)
                if signal is None:
                    continue
                gate = self.risk.check_entry(signal, state)
                if not gate:
                    continue
                sizing, size_gate = self.risk.size(signal, state)
                if sizing is None or not size_gate:
                    continue
                pending_entries[pair] = (signal, sizing)
                # Reserve the cash so two pairs on the same bar can't spend it twice.
                state = RiskState(
                    **{
                        **state.__dict__,
                        "cash_idr": state.cash_idr - sizing.notional,
                        "open_positions": state.open_positions + 1,
                        "exposure_idr": state.exposure_idr + sizing.notional,
                    }
                )

        equity = pd.Series(
            equity_vals, index=pd.to_datetime(equity_times, unit="s", utc=True), name="equity"
        )
        return BacktestResult(
            equity=equity,
            trades=all_trades,
            config=self.cfg,
            halted_at=halted_at,
            halt_reason=halt_reason,
        )
