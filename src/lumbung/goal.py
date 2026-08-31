"""Income-goal planning, and rule-based trim/exit checks for holdings you own.

Two separate jobs that share one theme -- being explicit about arithmetic that is
easy to feel your way through and get wrong:

* `plan_income_goal` answers "what capital does Rp X/month actually require, and
  how long does it take to get there". The honest answer is usually a much larger
  number and a much longer horizon than it feels like.
* `sell_signals` turns "should I sell?" into named, checkable rules. It never
  emits a bare "SELL" -- it reports which rules fired and why, because the
  decision depends on things this program cannot see (your job, your taxes, what
  else you need the money for).

Reference rates used for planning, current as of 2026-08-23:
  BI Rate 5.75% · SBN Ritel (ORI/SR/ST/SBR) coupons up to ~6.25%
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Blended long-run assumptions. Deliberately conservative: planning on optimistic
# returns is how people under-save and discover the shortfall too late to fix it.
ASSUMED_RETURNS = {
    "SBN retail bonds": 0.0625,
    "IDX dividend stocks": 0.070,
    "bank deposit": 0.0475,
}
DEFAULT_BLENDED = 0.070

# What each bucket actually pays, net of Indonesian tax. Used to derive the
# blended yield from a real allocation instead of assuming one.
#
# Note gold, cash and crypto are ZERO. That is the whole reason this exists: a
# flat 7% assumption quietly treats every rupiah as if it were earning, and a
# target allocation holding 20% in gold, cash and crypto cannot produce 7% of
# income no matter how large it grows. The error is not small -- it understated
# the capital required for Rp 3jt/month by about Rp 300 million.
# Assumed gross dividend yield for IDX dividend names, and the final PPh
# withheld on it. Kept as two named numbers with the arithmetic shown, because
# the constant below was labelled "net" while holding the gross figure -- and a
# comment claiming a number is net is not the same as it being net.
IDX_DIVIDEND_YIELD_GROSS = 0.055
DIVIDEND_TAX = 0.10

BUCKET_YIELD = {
    "stocks": IDX_DIVIDEND_YIELD_GROSS * (1 - DIVIDEND_TAX),  # 4.95% net
    "bonds": 0.0621,    # SBN Ritel, net of the 10% final PPh
    "savings": 0.048,   # digital bank, net of the 20% final PPh
    "gold": 0.0,        # pays nothing; it only moves in price
    "cash": 0.0,
    "crypto": 0.0,      # a trading sleeve, not an income sleeve
    "other": 0.0,
}


def blended_yield(weights: dict[str, float], yields: dict[str, float] | None = None) -> float:
    """Income rate implied by an allocation.

    `weights` maps bucket -> share (0..1). Buckets that pay nothing drag the
    blend down exactly as they do in reality, which is the point: living off the
    yield means living off the part that actually yields.
    """
    y = dict(BUCKET_YIELD)
    if yields:
        y.update(yields)
    return sum(w * y.get(name, 0.0) for name, w in weights.items())


@dataclass
class GoalPlan:
    monthly_target: float
    blended_return: float
    current_capital: float
    monthly_contribution: float

    @property
    def annual_target(self) -> float:
        return self.monthly_target * 12

    @property
    def capital_required(self) -> float:
        """Capital whose yield alone funds the target, without drawing it down."""
        return self.annual_target / self.blended_return if self.blended_return else float("inf")

    @property
    def gap(self) -> float:
        return max(0.0, self.capital_required - self.current_capital)

    @property
    def capital_multiple(self) -> float:
        return self.capital_required / self.current_capital if self.current_capital else 0.0

    @property
    def income_now(self) -> float:
        """What the current capital already throws off, per month."""
        return self.current_capital * self.blended_return / 12

    def years_to_target(self, *, max_years: int = 60) -> float | None:
        """Years until capital + contributions compound to the required amount.

        Monthly contributions, compounded monthly at blended_return/12.
        Returns None if the target is unreachable inside `max_years`.
        """
        r = self.blended_return / 12
        bal = self.current_capital
        target = self.capital_required
        if bal >= target:
            return 0.0
        for month in range(1, max_years * 12 + 1):
            bal = bal * (1 + r) + self.monthly_contribution
            if bal >= target:
                return round(month / 12, 1)
        return None

    def trajectory(self, years: int = 20, step: int = 1) -> list[tuple[int, float, float]]:
        """[(year, capital, monthly_income)] so the curve is visible, not implied."""
        r = self.blended_return / 12
        bal = self.current_capital
        rows = [(0, bal, bal * self.blended_return / 12)]
        for y in range(1, years + 1):
            for _ in range(12):
                bal = bal * (1 + r) + self.monthly_contribution
            if y % step == 0:
                rows.append((y, bal, bal * self.blended_return / 12))
        return rows


def plan_income_goal(
    *,
    monthly_target: float,
    current_capital: float,
    monthly_contribution: float = 0.0,
    blended_return: float = DEFAULT_BLENDED,
) -> GoalPlan:
    return GoalPlan(
        monthly_target=monthly_target,
        blended_return=blended_return,
        current_capital=current_capital,
        monthly_contribution=monthly_contribution,
    )


# --------------------------------------------------------------------- selling
@dataclass
class SellSignal:
    rule: str
    severity: str  # "info" | "watch" | "act"
    detail: str


@dataclass
class SellReview:
    ticker: str
    signals: list[SellSignal] = field(default_factory=list)
    suggested_trim_lots: int = 0
    trim_reason: str = ""

    @property
    def worst(self) -> str:
        order = {"info": 0, "watch": 1, "act": 2}
        return max((s.severity for s in self.signals), key=lambda s: order[s], default="info")

    @property
    def business_is_intact(self) -> bool:
        """True when nothing fired against the company itself, only the price."""
        broken = {"dividend cut", "payout unsustainable", "loss-making"}
        return not any(s.rule in broken for s in self.signals)


def sell_signals(
    report,
    *,
    portfolio_value: float,
    net_worth: float = 0.0,
    max_position_pct: float = 0.20,
    div_cut_pct: float = 0.30,
    prior_year_div: float | None = None,
) -> SellReview:
    """Rule-based review of one holding. Reports, never instructs.

    `report` is a holdings.HoldingReport.
    """
    rev = SellReview(ticker=report.holding.ticker)

    # Concentration is measured against **net worth**, not the stock sleeve.
    #
    # Measuring inside the sleeve is the intuitive choice and it is wrong for
    # anyone who owns one stock: the holding is then 100% of "the portfolio" by
    # definition, the rule fires permanently, and it demands a large sale that
    # buying a second stock is the only way to satisfy. It ignores that the same
    # money is already diversified across bonds, gold and savings.
    #
    # Callers that only know the sleeve may still pass `portfolio_value` alone;
    # `net_worth` is what should be passed when it is known.
    basis = net_worth if net_worth and net_worth > 0 else portfolio_value
    weight = report.market_value / basis if basis else 0.0
    scope = "net worth" if (net_worth and net_worth > 0) else "portfolio"

    # --- concentration: usually the largest real risk in a small portfolio ---
    if weight > max_position_pct:
        excess_value = report.market_value - basis * max_position_pct
        lots = int(excess_value // (report.price * 100))
        rev.signals.append(
            SellSignal(
                "concentration",
                "act",
                f"{weight * 100:.0f}% of your {scope} is this one stock "
                f"(target ceiling {max_position_pct * 100:.0f}%)",
            )
        )
        if lots > 0:
            rev.suggested_trim_lots = lots
            rev.trim_reason = (
                f"trimming {lots} lots (~Rp {lots * report.price * 100:,.0f}) brings it to "
                f"about {max_position_pct * 100:.0f}%"
            )

    # --- the business ---
    if prior_year_div and prior_year_div > 0:
        change = report.ttm_dividend_per_share / prior_year_div - 1
        if change < -div_cut_pct:
            rev.signals.append(
                SellSignal(
                    "dividend cut", "act",
                    f"dividend down {abs(change) * 100:.0f}% versus the prior year",
                )
            )
        elif change > 0.05:
            rev.signals.append(
                SellSignal(
                    "dividend growing", "info",
                    f"dividend up {change * 100:.0f}% versus the prior year",
                )
            )

    # --- the price ---
    if not report.uptrend:
        rev.signals.append(
            SellSignal(
                "trend broken", "watch",
                "EMA50 is below EMA200 — the market is not agreeing with this holding yet",
            )
        )
    if report.price <= report.low_52w * 1.02:
        rev.signals.append(
            SellSignal("near 52w low", "watch", "trading within 2% of its 52-week low")
        )

    # --- income while you wait ---
    if report.yield_on_market_pct >= 4:
        rev.signals.append(
            SellSignal(
                "paid to wait", "info",
                f"{report.yield_on_market_pct:.1f}% yield on today's price "
                f"(Rp {report.monthly_income:,.0f}/month)",
            )
        )
    return rev
