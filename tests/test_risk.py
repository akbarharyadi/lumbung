"""Every risk gate must actually block. A gate that silently passes is worse
than no gate, because you believe you are protected."""

from __future__ import annotations

import pytest

from lumbung.config import load_config
from lumbung.risk import Gate, RiskManager, RiskState, resume_after_halt, wib_day_key
from lumbung.strategy.base import Action, Signal


@pytest.fixture
def cfg():
    c = load_config()
    c.capital.sleeve_idr = 3_000_000
    return c


@pytest.fixture
def rm(cfg):
    return RiskManager(cfg)


def state(**kw) -> RiskState:
    base = dict(
        equity=3_000_000, peak_equity=3_000_000, day_start_equity=3_000_000,
        cash_idr=3_000_000, open_positions=0, exposure_idr=0.0, day_key=wib_day_key(),
    )
    base.update(kw)
    return RiskState(**base)


def signal(pair="btc_idr", price=1_000_000.0, stop=960_000.0) -> Signal:
    return Signal(action=Action.ENTER_LONG, pair=pair, price=price, stop=stop)


# ----------------------------------------------------------------- sizing
def test_size_risks_exactly_one_percent(rm, cfg):
    """qty * (entry - stop) should equal the risk budget."""
    s = signal(price=1_000_000, stop=960_000)  # 40k risk per coin
    sizing, gate = rm.size(s, state())
    assert gate.gate is Gate.OK
    expected_risk = cfg.capital.sleeve_idr * cfg.risk.risk_per_trade_pct
    assert sizing.risk_idr == pytest.approx(expected_risk, rel=1e-3)


def test_size_respects_position_cap(rm, cfg):
    """A very tight stop would otherwise demand a huge position."""
    s = signal(price=1_000_000, stop=999_000)  # 1k risk per coin -> 30 coins = 30m
    sizing, gate = rm.size(s, state())
    assert gate.gate is Gate.OK
    assert sizing.capped_by == "position_cap"
    cap = cfg.capital.sleeve_idr * cfg.risk.max_position_pct
    assert sizing.notional <= cap * 1.0001


def test_size_never_exceeds_available_cash(rm):
    sizing, gate = rm.size(signal(price=1_000_000, stop=999_500), state(cash_idr=200_000))
    assert gate.gate is Gate.OK
    assert sizing.notional <= 200_000


def test_size_rejects_below_min_notional(rm):
    sizing, gate = rm.size(signal(price=1_000_000, stop=999_000), state(cash_idr=20_000))
    assert sizing is None
    assert gate.gate is Gate.BELOW_MIN_NOTIONAL


def test_size_rejects_stop_above_entry(rm):
    sizing, gate = rm.size(signal(price=1_000_000, stop=1_100_000), state())
    assert sizing is None
    assert gate.gate is Gate.BAD_SIGNAL


def test_qty_rounds_down_not_up(rm):
    """Rounding up could breach a cap we just applied."""
    sizing, _ = rm.size(signal(price=333_333, stop=300_000), state(), amount_precision=2)
    assert sizing.qty == pytest.approx(round(sizing.qty, 2))
    assert sizing.qty * 100 == int(sizing.qty * 100)


# ------------------------------------------------------------------ gates
def test_max_positions_blocks(rm, cfg):
    g = rm.check_entry(signal(), state(open_positions=cfg.risk.max_concurrent_positions))
    assert g.gate is Gate.MAX_POSITIONS
    assert not g


def test_max_exposure_blocks(rm, cfg):
    over = cfg.capital.sleeve_idr * cfg.risk.max_total_exposure_pct + 1
    assert rm.check_entry(signal(), state(exposure_idr=over)).gate is Gate.MAX_EXPOSURE


def test_daily_loss_limit_blocks(rm, cfg):
    loss = 1 - cfg.risk.daily_loss_limit_pct - 0.001
    g = rm.check_entry(signal(), state(equity=3_000_000 * loss))
    assert g.gate is Gate.DAILY_LOSS_LIMIT


def test_drawdown_halts_and_liquidates(rm, cfg):
    eq = 3_000_000 * (1 - cfg.risk.max_drawdown_pct - 0.001)
    st = state(equity=eq, day_start_equity=eq)
    assert rm.check_entry(signal(), st).gate is Gate.MAX_DRAWDOWN
    assert rm.should_liquidate(st).gate is Gate.MAX_DRAWDOWN


def test_non_whitelisted_pair_blocked(rm):
    assert rm.check_entry(signal(pair="scam_idr"), state()).gate is Gate.NOT_WHITELISTED


def test_halted_blocks_everything(rm):
    g = rm.check_entry(signal(), state(halted=True, halt_reason="manual"))
    assert g.gate is Gate.HALTED


def test_clean_state_allows_entry(rm):
    assert rm.check_entry(signal(), state()).allowed


# --------------------------------------------------------------- deadlock
def test_resume_rebases_peak_so_it_does_not_instantly_rehalt(rm, cfg):
    """Regression: without rebasing the peak, /resume re-trips the drawdown gate
    on the next tick and the engine deadlocks -- entries blocked forever, so
    equity can never recover to the old peak."""
    eq = 3_000_000 * (1 - cfg.risk.max_drawdown_pct - 0.02)
    st = state(equity=eq, halted=True, halt_reason="drawdown")
    assert rm.check_entry(signal(), st).gate is Gate.HALTED

    resume_after_halt(st)
    assert st.peak_equity == eq
    assert st.drawdown_pct == 0.0
    assert rm.check_entry(signal(), st).allowed


def test_sweep_mode_disables_drawdown_gate(cfg):
    """Sweeps must not be silently truncated by a mid-window drawdown."""
    eq = 3_000_000 * (1 - cfg.risk.max_drawdown_pct - 0.05)
    st = state(equity=eq, day_start_equity=eq)
    assert RiskManager(cfg).check_entry(signal(), st).gate is Gate.MAX_DRAWDOWN
    assert RiskManager(cfg, enable_dd_halt=False).check_entry(signal(), st).allowed
