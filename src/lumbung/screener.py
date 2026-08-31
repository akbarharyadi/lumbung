"""IDX screener for deploying idle cash, weighted toward sustainable dividend income.

Why income and not momentum: the trend strategy in `strategy/` needs an uptrend,
and IDX is broadly below its 200-day average right now, so it returns nothing.
Dividends do not need an uptrend to pay.

**The trap this module exists to avoid.** A high yield is usually a *symptom*.
Dividend / price rises when the price falls, so the highest-yielding screen row is
often a company in trouble, or one that paid a one-off special that will not repeat.
So yield alone is never the score: it is gated on consistency (did they actually pay
every year), sustainability (is the payout ratio survivable), and stability (how much
do the annual payments swing). Rows that fail those tests are flagged, not hidden.

Nothing here is advice. It is a ranked, transparent screen with its reasoning shown.
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
import yaml

from .config import PROJECT_ROOT

log = logging.getLogger(__name__)

LOT_SIZE = 100
SUBSCRIPTION_IDR = 330_000

# Sectors that overlap with a large BBCA holding. Buying more of these adds income
# but not diversification -- Indonesian banks move together.
BANK_SECTORS = {"Financial Services", "Financials"}


@dataclass
class Candidate:
    ticker: str
    price: float
    sector: str = "?"
    name: str = ""

    # income
    ttm_div: float = 0.0
    div_by_year: dict[int, float] = field(default_factory=dict)
    payout_ratio: float | None = None

    # quality / valuation
    pe: float | None = None
    roe: float | None = None

    # liquidity & trend
    turnover: float = 0.0
    ema50: float = 0.0
    ema200: float = 0.0
    adx: float = 0.0
    high_52w: float = 0.0
    low_52w: float = 0.0
    chg_1y_pct: float = 0.0

    flags: list[str] = field(default_factory=list)
    score: float = 0.0
    score_parts: dict[str, float] = field(default_factory=dict)

    # -- derived ------------------------------------------------------------
    @property
    def short(self) -> str:
        return self.ticker.replace(".JK", "")

    @property
    def lot_cost(self) -> float:
        return self.price * LOT_SIZE

    @property
    def yield_pct(self) -> float:
        return self.ttm_div / self.price * 100 if self.price else 0.0

    @property
    def years_paid(self) -> int:
        """How many of the last 5 calendar years had any dividend."""
        return sum(1 for v in self.div_by_year.values() if v > 0)

    @property
    def div_stability(self) -> float:
        """1.0 = identical every year, 0.0 = wildly erratic. Uses 1 - CV, floored."""
        vals = [v for v in self.div_by_year.values() if v > 0]
        if len(vals) < 2:
            return 0.0
        mean = statistics.fmean(vals)
        if mean <= 0:
            return 0.0
        cv = statistics.pstdev(vals) / mean
        return max(0.0, 1.0 - cv)

    @property
    def uptrend(self) -> bool:
        return self.ema50 > self.ema200

    @property
    def pct_from_ema200(self) -> float:
        return (self.price / self.ema200 - 1) * 100 if self.ema200 else 0.0

    @property
    def pct_from_52w_high(self) -> float:
        return (self.price / self.high_52w - 1) * 100 if self.high_52w else 0.0

    def lots_for(self, budget: float) -> int:
        return int(budget // self.lot_cost) if self.lot_cost else 0

    def income_for(self, budget: float) -> float:
        return self.lots_for(budget) * LOT_SIZE * self.ttm_div

    def deployed_for(self, budget: float) -> float:
        return self.lots_for(budget) * self.lot_cost


def load_universe(path: str | Path | None = None) -> list[str]:
    p = Path(path) if path else PROJECT_ROOT / "config" / "idx_universe.yaml"
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return list(raw.get("universe", []))


def fetch_candidates(tickers: list[str], *, min_turnover: float = 1e9) -> list[Candidate]:
    """Pull prices, dividends and fundamentals. Skips anything too illiquid."""
    import warnings

    import yfinance as yf

    from .strategy.indicators import adx as adx_f
    from .strategy.indicators import ema

    warnings.filterwarnings("ignore")
    hist = yf.download(
        tickers, period="5y", interval="1d", group_by="ticker",
        auto_adjust=False, progress=False, threads=True,
    )

    out: list[Candidate] = []
    this_year = pd.Timestamp.now().year
    for t in tickers:
        try:
            df = hist[t].dropna().rename(columns=str.lower) if len(tickers) > 1 else hist
        except KeyError:
            log.warning("%s: no price data", t)
            continue
        if len(df) < 220:
            log.info("%s: too little history (%d bars)", t, len(df))
            continue

        price = float(df["close"].iloc[-1])
        turnover = float((df["close"] * df["volume"]).tail(60).median())
        if turnover < min_turnover:
            log.info("%s: illiquid (Rp %.0f/day)", t, turnover)
            continue

        c = Candidate(ticker=t, price=price, turnover=turnover)
        c.ema50 = float(ema(df["close"], 50).iloc[-1])
        c.ema200 = float(ema(df["close"], 200).iloc[-1])
        c.adx = float(adx_f(df, 14).iloc[-1])
        w52 = df["close"].tail(252)
        c.high_52w, c.low_52w = float(w52.max()), float(w52.min())
        if len(df) > 252:
            c.chg_1y_pct = (price / float(df["close"].iloc[-252]) - 1) * 100

        tk = yf.Ticker(t)
        try:
            div = tk.dividends
            if len(div):
                div.index = div.index.tz_localize(None)
                c.ttm_div = float(
                    div[div.index >= pd.Timestamp.now() - pd.Timedelta(days=365)].sum()
                )
                # Group by calendar year over the last 5 complete-ish years.
                for yr in range(this_year - 4, this_year + 1):
                    c.div_by_year[yr] = float(div[div.index.year == yr].sum())
        except Exception as exc:  # noqa: BLE001
            log.debug("%s dividends: %s", t, exc)

        try:
            info = tk.info
            c.sector = info.get("sector") or "?"
            c.name = info.get("shortName") or ""
            c.payout_ratio = info.get("payoutRatio")
            c.pe = info.get("trailingPE")
            c.roe = info.get("returnOnEquity")
        except Exception as exc:  # noqa: BLE001
            log.debug("%s info: %s", t, exc)

        out.append(c)
    return out


def score(
    candidates: list[Candidate],
    *,
    budget: float,
    avoid_sectors: set[str] | None = None,
    min_years_paid: int = 3,
) -> list[Candidate]:
    """Rank for income durability, then attach human-readable flags.

    Weights: yield 35, consistency 25, stability 15, sustainability 10,
    trend 10, diversification 5. Yield is the largest single term but a minority
    of the total, which is the point -- it cannot carry a bad row on its own.
    """
    avoid = avoid_sectors or set()
    ranked: list[Candidate] = []

    for c in candidates:
        flags: list[str] = []

        if c.lot_cost > budget:
            flags.append(f"1 lot costs Rp {c.lot_cost:,.0f} — over budget")

        # --- yield, credited only up to 10%: above that it is usually distress ---
        y = min(c.yield_pct, 10.0) / 10.0 * 35

        # --- did they actually pay, every year? ---
        consistency = c.years_paid / 5 * 25
        if c.years_paid < min_years_paid:
            flags.append(f"paid in only {c.years_paid} of the last 5 years")

        # --- how much do the annual payments swing? ---
        stability = c.div_stability * 15
        if c.div_stability < 0.5 and c.years_paid >= 2:
            flags.append("dividend size swings a lot year to year")

        # --- can they keep paying it? ---
        if c.payout_ratio is None:
            sustain = 5.0
        elif c.payout_ratio <= 0:
            sustain = 0.0
            flags.append("negative or zero payout ratio (loss-making?)")
        elif c.payout_ratio > 1.0:
            sustain = 0.0
            flags.append(f"paying out {c.payout_ratio * 100:.0f}% of earnings — above 100%")
        elif c.payout_ratio > 0.85:
            sustain = 4.0
            flags.append(f"high payout ratio {c.payout_ratio * 100:.0f}%")
        else:
            sustain = 10.0

        # --- trend: do not catch a falling knife for the yield ---
        if c.uptrend:
            trend = 10.0
        elif c.pct_from_ema200 > -10:
            trend = 6.0
        elif c.pct_from_ema200 > -20:
            trend = 3.0
        else:
            trend = 0.0
            flags.append(f"{abs(c.pct_from_ema200):.0f}% below its 200-day average")
        if c.chg_1y_pct < -30:
            flags.append(f"down {abs(c.chg_1y_pct):.0f}% over the past year")

        # --- diversification against what you already own ---
        diversify = 0.0 if c.sector in avoid else 5.0
        if c.sector in avoid:
            flags.append("same sector as your BBCA holding — adds income, not diversification")

        if c.yield_pct > 12:
            flags.append(
                f"{c.yield_pct:.1f}% yield is unusually high — check for a one-off special"
            )

        c.score_parts = {
            "yield": round(y, 1), "consistency": round(consistency, 1),
            "stability": round(stability, 1), "sustainability": round(sustain, 1),
            "trend": round(trend, 1), "diversify": round(diversify, 1),
        }
        c.score = round(sum(c.score_parts.values()), 1)
        c.flags = flags
        ranked.append(c)

    ranked.sort(key=lambda x: x.score, reverse=True)
    return ranked


def affordable(candidates: list[Candidate], budget: float) -> list[Candidate]:
    return [c for c in candidates if 0 < c.lot_cost <= budget]


def build_basket(
    candidates: list[Candidate], budget: float, *, n: int = 3, max_per_sector: int = 1
) -> list[tuple[Candidate, int]]:
    """Greedy split of `budget` across the top `n` names, one per sector.

    Whole lots only, so the split is rarely exact; leftover cash stays uninvested
    rather than being forced into an extra lot of whatever happens to be cheapest.
    """
    picks: list[Candidate] = []
    seen: dict[str, int] = {}
    for c in candidates:
        if len(picks) >= n:
            break
        if c.lot_cost > budget / n:
            continue  # cannot take a meaningful position at this budget slice
        if seen.get(c.sector, 0) >= max_per_sector:
            continue
        picks.append(c)
        seen[c.sector] = seen.get(c.sector, 0) + 1

    if not picks:
        return []

    slice_idr = budget / len(picks)
    return [(c, max(1, int(slice_idr // c.lot_cost))) for c in picks]
