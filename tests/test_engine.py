"""End-to-end lifecycle: signal -> post-only entry -> fill -> stop -> flat.

Uses a fake order book and the real PaperBroker, Journal, RiskManager and
Engine. Live runs frequently produce no signal at all, so without this the whole
entry/exit path could stay unexercised until it ran with real money.
"""

from __future__ import annotations

import numpy as np
import pytest

from lumbung.config import load_config
from lumbung.data import candles as candles_mod
from lumbung.engine import Engine
from lumbung.exchanges.indodax_public import BookTop
from lumbung.execution.broker import PaperBroker
from lumbung.journal import Journal
from lumbung.notify.app import Notifier

PAIR = "btc_idr"
TF = "240"


class FakePublic:
    """Order book we control. Spread is one tick so post-only fills are testable."""

    def __init__(self, price: float = 1_000_000.0) -> None:
        self.price = price
        self.tick = 1000.0

    def set(self, price: float) -> None:
        self.price = price

    def book_top(self, pair: str) -> BookTop:
        return BookTop(bid=self.price - self.tick, ask=self.price, bid_size=99.0, ask_size=99.0)

    def last_price(self, pair: str) -> float:
        return self.price

    def price_increment(self, pair: str) -> float:
        return self.tick

    def amount_precision(self, pair: str) -> int:
        return 8

    def trade_min_idr(self, pair: str) -> float:
        return 10_000.0

    def is_tradable(self, pair: str) -> bool:
        return True


class CapturingNotifier(Notifier):
    def __init__(self) -> None:
        self.messages: list[str] = []

    def send(self, message: str) -> None:
        self.messages.append(message)


def seed_breakout_candles(db_path, pair: str = PAIR, n: int = 300) -> float:
    """A long steady uptrend, then one bar that clears the Donchian channel."""
    conn = candles_mod.connect(db_path)
    closes = list(np.linspace(500_000, 900_000, n - 1)) + [1_000_000.0]
    rows = []
    for i, c in enumerate(closes):
        rows.append(
            {
                "time": 1_700_000_000 + i * 14400,
                "open": c * 0.999, "high": c * 1.002, "low": c * 0.997,
                "close": c, "volume": 1.0,
            }
        )
    candles_mod.upsert(conn, pair, TF, rows)
    conn.close()
    return closes[-1]


@pytest.fixture
def env(tmp_path):
    cfg = load_config()
    cfg.paths.db = str(tmp_path / "test.db")
    cfg.paths.halt_file = str(tmp_path / "HALT")
    cfg.capital.sleeve_idr = 3_000_000
    cfg.universe.pairs = [PAIR]
    cfg.universe.timeframe = TF
    # Short windows so 300 synthetic bars are enough to warm up.
    cfg.strategy.donchian_lookback = 20
    cfg.strategy.ema_fast, cfg.strategy.ema_slow = 10, 30
    cfg.strategy.adx_period = cfg.strategy.atr_period = 10
    cfg.strategy.adx_min = 0.0

    price = seed_breakout_candles(cfg.db_path)
    pub = FakePublic(price)
    journal = Journal(cfg.db_path)
    broker = PaperBroker(pub, cfg.costs, starting_idr=cfg.capital.sleeve_idr)
    notifier = CapturingNotifier()
    engine = Engine(cfg, journal, broker, pub, notifier, mode="paper")
    # Skip the network sync; candles are already seeded in the temp DB.
    engine.refresh_data = _local_refresh(engine)
    return engine, pub, journal, broker, notifier, cfg


def _local_refresh(engine):
    def refresh() -> bool:
        newest = 0
        for pair in engine.pairs:
            df = candles_mod.load(engine.candles_conn, pair, engine.tf)
            if df.empty:
                continue
            engine._prepared[pair] = engine.strategy.prepare(df)
            newest = max(newest, int(df["time"].iloc[-1]))
        if newest > engine._last_bar_time:
            engine._last_bar_time = newest
            return True
        return False

    return refresh


# ---------------------------------------------------------------- lifecycle
def test_breakout_places_a_post_only_entry_below_the_ask(env):
    engine, pub, journal, *_ = env
    engine.run_once()

    orders = journal.conn.execute("SELECT * FROM orders").fetchall()
    assert len(orders) == 1
    o = orders[0]
    assert o["purpose"] == "entry" and o["side"] == "buy" and o["order_type"] == "limit"
    assert o["status"] == "open"
    # Resting at the bid, strictly below the ask -- otherwise it is not a maker.
    assert o["price"] < pub.book_top(PAIR).ask


def test_entry_fills_then_opens_a_position_with_a_stop(env):
    engine, pub, journal, broker, notifier, cfg = env
    engine.run_once()                     # place entry at the bid
    pub.set(pub.price - 2 * pub.tick)     # market comes down to us
    engine.run_once()                     # fill + book the position

    pos = journal.positions()
    assert PAIR in pos
    p = pos[PAIR]
    assert p.qty > 0
    assert 0 < p.stop < p.entry_price
    assert p.initial_risk == pytest.approx(p.entry_price - p.stop)
    assert any("BUY" in m for m in notifier.messages)

    risked = p.qty * p.initial_risk
    budget = cfg.capital.sleeve_idr * cfg.risk.risk_per_trade_pct
    assert risked <= budget * 1.05  # never risk more than the configured budget


def test_price_through_the_stop_exits_at_market_and_goes_flat(env):
    engine, pub, journal, broker, notifier, _ = env
    engine.run_once()
    pub.set(pub.price - 2 * pub.tick)
    engine.run_once()
    stop = journal.positions()[PAIR].stop

    pub.set(stop * 0.98)  # gap through the stop
    engine.run_once()

    assert journal.positions() == {}
    closed = journal.closed_trades()
    assert len(closed) == 1 and closed[0]["exit_reason"] == "stop"
    assert any("SELL" in m for m in notifier.messages)
    # The coin was actually sold, not just forgotten about.
    avail, _ = broker.balances()
    assert avail.get("btc", 0.0) == pytest.approx(0.0, abs=1e-9)


def test_no_second_position_while_one_is_open(env):
    engine, pub, journal, *_ = env
    engine.run_once()
    pub.set(pub.price - 2 * pub.tick)
    engine.run_once()
    before = journal.conn.execute("SELECT COUNT(*) c FROM orders").fetchone()["c"]
    engine.run_once()
    engine.run_once()
    after = journal.conn.execute("SELECT COUNT(*) c FROM orders").fetchone()["c"]
    assert after == before
    assert len(journal.positions()) == 1


# -------------------------------------------------------------------- halts
def test_halt_file_stops_trading_before_any_order(env):
    engine, _, journal, _, _, cfg = env
    cfg.halt_path.write_text("stop\n", encoding="utf-8")
    engine.run_once()
    assert journal.conn.execute("SELECT COUNT(*) c FROM orders").fetchone()["c"] == 0
    assert journal.get_state("halted") is True


def test_flatten_all_closes_everything(env):
    engine, pub, journal, *_ = env
    engine.run_once()
    pub.set(pub.price - 2 * pub.tick)
    engine.run_once()
    assert journal.positions()

    engine.flatten_all(reason="test")
    assert journal.positions() == {}


def test_resume_clears_halt_and_rebases_peak(env):
    engine, _, journal, *_ = env
    engine.halt("test halt", flatten=False)
    assert journal.get_state("halted") is True

    engine.resume()
    assert journal.get_state("halted") is False
    st = engine.risk_state()
    assert st.drawdown_pct == pytest.approx(0.0)


def test_reconcile_drops_a_position_the_exchange_does_not_hold(env):
    """The exchange is the source of truth, not our journal."""
    engine, pub, journal, *_ = env
    engine.run_once()
    pub.set(pub.price - 2 * pub.tick)
    engine.run_once()
    assert journal.positions()

    engine.broker.bal["btc"] = 0.0  # e.g. sold by hand in the Indodax app
    engine.reconcile()
    assert journal.positions() == {}
