"""Purchase advice: affordability, goal cost, and the cash-vs-card decision."""

from __future__ import annotations

import pytest

from lumbung.spending import (
    CARD_MONTHLY_RATE,
    advise_payment,
    assess,
    by_category,
    connect,
    monthly_totals,
    recent,
    record,
)


def verdict(price, liquid=10_000_000, spending=7_000_000, surplus=10_000_000,
            months=6, delay=0.0):
    return assess(
        item="thing", price=price, liquid=liquid, spending_monthly=spending,
        surplus_monthly=surplus, emergency_months=months, goal_delay_months=delay,
    )


# ------------------------------------------------------------ affordability
def test_comfortably_affordable_is_yes():
    v = verdict(2_000_000, liquid=60_000_000)
    assert v.verdict == "YES"
    assert not v.warnings


def test_dipping_below_the_target_is_tight_not_no():
    """Below target but still a real buffer -- a judgement call, not a refusal."""
    v = verdict(5_000_000, liquid=45_000_000)   # 40jt left = 5.7 months
    assert v.verdict == "TIGHT"
    assert any("safety net" in w or "below" in w for w in v.warnings)


def test_leaving_under_three_months_is_no():
    v = verdict(30_000_000, liquid=50_000_000)  # 20jt left = 2.9 months
    assert v.verdict == "NO"


def test_more_than_you_have_is_no():
    v = verdict(14_000_000, liquid=10_000_000)
    assert v.verdict == "NO"
    assert v.liquid_after < 0
    assert any("do not have" in w for w in v.warnings)


def test_cost_is_expressed_in_months_of_saving():
    v = verdict(10_000_000, surplus=10_000_000)
    assert v.surplus_months == pytest.approx(1.0)


def test_goal_delay_is_surfaced_when_material():
    v = verdict(2_000_000, liquid=60_000_000, delay=2.4)
    assert any("goal" in r for r in v.reasons)
    assert not any("goal" in r for r in verdict(2_000_000, liquid=60_000_000).reasons)


# ------------------------------------------------------------ cash vs card
def test_no_cash_means_wait_not_credit():
    """A card defers a purchase; it never makes one affordable."""
    a = advise_payment(14_000_000, can_pay_cash=False, zero_percent_available=True)
    assert a.method == "wait"


def test_zero_percent_while_holding_cash_beats_paying_cash():
    a = advise_payment(
        14_000_000, can_pay_cash=True, zero_percent_available=True,
        tenor_months=12, savings_rate=0.06,
    )
    assert a.method == "credit0"
    assert a.float_benefit == pytest.approx(14_000_000 / 2 * 0.06)


def test_without_a_zero_offer_cash_wins_by_a_lot():
    a = advise_payment(14_000_000, can_pay_cash=True, zero_percent_available=False,
                       tenor_months=12)
    assert a.method == "cash"
    # 21%/yr of interest against ~3% of forgone savings is not close.
    assert a.credit_cost == pytest.approx(14_000_000 * CARD_MONTHLY_RATE * 12)
    assert a.credit_cost > 14_000_000 / 2 * 0.06


def test_bi_rate_cap_is_the_documented_one():
    assert CARD_MONTHLY_RATE == pytest.approx(0.0175)


# --------------------------------------------------------------- recording
def test_record_and_summarise(tmp_path):
    conn = connect(tmp_path / "e.db")
    record(conn, amount=14_000_000, item="RTX 5070 Ti", category="tech", method="credit0")
    record(conn, amount=250_000, item="groceries", category="food")
    record(conn, amount=180_000, item="more groceries", category="food")

    rows = recent(conn, days=30)
    assert len(rows) == 3

    cats = dict((c, amt) for c, amt, _ in by_category(conn, days=30))
    assert cats["tech"] == 14_000_000
    assert cats["food"] == 430_000
    # Ordered biggest first, so the thing worth noticing is at the top.
    assert by_category(conn, days=30)[0][0] == "tech"

    assert monthly_totals(conn, months=1)[-1][1] == pytest.approx(14_430_000)


def test_old_expenses_fall_out_of_the_window(tmp_path):
    import time as _t

    conn = connect(tmp_path / "e.db")
    record(conn, amount=100_000, item="old", category="food")
    conn.execute("UPDATE expenses SET ts=?", (int(_t.time()) - 200 * 86400,))
    conn.commit()
    assert recent(conn, days=90) == []
    assert recent(conn, days=365) != []


# ------------------------------------------------------- month-to-month view
def _seed(conn, offset_months, amounts):
    import time as _t

    ts = int(_t.time()) - offset_months * 31 * 86400
    for a in amounts:
        conn.execute(
            "INSERT INTO expenses(ts,amount,item,category,method) VALUES(?,?,?,?,?)",
            (ts, a, "x", "other", "cash"),
        )
    conn.commit()


def test_profile_averages_complete_months_only(tmp_path):
    """The current month is partial. Averaging it in would understate spending
    and hand back a surplus that does not exist."""
    from lumbung.spending import connect, spending_profile

    conn = connect(tmp_path / "e.db")
    _seed(conn, 2, [3_000_000, 2_500_000, 900_000])   # 6.4jt
    _seed(conn, 1, [6_000_000, 4_000_000, 1_200_000])  # 11.2jt
    _seed(conn, 0, [800_000, 400_000, 300_000])        # 1.5jt, in progress

    p = spending_profile(conn, budgeted=7_000_000, months=3)
    assert len(p.months) == 2
    assert p.current is not None and p.current[1] == pytest.approx(1_500_000)
    assert p.average == pytest.approx(8_800_000)
    assert p.basis == pytest.approx(8_800_000)


def test_profile_reports_the_swing_not_just_the_average():
    """Spending is lumpy; the range is what tells you the average is not the
    whole story."""
    from lumbung.spending import SpendingProfile

    p = SpendingProfile(
        months=[("2026-06", 6_400_000, 3), ("2026-07", 11_200_000, 3)],
        budgeted=7_000_000,
    )
    assert p.lowest == 6_400_000 and p.highest == 11_200_000
    assert p.swing_pct == pytest.approx(75.0)
    assert p.verdict == "over budget"


def test_overspending_reduces_the_deployable_surplus():
    from lumbung.spending import SpendingProfile

    p = SpendingProfile(months=[("2026-07", 8_800_000, 5)], budgeted=7_000_000)
    assert p.surplus(17_000_000) == pytest.approx(8_200_000)   # not the assumed 10jt


def test_untracked_months_fall_back_to_the_budget(tmp_path):
    """Nothing recorded must not read as 'spent nothing, huge surplus'."""
    from lumbung.spending import connect, spending_profile

    p = spending_profile(connect(tmp_path / "e.db"), budgeted=7_000_000)
    assert not p.tracked
    assert p.basis == 7_000_000
    assert p.surplus(17_000_000) == pytest.approx(10_000_000)


def test_months_with_nothing_logged_are_skipped_not_counted_as_zero(tmp_path):
    from lumbung.spending import connect, spending_profile

    conn = connect(tmp_path / "e.db")
    _seed(conn, 1, [5_000_000, 1_000_000, 500_000])
    p = spending_profile(conn, budgeted=7_000_000, months=3)
    assert [m for m, _, _ in p.months] == [p.months[0][0]]
    assert p.average == pytest.approx(6_500_000)


def test_a_thinly_logged_month_is_not_averaged_with_full_ones():
    """April held six credit-card rows recovered from a statement; June and July
    held forty each. Averaging all three dragged the figure down by a third and
    nothing on screen contradicted it."""
    from lumbung.spending import SpendingProfile

    p = SpendingProfile(
        months=[
            ("2026-04", 2_385_827, 6),    # statement backfill, not a real month
            ("2026-06", 13_286_846, 41),
            ("2026-07", 27_515_986, 42),
        ],
        budgeted=12_000_000,
    )
    assert [m[0] for m in p.tracked_months] == ["2026-06", "2026-07"]
    assert p.average == pytest.approx((13_286_846 + 27_515_986) / 2)


def test_the_floor_adapts_rather_than_locking_out_a_low_volume_logger():
    """Someone who records eight purchases a month is not under-logging -- that
    is their whole month. A flat threshold tuned to a heavy logger would drop
    every month they have."""
    from lumbung.spending import SpendingProfile

    p = SpendingProfile(
        months=[("2026-06", 4_000_000, 7), ("2026-07", 4_400_000, 8)],
        budgeted=4_000_000,
    )
    assert len(p.tracked_months) == 2
    assert p.tracked


def test_the_floor_never_drops_below_the_absolute_minimum():
    """A single-entry month stays untracked even when it is the busiest one."""
    from lumbung.spending import SpendingProfile

    p = SpendingProfile(months=[("2026-05", 501_750, 1)], budgeted=7_000_000)
    assert p.tracked_months == []
    assert not p.tracked
    assert p.basis == 7_000_000  # falls back to the budget, not to zero


def test_a_refused_sync_reports_a_reason_rather_than_going_quiet(tmp_path, monkeypatch):
    """The daily digest only spoke when the sync CHANGED something, so the guard
    that matters most -- actuals diverging too far to apply unattended -- fired
    into silence every day. The refusal must carry enough to report."""
    import datetime

    import lumbung.web.settings as st
    from lumbung.spending import connect, record

    holdings = tmp_path / "holdings.yaml"
    holdings.write_text(
        "cashflow:\n  income_monthly: 17000000.0\n  spending_monthly: 7000000.0\n"
        "  payday_day: 25\nstocks: []\ncash_idr: 1000000\n"
        "# padding so the small-config guard does not trip\n" + "# x\n" * 40,
        encoding="utf-8",
    )
    monkeypatch.setattr(st, "_holdings_path", lambda: holdings)

    conn = connect(tmp_path / "t.db")
    today = datetime.date.today().replace(day=1)
    for back in (1, 2):
        month = (today - datetime.timedelta(days=1)).replace(day=1) if back == 1 else (
            (today - datetime.timedelta(days=1)).replace(day=1) - datetime.timedelta(days=1)
        ).replace(day=1)
        for i in range(12):
            ts = int(datetime.datetime(month.year, month.month, i + 1, 12).timestamp())
            conn.execute(
                "INSERT INTO expenses(ts,amount,item,category,method) VALUES(?,?,?,?,?)",
                (ts, 1_800_000, "x", "other", "cash"),
            )
    conn.commit()

    out = st.sync_spending_from_actuals(conn, months=3)
    assert out["changed"] is False, "a 3x divergence must not be applied unattended"
    assert out["reason"], "a refusal with no reason is indistinguishable from silence"
    assert out["actual_average"] > out["spending_monthly"]
    # and the stored figure is untouched
    assert "spending_monthly: 7000000.0" in holdings.read_text(encoding="utf-8")


def test_category_plan_rows_sum_to_the_headline_spending_figure(tmp_path):
    """A category table that disagreed with the number on Home would be a new
    version of the oldest bug here. Same tracked months, same exclusions."""
    import datetime

    from lumbung.spending import category_plan, connect, spending_profile

    conn = connect(tmp_path / "t.db")
    today = datetime.date.today().replace(day=1)
    prev = (today - datetime.timedelta(days=1)).replace(day=1)
    prev2 = (prev - datetime.timedelta(days=1)).replace(day=1)
    for month in (prev, prev2):
        for i in range(12):
            ts = int(datetime.datetime(month.year, month.month, i + 1, 12).timestamp())
            conn.execute(
                "INSERT INTO expenses(ts,amount,item,category,method) VALUES(?,?,?,?,?)",
                (ts, 500_000, "groceries", "food", "cash"),
            )
        # one lumpy item per month that the baseline excludes
        ts = int(datetime.datetime(month.year, month.month, 15, 12).timestamp())
        conn.execute(
            "INSERT INTO expenses(ts,amount,item,category,method) VALUES(?,?,?,?,?)",
            (ts, 9_000_000, "Widget car repair", "transport", "cash"),
        )
    conn.commit()

    exclude = ("%car repair%",)
    plan = category_plan(conn, {"food": 4_000_000}, months=3, exclude=exclude)
    assert [r["category"] for r in plan] == ["food"], "the excluded item must not appear"
    assert plan[0]["average"] == pytest.approx(6_000_000)
    assert plan[0]["cut"] == pytest.approx(2_000_000)

    # and the same months the profile averages over
    prof = spending_profile(conn, 0.0, months=3)
    assert plan[0]["months_used"] == len(prof.tracked_months)


def test_a_target_above_what_you_spend_is_not_headroom(tmp_path):
    """A generous ceiling must never report a negative cut -- that would read as
    permission to spend up to it."""
    import datetime

    from lumbung.spending import category_plan, connect

    conn = connect(tmp_path / "t.db")
    prev = (datetime.date.today().replace(day=1) - datetime.timedelta(days=1)).replace(day=1)
    for i in range(12):
        ts = int(datetime.datetime(prev.year, prev.month, i + 1, 12).timestamp())
        conn.execute(
            "INSERT INTO expenses(ts,amount,item,category,method) VALUES(?,?,?,?,?)",
            (ts, 100_000, "x", "fun", "cash"),
        )
    conn.commit()
    plan = category_plan(conn, {"fun": 5_000_000}, months=3)
    assert plan[0]["cut"] == 0.0
    assert plan[0]["has_target"] is True
    conn.close()

# -- statement reconciliation -------------------------------------------------
def test_reconcile_statement_dedupes_and_splits_flows(tmp_path):
    import time as time_mod

    from lumbung.spending import connect, reconcile_statement, record

    conn = connect(tmp_path / "t.db")
    aug3 = int(time_mod.mktime((2026, 8, 3, 0, 0, 0, 0, 0, -1)))
    record(conn, amount=45_000, item="Kopi Kenangan", category="food", ts=aug3)

    res = reconcile_statement(conn, [
        {"date": "2026-08-03", "amount": -45_000, "item": "Kopi Kenangan",
         "category": "food"},                     # already logged -> skipped
        {"date": "2026-08-04", "amount": -17_000, "item": "Biaya Admin BCA",
         "category": "fees"},                     # new expense
        {"date": "2026-08-05", "amount": 2_000_000, "item": "Transfer Masuk",
         "category": "other"},                    # credit -> income
        {"date": "not-a-date", "amount": 1, "item": "x"},  # unreadable
    ], note="statement AGU")
    assert len(res["recorded"]) == 2
    assert res["skipped"] == [{"item": "Kopi Kenangan", "amount": 45_000}]
    assert len(res["failed"]) == 1
    exp = recent(conn, days=400)
    assert any(r["item"] == "Biaya Admin BCA" and r["method"] == "statement"
               for r in exp)
    inc = conn.execute("SELECT amount,source FROM income WHERE source='Transfer Masuk'").fetchall()
    assert inc and inc[0][0] == 2_000_000
    # the recorded expense carries the STATEMENT's date, not today's
    admin = next(r for r in exp if r["item"] == "Biaya Admin BCA")
    assert time_mod.strftime("%Y-%m-%d", time_mod.localtime(admin["ts"])) == "2026-08-04"
