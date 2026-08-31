"""Donchian breakout with a trend filter. Long-only (Indodax spot has no shorting).

Entry  : close breaks the prior N-bar high, EMA(fast) > EMA(slow), ADX > threshold
Stop   : entry - stop_atr_mult * ATR
Trail  : chandelier -- highest close since entry - trail_atr_mult * ATR, ratchets up
Partial: sell `partial_tp_frac` at +partial_tp_r R, then stop moves to breakeven
Exit   : EMA(fast) crosses below EMA(slow)

The same object drives the backtest and the live engine, so a signal can never
mean two different things in the two paths.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from ..config import StrategyCfg
from .base import Action, Signal
from .indicators import adx, atr, donchian_high, ema


@dataclass
class PositionState:
    """Everything the strategy needs to manage an open position."""

    pair: str
    entry_price: float
    qty: float
    stop: float
    initial_risk: float  # entry - initial stop, per coin. The "R" unit.
    highest_close: float
    partial_done: bool = False

    def r_multiple(self, price: float) -> float:
        if self.initial_risk <= 0:
            return 0.0
        return (price - self.entry_price) / self.initial_risk


@dataclass(frozen=True)
class ExitDecision:
    kind: str  # "stop" | "partial_tp" | "trend_exit"
    fraction: float  # 1.0 = close all, 0.5 = scale out half
    urgent: bool  # True -> market order (accept taker fee); False -> post-only limit
    price: float
    reason: str


class DonchianTrend:
    name = "donchian_trend"

    def __init__(self, cfg: StrategyCfg) -> None:
        self.cfg = cfg

    @property
    def warmup_bars(self) -> int:
        """Bars needed before any signal is trustworthy."""
        c = self.cfg
        return max(c.ema_slow, c.donchian_lookback, c.atr_period, c.adx_period) + 5

    # -- indicators --------------------------------------------------------
    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        """Attach indicator columns. Returns a copy; the input is not mutated."""
        c = self.cfg
        out = df.copy()
        out["ema_fast"] = ema(out["close"], c.ema_fast)
        out["ema_slow"] = ema(out["close"], c.ema_slow)
        out["atr"] = atr(out, c.atr_period)
        out["adx"] = adx(out, c.adx_period)
        out["donchian_hi"] = donchian_high(out["high"], c.donchian_lookback)
        return out

    # -- entry -------------------------------------------------------------
    def entry_signal(self, df: pd.DataFrame, i: int, pair: str) -> Signal | None:
        """Evaluate bar `i` (must be a CLOSED bar) for a long entry."""
        c = self.cfg
        row = df.iloc[i]

        needed = ("ema_fast", "ema_slow", "atr", "adx", "donchian_hi")
        if any(pd.isna(row[k]) for k in needed):
            return None

        breakout = row["close"] > row["donchian_hi"]
        uptrend = row["ema_fast"] > row["ema_slow"]
        trending = row["adx"] > c.adx_min
        if not (breakout and uptrend and trending):
            return None

        price = float(row["close"])
        atr_v = float(row["atr"])
        stop = price - c.stop_atr_mult * atr_v
        if stop <= 0 or atr_v <= 0:
            return None

        return Signal(
            action=Action.ENTER_LONG,
            pair=pair,
            price=price,
            stop=stop,
            atr=atr_v,
            bar_time=int(row["time"]),
            reason=(
                f"{c.donchian_lookback}b breakout >{row['donchian_hi']:,.0f}, "
                f"EMA{c.ema_fast}>EMA{c.ema_slow}, ADX {row['adx']:.1f}"
            ),
            meta={"adx": float(row["adx"]), "donchian_hi": float(row["donchian_hi"])},
        )

    # -- management --------------------------------------------------------
    def update_trail(self, df: pd.DataFrame, i: int, pos: PositionState) -> float:
        """New stop for bar `i`. Ratchets up only -- a stop must never widen."""
        c = self.cfg
        row = df.iloc[i]
        atr_v = row["atr"]
        if pd.isna(atr_v) or atr_v <= 0:
            return pos.stop

        chandelier = max(pos.highest_close, float(row["close"])) - c.trail_atr_mult * float(atr_v)
        candidate = chandelier
        if pos.partial_done:
            # After scaling out, never give back more than the entry price.
            candidate = max(candidate, pos.entry_price)
        return max(pos.stop, candidate)

    def check_exit(self, df: pd.DataFrame, i: int, pos: PositionState) -> ExitDecision | None:
        """Exit decision for bar `i`, checked in priority order: stop, TP, trend.

        Stop uses the bar's LOW so an intrabar breach is caught; a gap-down open
        below the stop fills at the open, not the (better) stop price.
        """
        c = self.cfg
        row = df.iloc[i]

        if float(row["low"]) <= pos.stop:
            fill = min(pos.stop, float(row["open"]))  # gap-down protection
            return ExitDecision(
                kind="stop",
                fraction=1.0,
                urgent=True,
                price=fill,
                reason=f"stop {pos.stop:,.0f} hit",
            )

        if not pos.partial_done and pos.initial_risk > 0:
            tp_price = pos.entry_price + c.partial_tp_r * pos.initial_risk
            if float(row["high"]) >= tp_price:
                return ExitDecision(
                    kind="partial_tp",
                    fraction=c.partial_tp_frac,
                    urgent=False,
                    price=tp_price,
                    reason=f"+{c.partial_tp_r}R partial take-profit",
                )

        if not pd.isna(row["ema_fast"]) and not pd.isna(row["ema_slow"]):
            if row["ema_fast"] < row["ema_slow"]:
                return ExitDecision(
                    kind="trend_exit",
                    fraction=1.0,
                    urgent=False,
                    price=float(row["close"]),
                    reason=f"EMA{c.ema_fast} crossed below EMA{c.ema_slow}",
                )

        return None
