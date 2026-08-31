"""Second-stage sweep: how hard to deploy capital, given fixed strategy params.

Strategy shape is settled by scripts/sweep.py. This one asks the separate
question of how much risk to run -- concurrency, risk-per-trade, exposure caps,
and whether the partial take-profit helps or just caps the winners.
"""

from __future__ import annotations

import itertools
import logging

import pandas as pd

from lumbung.backtest import Backtester
from lumbung.config import load_config
from lumbung.data import candles
from lumbung.exchanges.indodax_public import IndodaxPublicClient

logging.getLogger("httpx").setLevel(logging.WARNING)

BEST = dict(donchian_lookback=55, stop_atr_mult=4.0, trail_atr_mult=4.5, adx_min=25.0)


def split(data, frac):
    times = sorted({int(t) for df in data.values() for t in df["time"]})
    cut = times[int(len(times) * frac)]
    return (
        {p: df[df["time"] <= cut] for p, df in data.items()},
        {p: df[df["time"] > cut] for p, df in data.items()},
    )


def main() -> None:
    base = load_config()
    conn = candles.connect(base.db_path)
    pub = IndodaxPublicClient()
    data = {p: candles.load(conn, p, "240") for p in base.universe.pairs}
    data = {p: d for p, d in data.items() if not d.empty}
    ticks = {p: pub.price_increment(p) for p in data}
    ins, oos = split(data, 0.65)

    rows = []
    grid = itertools.product(
        [3, 5, 8, 12],          # max concurrent positions
        [0.010, 0.015, 0.020],  # risk per trade
        [0.60, 0.90, 1.00],     # max total exposure
        [1.5, 999.0],           # partial TP at R (999 = disabled)
    )
    for mc, rpt, exp, tpr in grid:
        cfg = base.model_copy(deep=True)
        for k, v in BEST.items():
            setattr(cfg.strategy, k, v)
        cfg.strategy.partial_tp_r = tpr
        cfg.risk.max_concurrent_positions = mc
        cfg.risk.risk_per_trade_pct = rpt
        cfg.risk.max_total_exposure_pct = exp
        ri = Backtester(cfg, enable_dd_halt=False).run(ins, ticks=ticks)
        ro = Backtester(cfg, enable_dd_halt=False).run(oos, ticks=ticks)
        rows.append(
            {
                "maxpos": mc, "risk%": rpt * 100, "expo": exp,
                "tp_r": "off" if tpr > 100 else tpr,
                "is_ret": ri.total_return_pct, "is_dd": ri.max_drawdown_pct, "is_n": len(ri.closed_trades),
                "oos_ret": ro.total_return_pct, "oos_dd": ro.max_drawdown_pct, "oos_n": len(ro.closed_trades),
                "oos_mo": ro.monthly_return_pct,
            }
        )

    df = pd.DataFrame(rows)
    # Rank by out-of-sample return per unit of drawdown -- raw return alone just
    # picks whichever row took the most risk.
    df["oos_calmar"] = df.oos_ret / df.oos_dd.abs().clip(lower=0.1)
    df["is_calmar"] = df.is_ret / df.is_dd.abs().clip(lower=0.1)
    df = df.sort_values("oos_calmar", ascending=False)
    pd.set_option("display.width", 220)
    print(df.to_string(index=False, float_format=lambda v: f"{v:.2f}"))


if __name__ == "__main__":
    main()
