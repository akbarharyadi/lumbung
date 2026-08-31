"""Monitoring for stock positions you already own.

The bot does not trade these -- Stockbit has no retail trading API, and a position
this size should not be moved by an algorithm anyway. What this module does is
keep the facts in front of you: real P&L, what the dividend actually pays, what
the trend filter says, and whether a holding has crossed a line you set.

Dividend figures here are **trailing twelve months of declared payments**, not a
forecast. A company can cut at any time.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import yaml

from .config import PROJECT_ROOT, StocksCfg
from .strategy.indicators import adx, atr, donchian_high, ema

log = logging.getLogger(__name__)

LOT_SIZE = 100
# Fallback only. The real figure comes from `goals.subscription_idr` in
# holdings.yaml, because it is a personal number: a second profile has its own
# subscription, or none at all, and should not inherit this one.
SUBSCRIPTION_IDR = 330_000


@dataclass
class Holding:
    ticker: str
    lots: int
    avg_price: float
    note: str = ""
    # Exit levels, as a price per SHARE. Zero means "not set".
    #
    # Written down at purchase, before the position can argue with you. The whole
    # value of a level is that it was chosen while you were calm; deciding at the
    # moment the price moves is how a -13% becomes a -30%. Nothing here places an
    # order -- IDX orders stay manual -- so these are a reminder, not a stop.
    take_profit: float = 0.0
    cut_loss: float = 0.0

    @property
    def shares(self) -> int:
        return self.lots * LOT_SIZE

    @property
    def cost_basis(self) -> float:
        return self.shares * self.avg_price

    @property
    def risk_reward(self) -> float:
        """Reward divided by risk, both measured from the average price.

        Below 1.0 means you are risking more than you stand to make, which is a
        thing worth seeing *before* you buy rather than discovering afterwards.
        """
        if not (self.take_profit and self.cut_loss):
            return 0.0
        reward = self.take_profit - self.avg_price
        risk = self.avg_price - self.cut_loss
        return reward / risk if risk > 0 else 0.0


@dataclass
class AlertCfg:
    drawdown_warn_pct: float = 0.20
    below_ema200_pct: float = 0.10
    new_52w_low: bool = True


@dataclass
class HoldingReport:
    holding: Holding
    price: float
    ema50: float
    ema200: float
    adx: float
    atr: float
    donchian_hi: float
    high_52w: float
    low_52w: float
    ttm_dividend_per_share: float
    last_dividend_date: str
    alerts: list[str]
    # Set from StocksCfg by analyse(). Defaulted so a hand-built report in a
    # test still behaves like the real thing rather than reporting gross.
    dividend_tax_pct: float = 0.10

    # -- position economics ------------------------------------------------
    @property
    def market_value(self) -> float:
        return self.holding.shares * self.price

    @property
    def unrealised(self) -> float:
        return self.market_value - self.holding.cost_basis

    @property
    def unrealised_pct(self) -> float:
        cb = self.holding.cost_basis
        return (self.market_value / cb - 1) * 100 if cb else 0.0

    # -- exit levels -------------------------------------------------------
    @property
    def to_take_profit_pct(self) -> float:
        """How far the price still has to rise. Negative once it is through."""
        tp = self.holding.take_profit
        return (tp / self.price - 1) * 100 if (tp and self.price) else 0.0

    @property
    def to_cut_loss_pct(self) -> float:
        """How far the price can still fall. Negative once it is through."""
        cl = self.holding.cut_loss
        return (self.price / cl - 1) * 100 if (cl and self.price) else 0.0

    @property
    def take_profit_hit(self) -> bool:
        return bool(self.holding.take_profit) and self.price >= self.holding.take_profit

    @property
    def cut_loss_hit(self) -> bool:
        return bool(self.holding.cut_loss) and self.price <= self.holding.cut_loss

    @property
    def exit_state(self) -> str:
        if self.cut_loss_hit:
            return "cut loss reached"
        if self.take_profit_hit:
            return "take profit reached"
        if self.holding.take_profit or self.holding.cut_loss:
            return "between levels"
        return "no levels set"

    @property
    def breakeven_move_pct(self) -> float:
        return (self.holding.avg_price / self.price - 1) * 100 if self.price else 0.0

    # -- income ------------------------------------------------------------
    @property
    def annual_income_gross(self) -> float:
        """Declared dividend, before the final PPh is withheld."""
        return self.ttm_dividend_per_share * self.holding.shares

    @property
    def annual_income(self) -> float:
        """What actually reaches you.

        Net, deliberately. SBN coupons and savings interest are both stored net
        of their own (different) tax here, so a gross dividend sitting beside
        them silently wins comparisons it should sometimes lose.
        """
        return self.annual_income_gross * (1 - self.dividend_tax_pct)

    @property
    def monthly_income(self) -> float:
        return self.annual_income / 12

    @property
    def monthly_income_gross(self) -> float:
        return self.annual_income_gross / 12

    @property
    def yield_on_cost_pct(self) -> float:
        return (
            self.ttm_dividend_per_share / self.holding.avg_price * 100
            if self.holding.avg_price else 0.0
        )

    @property
    def yield_on_market_pct(self) -> float:
        return self.ttm_dividend_per_share / self.price * 100 if self.price else 0.0

    # -- trend -------------------------------------------------------------
    @property
    def uptrend(self) -> bool:
        return self.ema50 > self.ema200

    @property
    def signal(self) -> str:
        if self.price > self.donchian_hi and self.uptrend and self.adx > 25:
            return "BUY"
        if not self.uptrend:
            return "NO BUY - downtrend (EMA50 below EMA200)"
        return "NO BUY - no breakout"

    @property
    def from_52w_high_pct(self) -> float:
        return (self.price / self.high_52w - 1) * 100 if self.high_52w else 0.0


def _level(raw: dict, key: str, avg_price: float) -> float:
    """Read an exit level as either an absolute price or a percentage.

        take_profit: 8500        -> Rp 8,500 per share
        take_profit_pct: 15      -> 15% above the average price
        cut_loss_pct: -15        -> 15% below it

    Percentages are accepted because that is how the decision is actually made
    ("I'll cut at minus fifteen"), and converting by hand is a step at which
    people give up and set nothing. The sign on `cut_loss_pct` is ignored: a cut
    loss is always below the average price, and reading -15 and +15 differently
    would be a trap rather than a feature.
    """
    if raw.get(key) is not None:
        return float(raw[key])
    pct = raw.get(f"{key}_pct")
    if pct is None:
        return 0.0
    frac = abs(float(pct)) / 100.0
    return avg_price * (1 + frac) if key == "take_profit" else avg_price * (1 - frac)


def load_holdings(path: str | Path | None = None) -> tuple[list[Holding], float, AlertCfg]:
    p = Path(path) if path else PROJECT_ROOT / "config" / "holdings.yaml"
    if not p.exists():
        return [], 0.0, AlertCfg()
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    holdings = [
        Holding(
            ticker=h["ticker"], lots=int(h["lots"]), avg_price=float(h["avg_price"]),
            note=h.get("note", ""),
            take_profit=_level(h, "take_profit", float(h["avg_price"])),
            cut_loss=_level(h, "cut_loss", float(h["avg_price"])),
        )
        for h in raw.get("stocks", []) or []
    ]
    return holdings, float(raw.get("cash_idr", 0) or 0), AlertCfg(**(raw.get("alerts") or {}))


def analyse(
    holdings: list[Holding], cfg: StocksCfg, alerts: AlertCfg, *, period: str = "3y"
) -> list[HoldingReport]:
    """Price, trend, dividend and alert state for each holding."""
    import yfinance as yf

    reports: list[HoldingReport] = []
    for h in holdings:
        tk = yf.Ticker(h.ticker)
        hist = tk.history(period=period, auto_adjust=False).rename(columns=str.lower)
        if hist.empty or len(hist) < 210:
            log.warning("%s: not enough history to analyse", h.ticker)
            continue

        d = hist.copy()
        d["ema50"] = ema(d["close"], cfg.ema_fast)
        d["ema200"] = ema(d["close"], cfg.ema_slow)
        d["atr"] = atr(d, cfg.atr_period)
        d["adx"] = adx(d, cfg.atr_period)
        d["dh"] = donchian_high(d["high"], cfg.donchian_lookback)
        r = d.iloc[-1]
        price = float(r["close"])

        w52 = d["close"].tail(252)
        high_52w, low_52w = float(w52.max()), float(w52.min())

        div = tk.dividends
        ttm, last_date = 0.0, "-"
        if len(div):
            div.index = div.index.tz_localize(None)
            window = div[div.index >= pd.Timestamp.now() - pd.Timedelta(days=365)]
            ttm = float(window.sum())
            last_date = str(div.index[-1].date())

        fired: list[str] = []
        if price < h.avg_price * (1 - alerts.drawdown_warn_pct):
            fired.append(
                f"down {(1 - price / h.avg_price) * 100:.1f}% from your average "
                f"(threshold {alerts.drawdown_warn_pct:.0%})"
            )
        if not pd.isna(r["ema200"]) and price < float(r["ema200"]) * (1 - alerts.below_ema200_pct):
            fired.append(
                f"{(1 - price / float(r['ema200'])) * 100:.1f}% below its 200-day average"
            )
        if alerts.new_52w_low and price <= low_52w * 1.001:
            fired.append("at or near a new 52-week low")

        reports.append(
            HoldingReport(
                holding=h, price=price,
                ema50=float(r["ema50"]), ema200=float(r["ema200"]),
                adx=float(r["adx"]), atr=float(r["atr"]), donchian_hi=float(r["dh"]),
                high_52w=high_52w, low_52w=low_52w,
                ttm_dividend_per_share=ttm, last_dividend_date=last_date, alerts=fired,
                dividend_tax_pct=cfg.dividend_tax_pct,
            )
        )
    return reports


@dataclass
class PortfolioSummary:
    reports: list[HoldingReport]
    cash_idr: float
    crypto_equity: float = 0.0
    # Paper equity is simulated money drawn from the same cash, so counting both
    # would inflate the total. Only live crypto equity is real net worth.
    crypto_is_real: bool = False

    @property
    def stock_value(self) -> float:
        return sum(r.market_value for r in self.reports)

    @property
    def stock_cost(self) -> float:
        return sum(r.holding.cost_basis for r in self.reports)

    @property
    def stock_unrealised(self) -> float:
        return self.stock_value - self.stock_cost

    @property
    def total_value(self) -> float:
        crypto = self.crypto_equity if self.crypto_is_real else 0.0
        return self.stock_value + self.cash_idr + crypto

    @property
    def annual_income(self) -> float:
        return sum(r.annual_income for r in self.reports)

    @property
    def monthly_income(self) -> float:
        return self.annual_income / 12

    @property
    def subscription_coverage_pct(self) -> float:
        """How much of the recurring subscription the dividends already cover."""
        return self.monthly_income / SUBSCRIPTION_IDR * 100

    def as_message(self) -> str:
        """Compact summary, sized for a phone screen."""
        lines = [
            "📋 Portfolio",
            f"stocks   Rp {self.stock_value:,.0f} ({self.stock_unrealised:+,.0f})",
            f"cash     Rp {self.cash_idr:,.0f}",
        ]
        if self.crypto_equity:
            tag = "" if self.crypto_is_real else " (paper)"
            lines.append(f"crypto   Rp {self.crypto_equity:,.0f}{tag}")
        lines.append(f"total    Rp {self.total_value:,.0f}")
        lines.append("")
        lines.append(
            f"dividends Rp {self.monthly_income:,.0f}/mo "
            f"= {self.subscription_coverage_pct:.0f}% of your subscription"
        )
        for r in self.reports:
            tag = r.holding.ticker.replace(".JK", "")
            lines.append(f"\n{tag}: Rp {r.price:,.0f} ({r.unrealised_pct:+.2f}%) — {r.signal}")
            for a in r.alerts:
                lines.append(f"  ⚠️ {a}")
        return "\n".join(lines)
