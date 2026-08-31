"""Goal arithmetic and screener scoring."""

from __future__ import annotations

import pytest

from lumbung.goal import plan_income_goal, sell_signals
from lumbung.screener import Candidate, build_basket, score
from tests.test_holdings import report


# ------------------------------------------------------------------- goal
def test_capital_required_is_target_over_yield():
    p = plan_income_goal(monthly_target=3_000_000, current_capital=74_500_000, blended_return=0.07)
    assert p.annual_target == 36_000_000
    assert p.capital_required == pytest.approx(514_285_714, rel=1e-6)
    assert p.capital_multiple == pytest.approx(6.9, abs=0.05)


def test_contributions_dominate_compounding_at_this_size():
    """From Rp 74.5jt, saving is a far bigger lever than returns. Worth asserting
    because it is the single most decision-relevant fact in the plan."""
    base = dict(monthly_target=3_000_000, current_capital=74_500_000, blended_return=0.07)
    none = plan_income_goal(**base, monthly_contribution=0).years_to_target()
    small = plan_income_goal(**base, monthly_contribution=2_000_000).years_to_target()
    big = plan_income_goal(**base, monthly_contribution=10_000_000).years_to_target()
    assert none > small > big
    assert none == pytest.approx(27.8, abs=0.3)
    assert big == pytest.approx(3.2, abs=0.2)


def test_already_at_target_returns_zero_years():
    p = plan_income_goal(
        monthly_target=100_000, current_capital=500_000_000, blended_return=0.07
    )
    assert p.years_to_target() == 0.0


def test_unreachable_target_returns_none():
    p = plan_income_goal(
        monthly_target=500_000_000, current_capital=1_000_000,
        monthly_contribution=0, blended_return=0.07,
    )
    assert p.years_to_target(max_years=30) is None


def test_trajectory_grows_monotonically():
    p = plan_income_goal(
        monthly_target=3_000_000, current_capital=74_500_000,
        monthly_contribution=3_000_000, blended_return=0.07,
    )
    rows = p.trajectory(years=10)
    caps = [c for _, c, _ in rows]
    assert caps == sorted(caps)


# ------------------------------------------------------------------- sell
def test_concentration_fires_and_sizes_a_trim():
    r = report()  # BBCA: 10,000 sh @ 6450 = Rp 64.5jt
    rev = sell_signals(r, portfolio_value=74_500_000, max_position_pct=0.40)
    assert any(s.rule == "concentration" and s.severity == "act" for s in rev.signals)
    assert rev.suggested_trim_lots > 0
    # Trimming should leave roughly the target weight, never overshoot to zero.
    remaining = r.market_value - rev.suggested_trim_lots * r.price * 100
    assert 0 < remaining <= 74_500_000 * 0.41


def test_no_concentration_signal_when_position_is_small():
    rev = sell_signals(report(), portfolio_value=1_000_000_000, max_position_pct=0.40)
    assert not any(s.rule == "concentration" for s in rev.signals)
    assert rev.suggested_trim_lots == 0


def test_dividend_cut_is_an_act_signal_and_breaks_the_business_test():
    rev = sell_signals(report(div=200.0), portfolio_value=1e12, prior_year_div=336.0)
    assert any(s.rule == "dividend cut" and s.severity == "act" for s in rev.signals)
    assert rev.business_is_intact is False


def test_growing_dividend_keeps_business_intact():
    rev = sell_signals(report(div=356.0), portfolio_value=1e12, prior_year_div=300.0)
    assert any(s.rule == "dividend growing" for s in rev.signals)
    assert rev.business_is_intact is True


def test_broken_trend_is_only_a_watch_not_an_act():
    """A falling price with a healthy business should not read as an emergency."""
    rev = sell_signals(report(), portfolio_value=1e12, prior_year_div=300.0)
    trend = [s for s in rev.signals if s.rule == "trend broken"]
    assert trend and trend[0].severity == "watch"


# --------------------------------------------------------------- screener
def cand(ticker="XXXX.JK", price=1000.0, div=70.0, years=5, payout=0.6, sector="Utilities",
         ema50=1010.0, ema200=1000.0, **kw) -> Candidate:
    c = Candidate(ticker=ticker, price=price, sector=sector, ttm_div=div, payout_ratio=payout,
                  ema50=ema50, ema200=ema200, **kw)
    c.div_by_year = {2022 + i: (div if i < years else 0.0) for i in range(5)}
    return c


def test_yield_credit_is_capped_so_distress_cannot_win_on_yield_alone():
    normal = cand(div=100.0)      # 10% yield
    extreme = cand(div=300.0)     # 30% yield -- almost certainly a special or distress
    score([normal, extreme], budget=10_000_000)
    assert normal.score_parts["yield"] == extreme.score_parts["yield"] == 35.0
    assert any("unusually high" in f for f in extreme.flags)


def test_inconsistent_payer_is_flagged_and_scores_lower():
    steady, patchy = cand(years=5), cand(ticker="YYYY.JK", years=2)
    score([steady, patchy], budget=10_000_000)
    assert steady.score > patchy.score
    assert any("only 2 of the last 5" in f for f in patchy.flags)


def test_payout_above_100pct_zeroes_sustainability():
    c = cand(payout=2.05)  # UNVR-like
    score([c], budget=10_000_000)
    assert c.score_parts["sustainability"] == 0.0
    assert any("above 100%" in f for f in c.flags)


def test_same_sector_as_existing_holding_loses_the_diversification_points():
    bank = cand(sector="Financial Services")
    other = cand(ticker="ZZZZ.JK", sector="Utilities")
    score([bank, other], budget=10_000_000, avoid_sectors={"Financial Services"})
    assert bank.score_parts["diversify"] == 0.0
    assert other.score_parts["diversify"] == 5.0
    assert any("same sector" in f for f in bank.flags)


def test_deep_downtrend_zeroes_trend_points():
    falling = cand(price=700.0, ema50=800.0, ema200=1000.0)
    score([falling], budget=10_000_000)
    assert falling.score_parts["trend"] == 0.0


def test_basket_takes_one_name_per_sector():
    cands = [
        cand(ticker="A.JK", sector="Financial Services", div=100.0),
        cand(ticker="B.JK", sector="Financial Services", div=99.0),
        cand(ticker="C.JK", sector="Utilities", div=98.0),
        cand(ticker="D.JK", sector="Healthcare", div=97.0),
    ]
    ranked = score(cands, budget=10_000_000)
    picks = build_basket(ranked, 10_000_000, n=3)
    sectors = [c.sector for c, _ in picks]
    assert len(sectors) == len(set(sectors))


def test_basket_buys_whole_lots_within_budget():
    cands = [cand(ticker=f"{c}.JK", sector=s, price=p)
             for c, s, p in [("A", "Utilities", 1645), ("B", "Healthcare", 520), ("C", "Energy", 360)]]
    picks = build_basket(score(cands, budget=10_000_000), 10_000_000, n=3)
    total = sum(lots * c.lot_cost for c, lots in picks)
    assert total <= 10_000_000
    assert all(lots >= 1 for _, lots in picks)
