"""Tests for sell pricing and dividend-capture arithmetic.

The dividend-capture tests are the important ones. A calculator that quietly
assumed the price does not fully adjust on the ex-date would report a profit on
every trade, and it would be wrong in the direction that costs money.
"""

from __future__ import annotations

import pytest

from lumbung.trade_math import (
    PPH_DIVIDEND,
    SHARES_PER_LOT,
    breakeven_drop_ratio,
    quote_dividend_capture,
    quote_sell,
)

# Akbar's actual position, so the numbers are checkable against reality.
BBCA = dict(lots=100, price=6450.0, avg_price=7401.0)


# ------------------------------------------------------------------ selling
def test_sell_at_a_loss_reports_the_loss():
    q = quote_sell(**BBCA)
    assert q.proceeds == pytest.approx(64_500_000)
    assert q.cost_basis == pytest.approx(74_010_000)
    assert q.gross_pnl == pytest.approx(-9_510_000)
    assert q.gross_pnl_pct == pytest.approx(-12.85, abs=0.01)
    assert q.is_loss


def test_tax_is_charged_on_proceeds_even_at_a_loss():
    """The Indonesian asymmetry: 0.1% is final and levied on proceeds, so a
    losing sale costs tax AND yields nothing to offset."""
    q = quote_sell(**BBCA)
    assert q.pph == pytest.approx(64_500)
    assert q.pph > 0
    assert q.net_pnl < q.gross_pnl, "costs must make a loss worse, never better"


def test_net_is_gross_minus_every_cost():
    q = quote_sell(**BBCA)
    assert q.net_proceeds == pytest.approx(q.proceeds - q.pph - q.broker_fee)
    assert q.net_pnl == pytest.approx(q.net_proceeds - q.cost_basis)


def test_breakeven_sits_above_the_average_price():
    """"I will sell when it returns to what I paid" still leaves you down."""
    q = quote_sell(**BBCA)
    assert q.breakeven_price > q.avg_price
    # Selling exactly at break-even returns the cost basis.
    at_be = quote_sell(lots=100, price=q.breakeven_price, avg_price=7401.0)
    assert at_be.net_pnl == pytest.approx(0.0, abs=1.0)


def test_dividend_given_up_is_reported():
    q = quote_sell(**BBCA, annual_dividend_per_share=355.0)
    monthly = 100 * SHARES_PER_LOT * 355.0 / 12
    assert q.dividend_lost_monthly == pytest.approx(monthly)
    assert q.months_of_dividend_forgone > 0


def test_selling_a_winner_still_pays_tax():
    q = quote_sell(lots=10, price=9000.0, avg_price=7401.0)
    assert q.gross_pnl > 0
    assert q.pph == pytest.approx(9000.0 * 10 * SHARES_PER_LOT * 0.001)
    assert q.net_pnl < q.gross_pnl


def test_selling_nothing_costs_nothing():
    q = quote_sell(lots=0, price=6450.0, avg_price=7401.0)
    assert q.proceeds == 0
    assert q.costs == 0
    assert q.net_pnl == 0


# -------------------------------------------------------- dividend capture
CAPTURE = dict(ticker="BBCA.JK", lots=10, price=6450.0,
               dividend_per_share=355.0, days_to_ex=5)


def test_full_adjustment_makes_capture_lose_money():
    """The default and honest case: the price gives back the whole dividend."""
    c = quote_dividend_capture(**CAPTURE)
    assert not c.worth_it
    assert c.net_edge < 0
    assert "negative" in c.verdict


def test_the_loss_is_at_least_the_dividend_tax():
    """Even with zero fees, the 10% PPh alone sinks it."""
    c = quote_dividend_capture(**CAPTURE, fee_buy=0.0, fee_sell=0.0, pph_sale=0.0)
    assert c.net_edge == pytest.approx(-c.dividend_tax)
    assert not c.worth_it


def test_capture_only_wins_if_the_price_barely_adjusts():
    """It takes an implausibly small drop before the trade pays."""
    poor = quote_dividend_capture(**CAPTURE, drop_ratio=0.9)
    assert not poor.worth_it

    generous = quote_dividend_capture(**CAPTURE, drop_ratio=0.5)
    assert generous.worth_it, "a half-adjustment should be enough to profit"


def test_breakeven_drop_ratio_is_below_one():
    """The headline fact: the price must FAIL to adjust for capture to pay."""
    r = breakeven_drop_ratio()
    assert r < 1.0
    assert r == pytest.approx(1.0 - PPH_DIVIDEND)


def test_dividend_tax_is_applied():
    c = quote_dividend_capture(**CAPTURE)
    assert c.dividend_tax == pytest.approx(c.dividend_gross * PPH_DIVIDEND)
    assert c.dividend_net == pytest.approx(c.dividend_gross - c.dividend_tax)


def test_tax_exemption_improves_but_does_not_rescue_it():
    """PP 9/2021 reinvestment exemption. Better, still not free money."""
    c = quote_dividend_capture(**CAPTURE, pph_dividend=0.0)
    assert c.dividend_tax == 0
    assert not c.worth_it, "fees alone still make a full-adjustment capture lose"


def test_yield_and_edge_percentages_are_consistent():
    c = quote_dividend_capture(**CAPTURE)
    assert c.yield_pct == pytest.approx(355.0 / 6450.0 * 100)
    assert (c.edge_pct < 0) == (c.net_edge < 0)


def test_holding_recovers_the_costs_over_time():
    """The case that does work: own it, and the fees are paid back by dividends."""
    c = quote_dividend_capture(**CAPTURE)
    assert 0 < c.hold_months_to_recover < 12, (
        "a 5.5% payer should repay round-trip costs inside a year of holding"
    )
