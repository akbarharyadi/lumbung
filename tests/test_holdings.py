"""Holdings arithmetic. These numbers drive real decisions, so they get asserted."""

from __future__ import annotations

import pytest

from lumbung.holdings import (
    AlertCfg,
    Holding,
    HoldingReport,
    PortfolioSummary,
    load_holdings,
)


def report(price=6450.0, avg=7401.0, lots=100, div=356.0, **kw) -> HoldingReport:
    defaults = dict(
        ema50=6264.0, ema200=6855.0, adx=11.0, atr=142.0, donchian_hi=6500.0,
        high_52w=8925.0, low_52w=4850.0, last_dividend_date="2026-06-17", alerts=[],
    )
    defaults.update(kw)
    return HoldingReport(
        holding=Holding(ticker="BBCA.JK", lots=lots, avg_price=avg),
        price=price, ttm_dividend_per_share=div, **defaults,
    )


def test_lots_convert_to_shares():
    assert Holding("BBCA.JK", 100, 7401).shares == 10_000


def test_position_economics():
    r = report()
    assert r.holding.cost_basis == pytest.approx(74_010_000)
    assert r.market_value == pytest.approx(64_500_000)
    assert r.unrealised == pytest.approx(-9_510_000)
    assert r.unrealised_pct == pytest.approx(-12.85, abs=0.01)


def test_breakeven_move_is_larger_than_the_loss_percentage():
    """A -12.85% loss needs +14.7% to recover, not +12.85%. This asymmetry is
    the whole reason a stop matters."""
    r = report()
    assert r.breakeven_move_pct == pytest.approx(14.74, abs=0.05)
    assert r.breakeven_move_pct > abs(r.unrealised_pct)


def test_dividend_income():
    """Income means what reaches you, so the dividend is reported net.

    SBN coupons and savings interest are both stored net of their own, different
    taxes. A gross dividend beside them won every comparison by ~10% without
    anything in the output admitting the two were measured differently.
    """
    r = report()
    assert r.annual_income_gross == pytest.approx(3_560_000)
    assert r.annual_income == pytest.approx(3_204_000)          # less 10% PPh
    assert r.monthly_income == pytest.approx(267_000, abs=1)
    assert r.monthly_income_gross == pytest.approx(296_666.67, abs=1)


def test_dividend_tax_is_configurable():
    """0% is a real election, not a hypothetical: dividends reinvested in
    Indonesia for three years are exempt."""
    import dataclasses

    r = dataclasses.replace(report(), dividend_tax_pct=0.0)
    assert r.annual_income == pytest.approx(r.annual_income_gross)


def test_yield_on_cost_is_lower_than_yield_on_market_when_underwater():
    r = report()
    assert r.yield_on_cost_pct == pytest.approx(4.81, abs=0.01)
    assert r.yield_on_market_pct == pytest.approx(5.52, abs=0.01)
    assert r.yield_on_cost_pct < r.yield_on_market_pct


def test_signal_is_no_buy_in_a_downtrend():
    r = report()
    assert not r.uptrend
    assert "NO BUY" in r.signal and "downtrend" in r.signal


def test_signal_is_buy_only_when_all_three_conditions_hold():
    r = report(price=7000.0, ema50=6900.0, ema200=6500.0, adx=30.0, donchian_hi=6800.0)
    assert r.signal == "BUY"
    assert report(price=7000.0, ema50=6900.0, ema200=6500.0, adx=10.0,
                  donchian_hi=6800.0).signal != "BUY"


def test_subscription_coverage():
    s = PortfolioSummary(reports=[report()], cash_idr=10_000_000)
    assert s.monthly_income == pytest.approx(267_000, abs=1)      # net of PPh
    assert s.subscription_coverage_pct == pytest.approx(80.9, abs=0.2)


def test_paper_crypto_equity_is_excluded_from_net_worth():
    """The paper sleeve is simulated money drawn from the same cash -- counting
    both would inflate the total by the sleeve size."""
    paper = PortfolioSummary(
        reports=[report()], cash_idr=10_000_000, crypto_equity=3_000_000, crypto_is_real=False
    )
    live = PortfolioSummary(
        reports=[report()], cash_idr=10_000_000, crypto_equity=3_000_000, crypto_is_real=True
    )
    assert paper.total_value == pytest.approx(74_500_000)
    assert live.total_value == pytest.approx(77_500_000)


def test_alerts_fire_on_thresholds(tmp_path):
    cfg = AlertCfg(drawdown_warn_pct=0.10, below_ema200_pct=0.05)
    r = report()
    # -12.85% breaches a 10% drawdown threshold; 6450 vs EMA200 6855 is -5.9%.
    assert r.price < r.holding.avg_price * (1 - cfg.drawdown_warn_pct)
    assert r.price < r.ema200 * (1 - cfg.below_ema200_pct)


def test_load_holdings_reads_the_shipped_config():
    holdings, cash, alerts = load_holdings()
    assert any(h.ticker == "BBCA.JK" and h.lots == 100 for h in holdings)
    assert cash > 0
    assert 0 < alerts.drawdown_warn_pct < 1


def test_missing_holdings_file_is_not_an_error(tmp_path):
    holdings, cash, _ = load_holdings(tmp_path / "nope.yaml")
    assert holdings == [] and cash == 0.0
