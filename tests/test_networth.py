"""Balance-sheet arithmetic: allocation drift, emergency fund, surplus routing."""

from __future__ import annotations

import pytest

from lumbung.networth import Bucket, CashFlow, NetWorth, load_networth


def nw(stocks=64_500_000, gold=32_000_000, cash=10_000_000, bonds=0.0, crypto=0.0,
       income=17_000_000, spending=7_000_000) -> NetWorth:
    targets = {"stocks": 0.40, "bonds": 0.25, "gold": 0.15, "cash": 0.15,
               "crypto": 0.05, "other": 0.0}
    vals = {"stocks": stocks, "bonds": bonds, "gold": gold, "cash": cash,
            "crypto": crypto, "other": 0.0}
    return NetWorth(
        buckets={k: Bucket(k, vals[k], targets[k]) for k in vals},
        cashflow=CashFlow(income_monthly=income, spending_monthly=spending),
        positions=[("BBCA", stocks), ("Gold", gold)],
    )


# --------------------------------------------------------------- cash flow
def test_surplus_and_savings_rate():
    cf = CashFlow(income_monthly=17_000_000, spending_monthly=7_000_000)
    assert cf.surplus == 10_000_000
    assert cf.savings_rate == pytest.approx(0.588, abs=0.001)


def test_zero_income_does_not_divide_by_zero():
    assert CashFlow(0, 0).savings_rate == 0.0


# -------------------------------------------------------------- allocation
def test_total_and_weights():
    n = nw()
    assert n.total == 106_500_000
    assert n.buckets["stocks"].weight(n.total) == pytest.approx(0.6056, abs=0.001)
    assert n.buckets["gold"].weight(n.total) == pytest.approx(0.3005, abs=0.001)


def test_drift_reports_overweight_and_underweight():
    n = nw()
    assert n.buckets["stocks"].drift(n.total) == pytest.approx(20.6, abs=0.2)
    assert n.buckets["bonds"].drift(n.total) == pytest.approx(-25.0, abs=0.1)
    assert n.buckets["gold"].drift(n.total) == pytest.approx(15.0, abs=0.2)


def test_gap_is_positive_when_underweight_negative_when_over():
    n = nw()
    assert n.buckets["bonds"].gap_idr(n.total) > 0
    assert n.buckets["stocks"].gap_idr(n.total) < 0


def test_largest_position_detected():
    label, val, wt = nw().largest_position()
    assert label == "BBCA" and val == 64_500_000
    assert wt == pytest.approx(0.6056, abs=0.001)


# ---------------------------------------------------------- emergency fund
def test_cash_alone_is_thin_but_cash_plus_gold_covers_six_months():
    n = nw()
    assert n.months_covered_cash == pytest.approx(1.43, abs=0.02)
    assert n.months_covered_liquid == pytest.approx(6.0, abs=0.02)
    assert n.emergency_shortfall == 0.0


def test_shortfall_and_months_to_close_it():
    n = nw(gold=0, cash=5_000_000)
    assert n.emergency_target == 42_000_000
    assert n.emergency_shortfall == 37_000_000
    assert n.months_to_fund_emergency == pytest.approx(3.7, abs=0.01)


def test_no_surplus_means_the_shortfall_never_closes():
    n = nw(gold=0, cash=0, income=7_000_000, spending=7_000_000)
    assert n.cashflow.surplus == 0
    assert n.months_to_fund_emergency == 0.0  # sentinel: cannot be funded from surplus


# ------------------------------------------------------- surplus deployment
def test_surplus_goes_only_to_underweight_buckets():
    n = nw()
    alloc = dict(n.allocate_surplus())
    # Stocks and gold are overweight, so new money must not be added to them.
    assert "stocks" not in alloc
    assert "gold" not in alloc
    assert alloc["bonds"] > alloc["cash"] > alloc["crypto"]


def test_surplus_allocation_sums_to_the_amount():
    n = nw()
    alloc = n.allocate_surplus(10_000_000)
    assert sum(v for _, v in alloc) == pytest.approx(10_000_000, rel=1e-9)


def test_most_underweight_bucket_gets_the_most():
    n = nw()
    alloc = sorted(n.allocate_surplus(), key=lambda x: -x[1])
    assert alloc[0][0] == "bonds"  # 25pp under target, the biggest gap


def test_balanced_portfolio_falls_back_to_target_weights():
    n = nw(stocks=40_000_000, bonds=25_000_000, gold=15_000_000,
           cash=15_000_000, crypto=5_000_000)
    alloc = dict(n.allocate_surplus(1_000_000))
    assert alloc["stocks"] == pytest.approx(400_000, rel=0.02)


def test_zero_surplus_allocates_nothing():
    assert nw(income=7_000_000, spending=7_000_000).allocate_surplus() == []


# -------------------------------------------------------------- config load
def test_load_networth_reads_the_shipped_config():
    """Structure, not my example numbers -- those change as real balances do."""
    n = load_networth(stock_value=64_500_000)
    assert n.buckets["gold"].value > 0
    assert n.buckets["cash"].value > 0
    assert n.cashflow.surplus == n.cashflow.income_monthly - n.cashflow.spending_monthly
    assert n.emergency_months_target >= 1
    assert n.total == pytest.approx(
        64_500_000 + n.buckets["gold"].value + n.buckets["cash"].value
        + n.buckets["savings"].value + n.buckets["bonds"].value
        + n.buckets["crypto"].value + n.buckets["other"].value
    )


# ------------------------------------------------------------------ payday
def test_payday_fires_on_the_configured_day_only():
    cf = CashFlow(17_000_000, 7_000_000, payday_day=25)
    assert cf.is_payday(25) is True
    assert cf.is_payday(24) is False
    assert cf.is_payday(26) is False


def test_payday_disabled_when_day_is_zero():
    assert CashFlow(17_000_000, 7_000_000, payday_day=0).is_payday(25) is False


def test_late_month_payday_still_fires_in_a_short_month():
    """A payday_day of 29-31 would silently never fire in February. Falling back
    to the 28th means the reminder is never skipped."""
    for day in (29, 30, 31):
        assert CashFlow(1, 0, payday_day=day).is_payday(28) is True
    assert CashFlow(1, 0, payday_day=28).is_payday(28) is True
    assert CashFlow(1, 0, payday_day=25).is_payday(28) is False


def test_days_until_payday_counts_forward():
    cf = CashFlow(17_000_000, 7_000_000, payday_day=25)
    assert cf.days_until_payday(20) == 5
    assert cf.days_until_payday(25) == 0
    assert cf.days_until_payday(26) > 0        # wraps into next month


def test_days_until_payday_is_negative_when_disabled():
    assert CashFlow(1, 0, payday_day=0).days_until_payday(10) == -1


# ------------------------------------------------------- savings & auto-sync
def test_savings_is_liquid_but_cash_and_savings_stay_separate():
    """Both are spendable this week, but only one earns. Merging them would hide
    income you already have and overstate what the safety net costs."""
    from lumbung.networth import LIQUID

    assert "savings" in LIQUID and "cash" in LIQUID and "gold" in LIQUID
    from lumbung.networth import BUCKETS

    assert "savings" in BUCKETS and "cash" in BUCKETS


def test_other_asset_rate_produces_income():
    from lumbung.networth import OtherAsset

    sb = OtherAsset(name="Superbank", kind="savings", value_idr=8_000_000, rate=0.06)
    assert sb.annual_income == pytest.approx(480_000)
    gold = OtherAsset(name="Gold", kind="gold", value_idr=32_000_000, rate=0.0)
    assert gold.annual_income == 0.0


def test_savings_counts_toward_the_emergency_fund():
    from lumbung.networth import Bucket, CashFlow, NetWorth

    targets = dict.fromkeys(
        ("stocks", "bonds", "gold", "savings", "cash", "crypto", "other"), 0.0
    )
    vals = {**targets, "savings": 8_000_000, "cash": 2_000_000}
    n = NetWorth(
        buckets={k: Bucket(k, vals[k], targets[k]) for k in vals},
        cashflow=CashFlow(17_000_000, 7_000_000),
    )
    assert n.liquid_now == 10_000_000
    assert n.months_covered_liquid == pytest.approx(10 / 7, abs=0.01)


def test_live_crypto_value_overrides_the_recorded_figure():
    """The exchange is the source of truth for crypto, so a top-up needs no
    manual bookkeeping."""
    from lumbung.networth import load_networth

    recorded = load_networth(stock_value=0.0)
    live = load_networth(stock_value=0.0, crypto_value=1_234_567.0)
    assert live.buckets["crypto"].value == 1_234_567.0
    assert live.total == recorded.total - recorded.buckets["crypto"].value + 1_234_567.0


def test_shipped_target_allocation_sums_to_one():
    """A target set that does not sum to 100% silently distorts every drift
    number and the surplus split."""
    import yaml

    from lumbung.config import PROJECT_ROOT

    raw = yaml.safe_load((PROJECT_ROOT / "config" / "holdings.yaml").read_text(encoding="utf-8"))
    assert sum(raw["target_allocation"].values()) == pytest.approx(1.0)
