"""SBN vs savings. The tax asymmetry is the whole point, so it gets asserted."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from lumbung.bonds import (
    DEPOSIT_TAX,
    SAVINGS_TAX_FREE_BALANCE,
    SBN_TAX,
    Alternative,
    Offering,
    load_bonds,
    net_rate,
    recommend_tenor,
)


def offering(tenor=3, coupon=0.068, opens=-1, closes=+24, **kw):
    today = date.today()
    return Offering(
        series=f"SR025T{tenor}", kind="SR", tenor_years=tenor, coupon=coupon,
        opens=today + timedelta(days=opens), closes=today + timedelta(days=closes),
        matures=today + timedelta(days=365 * tenor), min_idr=1_000_000,
        tradeable=kw.get("tradeable", True),
    )


# ------------------------------------------------------------------- tax
def test_sbn_is_taxed_at_ten_percent_and_deposits_at_twenty():
    assert SBN_TAX == 0.10
    assert DEPOSIT_TAX == 0.20
    assert net_rate(0.068, "sbn") == pytest.approx(0.0612)
    assert net_rate(0.060, "deposit", balance=50_000_000) == pytest.approx(0.048)


def test_a_lower_headline_bond_beats_a_higher_headline_deposit_after_tax():
    """6.80% SBN vs 7.50% deposito: the deposit looks better and is not.
    This inversion is the reason the tool never compares gross rates."""
    sbn_net = net_rate(0.0680, "sbn")               # 6.12%
    dep_net = net_rate(0.0750, "deposit", balance=50_000_000)  # 6.00%
    assert sbn_net > dep_net


def test_small_savings_balances_are_untaxed():
    """Indonesian savings interest is only taxed above Rp 7.5jt."""
    assert net_rate(0.06, "deposit", balance=5_000_000) == pytest.approx(0.06)
    assert net_rate(0.06, "deposit", balance=SAVINGS_TAX_FREE_BALANCE + 1) == pytest.approx(0.048)


def test_untaxed_instruments_pass_through():
    assert net_rate(0.0575, "none") == pytest.approx(0.0575)


# -------------------------------------------------------------- offerings
def test_open_window_and_countdown():
    o = offering()
    assert o.is_open()
    assert o.days_left() == 24
    assert not offering(opens=+5, closes=+30).is_open()


def test_monthly_income_is_net_not_gross():
    o = offering(coupon=0.069)
    assert o.monthly_income(21_300_000) == pytest.approx(21_300_000 * 0.0621 / 12)


def test_tradeable_and_locked_read_differently():
    assert "secondary market" in offering(tradeable=True).liquidity
    assert "not tradeable" in offering(tradeable=False).liquidity


# ------------------------------------------------------------ tenor pick
def test_thin_buffer_gets_the_shorter_tenor_despite_the_lower_coupon():
    """Chasing +0.10% while the safety net is thin is how people end up
    redeeming early at a loss."""
    opts = [offering(3, 0.068), offering(5, 0.069)]
    pick, why = recommend_tenor(opts, months_of_buffer=2.0, emergency_target=6)
    assert pick.tenor_years == 3
    assert "safety net" in why


def test_covered_buffer_gets_the_higher_net_coupon():
    opts = [offering(3, 0.068), offering(5, 0.069)]
    pick, _ = recommend_tenor(opts, months_of_buffer=8.0, emergency_target=6)
    assert pick.tenor_years == 5


def test_no_open_offering_is_reported_not_guessed():
    pick, why = recommend_tenor([offering(opens=+10, closes=+40)],
                                months_of_buffer=8.0, emergency_target=6)
    assert pick is None and "No offering" in why


# ---------------------------------------------------------------- config
def test_shipped_calendar_loads_and_has_sr025():
    offerings, alts = load_bonds()
    assert any(o.series.startswith("SR025") for o in offerings)
    assert all(0 < o.coupon < 0.25 for o in offerings)
    assert any(a.name.startswith("Superbank") for a in alts)


def test_alternative_net_respects_the_balance_threshold():
    a = Alternative(name="savings", rate=0.06, liquid=True, taxed="deposit")
    assert a.net(5_000_000) == pytest.approx(0.06)
    assert a.net(50_000_000) == pytest.approx(0.048)
