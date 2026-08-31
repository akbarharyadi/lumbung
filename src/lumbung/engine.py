"""The trading engine: one loop, shared by paper and live.

Two clocks run at once, and conflating them is the classic bug:

* **Bar clock (4h)** -- entry and exit *signals* are evaluated only when a new
  candle closes. Acting on a partially formed bar means live behaviour diverges
  from every backtest number.
* **Tick clock (~20s)** -- stop-loss monitoring, order fills and the chase logic
  run every loop, because Indodax has no server-side stop order and a stop that
  is only checked every 4 hours is not a stop.

The consequence of that second point is operational, not technical: **the stop
only exists while this process is running.** Keep it up, or accept unbounded
downside on an open position.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field

import pandas as pd

from .config import Config
from .data import candles as candles_mod
from .exchanges.indodax_private import base_currency
from .exchanges.indodax_public import IndodaxPublicClient
from .execution.broker import Broker, PaperBroker
from .journal import Journal, OpenPosition
from .notify.app import Notifier
from .risk import RiskManager, RiskState, resume_after_halt, wib_day_key
from .strategy.donchian_trend import DonchianTrend, PositionState

log = logging.getLogger(__name__)


@dataclass
class EntryAttempt:
    """A post-only entry being worked across loop iterations."""

    pair: str
    qty: float
    stop: float
    signal_price: float
    client_order_id: str
    exchange_order_id: str | None
    placed_at: float
    chases: int = 0
    filled_qty: float = 0.0


@dataclass
class EngineStatus:
    mode: str
    equity: float
    cash: float
    positions: int
    exposure: float
    drawdown_pct: float
    day_pnl_pct: float
    halted: bool
    halt_reason: str
    last_bar: str = ""
    open_pairs: list[str] = field(default_factory=list)


class Engine:
    def __init__(
        self,
        cfg: Config,
        journal: Journal,
        broker: Broker,
        public: IndodaxPublicClient,
        notifier: Notifier,
        *,
        mode: str = "paper",
    ) -> None:
        self.cfg = cfg
        self.j = journal
        self.broker = broker
        self.public = public
        self.notify = notifier
        self.mode = mode
        self.strategy = DonchianTrend(cfg.strategy)
        self.risk = RiskManager(cfg)
        self.tf = cfg.universe.timeframe
        self.pairs = [p for p in cfg.universe.pairs if public.is_tradable(p)]
        self.candles_conn = candles_mod.connect(cfg.db_path)
        self.entries: dict[str, EntryAttempt] = {}
        self._prepared: dict[str, pd.DataFrame] = {}
        self._last_bar_time: int = int(self.j.get_state("last_bar_time", 0))
        self.j.set_state("mode", mode)  # so `lumbung status` can report it

    # ------------------------------------------------------------------ state
    def _positions(self) -> dict[str, PositionState]:
        return {
            p: PositionState(
                pair=op.pair, entry_price=op.entry_price, qty=op.qty, stop=op.stop,
                initial_risk=op.initial_risk, highest_close=op.highest_close,
                partial_done=op.partial_done,
            )
            for p, op in self.j.positions().items()
        }

    def _save_position(self, pos: PositionState, opened_at: int | None = None) -> None:
        existing = self.j.positions().get(pos.pair)
        self.j.upsert_position(
            OpenPosition(
                pair=pos.pair, qty=pos.qty, entry_price=pos.entry_price, stop=pos.stop,
                initial_risk=pos.initial_risk, highest_close=pos.highest_close,
                partial_done=pos.partial_done,
                opened_at=opened_at or (existing.opened_at if existing else int(time.time())),
            )
        )

    def _mark_prices(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for pair in set(list(self._positions()) + self.pairs):
            try:
                out[pair] = self.public.last_price(pair)
            except Exception as exc:  # noqa: BLE001
                log.warning("price %s failed: %s", pair, exc)
        return out

    def risk_state(self, prices: dict[str, float] | None = None) -> RiskState:
        prices = prices if prices is not None else self._mark_prices()
        avail, hold = self.broker.balances()
        cash = avail.get("idr", 0.0) + hold.get("idr", 0.0)
        positions = self._positions()
        exposure = sum(
            pos.qty * prices.get(pair, pos.entry_price) for pair, pos in positions.items()
        )
        equity = cash + exposure

        peak = float(self.j.get_state("peak_equity", equity) or equity)
        peak = max(peak, equity)
        self.j.set_state("peak_equity", peak)

        day_key = wib_day_key()
        stored_day = self.j.get_state("day_key")
        if stored_day != day_key:
            self.j.set_state("day_key", day_key)
            self.j.set_state("day_start_equity", equity)
        day_start = float(self.j.get_state("day_start_equity", equity) or equity)

        return RiskState(
            equity=equity, peak_equity=peak, day_start_equity=day_start, cash_idr=cash,
            open_positions=len(positions), exposure_idr=exposure, day_key=day_key,
            halted=bool(self.j.get_state("halted", False)),
            halt_reason=str(self.j.get_state("halt_reason", "") or ""),
        )

    def status(self) -> EngineStatus:
        st = self.risk_state()
        return EngineStatus(
            mode=self.mode, equity=st.equity, cash=st.cash_idr, positions=st.open_positions,
            exposure=st.exposure_idr, drawdown_pct=st.drawdown_pct * 100,
            day_pnl_pct=st.day_pnl_pct * 100, halted=st.halted, halt_reason=st.halt_reason,
            last_bar=(
                pd.to_datetime(self._last_bar_time, unit="s", utc=True).strftime("%Y-%m-%d %H:%M")
                if self._last_bar_time else "-"
            ),
            open_pairs=sorted(self._positions()),
        )

    # ------------------------------------------------------------ reconcile
    def reconcile(self) -> None:
        """Compare the journal against the exchange before touching anything.

        A restart must never assume the journal is current: orders may have
        filled or been cancelled by hand while the process was down.
        """
        self.j.event("reconcile", f"starting in {self.mode} mode")

        for row in self.j.live_orders():
            st = self.broker.status(
                pair=row["pair"],
                order_id=row["exchange_order_id"] or "",
                client_order_id=row["client_order_id"],
            )
            if st.status in ("filled", "cancelled", "rejected"):
                self.j.update_order(
                    row["client_order_id"], status=st.status,
                    filled_qty=st.filled_qty, avg_fill_price=st.avg_price, fee=st.fee,
                )
                self.j.event("reconcile", f"order {row['client_order_id']} -> {st.status}")

        avail, hold = self.broker.balances()
        for pair, pos in self._positions().items():
            coin = base_currency(pair)
            held = avail.get(coin, 0.0) + hold.get(coin, 0.0)
            if held < pos.qty * 0.98:
                msg = (
                    f"{pair}: journal says {pos.qty:.8f} but exchange holds {held:.8f}. "
                    "Adopting the exchange balance -- it is the source of truth."
                )
                self.j.event("reconcile", msg, level="warning")
                self.notify.send(f"⚠️ Reconcile mismatch\n{msg}")
                if held <= 0:
                    self.j.delete_position(pair)
                    self.j.close_trade(pair, exit_price=0.0, pnl=0.0, reason="reconciled_away")
                else:
                    pos.qty = held
                    self._save_position(pos)

        self.j.event("reconcile", "complete")

    # ------------------------------------------------------------ market data
    def refresh_data(self) -> bool:
        """Pull any newly closed candles. Returns True if a new bar appeared."""
        newest = 0
        for pair in self.pairs:
            try:
                candles_mod.sync(self.public, self.candles_conn, pair, self.tf, months=6)
            except Exception as exc:  # noqa: BLE001
                log.warning("candle sync %s failed: %s", pair, exc)
            df = candles_mod.load(self.candles_conn, pair, self.tf)
            if df.empty:
                continue
            self._prepared[pair] = self.strategy.prepare(df)
            newest = max(newest, int(df["time"].iloc[-1]))

        if newest > self._last_bar_time:
            self._last_bar_time = newest
            self.j.set_state("last_bar_time", newest)
            return True
        return False

    # -------------------------------------------------------------- entries
    def _work_entry_orders(self) -> None:
        """Chase resting post-only entries; give up if price runs away."""
        ex = self.cfg.execution
        for pair, att in list(self.entries.items()):
            st = self.broker.status(
                pair=pair, order_id=att.exchange_order_id or "",
                client_order_id=att.client_order_id,
            )
            if st.status == "filled":
                self._on_entry_filled(att, st.filled_qty or att.qty, st.avg_price)
                del self.entries[pair]
                continue
            if st.status in ("cancelled", "rejected"):
                self.j.update_order(att.client_order_id, status=st.status)
                del self.entries[pair]
                continue

            if time.time() - att.placed_at < ex.entry_chase_wait_sec:
                continue

            try:
                book = self.public.book_top(pair)
            except Exception:  # noqa: BLE001
                continue

            drift = (book.ask - att.signal_price) / att.signal_price
            if drift > ex.entry_max_slip_pct or att.chases >= ex.entry_chase_max:
                self.broker.cancel(pair=pair, order_id=att.exchange_order_id or "", side="buy")
                self.j.update_order(att.client_order_id, status="cancelled")
                self.j.event(
                    "entry_abandoned",
                    f"{pair}: price drifted {drift * 100:.2f}% past signal "
                    f"after {att.chases} chases",
                )
                del self.entries[pair]
                continue

            self.broker.cancel(pair=pair, order_id=att.exchange_order_id or "", side="buy")
            self.j.update_order(att.client_order_id, status="cancelled")
            coid = _coid("ent")
            placed = self.broker.place_post_only(
                pair=pair, side="buy", qty=att.qty, price=book.bid, client_order_id=coid
            )
            self.j.record_order(
                client_order_id=coid, pair=pair, side="buy", order_type="limit", qty=att.qty,
                price=book.bid, purpose="entry", status="open" if placed.accepted else "rejected",
                exchange_order_id=placed.exchange_order_id,
            )
            if placed.accepted:
                att.client_order_id = coid
                att.exchange_order_id = placed.exchange_order_id
                att.placed_at = time.time()
                att.chases += 1
            else:
                del self.entries[pair]

    def _work_exit_orders(self) -> None:
        """Poll resting post-only EXIT orders and book their fills.

        Entry orders are handled by `_work_entry_orders`; everything else that is
        still open on the exchange is an exit. Without this, live mode places a
        sell, the exchange fills it, and the journal keeps insisting we still own
        the position -- so the next loop tries to sell it again.
        """
        timeout = self.cfg.execution.exit_timeout_sec
        for row in self.j.live_orders():
            if row["purpose"] == "entry" or row["side"] != "sell":
                continue
            st = self.broker.status(
                pair=row["pair"],
                order_id=row["exchange_order_id"] or "",
                client_order_id=row["client_order_id"],
            )
            if st.status == "filled":
                qty = st.filled_qty or row["qty"]
                self.j.update_order(
                    row["client_order_id"], status="filled",
                    filled_qty=qty, avg_fill_price=st.avg_price, fee=st.fee,
                )
                self._close_out(row["pair"], qty, row["purpose"], price=st.avg_price or row["price"])
            elif st.status in ("cancelled", "rejected"):
                self.j.update_order(row["client_order_id"], status=st.status)
                self.j.event(
                    "exit_order_gone",
                    f"{row['pair']} exit {row['client_order_id']} -> {st.status}; "
                    "position still open, will be re-evaluated next bar",
                    level="warning",
                )
            elif time.time() - row["created_at"] > timeout:
                # The market walked away from our passive quote. Stop being
                # passive: cancel and take the taker fee rather than hold an
                # exit we already decided to make.
                self.broker.cancel(
                    pair=row["pair"], order_id=row["exchange_order_id"] or "", side="sell"
                )
                self.j.update_order(row["client_order_id"], status="cancelled")
                self.j.event(
                    "exit_escalated",
                    f"{row['pair']} post-only exit unfilled for {timeout}s -> market",
                    level="warning",
                )
                self._market_exit(row["pair"], row["qty"], row["purpose"])

    def _on_entry_filled(self, att: EntryAttempt, qty: float, price: float) -> None:
        price = price or att.signal_price
        initial_risk = price - att.stop
        if initial_risk <= 0:
            self.j.event("entry_bad_stop", f"{att.pair}: stop above fill, closing", level="error")
            self._market_exit(att.pair, qty, "bad_stop")
            return
        pos = PositionState(
            pair=att.pair, entry_price=price, qty=qty, stop=att.stop,
            initial_risk=initial_risk, highest_close=price,
        )
        self._save_position(pos, opened_at=int(time.time()))
        self.j.update_order(att.client_order_id, status="filled", filled_qty=qty, avg_fill_price=price)
        self.j.open_trade(
            pair=att.pair, entry_price=price, qty=qty, stop=att.stop, risk_idr=qty * initial_risk,
        )
        self.j.event("entry", f"{att.pair} {qty:.8f} @ {price:,.0f}, stop {att.stop:,.0f}")
        self.notify.send(
            f"🟢 BUY {att.pair.replace('_idr', '').upper()}\n"
            f"qty {qty:.8f} @ Rp {price:,.0f}\n"
            f"notional Rp {qty * price:,.0f}\n"
            f"stop Rp {att.stop:,.0f} (-{(1 - att.stop / price) * 100:.2f}%)"
        )

    def scan_entries(self, state: RiskState) -> None:
        positions = self._positions()
        for pair in self.pairs:
            if pair in positions or pair in self.entries:
                continue
            df = self._prepared.get(pair)
            if df is None or len(df) < self.strategy.warmup_bars:
                continue
            i = len(df) - 1
            signal = self.strategy.entry_signal(df, i, pair)
            if signal is None:
                continue

            gate = self.risk.check_entry(signal, state)
            if not gate:
                self.j.event("entry_blocked", f"{pair}: {gate.gate.value} -- {gate.detail}")
                continue
            sizing, size_gate = self.risk.size(
                signal, state,
                amount_precision=self.public.amount_precision(pair),
                exchange_min_idr=self.public.trade_min_idr(pair),
            )
            if sizing is None:
                self.j.event("entry_blocked", f"{pair}: {size_gate.gate.value} -- {size_gate.detail}")
                continue

            try:
                book = self.public.book_top(pair)
            except Exception:  # noqa: BLE001
                continue

            coid = _coid("ent")
            placed = self.broker.place_post_only(
                pair=pair, side="buy", qty=sizing.qty, price=book.bid, client_order_id=coid
            )
            self.j.record_order(
                client_order_id=coid, pair=pair, side="buy", order_type="limit", qty=sizing.qty,
                price=book.bid, purpose="entry",
                status="open" if placed.accepted else "rejected",
                exchange_order_id=placed.exchange_order_id, note=signal.reason,
            )
            if not placed.accepted:
                self.j.event("entry_rejected", f"{pair}: {placed.reason}")
                continue

            self.entries[pair] = EntryAttempt(
                pair=pair, qty=sizing.qty, stop=sizing.stop, signal_price=signal.price,
                client_order_id=coid, exchange_order_id=placed.exchange_order_id,
                placed_at=time.time(),
            )
            self.j.event("entry_placed", f"{pair} {sizing.qty:.8f} @ {book.bid:,.0f} ({signal.reason})")
            # One entry per bar keeps the reserved cash in `state` honest.
            state.open_positions += 1
            state.cash_idr -= sizing.notional
            state.exposure_idr += sizing.notional

    # --------------------------------------------------------------- exits
    def manage_positions(self, *, new_bar: bool) -> None:
        """Stops every tick (live price); signal-driven exits only on a new bar."""
        for pair, pos in self._positions().items():
            df = self._prepared.get(pair)
            try:
                price = self.public.last_price(pair)
            except Exception:  # noqa: BLE001
                continue

            if price <= pos.stop:
                self.j.event("stop_hit", f"{pair} price {price:,.0f} <= stop {pos.stop:,.0f}")
                self._market_exit(pair, pos.qty, "stop")
                continue

            if df is None or not new_bar:
                continue

            i = len(df) - 1
            decision = self.strategy.check_exit(df, i, pos)
            if decision is not None and decision.kind != "stop":
                qty_out = pos.qty if decision.fraction >= 1.0 else pos.qty * decision.fraction
                self._limit_exit(pair, qty_out, decision.kind, decision.reason)
                if decision.fraction < 1.0:
                    pos.partial_done = True
                    pos.stop = max(pos.stop, pos.entry_price)
                    self._save_position(pos)
                continue

            pos.highest_close = max(pos.highest_close, float(df["close"].iloc[i]))
            new_stop = self.strategy.update_trail(df, i, pos)
            if new_stop > pos.stop:
                self.j.event("trail", f"{pair} stop {pos.stop:,.0f} -> {new_stop:,.0f}")
                pos.stop = new_stop
            self._save_position(pos)

    def _market_exit(self, pair: str, qty: float, reason: str) -> None:
        coid = _coid("exi")
        placed = self.broker.place_market(pair=pair, side="sell", qty=qty, client_order_id=coid)
        self.j.record_order(
            client_order_id=coid, pair=pair, side="sell", order_type="market", qty=qty,
            price=None, purpose=reason, status="filled" if placed.accepted else "rejected",
            exchange_order_id=placed.exchange_order_id,
        )
        if not placed.accepted:
            self.j.event("exit_failed", f"{pair}: {placed.reason}", level="error")
            self.notify.send(f"🚨 EXIT FAILED {pair}: {placed.reason}\nManual action may be needed.")
            return
        self._close_out(pair, qty, reason)

    def _limit_exit(self, pair: str, qty: float, reason: str, detail: str) -> None:
        try:
            book = self.public.book_top(pair)
        except Exception:  # noqa: BLE001
            return self._market_exit(pair, qty, reason)
        coid = _coid("exi")
        placed = self.broker.place_post_only(
            pair=pair, side="sell", qty=qty, price=book.ask, client_order_id=coid
        )
        self.j.record_order(
            client_order_id=coid, pair=pair, side="sell", order_type="limit", qty=qty,
            price=book.ask, purpose=reason, status="open" if placed.accepted else "rejected",
            exchange_order_id=placed.exchange_order_id, note=detail,
        )
        if not placed.accepted:
            return self._market_exit(pair, qty, reason)
        self.j.event("exit_placed", f"{pair} sell {qty:.8f} @ {book.ask:,.0f} ({detail})")
        return None

    def _close_out(self, pair: str, qty: float, reason: str, price: float | None = None) -> None:
        pos = self._positions().get(pair)
        if pos is None:
            return
        if price is None or price <= 0:
            try:
                price = self.public.last_price(pair)
            except Exception:  # noqa: BLE001
                price = pos.entry_price
        pnl = (price - pos.entry_price) * qty
        remaining = pos.qty - qty
        if remaining <= 1e-12:
            self.j.delete_position(pair)
            self.j.close_trade(pair, exit_price=price, pnl=pnl, reason=reason)
        else:
            pos.qty = remaining
            self._save_position(pos)
        self.j.event("exit", f"{pair} {qty:.8f} @ {price:,.0f} ({reason}) pnl Rp {pnl:,.0f}")
        emoji = "🔴" if pnl < 0 else "🟢"
        self.notify.send(
            f"{emoji} SELL {pair.replace('_idr', '').upper()} ({reason})\n"
            f"qty {qty:.8f} @ Rp {price:,.0f}\n"
            f"P&L Rp {pnl:,.0f} ({pnl / (pos.entry_price * qty) * 100:+.2f}%)"
        )

    def flatten_all(self, reason: str = "manual") -> None:
        for pair, att in list(self.entries.items()):
            self.broker.cancel(pair=pair, order_id=att.exchange_order_id or "", side="buy")
            self.j.update_order(att.client_order_id, status="cancelled")
            del self.entries[pair]
        for pair, pos in self._positions().items():
            self._market_exit(pair, pos.qty, reason)

    def halt(self, reason: str, *, flatten: bool = True) -> None:
        self.j.set_state("halted", True)
        self.j.set_state("halt_reason", reason)
        self.j.event("halt", reason, level="error")
        if flatten:
            self.flatten_all(reason="halt")
        self.notify.send(f"🛑 HALTED\n{reason}\nNo new entries until you send /resume in the app.")

    def resume(self) -> None:
        st = self.risk_state()
        st = resume_after_halt(st)
        self.j.set_state("halted", False)
        self.j.set_state("halt_reason", "")
        self.j.set_state("peak_equity", st.peak_equity)
        self.j.set_state("day_start_equity", st.day_start_equity)
        self.j.event("resume", f"resumed; equity peak rebased to Rp {st.equity:,.0f}")
        self.notify.send(f"▶️ Resumed. Equity peak rebased to Rp {st.equity:,.0f}.")

    # ----------------------------------------------------------------- loop
    def run_once(self) -> EngineStatus:
        self.j.heartbeat()

        if isinstance(self.broker, PaperBroker):
            # Advance resting paper orders against the live book. Bookkeeping is
            # deliberately left to _work_entry_orders / _work_exit_orders so the
            # paper and live paths stay identical.
            for o in self.broker.poll_fills():
                self.j.event(
                    "paper_fill", f"{o.pair} {o.side} {o.filled_qty:.8f} @ {o.avg_price:,.0f}"
                )

        # A flatten queued by the dashboard. Only this process may touch the
        # exchange, so the API writes a flag and the engine acts on it here.
        req = self.j.get_state("flatten_request")
        if req and int(req) > int(self.j.get_state("flatten_done", 0) or 0):
            self.j.set_state("flatten_done", int(req))
            self.j.event("flatten", "flatten requested from dashboard")
            self.flatten_all(reason="dashboard_flat")

        if self.cfg.halt_path.exists():
            if not self.j.get_state("halted", False):
                self.halt(f"HALT file present at {self.cfg.halt_path}")
            return self.status()

        new_bar = self.refresh_data()
        self._work_entry_orders()
        self._work_exit_orders()
        self.manage_positions(new_bar=new_bar)

        state = self.risk_state()
        self.j.record_equity(state.equity, state.cash_idr, state.exposure_idr, state.open_positions)

        if not state.halted:
            liq = self.risk.should_liquidate(state)
            if not liq:
                self.halt(liq.detail)
                return self.status()
            if new_bar:
                self.scan_entries(state)

        return self.status()

    def run_forever(self) -> None:
        self.reconcile()
        self.notify.send(
            f"🤖 Lumbung started in *{self.mode}* mode\n"
            f"sleeve Rp {self.cfg.capital.sleeve_idr:,.0f} · {len(self.pairs)} pairs · "
            f"{self.tf} timeframe"
        )
        interval = self.cfg.execution.poll_interval_sec
        while True:
            try:
                self.run_once()
            except KeyboardInterrupt:
                self.j.event("shutdown", "interrupted by user")
                self.notify.send("⏹️ Lumbung stopped (positions left open).")
                return
            except Exception as exc:  # noqa: BLE001
                log.exception("loop error")
                self.j.event("loop_error", str(exc), level="error")
            time.sleep(interval)


def _coid(prefix: str) -> str:
    """Client order id: <=36 chars, unique, and readable in the Indodax UI."""
    return f"{prefix}-{uuid.uuid4().hex[:20]}"
