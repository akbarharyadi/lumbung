"""Quick backtest runner used during development."""
import json
import sys

from lumbung.backtest import Backtester
from lumbung.config import load_config
from lumbung.data import candles
from lumbung.exchanges.indodax_public import IndodaxPublicClient


def main() -> None:
    cfg = load_config()
    conn = candles.connect(cfg.db_path)
    pub = IndodaxPublicClient()
    data = {p: candles.load(conn, p, cfg.universe.timeframe) for p in cfg.universe.pairs}
    ticks = {p: pub.price_increment(p) for p in cfg.universe.pairs}
    res = Backtester(cfg).run(data, ticks=ticks)
    print(json.dumps(res.summary(), indent=2))
    print()
    mt = res.monthly_table()
    for _, r in mt.iterrows():
        bar = "#" * int(abs(r.return_pct))
        print(f"  {r.month}  {r.return_pct:+7.2f}%  {bar}")
    print()
    by_reason: dict[str, list] = {}
    for t in res.closed_trades:
        by_reason.setdefault(t.exit_reason, []).append(t)
    for reason, ts in sorted(by_reason.items()):
        pnl = sum(t.realized_pnl for t in ts)
        print(f"  {reason:15s} n={len(ts):4d}  pnl=Rp {pnl:>12,.0f}  avgR={sum(t.r_multiple for t in ts)/len(ts):+.3f}")
    print()
    per_pair: dict[str, list] = {}
    for t in res.closed_trades:
        per_pair.setdefault(t.pair, []).append(t)
    for p, ts in sorted(per_pair.items()):
        pnl = sum(t.realized_pnl for t in ts)
        wr = 100 * sum(1 for t in ts if t.realized_pnl > 0) / len(ts)
        print(f"  {p:9s} n={len(ts):4d}  pnl=Rp {pnl:>12,.0f}  win={wr:5.1f}%")


if __name__ == "__main__":
    sys.exit(main())
