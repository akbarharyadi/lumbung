"""Commitments: money already promised stops being investable.

The bug these exist for: with a Rp 31jt house payment due in five weeks, the
payday plan routed Rp 3.7jt/month into a three-year SBN. Nothing contradicted
it, so it read as sound advice.
"""
import textwrap
from datetime import date

import pytest

from lumbung.networth import (
    Bucket,
    CashFlow,
    Commitment,
    NetWorth,
    load_networth,
)

TODAY = date(2026, 8, 24)


def _nw(commitments=None, **kw):
    buckets = {
        "stocks": Bucket("stocks", 64_500_000, 0.40),
        "bonds": Bucket("bonds", 16_000_000, 0.20),
        "gold": Bucket("gold", 32_766_000, 0.10),
        "savings": Bucket("savings", 10_000_000, 0.20),
        "cash": Bucket("cash", 9_000_000, 0.07),
        "crypto": Bucket("crypto", 2_996_670, 0.03),
    }
    return NetWorth(
        buckets=buckets,
        cashflow=CashFlow(income_monthly=17_000_000, spending_monthly=7_000_000),
        commitments=commitments or [],
        **kw,
    )


def test_no_commitments_leaves_the_buffer_alone():
    nw = _nw()
    assert nw.committed_total(TODAY) == 0
    assert nw.free_liquid(TODAY) == nw.liquid_now


def test_a_promise_is_subtracted_from_the_buffer():
    nw = _nw([Commitment("KPR", 31_000_000, date(2026, 9, 30))])
    assert nw.liquid_now == pytest.approx(51_766_000)
    assert nw.free_liquid(TODAY) == pytest.approx(20_766_000)
    assert nw.months_covered_free(TODAY) == pytest.approx(20_766_000 / 7_000_000)


def test_emergency_shortfall_counts_only_free_money():
    """A buffer that only clears because a bill has not been paid is not a buffer."""
    assert _nw().emergency_shortfall == 0
    nw = _nw([Commitment("KPR", 31_000_000, date(2026, 9, 30))])
    assert nw.emergency_shortfall == pytest.approx(21_234_000)


def test_surplus_stays_liquid_while_a_promise_is_outstanding():
    nw = _nw([Commitment("KPR", 31_000_000, date(2026, 9, 30))])
    dest = dict(nw.allocate_surplus(10_000_000))
    assert "bonds" not in dest, "money needed in five weeks must not be locked up"
    assert "crypto" not in dest
    assert set(dest) <= {"cash", "savings", "gold"}
    assert sum(dest.values()) == pytest.approx(10_000_000)


def test_without_the_promise_the_same_surplus_reaches_bonds():
    """Guards the gate itself: the liquid-only path must not become permanent."""
    dest = dict(_nw().allocate_surplus(10_000_000))
    assert dest.get("bonds", 0) > 0


def test_a_distant_promise_does_not_freeze_todays_money():
    nw = _nw([Commitment("school fees", 31_000_000, date(2029, 1, 1))])
    assert nw.committed_total(TODAY) == 0
    assert dict(nw.allocate_surplus(10_000_000)).get("bonds", 0) > 0


def test_an_undated_promise_binds():
    """No date is not the same as far away -- it is a date you cannot plan around."""
    nw = _nw([Commitment("family loan", 31_000_000, None)])
    assert nw.committed_total(TODAY) == pytest.approx(31_000_000)
    assert "bonds" not in dict(nw.allocate_surplus(10_000_000))


def test_an_overdue_promise_still_binds():
    nw = _nw([Commitment("late bill", 31_000_000, date(2026, 1, 1))])
    assert nw.committed_total(TODAY) == pytest.approx(31_000_000)


def test_liquid_only_falls_back_to_savings_when_every_bucket_is_at_target():
    nw = _nw([Commitment("KPR", 31_000_000, date(2026, 9, 30))])
    for b in nw.buckets.values():
        b.target_pct = 0.0
    assert nw.allocate_surplus(10_000_000) == [("savings", 10_000_000)]


def test_yaml_round_trip(tmp_path):
    """Dates arrive from YAML as date objects or strings; both must load."""
    p = tmp_path / "h.yaml"
    p.write_text(
        "cash_idr: 9000000\n"
        "cashflow: {income_monthly: 17000000, spending_monthly: 7000000}\n"
        "commitments:\n"
        "  - {name: dated, amount_idr: 1000000, due: 2026-09-30}\n"
        "  - {name: stringy, amount_idr: 2000000, due: '2026-10-31'}\n"
        "  - {name: undated, amount_idr: 3000000}\n"
        "  - {name: broken, amount_idr: 4000000, due: not-a-date}\n",
        encoding="utf-8",
    )
    nw = load_networth(p)
    by = {c.name: c for c in nw.commitments}
    assert by["dated"].due == date(2026, 9, 30)
    assert by["stringy"].due == date(2026, 10, 31)
    assert by["undated"].due is None
    assert by["broken"].due is None, "an unreadable date must not crash the load"

# -- exit costs -------------------------------------------------------------
def test_exit_cost_override_replaces_the_class_default(tmp_path):
    """What leaving a position costs is a fact about your provider.

    Pegadaian quotes Tabungan Emas at the buyback rate, so the recorded value is
    already what you would receive -- charging the class-wide 3% spread on top
    overstated the cost of trimming gold by about Rp 577.000.
    """
    p = tmp_path / "h.yaml"
    p.write_text(
        textwrap.dedent("""\
            cash_idr: 9000000
            exit_costs:
              gold: {pct: 0.0, why: quoted at buyback}
              stocks: 0.004
            """),
        encoding="utf-8",
    )
    nw = load_networth(p)
    assert nw.exit_costs["gold"] == (0.0, "quoted at buyback")
    assert nw.exit_costs["stocks"] == (0.004, ""), "a bare number is allowed too"


def test_no_exit_cost_config_leaves_the_defaults_alone(tmp_path):
    p = tmp_path / "h.yaml"
    p.write_text("cash_idr: 9000000\n", encoding="utf-8")
    assert load_networth(p).exit_costs == {}


# -- wishes -----------------------------------------------------------------
def test_a_wish_never_binds_the_buffer():
    """A want is not a debt.

    If wanting an RTX shrank the safety net, wanting things would read as being
    poor and would quietly stop the payday plan investing.
    """
    nw = _nw([Commitment("RTX 5070 Ti", 15_000_000, date(2026, 9, 1), kind="wish")])
    assert nw.committed_total(TODAY) == 0
    assert nw.free_liquid(TODAY) == nw.liquid_now
    assert nw.emergency_shortfall == 0
    assert dict(nw.allocate_surplus(10_000_000)).get("bonds", 0) > 0


def test_wishes_are_listed_separately_from_obligations():
    nw = _nw([
        Commitment("KPR", 31_000_000, date(2026, 9, 30)),
        Commitment("RTX 5070 Ti", 15_000_000, None, kind="wish"),
    ])
    assert [w.name for w in nw.wishes()] == ["RTX 5070 Ti"]
    assert [c.name for c in nw.binding_commitments(TODAY)] == ["KPR"]


def test_an_undated_wish_still_does_not_bind():
    """Undated obligations bind; undated wishes must not, or the two collapse."""
    nw = _nw([Commitment("someday", 50_000_000, None, kind="wish")])
    assert nw.committed_total(TODAY) == 0


# -- income arriving before a bill ------------------------------------------
def test_paydays_before_counts_salaries_in_between():
    nw = _nw()
    nw.cashflow.payday_day = 25
    # 24 Aug -> 30 Sep spans the 25th of August and the 25th of September.
    assert nw.paydays_before(date(2026, 9, 30), TODAY) == 2
    assert nw.paydays_before(date(2026, 8, 24), TODAY) == 0
    assert nw.paydays_before(None, TODAY) == 0


def test_payday_on_the_31st_still_counts_in_a_short_month():
    nw = _nw()
    nw.cashflow.payday_day = 31
    # September has 30 days; the payday has to land on the 30th, not vanish.
    assert nw.paydays_before(date(2026, 10, 1), date(2026, 9, 1)) == 1


def test_a_bill_after_two_paydays_is_mostly_paid_by_them():
    """The naive version charges the whole bill to today's cash and reports a
    crisis the calendar resolves by itself."""
    nw = _nw([Commitment("KPR", 31_000_000, date(2026, 9, 30))])
    nw.cashflow.payday_day = 25
    assert nw.committed_total(TODAY) == pytest.approx(31_000_000)
    # Two paydays at Rp 10jt surplus arrive first.
    assert nw.committed_net_of_income(TODAY) == pytest.approx(11_000_000)


def test_a_bill_due_before_the_next_payday_is_charged_in_full():
    nw = _nw([Commitment("due now", 5_000_000, date(2026, 8, 24))])
    nw.cashflow.payday_day = 25
    assert nw.committed_net_of_income(TODAY) == pytest.approx(5_000_000)


def test_income_cannot_make_a_commitment_negative():
    """A small bill far away is covered, not a credit."""
    nw = _nw([Commitment("small", 1_000_000, date(2027, 1, 1))])
    nw.cashflow.payday_day = 25
    assert nw.committed_net_of_income(TODAY) == 0


def test_kind_round_trips_through_yaml(tmp_path):
    p = tmp_path / "h.yaml"
    p.write_text(
        textwrap.dedent("""\
            cash_idr: 9000000
            commitments:
              - {name: KPR, amount_idr: 31000000, due: 2026-09-30}
              - {name: RTX, amount_idr: 15000000, kind: wish}
            """),
        encoding="utf-8",
    )
    by = {c.name: c for c in load_networth(p).commitments}
    assert by["KPR"].kind == "obligation", "the default must be the binding one"
    assert by["RTX"].is_wish


# -- buy simulation ---------------------------------------------------------
def _real(commitments=None):
    """His actual balance sheet, so the numbers below are the ones he sees."""
    nw = _nw(commitments)
    nw.cashflow.payday_day = 25
    return nw


def test_a_purchase_that_breaks_the_buffer_is_not_safe_now():
    nw = _real()
    plan = nw.purchase_plan(17_438_001, today=TODAY)
    assert plan["safe_now"] is False


def test_an_outstanding_bill_is_subtracted_before_judging_safety():
    """The trap this exists for.

    Done by hand, this said August: liquid looked ample only because a Rp 31jt
    bill had not landed yet. Counting the buffer without the debt is how you
    approve a purchase that makes the debt unpayable.
    """
    price = 17_438_001
    without = _real().purchase_plan(price, today=TODAY)
    with_kpr = _real(
        [Commitment("KPR", 31_000_000, date(2026, 9, 30))]
    ).purchase_plan(price, today=TODAY)
    assert with_kpr["when"] > without["when"], "an unpaid bill must push the date out"


def test_the_walk_uses_the_real_payday_plan():
    """Money the plan sends to bonds stops being a safety net.

    Assuming every rupiah stays liquid moved the answer a full month, which is
    exactly the error that produced a wrong date by hand.
    """
    nw = _real([Commitment("KPR", 31_000_000, date(2026, 9, 30))])
    plan = nw.purchase_plan(17_438_001, today=TODAY)
    assert plan["when"] == date(2026, 12, 25)
    assert plan["months_after"] >= 6.0


def test_a_cheap_wish_is_safe_immediately():
    nw = _real()
    plan = nw.purchase_plan(1_000_000, today=TODAY)
    assert plan["safe_now"] is True
    assert plan["when"] is None


def test_an_impossible_wish_reports_no_date_rather_than_a_wrong_one():
    nw = _real()
    plan = nw.purchase_plan(5_000_000_000, today=TODAY)
    assert plan["safe_now"] is False
    assert plan["when"] is None, "better to say 'not within the horizon' than to guess"


def test_the_simulation_does_not_mutate_the_real_balance_sheet():
    """It walks a copy. Reporting must never move someone's money."""
    nw = _real([Commitment("KPR", 31_000_000, date(2026, 9, 30))])
    before = {n: b.value for n, b in nw.buckets.items()}
    names_before = [c.name for c in nw.commitments]
    nw.purchase_plan(17_438_001, today=TODAY)
    assert {n: b.value for n, b in nw.buckets.items()} == before
    assert [c.name for c in nw.commitments] == names_before


def test_wishes_are_excluded_from_the_walk():
    """A wish must not hold back the money being simulated for another wish."""
    a = _real().purchase_plan(17_438_001, today=TODAY)
    b = _real(
        [Commitment("something else", 40_000_000, None, kind="wish")]
    ).purchase_plan(17_438_001, today=TODAY)
    assert a["when"] == b["when"]


def test_buffer_at_the_safe_date_actually_clears_the_target():
    nw = _real([Commitment("KPR", 31_000_000, date(2026, 9, 30))])
    plan = nw.purchase_plan(17_438_001, today=TODAY)
    assert plan["liquid_after"] >= nw.emergency_target


# -- ordering ---------------------------------------------------------------
def test_wishes_are_ordered_by_when_you_can_have_them():
    """Not by price. The card answers "what is next", and the cheap thing is
    not always the next thing."""
    from lumbung.web.server import _wishes_soonest_first

    nw = _real([
        Commitment("expensive but soon", 5_000_000, None, kind="wish"),
        Commitment("cheap but far", 150_000_000, None, kind="wish"),
    ])
    names = [r["name"] for r in _wishes_soonest_first(nw)]
    assert names == ["expensive but soon", "cheap but far"]


def test_something_affordable_today_sorts_first():
    from lumbung.web.server import _wishes_soonest_first

    nw = _real([
        Commitment("big", 150_000_000, None, kind="wish"),
        Commitment("small", 500_000, None, kind="wish"),
    ])
    rows = _wishes_soonest_first(nw)
    assert rows[0]["name"] == "small"
    assert rows[0]["safe_now"] is True


def test_out_of_reach_wishes_sort_last_not_first():
    """A null date must not sort as "soonest" -- that would put the impossible
    thing at the top of the list."""
    from lumbung.web.server import _wishes_soonest_first

    nw = _real([
        Commitment("a yacht", 5_000_000_000, None, kind="wish"),
        Commitment("reachable", 20_000_000, None, kind="wish"),
    ])
    rows = _wishes_soonest_first(nw)
    assert rows[0]["name"] == "reachable"
    assert rows[-1]["name"] == "a yacht"
    assert rows[-1]["safe_when"] is None


def test_obligations_never_appear_in_the_wish_list():
    from lumbung.web.server import _wishes_soonest_first

    nw = _real([
        Commitment("KPR", 31_000_000, date(2026, 9, 30)),
        Commitment("RTX", 17_438_001, None, kind="wish"),
    ])
    assert [r["name"] for r in _wishes_soonest_first(nw)] == ["RTX"]


# -- possessions ------------------------------------------------------------
def test_possessions_never_enter_investable_net_worth():
    """The whole reason this category exists.

    A house in the same pool as the shares turns a real 48% concentration into a
    comfortable 12%. The risk does not change; only the warning disappears.
    """
    from lumbung.networth import Possession

    plain = _nw()
    rich = _nw()
    rich.possessions = [
        Possession("House", 400_000_000, depreciating=False),
        Possession("Car", 120_000_000),
    ]
    assert rich.total == plain.total
    assert rich.possessions_total == 520_000_000
    assert rich.total_with_possessions == plain.total + 520_000_000


def test_concentration_is_measured_against_investable_only():
    from lumbung.networth import Possession

    nw = _nw()
    before = nw.buckets["stocks"].weight(nw.total)
    nw.possessions = [Possession("House", 400_000_000, depreciating=False)]
    assert nw.buckets["stocks"].weight(nw.total) == before


def test_possessions_do_not_move_the_allocation_plan():
    from lumbung.networth import Possession

    nw = _nw()
    before = dict(nw.allocate_surplus(10_000_000))
    nw.possessions = [Possession("House", 400_000_000, depreciating=False)]
    assert dict(nw.allocate_surplus(10_000_000)) == before


def test_possessions_do_not_count_as_emergency_buffer():
    """You cannot eat a MacBook. Liquidity is cash, savings and gold."""
    from lumbung.networth import Possession

    nw = _nw()
    before = nw.liquid_now
    nw.possessions = [Possession("Car", 120_000_000)]
    assert nw.liquid_now == before
    assert nw.emergency_shortfall == _nw().emergency_shortfall


def test_possessions_load_from_yaml(tmp_path):
    p = tmp_path / "h.yaml"
    p.write_text(
        textwrap.dedent("""\
            cash_idr: 9000000
            possessions:
              - {name: House, value_idr: 400000000, depreciating: false}
              - {name: Car, value_idr: 120000000}
              - {name: Laptop}
            """),
        encoding="utf-8",
    )
    nw = load_networth(p)
    by = {x.name: x for x in nw.possessions}
    assert by["House"].depreciating is False
    assert by["Car"].depreciating is True, "things lose value unless you say otherwise"
    assert by["Laptop"].value_idr == 0, "an unvalued possession loads as zero, not a crash"
    assert nw.possessions_total == 520_000_000
