"""Retail government bonds (SBN Ritel), compared on the number that matters.

The headline rates make SBN and a digital-bank savings account look similar --
6.80% against 6.00%. They are not, because they are taxed differently:

    SBN coupon            10% final PPh
    deposit / savings     20% final PPh

So 6.80% gross becomes **6.12% net**, while 6.00% gross becomes **4.80% net**.
The gap is 1.3 percentage points, roughly double what the headline suggests.
Comparing gross rates across instruments with different tax treatment is the
single easiest way to pick the wrong one, so everything here is computed net.

One exception worth knowing: Indonesian savings-account interest is only taxed
once the balance exceeds Rp 7.5jt. Below that, a savings account really does
earn its gross rate, which is why small balances are treated separately.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import yaml

from .config import PROJECT_ROOT

SBN_TAX = 0.10          # PPh final on SBN coupons
DEPOSIT_TAX = 0.20      # PPh final on deposit and savings interest
SAVINGS_TAX_FREE_BALANCE = 7_500_000  # below this, savings interest is untaxed


def net_rate(gross: float, taxed: str, *, balance: float = 0.0) -> float:
    """Annual rate after the tax that actually applies to that instrument."""
    if taxed == "sbn":
        return gross * (1 - SBN_TAX)
    if taxed == "deposit":
        if balance and balance <= SAVINGS_TAX_FREE_BALANCE:
            return gross
        return gross * (1 - DEPOSIT_TAX)
    return gross


@dataclass
class Offering:
    series: str
    kind: str
    tenor_years: int
    coupon: float
    opens: date
    closes: date
    matures: date
    min_idr: float
    tradeable: bool
    note: str = ""

    @property
    def net_coupon(self) -> float:
        return net_rate(self.coupon, "sbn")

    def is_open(self, today: date | None = None) -> bool:
        d = today or date.today()
        return self.opens <= d <= self.closes

    def days_left(self, today: date | None = None) -> int:
        return (self.closes - (today or date.today())).days

    def monthly_income(self, amount: float) -> float:
        return amount * self.net_coupon / 12

    @property
    def liquidity(self) -> str:
        if self.tradeable:
            return "sellable on the secondary market (at a price that can move)"
        return "not tradeable; early redemption only, usually once, with a fee"


@dataclass
class Alternative:
    name: str
    rate: float
    liquid: bool
    taxed: str
    note: str = ""

    def net(self, balance: float = 0.0) -> float:
        return net_rate(self.rate, self.taxed, balance=balance)

    def monthly_income(self, amount: float) -> float:
        return amount * self.net(amount) / 12


def load_bonds(path: str | Path | None = None) -> tuple[list[Offering], list[Alternative]]:
    p = Path(path) if path else PROJECT_ROOT / "config" / "bonds.yaml"
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    offerings = [
        Offering(
            series=o["series"], kind=o.get("kind", "SR"),
            tenor_years=int(o["tenor_years"]), coupon=float(o["coupon"]),
            opens=_d(o["opens"]), closes=_d(o["closes"]), matures=_d(o["matures"]),
            min_idr=float(o.get("min_idr", 1_000_000)),
            tradeable=bool(o.get("tradeable", False)), note=o.get("note", ""),
        )
        for o in raw.get("offerings", []) or []
    ]
    alts = [
        Alternative(
            name=a["name"], rate=float(a["rate"]), liquid=bool(a.get("liquid", False)),
            taxed=a.get("taxed", "none"), note=a.get("note", ""),
        )
        for a in raw.get("alternatives", []) or []
    ]
    return offerings, alts


def _d(v) -> date:
    return v if isinstance(v, date) else date.fromisoformat(str(v))


def recommend_tenor(
    offerings: list[Offering], *, months_of_buffer: float, emergency_target: int
) -> tuple[Offering | None, str]:
    """Pick a tenor from how much slack you have, not from the highest coupon.

    A longer bond pays more, but locking money away is only sensible once the
    safety net is already covered elsewhere. Chasing the extra 0.10% while your
    buffer is thin is how people end up redeeming early at a loss.
    """
    open_now = [o for o in offerings if o.is_open()]
    if not open_now:
        return None, "No offering is open right now."

    if months_of_buffer < emergency_target:
        shortest = min(open_now, key=lambda o: o.tenor_years)
        return shortest, (
            f"Your safety net is {months_of_buffer:.1f} months against a "
            f"{emergency_target}-month target, so take the shorter tenor. The extra "
            "0.10% on the longer one is not worth locking money you may need."
        )

    longest = max(open_now, key=lambda o: o.net_coupon)
    return longest, (
        "Your safety net is covered, so the longer tenor is fine — it pays more "
        "and you are not relying on this money."
    )
