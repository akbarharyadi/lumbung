"""The "what should I actually do" list.

Every recommendation in this project already existed, scattered across modules
and printed by different commands. Scattered advice is advice you do not follow.
This gathers it into one ranked list.

**There is no tick box, deliberately.** Every item is computed from live numbers,
so acting on one changes the numbers and the item disappears on its own. A
manual "done" flag would be a second source of truth for something the data
already knows -- and a stale tick could hide a real problem, which is the worst
thing a checklist can do.

Ranked by what actually hurts: the safety net above allocation drift, drift
above a bond offering you can skip.

Nothing here places an order or edits config. It reports, you act, the numbers
move, the item goes.
"""

from __future__ import annotations

import hashlib
import logging
import math
from dataclasses import dataclass
from datetime import date

from .networth import LIQUID

log = logging.getLogger(__name__)

# Ranked worst-first. The order is the priority: a thin emergency fund outranks
# a tidy allocation, and both outrank a bond offering you can simply skip.
SEVERITY = {"urgent": 0, "soon": 1, "idea": 2}

# What it costs to sell out of each bucket, as a fraction, plus why. These are
# the numbers that decide whether trimming an overweight is worth doing now or
# worth waiting out -- and they differ by more than an order of magnitude, which
# is exactly why one blanket "never sell" rule was wrong.
EXIT_COST = {
    "gold": (0.03, "Pegadaian buy/sell spread, roughly 2-3%"),
    "stocks": (0.0025, "0.1% final PPh on proceeds plus about 0.15% brokerage"),
    "crypto": (0.0021, "0.21% final PPh on the sale"),
    "bonds": (0.01, "secondary-market price risk; SR trades above or below par"),
    "savings": (0.0, "just a transfer"),
    "cash": (0.0, "just a transfer"),
}

# Below this share of net worth an overweight is noise, not a decision.
DRIFT_BAND = 0.05

# How long after payday the deployment reminder stays up. Long enough to survive
# a weekend and a busy week; short enough that it is gone before the next one.
PAYDAY_WINDOW_DAYS = 7


@dataclass
class Action:
    kind: str                  # stable machine key, e.g. "emergency_fund"
    subject: str               # what it is about, e.g. "bonds" or "BBCA"
    title: str
    detail: str
    severity: str = "idea"
    amount: float = 0.0        # rupiah, 0 when not a money move
    stale_hint: str = ""       # which setting to update once this is done

    @property
    def id(self) -> str:
        return hashlib.sha1(f"{self.kind}|{self.subject}".encode()).hexdigest()[:16]

    @property
    def rank(self) -> int:
        return SEVERITY.get(self.severity, 9)

    def as_dict(self) -> dict:
        return {
            "id": self.id, "kind": self.kind, "subject": self.subject,
            "title": self.title, "detail": self.detail, "severity": self.severity,
            "amount": self.amount, "stale_hint": self.stale_hint,
        }


# -- generation --------------------------------------------------------------
def build_actions(nw, *, offerings=None, reviews=None, bot=None,
                  weight_ceiling: float = 0.20,
                  today: date | None = None) -> list[Action]:
    """Turn the current picture into a ranked to-do list.

    Pure: takes already-loaded objects and returns Actions. That is what makes it
    testable without a database, an exchange or a network.
    """
    out: list[Action] = []
    today = today or date.today()

    # 0. Commitments. Ahead of the emergency fund only in the sense that it
    #    explains it: a promise is why the buffer looks thin, and paying it is
    #    not optional.
    # Obligations only. A wish on the to-do list is not a to-do, and the text
    # below would tell them it is holding money back when it is not -- the exact
    # confusion the obligation/wish split exists to prevent.
    _due = sorted(
        (c for c in nw.commitments if not c.is_wish),
        key=lambda x: (x.due is None, x.due),
    )
    for c in _due:
        d = c.days_away(today)
        if d is not None and d < 0:
            sev = "urgent"
            when = f"was due {-d} days ago"
        elif d is None:
            sev = "soon"
            when = "no date recorded"
        elif d <= 45:
            sev = "soon"
            when = f"due in {d} days"
        else:
            continue  # further out than the planning horizon needs to shout about
        out.append(Action(
            kind="commitment", subject=c.name,
            title=f"{c.name}: Rp {c.amount_idr:,.0f} {when}",
            detail=(
                f"Held liquid and kept out of bonds and the trading sleeve until "
                f"it is paid. Free buffer after it: "
                f"Rp {nw.free_liquid(today):,.0f}"
                + (f". {c.note.rstrip('. ')}." if c.note else ".")
                + " Delete it from holdings.yaml once it is done, or it will keep "
                "holding money back."
            ),
            severity=sev, amount=c.amount_idr, stale_hint="commitments",
        ))

    # 1. Emergency fund. Outranks everything else: allocation elegance is no use
    #    if one bad month forces you to sell something at the wrong time.
    short = nw.emergency_shortfall
    if short > 0:
        # Report the buffer net of promises: the gross figure is the one that
        # looks fine right up until the bill lands.
        months = nw.months_covered_free()
        committed = nw.committed_total()
        # Spell out what is already counted. "Top up your safety net" with no
        # composition invites the reasonable question "counted from what?" -- and
        # the answer matters, because gold is in there and gold is not cash.
        parts = ", ".join(
            f"{name} Rp {nw.buckets[name].value:,.0f}"
            for name in LIQUID
            if name in nw.buckets and nw.buckets[name].value > 0
        )
        out.append(Action(
            kind="emergency_fund", subject="liquid",
            title=f"Top up your safety net by Rp {short:,.0f}",
            detail=(
                f"Counted so far: {parts} = Rp {nw.liquid_now:,.0f}. "
                + (
                    f"Rp {committed:,.0f} of that is already promised, leaving "
                    f"Rp {nw.free_liquid():,.0f} free. "
                    if committed > 0 else ""
                )
                + f"That is {months:.1f} months of "
                f"Rp {nw.cashflow.spending_monthly:,.0f} spending against a "
                f"{nw.emergency_months_target}-month target "
                f"(Rp {nw.emergency_target:,.0f}). "
                f"At Rp {nw.cashflow.surplus:,.0f}/month that is "
                f"{nw.months_to_fund_emergency:.1f} months of saving. "
                "Gold is counted because Pegadaian sells same-day, but it moves in "
                "price — if you want a floor that cannot fall, hold more of it in "
                "savings."
            ),
            severity="urgent" if months < 3 else "soon",
            amount=short,
            stale_hint="cash_idr",
        ))

    # 1b. Do the two targets even agree?
    #
    # Allocation says what share each bucket should be; the safety net says how
    # many months of spending must be liquid. Nothing forces them to be
    # compatible, and when they are not the dashboard shows every allocation on
    # target AND a permanent red safety-net warning -- a state no amount of
    # saving can leave. Better to say the targets disagree than to nag forever
    # about a shortfall that is arithmetically unreachable.
    liquid_target_pct = sum(
        nw.buckets[b].target_pct for b in LIQUID if b in nw.buckets
    )
    net_total = nw.total
    if net_total > 0 and liquid_target_pct > 0 and nw.emergency_target > 0:
        supported = liquid_target_pct * net_total
        if supported < nw.emergency_target * 0.9:
            months_supported = (
                supported / nw.cashflow.spending_monthly
                if nw.cashflow.spending_monthly else 0
            )
            needed_pct = nw.emergency_target / net_total * 100
            out.append(Action(
                kind="target_conflict", subject="targets",
                title="Your two targets cannot both be met",
                detail=(
                    f"Allocation puts {liquid_target_pct * 100:.0f}% in liquid "
                    f"buckets (Rp {supported:,.0f}), which is "
                    f"{months_supported:.1f} months of spending. The safety net "
                    f"asks for {nw.emergency_months_target} months "
                    f"(Rp {nw.emergency_target:,.0f}). Even hitting every "
                    "allocation target exactly would leave it short. Either drop "
                    f"the safety net to about {months_supported:.0f} months, or "
                    f"raise the liquid targets to about {needed_pct:.0f}% of net "
                    "worth. Both are defensible; keeping both as they are is not."
                ),
                severity="soon",
                stale_hint="emergency_fund_months",
            ))

    # 2. Payday deployment. Only AFTER the money has landed, never before.
    #
    # This used to fire five days ahead, which is advice you cannot act on: the
    # salary is not in the account yet. A to-do you are unable to complete is
    # the fastest way to train someone to ignore the list. So the window opens
    # on payday and stays open for a few days afterwards, which is when the
    # money is actually sitting there waiting to be moved.
    surplus = nw.cashflow.surplus
    payday_day = nw.cashflow.payday_day
    landed = False
    if payday_day:
        today_day = today.day
        # Short months: a payday set to the 31st lands on the last day there is.
        import calendar

        last = calendar.monthrange(today.year, today.month)[1]
        effective = min(payday_day, last)
        landed = effective <= today_day <= effective + PAYDAY_WINDOW_DAYS

    if surplus > 0 and landed:
        split = nw.allocate_surplus(surplus)
        if split:
            parts = ", ".join(f"{name} Rp {amt:,.0f}" for name, amt in split)
            out.append(Action(
                kind="deploy_surplus", subject="payday",
                title=f"Deploy Rp {surplus:,.0f} of surplus",
                detail=(
                    "The salary has landed. Sending it where you are furthest "
                    f"below target: {parts}."
                ),
                severity="soon", amount=surplus,
                stale_hint="cash_idr",
            ))

    # 3. Allocation drift, worst bucket first. Only underweight buckets get an
    #    action: fixing overweight means selling, which costs tax and fees, and
    #    new money fixes it for free given a little patience.
    total = nw.total
    if total > 0:
        for name, b in sorted(nw.buckets.items(), key=lambda kv: kv[1].gap_idr(total),
                              reverse=True):
            gap = b.gap_idr(total)
            if b.target_pct <= 0 or gap <= total * 0.05:
                continue
            out.append(Action(
                kind="rebalance", subject=name,
                title=f"Add Rp {gap:,.0f} to {name}",
                detail=(
                    f"{name} is {b.value / total * 100:.0f}% of net worth against a "
                    f"{b.target_pct * 100:.0f}% target. New money closes this without "
                    "selling anything."
                ),
                severity="idea", amount=gap,
            ))

        # Overweight buckets. These used to be omitted entirely on the grounds
        # that selling costs tax and new money fixes drift for free. That was
        # half right: waiting IS usually better, but saying nothing left you
        # looking at a +9 on the dashboard with no explanation of why nothing was
        # recommended. So the excess is surfaced with BOTH paths priced, and the
        # recommendation stays "wait" only where waiting is genuinely short.
        surplus = max(0.0, nw.cashflow.surplus)
        for name, b in sorted(nw.buckets.items(), key=lambda kv: kv[1].value, reverse=True):
            if b.target_pct <= 0:
                continue
            excess = b.value - b.target_pct * total
            if excess <= total * DRIFT_BAND:
                continue

            rate, why = EXIT_COST.get(name, (0.0, ""))
            # Your provider's real number beats the class-wide default. Pegadaian
            # quotes Tabungan Emas at the buyback rate, so the recorded value is
            # already what you would receive and the spread is not charged twice.
            if name in getattr(nw, "exit_costs", {}):
                rate, override_why = nw.exit_costs[name]
                why = override_why or f"{rate:.2%}, from your exit_costs config"
            cost = excess * rate
            # Growing into the target: how big net worth must get for this
            # holding to be the right share of it, without selling anything.
            need_total = b.value / b.target_pct
            months = (need_total - total) / surplus if surplus > 0 else float("inf")

            if months <= 6:
                advice = (
                    f"New money closes this in about {months:.0f} months, which is "
                    f"cheaper than the Rp {cost:,.0f} it costs to sell."
                )
            elif months == float("inf"):
                advice = f"Trimming costs about Rp {cost:,.0f} ({why})."
            else:
                advice = (
                    f"New money would take about {months:.0f} months to fix this. "
                    f"Trimming Rp {excess:,.0f} now costs about Rp {cost:,.0f} "
                    f"({why}) — worth weighing against waiting that long."
                )

            out.append(Action(
                kind="overweight", subject=name,
                title=f"{name} is Rp {excess:,.0f} above target",
                detail=(
                    f"{name} is {b.value / total * 100:.0f}% of net worth against a "
                    f"{b.target_pct * 100:.0f}% target. " + advice
                ),
                severity="idea", amount=excess,
            ))

    # 4. Bond offerings that are open and closing.
    for o in offerings or []:
        if not o.is_open(today):
            continue
        left = o.days_left(today)
        out.append(Action(
            kind="bond_offer", subject=o.series,
            title=f"{o.series} closes in {left} day(s)",
            detail=(
                f"{o.coupon * 100:.2f}% gross, {o.net_coupon * 100:.2f}% net of the 10% "
                f"SBN tax, minimum Rp {o.min_idr:,.0f}. {o.liquidity.capitalize()}."
            ),
            severity="soon" if left <= 7 else "idea",
            stale_hint="other_assets",
        ))

    # 5. Stock concentration and business signals. Only "act" is surfaced here:
    #    "watch" is for reading, not for a to-do list, and a checklist that
    #    fills up with things you are not meant to do yet is one you stop using.
    for rev in reviews or []:
        for sig in rev.signals:
            if sig.severity != "act":
                continue
            detail = sig.detail
            if rev.trim_reason:
                detail = f"{detail} — {rev.trim_reason}"

            # Concentration and the asset-class target are two different rules,
            # and when you own exactly one stock they disagree. "Stocks should be
            # 40% of net worth" and "no single stock above 20%" can only both be
            # true with at least two names -- so a bare "trim to 20%" is the
            # wrong instruction: it fixes concentration by making the class
            # badly underweight, and leaves the proceeds sitting in cash.
            if sig.rule == "concentration":
                cls = nw.buckets["stocks"].target_pct if "stocks" in nw.buckets else 0.0
                ceiling = weight_ceiling
                if cls > ceiling > 0:
                    names = math.ceil(cls / ceiling)
                    detail += (
                        f". Note the asset-class target is {cls * 100:.0f}%, so "
                        f"selling down to {ceiling * 100:.0f}% and stopping would "
                        f"leave stocks badly underweight. Both rules are only "
                        f"satisfiable with at least {names} names: either move the "
                        "proceeds straight into other stocks, or add other names "
                        "with new money and let this one's share fall on its own"
                    )
            # ".JK" is the Yahoo Finance suffix for the Jakarta exchange -- an
            # implementation detail of where the price came from, not something
            # that belongs in front of someone reading their own portfolio.
            short = rev.ticker.replace(".JK", "")
            out.append(Action(
                kind=f"stock_{sig.rule.replace(' ', '_')}", subject=short,
                title=f"{short}: {sig.rule}",
                detail=detail,
                severity="soon",
            ))

    # 6. The bot, when it has stopped itself. A halted engine is silent by
    #    design, and silence is exactly what you fail to notice.
    if bot and bot.get("halted"):
        out.append(Action(
            kind="bot_halted", subject="engine",
            title="The trading engine has halted itself",
            detail=(
                "It hit the drawdown limit and will not open new positions until "
                "you resume it. Check what happened before restarting."
            ),
            severity="urgent",
        ))

    out.sort(key=lambda a: (a.rank, -a.amount))
    return out
