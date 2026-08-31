"""A spending limit must not shrink the safety net.

One field used to do two jobs: it sized the emergency fund and it was printed as
"your monthly budget". Setting a target below reality would have cut the buffer
by six times the gap -- Rp 60jt instead of Rp 71,8jt -- which is the opposite of
what a budget is for. Aiming to spend less does not make six months of real life
any cheaper.
"""
import textwrap

import pytest

from lumbung.networth import Bucket, CashFlow, NetWorth, load_networth

MEASURED = 11_967_121.0
LIMIT = 10_000_000.0


def _nw(limit=0.0):
    return NetWorth(
        buckets={
            "cash": Bucket("cash", 6_900_000, 0.07),
            "savings": Bucket("savings", 10_000_000, 0.20),
            "gold": Bucket("gold", 32_766_000, 0.10),
        },
        cashflow=CashFlow(
            income_monthly=17_000_000,
            spending_monthly=MEASURED,
            spending_limit=limit,
        ),
    )


def test_the_limit_does_not_move_the_emergency_target():
    """The whole reason the two numbers are separate."""
    without = _nw().emergency_target
    with_limit = _nw(LIMIT).emergency_target
    assert without == with_limit == pytest.approx(MEASURED * 6)
    assert with_limit != LIMIT * 6


def test_the_limit_does_not_flatter_the_surplus():
    assert _nw(LIMIT).cashflow.surplus == _nw().cashflow.surplus


def test_budget_is_the_limit_when_one_is_set():
    assert _nw(LIMIT).cashflow.budget == LIMIT


def test_budget_falls_back_to_measured_spending():
    """No limit must not read as 'no budget' -- every month still has a number."""
    assert _nw().cashflow.budget == MEASURED
    assert _nw().cashflow.has_limit is False


def test_limit_gap_is_what_it_asks_you_to_cut():
    assert _nw(LIMIT).cashflow.limit_gap == pytest.approx(MEASURED - LIMIT)


def test_a_limit_above_actual_spending_reports_a_negative_gap():
    """Not an error, but it is not a limit either, and the sign says so."""
    nw = _nw(20_000_000)
    assert nw.cashflow.limit_gap < 0


def test_no_limit_means_no_gap():
    assert _nw().cashflow.limit_gap == 0.0


def test_limit_round_trips_through_yaml(tmp_path):
    p = tmp_path / "h.yaml"
    p.write_text(
        textwrap.dedent("""\
            cash_idr: 6900000
            cashflow:
              income_monthly: 17000000
              spending_monthly: 11967121
              spending_limit: 10000000
            """),
        encoding="utf-8",
    )
    cf = load_networth(p).cashflow
    assert cf.spending_limit == LIMIT
    assert cf.spending_monthly == MEASURED
    assert cf.budget == LIMIT


def test_an_absent_limit_loads_as_zero(tmp_path):
    p = tmp_path / "h.yaml"
    p.write_text(
        "cash_idr: 1\ncashflow: {income_monthly: 17000000, spending_monthly: 7000000}\n",
        encoding="utf-8",
    )
    cf = load_networth(p).cashflow
    assert cf.spending_limit == 0.0
    assert cf.budget == 7_000_000
