"""Technical indicators. Pure pandas, no external TA dependency.

Every function returns a Series aligned to the input index. Values that need more
history than is available are NaN -- callers must check, never fillna, because a
filled indicator silently fabricates signals.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    return pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder's ATR (RMA smoothing, not a simple mean)."""
    tr = true_range(df)
    return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder's ADX. Measures trend strength regardless of direction."""
    up = df["high"].diff()
    down = -df["low"].diff()

    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)

    alpha = 1 / period
    atr_ = true_range(df).ewm(alpha=alpha, adjust=False, min_periods=period).mean()
    plus_di = (
        100
        * pd.Series(plus_dm, index=df.index).ewm(alpha=alpha, adjust=False, min_periods=period).mean()
        / atr_
    )
    minus_di = (
        100
        * pd.Series(minus_dm, index=df.index)
        .ewm(alpha=alpha, adjust=False, min_periods=period)
        .mean()
        / atr_
    )

    denom = (plus_di + minus_di).replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / denom
    return dx.ewm(alpha=alpha, adjust=False, min_periods=period).mean()


def donchian_high(series: pd.Series, lookback: int) -> pd.Series:
    """Highest value over the PRIOR `lookback` bars, excluding the current one.

    The shift is what makes this usable: comparing a bar's close against a window
    that includes that same bar can never produce a breakout.
    """
    return series.shift(1).rolling(lookback, min_periods=lookback).max()


def donchian_low(series: pd.Series, lookback: int) -> pd.Series:
    return series.shift(1).rolling(lookback, min_periods=lookback).min()
