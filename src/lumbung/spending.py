"""Purchase advice and expense tracking.

The useful part of "can I afford this?" is arithmetic, not language, so none of
this needs a model:

* **Can you pay without breaking the safety net?** Liquid money minus the price,
  against months of spending covered.
* **What does it actually cost you?** Not the price -- the delay it adds to your
  income goal. Rp 14jt is abstract; "five weeks further from Rp 3jt/month" is not.
* **Cash or credit?** Mostly determined by one fact: whether the offer is a real
  0% instalment or revolving interest.

Indonesian credit-card rules, current as of 2026-08:
  * Bank Indonesia caps card interest at **1.75%/month (~21%/year)**, unchanged
    since July 2021.
  * 0% instalment ("cicilan 0%") is a merchant promotion, widely available --
    BRI runs tenors to 36 months from Rp 100rb.

Those two produce opposite answers, which is why "use a credit card" is never
advice on its own.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

# Bank Indonesia ceiling. A card charging revolving interest costs this much.
CARD_MONTHLY_RATE = 0.0175
CARD_ANNUAL_RATE = 0.21

EXPENSE_SCHEMA = """
CREATE TABLE IF NOT EXISTS expenses (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       INTEGER NOT NULL,
    amount   REAL    NOT NULL,
    item     TEXT    NOT NULL,
    category TEXT    NOT NULL DEFAULT 'other',
    method   TEXT    NOT NULL DEFAULT 'cash',   -- cash | credit | credit0
    note     TEXT
);
CREATE INDEX IF NOT EXISTS idx_expenses_ts ON expenses(ts);

-- Money that arrives OUTSIDE the salary: a bonus, a side job, a refund, a
-- dividend paid in cash. Kept in its own table rather than as a negative
-- expense, because it behaves differently in every calculation that matters:
--
--   * it must NOT change `income_monthly`, which is the recurring figure the
--     surplus and the emergency-fund target are built on. A one-off bonus that
--     quietly inflated "monthly income" would inflate the surplus, shrink the
--     apparent months-to-target, and flatter every projection.
--   * it IS deployable immediately and in full, unlike salary which has a month
--     of spending to cover first.
--
-- So: recurring income lives in holdings.yaml, windfalls live here.
CREATE TABLE IF NOT EXISTS income (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       INTEGER NOT NULL,
    amount   REAL    NOT NULL,
    source   TEXT    NOT NULL,
    kind     TEXT    NOT NULL DEFAULT 'other',  -- bonus | side | refund | dividend | gift | other
    deployed INTEGER NOT NULL DEFAULT 0,        -- 1 once you have actually invested it
    note     TEXT
);
CREATE INDEX IF NOT EXISTS idx_income_ts ON income(ts);
"""

INCOME_KINDS = ("bonus", "side", "refund", "dividend", "gift", "other")

CATEGORIES = (
    "tech", "food", "transport", "home", "health", "family",
    "fun", "fees", "gift", "other",
)


@dataclass
class Verdict:
    """The answer to 'can I buy this', with the reasoning kept visible."""

    item: str
    price: float
    liquid_before: float
    liquid_after: float
    spending_monthly: float
    emergency_target_months: int
    goal_delay_months: float
    surplus_monthly: float

    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    # -- affordability -----------------------------------------------------
    @property
    def months_covered_after(self) -> float:
        return self.liquid_after / self.spending_monthly if self.spending_monthly else 0.0

    @property
    def surplus_months(self) -> float:
        """How many months of saving this purchase consumes."""
        return self.price / self.surplus_monthly if self.surplus_monthly else float("inf")

    @property
    def breaks_safety_net(self) -> bool:
        return self.months_covered_after < self.emergency_target_months

    @property
    def leaves_nothing(self) -> bool:
        return self.months_covered_after < 3

    @property
    def verdict(self) -> str:
        if self.liquid_after < 0:
            return "NO"
        if self.leaves_nothing:
            return "NO"
        if self.breaks_safety_net:
            return "TIGHT"
        return "YES"

    @property
    def headline(self) -> str:
        return {
            "YES": "Yes — you can buy this without breaking anything.",
            "TIGHT": "Possible, but it eats into your safety net.",
            "NO": "Not without leaving yourself exposed.",
        }[self.verdict]


@dataclass
class PaymentAdvice:
    method: str            # "cash" | "credit0" | "wait"
    detail: str
    credit_cost: float = 0.0      # extra rupiah if revolving interest is used
    float_benefit: float = 0.0    # interest earned by keeping cash during a 0% plan


def advise_payment(
    price: float,
    *,
    can_pay_cash: bool,
    zero_percent_available: bool,
    tenor_months: int = 12,
    savings_rate: float = 0.06,
) -> PaymentAdvice:
    """Cash or card, decided by the two facts that actually matter.

    A 0% instalment while you *hold* the cash is genuinely better: the money
    keeps earning in a savings account while you pay it off. Revolving interest
    at the 1.75%/month cap is the opposite, and no amount of framing fixes it.
    """
    if not can_pay_cash:
        return PaymentAdvice(
            "wait",
            "You cannot cover this from liquid money. A card would not make it "
            "affordable, only deferred — and at 1.75%/month, more expensive.",
            credit_cost=price * CARD_MONTHLY_RATE * tenor_months,
        )

    if zero_percent_available:
        # Average outstanding over the tenor is roughly half the price.
        benefit = price / 2 * savings_rate * (tenor_months / 12)
        return PaymentAdvice(
            "credit0",
            f"Take the 0% instalment and keep the cash in savings. Over "
            f"{tenor_months} months at {savings_rate:.0%} that is about "
            f"Rp {benefit:,.0f} of interest you would otherwise give up. "
            "Only do this if you will actually pay every instalment on time — "
            "one missed payment moves the whole balance to 1.75%/month.",
            float_benefit=benefit,
        )

    cost = price * CARD_MONTHLY_RATE * tenor_months
    return PaymentAdvice(
        "cash",
        f"Pay cash. Without a 0% offer a card costs 1.75%/month — about "
        f"Rp {cost:,.0f} extra over {tenor_months} months, against roughly "
        f"Rp {price / 2 * savings_rate * (tenor_months / 12):,.0f} of savings "
        "interest you would keep. The card loses by a wide margin.",
        credit_cost=cost,
    )


def assess(
    *,
    item: str,
    price: float,
    liquid: float,
    spending_monthly: float,
    surplus_monthly: float,
    emergency_months: int,
    goal_delay_months: float,
) -> Verdict:
    v = Verdict(
        item=item, price=price, liquid_before=liquid, liquid_after=liquid - price,
        spending_monthly=spending_monthly, emergency_target_months=emergency_months,
        goal_delay_months=goal_delay_months, surplus_monthly=surplus_monthly,
    )

    v.reasons.append(
        f"{v.surplus_months:.1f} months of your saving, "
        f"or {price / spending_monthly:.1f} months of living costs"
        if spending_monthly else f"{v.surplus_months:.1f} months of your saving"
    )
    if goal_delay_months >= 0.1:
        v.reasons.append(
            f"pushes your income goal back about "
            f"{_human_delay(goal_delay_months)}"
        )

    if v.liquid_after < 0:
        v.warnings.append("You do not have this much liquid money.")
    elif v.leaves_nothing:
        v.warnings.append(
            f"Leaves only {v.months_covered_after:.1f} months of spending covered. "
            "Under three months is where a broken laptop becomes a debt problem."
        )
    elif v.breaks_safety_net:
        v.warnings.append(
            f"Drops your safety net to {v.months_covered_after:.1f} months, "
            f"below your {emergency_months}-month target. Rebuilding takes "
            f"{(spending_monthly * emergency_months - v.liquid_after) / surplus_monthly:.1f} "
            "months of saving."
            if surplus_monthly else "below your target."
        )
    return v


def _human_delay(months: float) -> str:
    if months < 1:
        return f"{months * 4.35:.0f} weeks"
    if months < 18:
        return f"{months:.1f} months"
    return f"{months / 12:.1f} years"


# --------------------------------------------------------------- recording
def connect(db_path: str | Path) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.executescript(EXPENSE_SCHEMA)
    conn.commit()
    return conn


def record(
    conn: sqlite3.Connection, *, amount: float, item: str,
    category: str = "other", method: str = "cash", note: str = "",
) -> int:
    cur = conn.execute(
        "INSERT INTO expenses(ts,amount,item,category,method,note) VALUES(?,?,?,?,?,?)",
        (int(time.time()), amount, item, category.lower(), method.lower(), note),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


# -- income ------------------------------------------------------------------
def record_income(
    conn: sqlite3.Connection, *, amount: float, source: str,
    kind: str = "other", note: str = "",
) -> int:
    cur = conn.execute(
        "INSERT INTO income(ts,amount,source,kind,note) VALUES(?,?,?,?,?)",
        (int(time.time()), amount, source, kind.lower(), note),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def recent_income(conn: sqlite3.Connection, days: int = 365) -> list[sqlite3.Row]:
    since = int(time.time()) - days * 86400
    return conn.execute(
        "SELECT * FROM income WHERE ts>=? ORDER BY ts DESC", (since,)
    ).fetchall()


def undeployed_income(conn: sqlite3.Connection) -> float:
    """Windfall money recorded but not yet invested.

    This is the number worth surfacing: a bonus that sits in the current account
    is the most common way a good month quietly turns into nothing.
    """
    row = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) AS t FROM income WHERE deployed = 0"
    ).fetchone()
    return float(row["t"])


def mark_income_deployed(conn: sqlite3.Connection, income_id: int) -> bool:
    cur = conn.execute("UPDATE income SET deployed = 1 WHERE id = ?", (income_id,))
    conn.commit()
    return cur.rowcount > 0


def income_by_kind(conn: sqlite3.Connection, days: int = 365) -> list[tuple[str, float, int]]:
    since = int(time.time()) - days * 86400
    rows = conn.execute(
        "SELECT kind, SUM(amount) AS total, COUNT(*) AS n FROM income "
        "WHERE ts>=? GROUP BY kind ORDER BY total DESC",
        (since,),
    ).fetchall()
    return [(r["kind"], float(r["total"]), int(r["n"])) for r in rows]


def recent(conn: sqlite3.Connection, days: int = 90) -> list[sqlite3.Row]:
    since = int(time.time()) - days * 86400
    return conn.execute(
        "SELECT * FROM expenses WHERE ts>=? ORDER BY ts DESC", (since,)
    ).fetchall()


def by_category(conn: sqlite3.Connection, days: int = 90) -> list[tuple[str, float, int]]:
    since = int(time.time()) - days * 86400
    rows = conn.execute(
        "SELECT category, SUM(amount) AS total, COUNT(*) AS n FROM expenses "
        "WHERE ts>=? GROUP BY category ORDER BY total DESC",
        (since,),
    ).fetchall()
    return [(r["category"], float(r["total"]), int(r["n"])) for r in rows]


def monthly_totals(conn: sqlite3.Connection, months: int = 6) -> list[tuple[str, float]]:
    since = int(time.time()) - months * 31 * 86400
    rows = conn.execute(
        "SELECT strftime('%Y-%m', ts, 'unixepoch', 'localtime') AS m, "
        "SUM(amount) AS total FROM expenses WHERE ts>=? GROUP BY m ORDER BY m",
        (since,),
    ).fetchall()
    return [(r["m"], float(r["total"])) for r in rows]


def category_plan(
    conn: sqlite3.Connection,
    targets: dict[str, float],
    *,
    months: int = 3,
    budgeted: float = 0.0,
    exclude: tuple[str, ...] = (),
) -> list[dict[str, float | str]]:
    """What each category costs per month, against what it is meant to cost.

    Built on the SAME tracked months the rest of the app averages over, so a
    category total and the headline spending figure can never disagree. A
    category with no target still appears -- you cannot decide what to cut
    while a fifth of the money is invisible.

    `exclude` drops items by SQL LIKE pattern, for costs that are real but are
    not a monthly habit: a car repair, a financed purchase booked whole.
    """
    prof = spending_profile(conn, budgeted, months=months)
    keys = [k for k, _, _ in prof.tracked_months]
    if not keys:
        return []

    where = " OR ".join(["item LIKE ?"] * len(exclude))
    skip = f" AND NOT ({where})" if exclude else ""
    placeholders = ",".join("?" * len(keys))
    rows = conn.execute(
        "SELECT category, COALESCE(SUM(amount),0) AS total FROM expenses "
        f"WHERE strftime('%Y-%m', ts, 'unixepoch', 'localtime') IN ({placeholders})"
        f"{skip} GROUP BY category",
        (*keys, *exclude),
    ).fetchall()
    n = len(keys)

    cur_rows = conn.execute(
        "SELECT category, COALESCE(SUM(amount),0) AS total FROM expenses "
        "WHERE strftime('%Y-%m', ts, 'unixepoch', 'localtime')=?"
        f"{skip} GROUP BY category",
        (month_key(0), *exclude),
    ).fetchall()
    current = {r["category"]: float(r["total"]) for r in cur_rows}

    out: list[dict[str, float | str]] = []
    for r in rows:
        cat = str(r["category"])
        avg = float(r["total"]) / n
        target = float(targets.get(cat, 0.0) or 0.0)
        out.append({
            "category": cat,
            "average": avg,
            "target": target,
            # Only a target BELOW what you spend is a cut. A generous target is
            # not headroom to spend into, so it never reports a negative cut.
            "cut": max(0.0, avg - target) if target else 0.0,
            "has_target": bool(target),
            "current": current.get(cat, 0.0),
            "months_used": n,
        })
    out.sort(key=lambda d: (-float(d["cut"]), -float(d["average"])))
    return out


def month_key(offset: int = 0) -> str:
    """'YYYY-MM' for the current month, or `offset` months back."""
    import datetime

    now = datetime.date.today().replace(day=1)
    for _ in range(offset):
        now = (now - datetime.timedelta(days=1)).replace(day=1)
    return now.strftime("%Y-%m")


def month_total(conn: sqlite3.Connection, offset: int = 0) -> float:
    """Total recorded for a given month. 0 means nothing was recorded, which is
    not the same as nothing was spent -- callers must say which they mean."""
    row = conn.execute(
        "SELECT COALESCE(SUM(amount),0) AS t FROM expenses "
        "WHERE strftime('%Y-%m', ts, 'unixepoch', 'localtime')=?",
        (month_key(offset),),
    ).fetchone()
    return float(row["t"])


def month_count(conn: sqlite3.Connection, offset: int = 0) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM expenses "
        "WHERE strftime('%Y-%m', ts, 'unixepoch', 'localtime')=?",
        (month_key(offset),),
    ).fetchone()
    return int(row["n"])


@dataclass
class MonthReview:
    """Last month's actual spending against the budget you assumed."""

    month: str
    actual: float
    budgeted: float
    entries: int

    @property
    def tracked(self) -> bool:
        """Did you record enough for the number to mean anything?"""
        return self.entries >= 3

    @property
    def variance(self) -> float:
        return self.actual - self.budgeted

    @property
    def variance_pct(self) -> float:
        return (self.variance / self.budgeted * 100) if self.budgeted else 0.0

    @property
    def verdict(self) -> str:
        if not self.tracked:
            return "untracked"
        if self.variance_pct > 10:
            return "over"
        if self.variance_pct < -10:
            return "under"
        return "on budget"

    def adjusted_surplus(self, income: float) -> float:
        """Surplus computed from what you actually spent, not what you assumed.

        Falls back to the budget when too little was recorded to trust.
        """
        spend = self.actual if self.tracked else self.budgeted
        return max(0.0, income - spend)


def review_last_month(conn: sqlite3.Connection, budgeted: float) -> MonthReview:
    return MonthReview(
        month=month_key(1), actual=month_total(conn, 1),
        budgeted=budgeted, entries=month_count(conn, 1),
    )


@dataclass
class SpendingProfile:
    """What you actually spend, and how much it moves around.

    Spending is lumpy -- a quiet month and a month with a wedding, a service and
    new tyres are not the same month. Allocating from the *last* month alone
    would swing the plan around for no good reason, so the deployable surplus is
    computed from a trailing average while the range is shown separately. You
    see the volatility; you do not get whipsawed by it.
    """

    months: list[tuple[str, float, int]]   # COMPLETE months only, oldest first
    budgeted: float
    # The month in progress, kept out of the average: a month that is 10 days old
    # would otherwise drag the average down and inflate the apparent surplus.
    current: tuple[str, float, int] | None = None

    # A month with almost nothing in it is far more often a month you forgot to
    # log than a month you barely spent in. A flat "3 entries" floor could not
    # tell those apart: an April holding six credit-card rows recovered from a
    # statement passed it, and dragged the average down by a third. The test is
    # relative instead, so it adapts to how much any given person actually logs.
    MIN_ENTRIES_ABS = 3
    MIN_ENTRIES_FRACTION = 0.25

    @property
    def _entry_floor(self) -> int:
        """How many entries a month needs before it is worth averaging."""
        import math

        busiest = max((n for _, _, n in self.months), default=0)
        return max(self.MIN_ENTRIES_ABS, math.ceil(busiest * self.MIN_ENTRIES_FRACTION))

    @property
    def tracked_months(self) -> list[tuple[str, float, int]]:
        """Months with enough entries to be worth averaging."""
        floor = self._entry_floor
        return [m for m in self.months if m[2] >= floor]

    @property
    def tracked(self) -> bool:
        return len(self.tracked_months) >= 1

    @property
    def average(self) -> float:
        rows = self.tracked_months
        return sum(t for _, t, _ in rows) / len(rows) if rows else 0.0

    @property
    def last(self) -> tuple[str, float, int] | None:
        """Most recent COMPLETE month."""
        return self.months[-1] if self.months else None

    @property
    def lowest(self) -> float:
        rows = self.tracked_months
        return min((t for _, t, _ in rows), default=0.0)

    @property
    def highest(self) -> float:
        rows = self.tracked_months
        return max((t for _, t, _ in rows), default=0.0)

    @property
    def swing_pct(self) -> float:
        """How far the highest month sits above the lowest, as a percentage."""
        return ((self.highest - self.lowest) / self.lowest * 100) if self.lowest else 0.0

    @property
    def basis(self) -> float:
        """The spending figure to plan with: measured average, else your budget."""
        return self.average if self.tracked else self.budgeted

    @property
    def basis_label(self) -> str:
        n = len(self.tracked_months)
        return f"{n}-month average" if self.tracked else "budget (nothing recorded yet)"

    def surplus(self, income: float) -> float:
        return max(0.0, income - self.basis)

    def variance_pct(self) -> float:
        return ((self.basis - self.budgeted) / self.budgeted * 100) if self.budgeted else 0.0

    @property
    def verdict(self) -> str:
        if not self.tracked:
            return "untracked"
        v = self.variance_pct()
        if v > 10:
            return "over budget"
        if v < -10:
            return "under budget"
        return "on budget"


def spending_profile(
    conn: sqlite3.Connection, budgeted: float, months: int = 3
) -> SpendingProfile:
    """Trailing spending over COMPLETE months, plus the month in progress.

    Two deliberate exclusions:
      * the current month is reported separately, never averaged -- it is
        partial, and averaging it in would understate your real spending;
      * months with nothing recorded are dropped rather than counted as zero,
        because "I spent nothing" and "I forgot to log it" are not the same.
    """
    rows: list[tuple[str, float, int]] = []
    for offset in range(months, 0, -1):
        count = month_count(conn, offset)
        if count == 0:
            continue
        rows.append((month_key(offset), month_total(conn, offset), count))

    cur_count = month_count(conn, 0)
    current = (month_key(0), month_total(conn, 0), cur_count) if cur_count else None
    return SpendingProfile(months=rows, budgeted=budgeted, current=current)
