"""Strategy logic and backtester accounting on synthetic, hand-checkable data."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from lumbung.backtest import Backtester, Fill, Trade
from lumbung.config import load_config
from lumbung.strategy.donchian_trend import DonchianTrend, PositionState
from lumbung.strategy.indicators import atr, donchian_high, ema


def make_df(closes: list[float], *, start: int = 1_600_000_000, step: int = 14400) -> pd.DataFrame:
    n = len(closes)
    c = np.array(closes, dtype=float)
    df = pd.DataFrame(
        {
            "time": [start + i * step for i in range(n)],
            "open": c,
            "high": c * 1.005,
            "low": c * 0.995,
            "close": c,
            "volume": np.ones(n),
        }
    )
    return df.set_index(pd.to_datetime(df["time"], unit="s", utc=True))


# ------------------------------------------------------------- indicators
def test_donchian_excludes_current_bar():
    """If the window included the current bar, a breakout could never trigger."""
    s = pd.Series([1.0, 2, 3, 10])
    hi = donchian_high(s, 3)
    assert hi.iloc[3] == 3.0  # max of 1,2,3 -- not 10
    assert pd.isna(hi.iloc[2])


def test_ema_needs_full_period_before_emitting():
    e = ema(pd.Series(range(10), dtype=float), 5)
    assert e.iloc[:4].isna().all()
    assert not pd.isna(e.iloc[4])


def test_atr_is_positive_and_warms_up():
    df = make_df(list(np.linspace(100, 200, 40)))
    a = atr(df, 14)
    assert a.iloc[:13].isna().all()
    assert (a.dropna() > 0).all()


# --------------------------------------------------------------- strategy
@pytest.fixture
def strat():
    cfg = load_config().strategy
    cfg.donchian_lookback = 10
    cfg.ema_fast = 5
    cfg.ema_slow = 20
    cfg.adx_period = 5
    cfg.atr_period = 5
    cfg.adx_min = 0.0
    return DonchianTrend(cfg)


def test_no_entry_during_warmup(strat):
    df = strat.prepare(make_df([100] * 10))
    assert strat.entry_signal(df, 9, "btc_idr") is None


def test_breakout_in_uptrend_gives_signal_with_stop_below_entry(strat):
    closes = list(np.linspace(100, 150, 60)) + [200.0]
    df = strat.prepare(make_df(closes))
    sig = strat.entry_signal(df, len(df) - 1, "btc_idr")
    assert sig is not None
    assert sig.stop < sig.price
    assert sig.risk_per_unit > 0


def test_no_entry_in_downtrend(strat):
    df = strat.prepare(make_df(list(np.linspace(200, 100, 80))))
    assert strat.entry_signal(df, len(df) - 1, "btc_idr") is None


def test_trailing_stop_ratchets_up_only(strat):
    df = strat.prepare(make_df(list(np.linspace(100, 200, 60))))
    pos = PositionState("btc_idr", 100.0, 1.0, 90.0, 10.0, 100.0)
    first = strat.update_trail(df, 40, pos)
    pos.stop = first
    pos.highest_close = float(df["close"].iloc[40])
    # A lower bar must never widen the stop.
    assert strat.update_trail(df, 30, pos) >= first


def test_stop_exit_fills_at_the_gap_not_the_stop(strat):
    """A gap-down open must not be credited with the better stop price."""
    df = strat.prepare(make_df(list(np.linspace(100, 200, 60))))
    i = len(df) - 1
    df.loc[df.index[i], ["open", "low"]] = [150.0, 140.0]
    pos = PositionState("btc_idr", 200.0, 1.0, stop=180.0, initial_risk=20.0, highest_close=200.0)
    d = strat.check_exit(df, i, pos)
    assert d is not None and d.kind == "stop"
    assert d.price == 150.0  # the gapped open, not 180
    assert d.urgent is True  # -> market order


def test_partial_tp_disabled_by_default_config():
    """Config ships with partial TP off; the sweep showed it destroys returns."""
    assert load_config().strategy.partial_tp_r > 100


# ------------------------------------------------------------- accounting
def test_trade_pnl_is_net_of_all_fees():
    t = Trade(
        pair="btc_idr", entry_time=0, entry_price=100.0, qty=2.0, initial_stop=90.0,
        initial_risk_idr=20.0, entry_fee=1.0,
    )
    t.exits.append(Fill(time=1, price=120.0, qty=2.0, fee=3.0, reason="trend_exit"))
    # gross 240 - basis 200 - exit fee 3 - entry fee 1 = 36
    assert t.realized_pnl == pytest.approx(36.0)
    assert t.r_multiple == pytest.approx(36.0 / 20.0)


def test_partial_exit_prorates_the_entry_fee():
    t = Trade(
        pair="btc_idr", entry_time=0, entry_price=100.0, qty=2.0, initial_stop=90.0,
        initial_risk_idr=20.0, entry_fee=2.0,
    )
    t.exits.append(Fill(time=1, price=110.0, qty=1.0, fee=1.0, reason="partial_tp"))
    # gross 110 - basis 100 - exit fee 1 - half the entry fee (1.0) = 8
    assert t.realized_pnl == pytest.approx(8.0)
    assert t.qty_remaining == pytest.approx(1.0)


def test_costs_make_a_flat_market_lose_money():
    """Sanity check on the cost model: churn without edge must bleed."""
    cfg = load_config()
    assert cfg.costs.round_trip_maker_pct > 0
    assert cfg.costs.sell_cost_pct(taker=True) > cfg.costs.sell_cost_pct(taker=False)
    assert cfg.costs.sell_cost_pct(taker=False) > cfg.costs.buy_cost_pct(taker=False)


def test_backtest_is_deterministic():
    cfg = load_config()
    cfg.strategy.donchian_lookback = 10
    cfg.strategy.ema_fast, cfg.strategy.ema_slow = 5, 20
    cfg.strategy.adx_period = cfg.strategy.atr_period = 5
    cfg.strategy.adx_min = 0.0
    rng = np.random.default_rng(42)
    closes = list(100 * np.exp(np.cumsum(rng.normal(0.001, 0.02, 400))))
    data = {"btc_idr": make_df(closes)}
    a = Backtester(cfg).run(data, ticks={"btc_idr": 1.0})
    b = Backtester(cfg).run(data, ticks={"btc_idr": 1.0})
    assert a.end_equity == b.end_equity
    assert len(a.closed_trades) == len(b.closed_trades)


def test_backtest_never_spends_more_cash_than_it_has():
    cfg = load_config()
    cfg.capital.sleeve_idr = 1_000_000
    cfg.strategy.donchian_lookback = 5
    cfg.strategy.ema_fast, cfg.strategy.ema_slow = 3, 8
    cfg.strategy.adx_period = cfg.strategy.atr_period = 3
    cfg.strategy.adx_min = 0.0
    rng = np.random.default_rng(7)
    data = {
        p: make_df(list(100 * np.exp(np.cumsum(rng.normal(0.002, 0.03, 300)))))
        for p in ("btc_idr", "eth_idr", "sol_idr")
    }
    res = Backtester(cfg).run(data, ticks=dict.fromkeys(data, 1.0))
    assert (res.equity >= 0).all()
