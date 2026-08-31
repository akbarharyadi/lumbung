"""What a stock trade actually costs, and whether a dividend is worth chasing.

Two calculators that exist to replace a feeling with a number.

**Selling.** "Should I sell?" usually gets answered on the percentage shown in
the broker app, which is the one number that ignores everything you pay to act
on it. This computes proceeds, the realised result, the 0.1% final PPh, the
broker fee, and the income you stop receiving -- so "I'll have fresh cash" has
its price attached.

**Dividend hunting.** Buying just before an ex-date feels like free money and
almost never is: on the ex-date the price drops by roughly the dividend, which
is mechanical rather than sentiment. The calculator below states that plainly
and does the arithmetic anyway, because being told "no" with numbers is more
convincing than being told "no".

Indonesian specifics that decide both answers:

* **0.1% PPh final on sale proceeds**, whether you gained or lost. There is no
  capital-loss offset for retail equity here -- so selling a loser gives you
  nothing to deduct, and the usual tax-loss-harvesting argument does not exist.
* **10% final PPh on dividends** for individuals, unless reinvested in Indonesia
  under PP 9/2021, in which case it can be exempt. Defaults assume the tax is
  paid, because assuming the exemption and being wrong overstates every result.
* Broker fees around 0.15% buy / 0.25% sell (the sell figure already includes
  the 0.1% PPh at most brokers; here they are kept separate so both are visible).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

SHARES_PER_LOT = 100

# Defaults; overridable per call. See config.StocksCfg for the configured pair.
FEE_BUY = 0.0015
FEE_SELL = 0.0015          # broker commission only
PPH_SALE = 0.001           # 0.1% final, on PROCEEDS, gain or loss
PPH_DIVIDEND = 0.10        # 10% final for individuals, unless PP 9/2021 applies


def _pct(part: float, whole: float) -> float:
    return part / whole * 100 if whole else 0.0


# --------------------------------------------------------------------- selling
@dataclass
class SellQuote:
    """Everything that changes if you sell, in rupiah."""

    lots: int
    price: float
    avg_price: float
    proceeds: float
    cost_basis: float
    gross_pnl: float
    pph: float
    broker_fee: float
    net_proceeds: float
    net_pnl: float
    dividend_lost_monthly: float
    breakeven_price: float

    @property
    def gross_pnl_pct(self) -> float:
        return _pct(self.gross_pnl, self.cost_basis)

    @property
    def net_pnl_pct(self) -> float:
        return _pct(self.net_pnl, self.cost_basis)

    @property
    def costs(self) -> float:
        return self.pph + self.broker_fee

    @property
    def is_loss(self) -> bool:
        return self.net_pnl < 0

    @property
    def months_of_dividend_forgone(self) -> float:
        """How many months of dividends the selling costs alone consume."""
        if self.dividend_lost_monthly <= 0:
            return 0.0
        return self.costs / self.dividend_lost_monthly


def quote_sell(
    *,
    lots: int,
    price: float,
    avg_price: float,
    annual_dividend_per_share: float = 0.0,
    fee_sell: float = FEE_SELL,
    pph_sale: float = PPH_SALE,
) -> SellQuote:
    """Price a sale of `lots` at `price`, against a cost basis of `avg_price`."""
    shares = max(0, int(lots)) * SHARES_PER_LOT
    proceeds = shares * price
    cost_basis = shares * avg_price
    pph = proceeds * pph_sale
    broker = proceeds * fee_sell
    net_proceeds = proceeds - pph - broker

    # Break-even is the price at which the sale returns the cost basis *after*
    # costs -- always above avg_price, which is why "I'll sell when it gets back
    # to what I paid" leaves you slightly down.
    denom = 1 - pph_sale - fee_sell
    breakeven = avg_price / denom if denom > 0 else float("inf")

    return SellQuote(
        lots=int(lots),
        price=price,
        avg_price=avg_price,
        proceeds=proceeds,
        cost_basis=cost_basis,
        gross_pnl=proceeds - cost_basis,
        pph=pph,
        broker_fee=broker,
        net_proceeds=net_proceeds,
        net_pnl=net_proceeds - cost_basis,
        dividend_lost_monthly=shares * annual_dividend_per_share / 12,
        breakeven_price=breakeven,
    )


# ----------------------------------------------------------- dividend hunting
@dataclass
class DividendCapture:
    """Buy before the ex-date, collect, sell after. Priced honestly."""

    ticker: str
    lots: int
    price: float
    dividend_per_share: float
    days_to_ex: int
    buy_cost: float
    dividend_gross: float
    dividend_tax: float
    dividend_net: float
    expected_drop: float
    round_trip_cost: float
    net_edge: float
    hold_months_to_recover: float

    @property
    def yield_pct(self) -> float:
        return _pct(self.dividend_per_share, self.price)

    @property
    def edge_pct(self) -> float:
        return _pct(self.net_edge, self.buy_cost)

    @property
    def worth_it(self) -> bool:
        return self.net_edge > 0

    @property
    def verdict(self) -> str:
        if self.worth_it:
            return "positive on paper"
        return "negative — the drop and the tax exceed the dividend"


def quote_dividend_capture(
    *,
    ticker: str,
    lots: int,
    price: float,
    dividend_per_share: float,
    days_to_ex: int,
    drop_ratio: float = 1.0,
    fee_buy: float = FEE_BUY,
    fee_sell: float = FEE_SELL,
    pph_sale: float = PPH_SALE,
    pph_dividend: float = PPH_DIVIDEND,
) -> DividendCapture:
    """Price a dividend-capture round trip.

    `drop_ratio` is how much of the dividend the price gives back on the ex-date.
    It defaults to **1.0** -- the full dividend -- because that is the mechanical
    expectation and the honest null hypothesis. Lowering it is a bet that the
    market will not fully adjust, and it should be entered deliberately, not
    assumed by a default that quietly makes every trade look good.
    """
    shares = max(0, int(lots)) * SHARES_PER_LOT
    gross_value = shares * price
    buy_cost = gross_value * (1 + fee_buy)

    div_gross = shares * dividend_per_share
    div_tax = div_gross * pph_dividend
    div_net = div_gross - div_tax

    expected_drop = shares * dividend_per_share * drop_ratio

    # Selling after the ex-date, at the dropped price.
    sale_value = gross_value - expected_drop
    round_trip = gross_value * fee_buy + sale_value * (fee_sell + pph_sale)

    net_edge = div_net - expected_drop - round_trip

    monthly_div = shares * dividend_per_share / 12
    recover = round_trip / monthly_div if monthly_div > 0 else float("inf")

    return DividendCapture(
        ticker=ticker,
        lots=int(lots),
        price=price,
        dividend_per_share=dividend_per_share,
        days_to_ex=days_to_ex,
        buy_cost=buy_cost,
        dividend_gross=div_gross,
        dividend_tax=div_tax,
        dividend_net=div_net,
        expected_drop=expected_drop,
        round_trip_cost=round_trip,
        net_edge=net_edge,
        hold_months_to_recover=recover,
    )


def breakeven_drop_ratio(
    *, fee_buy: float = FEE_BUY, fee_sell: float = FEE_SELL,
    pph_sale: float = PPH_SALE, pph_dividend: float = PPH_DIVIDEND,
) -> float:
    """How little of the dividend the price must give back for capture to pay.

    Expressed as a fraction of the dividend. Anything at or above this and the
    trade loses money. It is well below 1.0, which is the whole story: the price
    has to *fail* to adjust by a wide margin before chasing the dividend beats
    simply owning the shares.
    """
    # net_edge > 0  <=>  d(1-t) - d*k - costs > 0. Costs scale with price rather
    # than the dividend, so the exact threshold depends on yield; this returns
    # the tax-only bound, which is the ceiling any real trade must beat.
    return 1.0 - pph_dividend


def days_until(target: date, today: date | None = None) -> int:
    return (target - (today or date.today())).days
