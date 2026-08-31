"""Tests for the recommendation list.

There is no tick box: every item is computed from live numbers, so acting on one
changes the numbers and the item disappears by itself. What is worth testing is
therefore the generation and the ranking -- and above all the refusals, since a
list that recommends the wrong action confidently is worse than no list.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from lumbung.actions import build_actions


# -- doubles ----------------------------------------------------------------
@dataclass
class FakeBucket:
    value: float
    target_pct: float

    def gap_idr(self, total: float) -> float:
        return self.target_pct * total - self.value


@dataclass
class FakeCashFlow:
    """Mirrors networth.CashFlow. Keep the method NAMES identical -- an earlier
    version of this double invented `days_to_payday`, the tests passed, and the
    endpoint blew up on the real object at request time."""

    income_monthly: float = 17_000_000
    spending_monthly: float = 7_000_000
    payday_day: int = 25
    _days: int = 99

    @property
    def surplus(self) -> float:
        return self.income_monthly - self.spending_monthly

    def days_until_payday(self, today=None) -> int:
        return self._days

    def is_payday(self, today=None) -> bool:
        return False


@dataclass
class FakeNetWorth:
    """Every field build_actions reads from networth.NetWorth.

    Kept in step with the real class deliberately. Twice already a missing
    attribute here passed the suite and then raised at request time, which is the
    one place a balance-sheet bug should never first appear.
    """

    buckets: dict
    cashflow: FakeCashFlow = field(default_factory=FakeCashFlow)
    emergency_months_target: int = 6
    emergency_shortfall: float = 0.0
    months_covered_liquid: float = 6.0
    months_to_fund_emergency: float = 0.0
    liquid_now: float = 52_000_000.0
    emergency_target: float = 42_000_000.0
    commitments: list = field(default_factory=list)
    exit_costs: dict = field(default_factory=dict)

    @property
    def total(self) -> float:
        return sum(b.value for b in self.buckets.values())

    def committed_total(self, today=None) -> float:
        return sum(c.amount_idr for c in self.commitments if c.is_binding(today))

    def free_liquid(self, today=None) -> float:
        return self.liquid_now - self.committed_total(today)

    def months_covered_free(self, today=None) -> float:
        # With nothing promised this IS the gross figure, exactly as in the real
        # class. Tests express a thin buffer through months_covered_liquid, and
        # deriving it from liquid_now here would quietly ignore them.
        if not self.commitments:
            return self.months_covered_liquid
        s = self.cashflow.spending_monthly
        return self.free_liquid(today) / s if s else 0.0

    def allocate_surplus(self, amount=None):
        return [("bonds", 6_000_000.0), ("stocks", 4_000_000.0)]


def nw(**kw) -> FakeNetWorth:
    buckets = kw.pop("buckets", {
        "bonds": FakeBucket(0, 0.0),
        "cash": FakeBucket(10_000_000, 0.0),
        "savings": FakeBucket(10_000_000, 0.0),
        "gold": FakeBucket(32_000_000, 0.0),
    })
    return FakeNetWorth(buckets=buckets, **kw)


# -- generation -------------------------------------------------------------
def test_no_problems_means_no_actions():
    assert build_actions(nw()) == []


def test_emergency_shortfall_is_urgent_when_under_three_months():
    acts = build_actions(nw(emergency_shortfall=20_000_000, months_covered_liquid=1.5))
    assert acts[0].kind == "emergency_fund"
    assert acts[0].severity == "urgent"


def test_emergency_shortfall_is_only_soon_when_buffer_is_healthy():
    acts = build_actions(nw(emergency_shortfall=2_000_000, months_covered_liquid=5.5))
    assert acts[0].severity == "soon"


def test_emergency_fund_outranks_rebalancing():
    """A thin safety net must sort above a tidy allocation."""
    buckets = {"bonds": FakeBucket(0, 0.5), "cash": FakeBucket(10_000_000, 0.0)}
    acts = build_actions(
        nw(buckets=buckets, emergency_shortfall=5_000_000, months_covered_liquid=2.0)
    )
    assert acts[0].kind == "emergency_fund"
    assert any(a.kind == "rebalance" for a in acts)


def test_deployment_does_not_appear_before_the_money_lands():
    """Advice you cannot act on trains you to ignore the list."""
    cf = FakeCashFlow(payday_day=25)
    before = build_actions(nw(cashflow=cf), today=date(2026, 8, 23))
    assert not any(a.kind == "deploy_surplus" for a in before)


def test_deployment_appears_once_the_salary_has_landed():
    cf = FakeCashFlow(payday_day=25)
    on_day = build_actions(nw(cashflow=cf), today=date(2026, 8, 25))
    assert any(a.kind == "deploy_surplus" for a in on_day)

    a_few_days_later = build_actions(nw(cashflow=cf), today=date(2026, 8, 28))
    assert any(a.kind == "deploy_surplus" for a in a_few_days_later)


def test_deployment_expires_before_the_next_payday():
    cf = FakeCashFlow(payday_day=25)
    much_later = build_actions(nw(cashflow=cf), today=date(2026, 9, 10))
    assert not any(a.kind == "deploy_surplus" for a in much_later)


def test_payday_on_the_31st_still_lands_in_a_short_month():
    """A payday set to the 31st must not silently skip February."""
    cf = FakeCashFlow(payday_day=31)
    acts = build_actions(nw(cashflow=cf), today=date(2026, 2, 28))
    assert any(a.kind == "deploy_surplus" for a in acts)


def test_ticker_is_shown_without_the_yahoo_suffix():
    from lumbung.goal import SellReview, SellSignal

    rev = SellReview(ticker="BBCA.JK")
    rev.signals = [SellSignal("concentration", "act", "48% of your net worth")]
    acts = build_actions(nw(), reviews=[rev])
    stock = [a for a in acts if a.kind.startswith("stock_")][0]
    assert stock.subject == "BBCA"
    assert ".JK" not in stock.title


def test_overweight_never_produces_a_buy_action():
    """An overweight bucket must never be told to buy more of itself."""
    buckets = {"gold": FakeBucket(50_000_000, 0.10), "bonds": FakeBucket(0, 0.0)}
    acts = build_actions(nw(buckets=buckets))
    assert not any(a.kind == "rebalance" and a.subject == "gold" for a in acts)


def test_overweight_is_surfaced_rather_than_silently_dropped():
    """Omitting it entirely left a +9 on the dashboard with nothing explaining
    why no action was suggested. The excess is reported; the advice is separate.
    """
    buckets = {"gold": FakeBucket(50_000_000, 0.10), "bonds": FakeBucket(0, 0.0)}
    acts = build_actions(nw(buckets=buckets))
    over = [a for a in acts if a.kind == "overweight"]
    assert len(over) == 1
    assert over[0].subject == "gold"
    assert over[0].severity == "idea", "an overweight is never urgent"


def test_overweight_names_the_cost_of_selling():
    """Gold and stocks differ by more than 10x in exit cost; the advice must
    reflect which one it is talking about."""
    gold = build_actions(nw(buckets={"gold": FakeBucket(50_000_000, 0.10)}))
    detail = [a for a in gold if a.kind == "overweight"][0].detail
    assert "spread" in detail, "gold's cost is the Pegadaian spread, not tax"

    stocks = build_actions(nw(buckets={"stocks": FakeBucket(50_000_000, 0.10)}))
    detail = [a for a in stocks if a.kind == "overweight"][0].detail
    assert "PPh" in detail


def test_a_quickly_diluted_overweight_recommends_waiting():
    """When new money fixes it soon, waiting genuinely is cheaper than selling."""
    buckets = {"stocks": FakeBucket(40_000_000, 0.35), "cash": FakeBucket(60_000_000, 0.0)}
    acts = build_actions(nw(buckets=buckets))
    over = [a for a in acts if a.kind == "overweight"]
    if over:
        assert "cheaper than" in over[0].detail


def test_small_overweight_is_ignored_as_noise():
    buckets = {"gold": FakeBucket(21_000_000, 0.20), "cash": FakeBucket(79_000_000, 0.0)}
    acts = build_actions(nw(buckets=buckets))
    assert not [a for a in acts if a.kind == "overweight" and a.subject == "gold"]


def test_small_drift_is_not_worth_an_action():
    buckets = {"bonds": FakeBucket(96_000_000, 1.0)}
    assert not any(a.kind == "rebalance" for a in build_actions(nw(buckets=buckets)))


def test_halted_engine_is_urgent():
    acts = build_actions(nw(), bot={"halted": True})
    assert acts[0].kind == "bot_halted"
    assert acts[0].severity == "urgent"


def test_only_act_level_stock_signals_become_actions():
    from lumbung.goal import SellReview, SellSignal

    rev = SellReview(ticker="BBCA")
    rev.signals = [
        SellSignal("concentration", "act", "48% of your portfolio"),
        SellSignal("price", "watch", "down a bit"),
    ]
    acts = build_actions(nw(), reviews=[rev])
    stock = [a for a in acts if a.subject == "BBCA"]
    assert len(stock) == 1
    assert "concentration" in stock[0].title


def test_bond_offer_urgency_rises_near_the_close(monkeypatch):
    from lumbung.bonds import Offering

    o = Offering(
        series="SR025T3", kind="SR", tenor_years=3, coupon=0.068,
        opens=date(2026, 8, 1), closes=date(2026, 9, 16), matures=date(2029, 9, 10),
        min_idr=1_000_000, tradeable=True,
    )
    soon = build_actions(nw(), offerings=[o], today=date(2026, 9, 12))
    assert [a for a in soon if a.kind == "bond_offer"][0].severity == "soon"

    later = build_actions(nw(), offerings=[o], today=date(2026, 8, 20))
    assert [a for a in later if a.kind == "bond_offer"][0].severity == "idea"


def test_closed_offerings_are_not_listed():
    from lumbung.bonds import Offering

    o = Offering(
        series="OLD", kind="SR", tenor_years=3, coupon=0.068,
        opens=date(2026, 1, 1), closes=date(2026, 2, 1), matures=date(2029, 1, 1),
        min_idr=1_000_000, tradeable=True,
    )
    assert not build_actions(nw(), offerings=[o], today=date(2026, 8, 23))


# -- concentration is measured against the whole balance sheet ---------------
class FakeHolding:
    ticker = "BBCA.JK"


class FakeReport:
    """Every field sell_signals reads from holdings.HoldingReport.

    Kept complete on purpose: a partial double passes until the code touches the
    field you left out, and then fails at request time rather than in the suite.
    """

    def __init__(self, value: float, price: float = 6450.0):
        self.holding = FakeHolding()
        self.market_value = value
        self.price = price
        self.ttm_dividend_per_share = 0.0
        self.uptrend = True
        self.yield_on_market_pct = 5.5
        self.monthly_income = value * 0.055 / 12
        self.low_52w = price * 0.85


def _concentration(rev):
    return [s for s in rev.signals if s.rule == "concentration"]


def test_single_stock_is_not_100pct_of_a_diversified_balance_sheet():
    """The bug this guards: measured inside the stock sleeve, the only stock you
    own is 100% of "the portfolio" by definition, so the rule fires forever and
    demands a large sale that owning bonds and gold should already have answered.
    """
    from lumbung.goal import sell_signals

    r = FakeReport(64_500_000)
    sleeve_only = sell_signals(r, portfolio_value=64_500_000)
    assert _concentration(sleeve_only), "sanity: sleeve basis does flag it"
    assert "100%" in _concentration(sleeve_only)[0].detail

    whole = sell_signals(r, portfolio_value=64_500_000, net_worth=135_496_670)
    assert "48%" in _concentration(whole)[0].detail
    assert "net worth" in _concentration(whole)[0].detail


def test_net_worth_basis_shrinks_the_suggested_trim():
    from lumbung.goal import sell_signals

    r = FakeReport(64_500_000)
    sleeve = sell_signals(r, portfolio_value=64_500_000)
    whole = sell_signals(r, portfolio_value=64_500_000, net_worth=135_496_670)
    assert whole.suggested_trim_lots < sleeve.suggested_trim_lots


def test_no_concentration_signal_once_the_holding_is_within_the_ceiling():
    """The default ceiling is 20% of net worth, not 40%.

    Published guidance puts 5-10% per position as the consensus and calls 10-20%
    a red flag; the old 0.40 was looser than any source and made the rule almost
    unreachable. 22% must now fire, 15% must not.
    """
    from lumbung.goal import sell_signals

    over = sell_signals(FakeReport(30_000_000), portfolio_value=30_000_000,
                        net_worth=135_496_670)
    assert _concentration(over), "22% of net worth should breach a 20% ceiling"

    under = sell_signals(FakeReport(20_000_000), portfolio_value=20_000_000,
                         net_worth=135_496_670)
    assert not _concentration(under), "15% of net worth is within the ceiling"


def test_missing_net_worth_falls_back_to_the_sleeve():
    """Callers that do not know net worth must still get a usable answer."""
    from lumbung.goal import sell_signals

    r = FakeReport(64_500_000)
    rev = sell_signals(r, portfolio_value=64_500_000, net_worth=0.0)
    assert _concentration(rev)
    assert "portfolio" in _concentration(rev)[0].detail


# -- the blended yield behind the goal ---------------------------------------
def test_blended_yield_counts_zero_yielding_buckets():
    """The bug this guards: a flat 7% treated gold, cash and crypto as if they
    earned. A fifth of the target allocation pays nothing, and assuming
    otherwise understated the capital needed for Rp 3jt/month by ~Rp 300jt.

    Was ~4.4% until the stock figure was corrected too: the constant was
    commented "net of the 10% final PPh" while holding the gross 5.5%. Netting
    it moves the blend to ~4.18% and the capital to about Rp 861jt.
    """
    from lumbung.goal import blended_yield

    weights = {"stocks": 0.40, "bonds": 0.20, "gold": 0.10,
               "savings": 0.20, "cash": 0.07, "crypto": 0.03}
    rate = blended_yield(weights)
    assert 0.041 < rate < 0.043, f"expected ~4.18%, got {rate:.4f}"
    assert rate < 0.07, "must be well below the old flat assumption"


def test_all_gold_yields_nothing():
    from lumbung.goal import blended_yield

    assert blended_yield({"gold": 1.0}) == 0.0
    assert blended_yield({"cash": 1.0}) == 0.0
    assert blended_yield({"crypto": 1.0}) == 0.0


def test_more_bonds_raises_the_blend():
    from lumbung.goal import blended_yield

    conservative = blended_yield({"bonds": 0.5, "savings": 0.5})
    with_gold = blended_yield({"bonds": 0.4, "savings": 0.4, "gold": 0.2})
    assert with_gold < conservative, "adding a zero-yield asset must lower the blend"


def test_capital_required_scales_with_the_real_yield():
    """Halve the yield and you need roughly twice the capital."""
    from lumbung.goal import blended_yield, plan_income_goal

    honest = blended_yield({"stocks": 0.40, "bonds": 0.20, "gold": 0.10,
                            "savings": 0.20, "cash": 0.07, "crypto": 0.03})
    a = plan_income_goal(monthly_target=3_000_000, current_capital=0,
                         blended_return=honest)
    b = plan_income_goal(monthly_target=3_000_000, current_capital=0,
                         blended_return=0.07)
    assert a.capital_required > b.capital_required * 1.5


def test_a_wish_never_becomes_a_checklist_item():
    """A wish is not a to-do.

    It leaked once: an undated wish produced a "no date recorded / SOON" action
    whose text said it was being held out of bonds and the trading sleeve. That
    is false for a wish, and it contradicted the split in the one place the user
    actually reads.
    """
    from datetime import date as _date

    from lumbung.networth import Commitment

    acts = build_actions(nw(commitments=[
        Commitment("KPR", 31_000_000, _date(2026, 9, 30)),
        Commitment("RTX", 17_438_001, None, kind="wish"),
        Commitment("house", 150_000_000, None, kind="wish"),
    ]), today=_date(2026, 8, 24))
    subjects = [a.subject for a in acts if a.kind == "commitment"]
    assert subjects == ["KPR"], f"wishes leaked onto the checklist: {subjects}"
