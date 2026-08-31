"""Parameter sweep with a walk-forward split.

In-sample = first 65% of the window, out-of-sample = last 35%. A config only
counts if it works on BOTH. Picking the peak in-sample row is how you build a
bot that backtests beautifully and loses money live.
"""

from __future__ import annotations

import itertools
import logging
import sys

import pandas as pd

from lumbung.backtest import Backtester
from lumbung.config import load_config
from lumbung.data import candles
from lumbung.exchanges.indodax_public import IndodaxPublicClient

logging.getLogger("httpx").setLevel(logging.WARNING)


def split(data: dict[str, pd.DataFrame], frac: float):
    times = sorted({int(t) for df in data.values() for t in df["time"]})
    cut = times[int(len(times) * frac)]
    ins = {p: df[df["time"] <= cut] for p, df in data.items()}
    oos = {p: df[df["time"] > cut] for p, df in data.items()}
    return ins, oos


def main() -> None:
    tf = sys.argv[1] if len(sys.argv) > 1 else "240"
    base = load_config()
    conn = candles.connect(base.db_path)
    pub = IndodaxPublicClient()
    pairs = base.universe.pairs
    ticks = {p: pub.price_increment(p) for p in pairs}
    data = {p: candles.load(conn, p, tf) for p in pairs}
    data = {p: df for p, df in data.items() if not df.empty}
    ins, oos = split(data, 0.65)
    print(f"tf={tf}  pairs={len(data)}  bars={len(next(iter(data.values())))}  "
          f"IS={len(next(iter(ins.values())))} OOS={len(next(iter(oos.values())))}\n")

    grid = list(
        itertools.product(
            [20, 30, 40, 55],       # donchian lookback
            [2.0, 3.0, 4.0],        # stop atr mult
            [3.0, 4.5, 6.0],        # trail atr mult
            [0.0, 20.0, 25.0],      # adx min
        )
    )
    rows = []
    for dl, sm, tm, ax in grid:
        cfg = base.model_copy(deep=True)
        cfg.universe.timeframe = tf
        cfg.strategy.donchian_lookback = dl
        cfg.strategy.stop_atr_mult = sm
        cfg.strategy.trail_atr_mult = tm
        cfg.strategy.adx_min = ax
        try:
            ri = Backtester(cfg, enable_dd_halt=False).run(ins, ticks=ticks)
            ro = Backtester(cfg, enable_dd_halt=False).run(oos, ticks=ticks)
        except Exception as exc:  # noqa: BLE001
            print("fail", dl, sm, tm, ax, exc)
            continue
        rows.append(
            {
                "dl": dl, "stop": sm, "trail": tm, "adx": ax,
                "is_ret": ri.total_return_pct, "is_dd": ri.max_drawdown_pct,
                "is_n": len(ri.closed_trades), "is_pf": ri.profit_factor,
                "oos_ret": ro.total_return_pct, "oos_dd": ro.max_drawdown_pct,
                "oos_n": len(ro.closed_trades), "oos_pf": ro.profit_factor,
                "oos_mo": ro.monthly_return_pct,
            }
        )

    df = pd.DataFrame(rows)
    if df.empty:
        print("no results")
        return
    df["both_pos"] = (df.is_ret > 0) & (df.oos_ret > 0)
    df = df.sort_values(["both_pos", "oos_ret"], ascending=[False, False])
    pd.set_option("display.width", 200)
    print(df.head(25).to_string(index=False, float_format=lambda v: f"{v:.2f}"))
    print(f"\nconfigs positive in BOTH windows: {int(df.both_pos.sum())} / {len(df)}")


if __name__ == "__main__":
    main()
