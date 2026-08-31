"""Full-window runs of the shortlisted configs, with the drawdown halt ON.

The sweeps ran with the halt off so parameter sets stayed comparable. This is the
opposite check: does the config survive the circuit breaker it will actually run
under, and what does the month-to-month experience look like?
"""

from __future__ import annotations

import logging

import pandas as pd

from lumbung.backtest import Backtester
from lumbung.config import load_config
from lumbung.data import candles
from lumbung.exchanges.indodax_public import IndodaxPublicClient

logging.getLogger("httpx").setLevel(logging.WARNING)

STRAT = dict(donchian_lookback=55, stop_atr_mult=4.0, trail_atr_mult=4.5, adx_min=25.0)

CANDIDATES = {
    "A conservative": dict(risk=0.010, maxpos=6, expo=0.75, tp=999.0, dd=0.15),
    "B balanced": dict(risk=0.015, maxpos=8, expo=0.90, tp=999.0, dd=0.18),
    "C aggressive": dict(risk=0.020, maxpos=8, expo=1.00, tp=999.0, dd=0.22),
    "D with partial TP": dict(risk=0.010, maxpos=8, expo=0.60, tp=1.5, dd=0.15),
}


def main() -> None:
    base = load_config()
    conn = candles.connect(base.db_path)
    pub = IndodaxPublicClient()
    data = {p: candles.load(conn, p, "240") for p in base.universe.pairs}
    data = {p: d for p, d in data.items() if not d.empty}
    ticks = {p: pub.price_increment(p) for p in data}

    results = {}
    for name, c in CANDIDATES.items():
        cfg = base.model_copy(deep=True)
        for k, v in STRAT.items():
            setattr(cfg.strategy, k, v)
        cfg.strategy.partial_tp_r = c["tp"]
        cfg.risk.risk_per_trade_pct = c["risk"]
        cfg.risk.max_concurrent_positions = c["maxpos"]
        cfg.risk.max_total_exposure_pct = c["expo"]
        cfg.risk.max_drawdown_pct = c["dd"]
        res = Backtester(cfg, halt_is_terminal=True).run(data, ticks=ticks)
        results[name] = res
        s = res.summary()
        print(
            f"{name:20s} ret {s['total_return_pct']:>7.1f}%  /mo {s['monthly_return_pct']:>5.2f}%  "
            f"maxDD {s['max_drawdown_pct']:>6.1f}%  n={s['trades']:>3d}  "
            f"PF {s['profit_factor']:.2f}  avgR {s['avg_r']:+.2f}  "
            f"halt={'YES ' + str(s['halt_reason'])[:28] if s['halted_at'] else 'no'}"
        )

    print("\n--- monthly returns (%) ---")
    tables = {n: r.monthly_table().set_index("month")["return_pct"] for n, r in results.items()}
    mt = pd.DataFrame(tables)
    print(mt.to_string(float_format=lambda v: f"{v:+6.2f}"))

    print("\n--- month distribution ---")
    for n, col in mt.items():
        col = col.dropna()
        if col.empty:
            continue
        print(
            f"{n:20s} months={len(col):3d}  positive={100 * (col > 0).mean():4.0f}%  "
            f"best {col.max():+6.2f}%  worst {col.min():+6.2f}%  median {col.median():+5.2f}%"
        )


if __name__ == "__main__":
    main()
