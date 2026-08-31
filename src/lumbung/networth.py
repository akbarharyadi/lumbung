"""Whole-balance-sheet view: allocation, emergency fund, and where the surplus goes.

This module exists because the interesting question stopped being "which stock".
With a monthly surplus larger than most of the positions being argued about, the
decisions that actually move the outcome are:

  1. is there a cash buffer, so a bad month never forces a sale at the worst time
  2. how concentrated is the whole thing, not just the stock sleeve
  3. where does next month's surplus go

Directing *new* money at the underweight buckets rebalances without selling
anything -- no realised losses, no 0.1% PPh on the proceeds, no tax event at all.
That is nearly always the cheaper way to fix an allocation when fresh cash is
arriving every month.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from calendar import monthrange
from datetime import date, timedelta
from pathlib import Path

import yaml

from .config import PROJECT_ROOT

log = logging.getLogger(__name__)

# Buckets the planner understands. Anything else in other_assets lands in "other".
#
# `savings` is deliberately separate from `cash`. Money in a 0% current account
# and money in a 6-7.5% digital-bank account are both instantly spendable, but
# only one of them earns. Lumping them together hides income you already have
# and makes the safety net look more expensive than it is.
BUCKETS = ("stocks", "bonds", "gold", "savings", "cash", "crypto", "other")

# Buckets you could spend from this week without selling an investment.
LIQUID = ("cash", "savings", "gold")

# How far ahead a promise still constrains today's money. Inside a year, a bill
# you have agreed to pay is not investable -- you would be selling something to
# meet it. Beyond a year there is time to save into it out of income, so it
# should not freeze the whole plan.
COMMITMENT_HORIZON_DAYS = 365


@dataclass
class Commitment:
    """Money already promised to something, with a date.

    Not a budget line and not a goal: a bill you have agreed to pay. The
    difference matters, because this is subtracted from the buffer rather than
    aimed at with new savings.
    """

    name: str
    amount_idr: float
    due: date | None = None
    note: str = ""
    # "obligation" -- you have agreed to pay it, so it binds the buffer.
    # "wish" -- you are considering it. Listed and priced, never binding: a want
    # that shrank your safety net would make wanting things look like poverty.
    kind: str = "obligation"

    @property
    def is_wish(self) -> bool:
        return self.kind == "wish"

    def days_away(self, today: date | None = None) -> int | None:
        if self.due is None:
            return None
        return (self.due - (today or date.today())).days

    def is_binding(self, today: date | None = None) -> bool:
        """Close enough to constrain what today's surplus may be locked into.

        Undated commitments bind: a promise with no date you can point at is one
        you could be asked to honour at any time, and treating it as distant is
        the optimistic reading."""
        if self.is_wish:
            return False
        d = self.days_away(today)
        return d is None or d <= COMMITMENT_HORIZON_DAYS


@dataclass
class OtherAsset:
    name: str
    kind: str
    value_idr: float
    note: str = ""
    rate: float = 0.0  # annual yield as a fraction, e.g. 0.06 for 6% p.a.

    @property
    def annual_income(self) -> float:
        return self.value_idr * self.rate


@dataclass
class Possession:
    """Something you own that is not an investment.

    Deliberately outside net worth as the planner uses it. Not because it is not
    yours, but because counting it would silence the rules that are telling you
    the most useful things -- and it would not change a single risk you carry.
    """

    name: str
    value_idr: float
    note: str = ""
    depreciating: bool = True


@dataclass
class CashFlow:
    income_monthly: float = 0.0
    # What he actually spends, measured. This sizes the emergency fund, so it
    # must never be replaced by a target -- aiming to spend less does not make
    # six months of real life any cheaper.
    spending_monthly: float = 0.0
    # What he is aiming to spend. 0 means no limit set, and then the measured
    # figure is used so nothing reads as "no budget".
    spending_limit: float = 0.0
    payday_day: int = 0          # day of month; 0 disables the reminder

    @property
    def budget(self) -> float:
        """The number a month is judged against."""
        return self.spending_limit or self.spending_monthly

    @property
    def has_limit(self) -> bool:
        return self.spending_limit > 0

    @property
    def limit_gap(self) -> float:
        """How much the limit asks him to cut. Negative means it is above what
        he already spends, which is not a limit at all."""
        return self.spending_monthly - self.spending_limit if self.has_limit else 0.0

    def is_payday(self, today: int | None = None) -> bool:
        """True on payday. Also true on the 28th when payday_day is 29-31, so a
        short month never skips the reminder entirely."""
        import datetime

        if not self.payday_day:
            return False
        d = today if today is not None else datetime.datetime.now().day
        if d == self.payday_day:
            return True
        return self.payday_day > 28 and d == 28

    def days_until_payday(self, today: int | None = None) -> int:
        import calendar
        import datetime

        if not self.payday_day:
            return -1
        now = datetime.datetime.now()
        d = today if today is not None else now.day
        last = calendar.monthrange(now.year, now.month)[1]
        target = min(self.payday_day, last)
        return target - d if target >= d else (last - d) + min(self.payday_day, last)

    @property
    def surplus(self) -> float:
        return self.income_monthly - self.spending_monthly

    @property
    def savings_rate(self) -> float:
        return self.surplus / self.income_monthly if self.income_monthly else 0.0


@dataclass
class Bucket:
    name: str
    value: float
    target_pct: float

    def weight(self, total: float) -> float:
        return self.value / total if total else 0.0

    def drift(self, total: float) -> float:
        """Actual minus target, in percentage points."""
        return (self.weight(total) - self.target_pct) * 100

    def gap_idr(self, total: float) -> float:
        """Rupiah needed to reach target. Negative means overweight."""
        return self.target_pct * total - self.value


@dataclass
class Goals:
    """The numbers that are personal rather than structural.

    These were hardcoded to one person's figures. That is fine for one user and
    quietly wrong for a second: a brother running his own profile would have seen
    someone else's Rp 3jt/month target presented as his own. Anything that
    describes *whose* money this is belongs in config, not in the source.
    """

    monthly_income_target: float = 3_000_000.0
    subscription_idr: float = 330_000.0
    # Per-category monthly ceilings, e.g. {"food": 3_100_000}. Personal, so it
    # belongs beside the other goals rather than in source.
    spending_targets: dict[str, float] = field(default_factory=dict)
    # SQL LIKE patterns for costs that are real but are not a monthly habit: a
    # car repair, a financed purchase, an irregular contractor. These are the
    # same items the baseline excludes, and they live here for two reasons --
    # they are one person's payees, and a category total that disagreed with
    # the headline spending figure would be a new version of the oldest bug in
    # this app.
    spending_excludes: list[str] = field(default_factory=list)


@dataclass
class NetWorth:
    buckets: dict[str, Bucket]
    cashflow: CashFlow
    goals: Goals = field(default_factory=Goals)
    # bucket -> how this person actually moves money into it. Config, not code:
    # the tool has no API to any of these and cannot know which you use.
    providers: dict[str, str] = field(default_factory=dict)
    emergency_months_target: int = 6
    positions: list[tuple[str, float]] = field(default_factory=list)  # (label, value)
    other_assets: list[OtherAsset] = field(default_factory=list)
    commitments: list[Commitment] = field(default_factory=list)
    # bucket -> (rate, why). Overrides the built-in exit-cost table. What it
    # costs you to leave a position is a fact about your provider and your
    # account, not about the asset class, so it does not belong in source.
    exit_costs: dict[str, tuple[float, str]] = field(default_factory=dict)
    # Never part of `total`. See the Possession docstring for why.
    possessions: list[Possession] = field(default_factory=list)

    @property
    def total(self) -> float:
        """Investable net worth: the denominator every rule here measures against."""
        return sum(b.value for b in self.buckets.values())

    @property
    def possessions_total(self) -> float:
        return sum(p.value_idr for p in self.possessions)

    @property
    def total_with_possessions(self) -> float:
        """Everything owned. For looking at, not for dividing by."""
        return self.total + self.possessions_total

    # -- emergency fund ----------------------------------------------------
    @property
    def liquid_now(self) -> float:
        """What you could actually spend this week: cash, digital-bank savings,
        and gold. Pegadaian sells in-app same day, so gold counts -- it just
        costs a few percent of spread to use."""
        return sum(self.buckets[b].value for b in LIQUID if b in self.buckets)

    @property
    def savings_income_monthly(self) -> float:
        """Interest thrown off by savings/deposits, from the rates you recorded."""
        return sum(a.annual_income for a in self.other_assets) / 12

    def binding_commitments(self, today: date | None = None) -> list[Commitment]:
        return [c for c in self.commitments if c.is_binding(today)]

    def committed_total(self, today: date | None = None) -> float:
        """Rupiah promised away inside the horizon."""
        return sum(c.amount_idr for c in self.binding_commitments(today))

    def wishes(self) -> list[Commitment]:
        return [c for c in self.commitments if c.is_wish]

    def paydays_before(self, when: date | None, today: date | None = None) -> int:
        """How many salaries land between now and a date.

        Counting them is the difference between "you cannot afford this" and
        "you cannot afford this yet", and only one of those is true.
        """
        day = self.cashflow.payday_day
        if not day or when is None:
            return 0
        today = today or date.today()
        n, cur = 0, today
        while cur < when:
            cur += timedelta(days=1)
            # Clamp to month end, so a payday on the 31st still lands in April.
            last = monthrange(cur.year, cur.month)[1]
            if cur.day == min(day, last):
                n += 1
        return n

    def payday_dates(self, count: int, today: date | None = None) -> list[date]:
        """The next `count` salary dates."""
        day = self.cashflow.payday_day
        if not day or count <= 0:
            return []
        today = today or date.today()
        out: list[date] = []
        cur = today
        while len(out) < count:
            cur += timedelta(days=1)
            last = monthrange(cur.year, cur.month)[1]
            if cur.day == min(day, last):
                out.append(cur)
        return out

    def purchase_plan(
        self, price: float, *, horizon: int = 60, today: date | None = None
    ) -> dict:
        """When this could be bought with the safety net still intact.

        Walks forward payday by payday through the *real* allocation rules, so
        money the plan sends to bonds stops counting as buffer -- which is
        exactly the thing that is easy to forget when doing it in your head.

        Two conditions, both required: enough spendable cash to actually pay,
        and enough liquid left afterwards to cover the emergency target.
        """
        import copy

        today = today or date.today()
        sim = copy.deepcopy(self)
        sim.commitments = [c for c in sim.commitments if not c.is_wish]
        target = sim.emergency_target
        spend = sim.cashflow.spending_monthly

        def liquid() -> float:
            return sum(sim.buckets[b].value for b in LIQUID if b in sim.buckets)

        def spendable() -> float:
            return sum(
                sim.buckets[b].value for b in ("cash", "savings") if b in sim.buckets
            )

        def outstanding() -> float:
            """Bills still unpaid at this point in the walk."""
            return sum(c.amount_idr for c in sim.commitments)

        def verdict() -> tuple[bool, float]:
            # Minus the price AND minus everything still owed. Leaving the owed
            # part out is what made a hand-done version of this say August: the
            # buffer looked fine only because a Rp 31jt bill had not landed yet.
            after = liquid() - price - outstanding()
            return (spendable() >= price and after >= target), after

        ok, after = verdict()
        if ok:
            return {
                "safe_now": True, "when": None, "liquid_after": after,
                "months_after": after / spend if spend else 0.0,
                "shortfall": 0.0, "horizon": horizon,
            }
        now_after = after
        now_short = max(0.0, target - after, price - spendable())

        for d in sim.payday_dates(horizon, today):
            # Bills first: a commitment due before this salary is already paid.
            for c in list(sim.commitments):
                if c.due is not None and c.due <= d:
                    owed = c.amount_idr
                    for b in ("cash", "savings", "gold"):
                        if b not in sim.buckets:
                            continue
                        take = min(owed, sim.buckets[b].value)
                        sim.buckets[b].value -= take
                        owed -= take
                    sim.commitments.remove(c)
            for name, amount in sim.allocate_surplus():
                if name in sim.buckets:
                    sim.buckets[name].value += amount
            ok, after = verdict()
            if ok:
                return {
                    "safe_now": False, "when": d, "liquid_after": after,
                    "months_after": after / spend if spend else 0.0,
                    "shortfall": now_short, "now_months": (
                        now_after / spend if spend else 0.0
                    ),
                    "horizon": horizon,
                }

        return {
            "safe_now": False, "when": None, "liquid_after": now_after,
            "months_after": now_after / spend if spend else 0.0,
            "shortfall": now_short,
            "now_months": now_after / spend if spend else 0.0,
            "horizon": horizon,
        }

    def committed_net_of_income(self, today: date | None = None) -> float:
        """What today's cash actually has to cover.

        A bill due after two paydays is largely paid by those paydays. Charging
        the whole amount against today's balance reports a crisis that the
        calendar resolves on its own.
        """
        surplus = max(0.0, self.cashflow.surplus)
        total = 0.0
        for c in self.binding_commitments(today):
            arriving = self.paydays_before(c.due, today) * surplus
            total += max(0.0, c.amount_idr - arriving)
        return total

    def free_liquid(self, today: date | None = None) -> float:
        """Liquid money that is not already spoken for.

        This is the honest buffer. Holding Rp 51jt against a Rp 31jt bill due
        next month is not a Rp 51jt safety net, and reporting it as one is how
        you discover the problem in the month you can least afford to."""
        return self.liquid_now - self.committed_total(today)

    def months_covered_free(self, today: date | None = None) -> float:
        s = self.cashflow.spending_monthly
        return self.free_liquid(today) / s if s else 0.0

    @property
    def emergency_target(self) -> float:
        return self.cashflow.spending_monthly * self.emergency_months_target

    @property
    def months_covered_cash(self) -> float:
        s = self.cashflow.spending_monthly
        return self.buckets["cash"].value / s if s else 0.0

    @property
    def months_covered_liquid(self) -> float:
        s = self.cashflow.spending_monthly
        return self.liquid_now / s if s else 0.0

    @property
    def emergency_shortfall(self) -> float:
        # Against *free* liquid, not gross. Money owed to something else is not
        # buffer, however liquid the account it sits in.
        return max(0.0, self.emergency_target - self.free_liquid())

    @property
    def months_to_fund_emergency(self) -> float:
        if self.emergency_shortfall <= 0 or self.cashflow.surplus <= 0:
            return 0.0
        return self.emergency_shortfall / self.cashflow.surplus

    # -- concentration -----------------------------------------------------
    def largest_position(self) -> tuple[str, float, float]:
        """(label, value, weight) of the single biggest holding."""
        if not self.positions:
            return ("-", 0.0, 0.0)
        label, val = max(self.positions, key=lambda x: x[1])
        return (label, val, val / self.total if self.total else 0.0)

    # -- deployment --------------------------------------------------------
    def allocate_surplus(self, amount: float | None = None) -> list[tuple[str, float]]:
        """Split `amount` across whichever buckets are furthest below target.

        Proportional to each bucket's shortfall, so the most underweight gets the
        most. Buckets already at or above target get nothing -- new money goes
        where it is missing rather than being spread evenly for tidiness.
        """
        amount = self.cashflow.surplus if amount is None else amount
        if amount <= 0:
            return []
        # A promise you have already made outranks a target allocation. Until the
        # buffer clears what is owed, new money stays where it can be spent --
        # otherwise the plan locks the very rupiah the bill needs.
        if self.commitments and self.free_liquid() + amount < self.emergency_target:
            return self._allocate_liquid_only(amount)
        total_after = self.total + amount
        gaps = {
            name: max(0.0, b.target_pct * total_after - b.value)
            for name, b in self.buckets.items()
        }
        pool = sum(gaps.values())
        if pool <= 0:
            return [(name, amount * b.target_pct) for name, b in self.buckets.items()]
        return [
            (name, amount * gap / pool) for name, gap in gaps.items() if gap > 0
        ]

    def _allocate_liquid_only(self, amount: float) -> list[tuple[str, float]]:
        """Deploy into buckets you can still spend from, worst-shortfall first.

        Gold is liquid but is not a destination here: it is normally already
        overweight, and its gap comes out negative, which excludes it by itself.
        """
        total_after = self.total + amount
        gaps = {
            name: max(0.0, self.buckets[name].target_pct * total_after - self.buckets[name].value)
            for name in LIQUID
            if name in self.buckets
        }
        pool = sum(gaps.values())
        if pool <= 0:
            # Every liquid bucket is at target and money is still owed. Savings
            # is the least-bad home: spendable, and it at least earns.
            return [("savings", amount)]
        return [(name, amount * gap / pool) for name, gap in gaps.items() if gap > 0]

    def years_of_spending(self) -> float:
        s = self.cashflow.spending_monthly * 12
        return self.total / s if s else 0.0


def live_crypto_value() -> float | None:
    """Real IDR + coin value sitting on Indodax, or None if it cannot be read.

    Indodax is the only holding here with an API, so it is the only one that
    never needs to be typed in: top up the exchange and the balance sheet
    notices by itself on the next refresh.
    """
    from .config import get_secrets

    sec = get_secrets()
    if not sec.has_indodax:
        return None
    try:
        from .exchanges.indodax_public import IndodaxPublicClient
        from .exchanges.indodax_v2 import IndodaxV2Client

        client = IndodaxV2Client(
            sec.indodax_key.get_secret_value(), sec.indodax_secret.get_secret_value()
        )
        avail, held = client.balances()
        total = avail.get("idr", 0.0) + held.get("idr", 0.0)
        coins = {
            k: (avail.get(k, 0.0) + held.get(k, 0.0))
            for k in set(avail) | set(held)
            if k != "idr" and (avail.get(k, 0.0) + held.get(k, 0.0)) > 0
        }
        if coins:
            pub = IndodaxPublicClient()
            for coin, qty in coins.items():
                try:
                    total += qty * pub.last_price(f"{coin}_idr")
                except Exception:  # noqa: BLE001  -- unlisted or delisted coin
                    continue
        return total
    except Exception as exc:  # noqa: BLE001
        log.debug("live crypto balance unavailable: %s", exc)
        return None


def load_networth(
    path: str | Path | None = None,
    *,
    stock_value: float | None = None,
    crypto_value: float | None = None,
) -> NetWorth:
    """Build the balance sheet from holdings.yaml.

    `stock_value` is the live market value of the equity sleeve; pass it in from
    holdings.analyse() so this module never has to touch the network itself.
    `crypto_value` overrides the recorded crypto figure with the live exchange
    balance -- pass `live_crypto_value()` to have top-ups picked up for free.
    """
    p = Path(path) if path else PROJECT_ROOT / "config" / "holdings.yaml"
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}

    others = [
        OtherAsset(
            name=o.get("name", "?"), kind=str(o.get("kind", "other")).lower(),
            value_idr=float(o.get("value_idr", 0) or 0), note=o.get("note", ""),
            rate=float(o.get("rate", 0) or 0),
        )
        for o in raw.get("other_assets", []) or []
    ]
    cash = float(raw.get("cash_idr", 0) or 0)
    targets = raw.get("target_allocation", {}) or {}
    cf = CashFlow(**(raw.get("cashflow", {}) or {}))
    goals = Goals(**(raw.get("goals", {}) or {}))
    providers = {str(k): str(v) for k, v in (raw.get("providers") or {}).items()}

    possessions = [
        Possession(
            name=str(x.get("name", "?")),
            value_idr=float(x.get("value_idr", 0) or 0),
            note=str(x.get("note", "")),
            depreciating=bool(x.get("depreciating", True)),
        )
        for x in raw.get("possessions", []) or []
    ]

    exit_costs: dict[str, tuple[float, str]] = {}
    for k, v in (raw.get("exit_costs") or {}).items():
        if isinstance(v, dict):
            exit_costs[str(k)] = (float(v.get("pct", 0) or 0), str(v.get("why", "")))
        else:
            exit_costs[str(k)] = (float(v or 0), "")

    commitments = []
    for c in raw.get("commitments", []) or []:
        due = c.get("due")
        if isinstance(due, str):
            try:
                due = date.fromisoformat(due.strip())
            except ValueError:
                log.warning("commitment %r has an unreadable due date %r", c.get("name"), due)
                due = None
        elif not isinstance(due, date):
            due = None
        commitments.append(Commitment(
            name=str(c.get("name", "?")),
            amount_idr=float(c.get("amount_idr", 0) or 0),
            due=due, note=str(c.get("note", "")),
            kind=str(c.get("kind", "obligation")).lower(),
        ))

    values = dict.fromkeys(BUCKETS, 0.0)
    values["cash"] = cash
    values["stocks"] = stock_value if stock_value is not None else 0.0
    for o in others:
        values[o.kind if o.kind in BUCKETS else "other"] += o.value_idr
    if crypto_value is not None:
        # The exchange is the source of truth for crypto, not the yaml file.
        values["crypto"] = crypto_value

    buckets = {
        name: Bucket(name=name, value=values[name], target_pct=float(targets.get(name, 0.0)))
        for name in BUCKETS
        if values[name] > 0 or targets.get(name)
    }
    for name in BUCKETS:
        buckets.setdefault(name, Bucket(name, values[name], float(targets.get(name, 0.0))))

    positions: list[tuple[str, float]] = [(o.name, o.value_idr) for o in others]
    return NetWorth(
        buckets=buckets, cashflow=cf, goals=goals, providers=providers,
        emergency_months_target=int(raw.get("emergency_fund_months", 6) or 6),
        positions=positions, other_assets=others, commitments=commitments,
        exit_costs=exit_costs, possessions=possessions,
    )
