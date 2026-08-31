"""Broker abstraction so paper and live share one code path.

The engine only ever talks to this interface. That is deliberate: if paper and
live used different code, the thing you tested for three days would not be the
thing that later spends your money.

PaperBroker prices against the REAL live order book -- it fakes the balance, not
the market.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Protocol

from ..config import CostsCfg
from ..exchanges.indodax_private import IndodaxPrivateClient, base_currency
from ..exchanges.indodax_public import IndodaxPublicClient, round_to_increment

log = logging.getLogger(__name__)


@dataclass
class PlacedOrder:
    client_order_id: str
    exchange_order_id: str | None
    accepted: bool
    reason: str = ""


@dataclass
class OrderStatus:
    status: str  # open | filled | cancelled | rejected | unknown
    filled_qty: float = 0.0
    avg_price: float = 0.0
    fee: float = 0.0


class Broker(Protocol):
    def balances(self) -> tuple[dict[str, float], dict[str, float]]: ...
    def place_post_only(
        self, *, pair: str, side: str, qty: float, price: float, client_order_id: str
    ) -> PlacedOrder: ...
    def place_market(
        self, *, pair: str, side: str, client_order_id: str,
        qty: float | None = None, idr: float | None = None,
    ) -> PlacedOrder: ...
    def cancel(self, *, pair: str, order_id: str, side: str) -> bool: ...
    def status(self, *, pair: str, order_id: str, client_order_id: str) -> OrderStatus: ...


class LiveBroker:
    """Real orders on Indodax. Entries and non-urgent exits are post-only (MOC)."""

    def __init__(self, client: IndodaxPrivateClient, public: IndodaxPublicClient) -> None:
        self.client = client
        self.public = public

    def balances(self) -> tuple[dict[str, float], dict[str, float]]:
        return self.client.balances()

    def place_post_only(
        self, *, pair: str, side: str, qty: float, price: float, client_order_id: str
    ) -> PlacedOrder:
        tick = self.public.price_increment(pair)
        # Round the resting price AWAY from the market so the order stays a maker:
        # a buy rounds down, a sell rounds up.
        px = round_to_increment(price, tick, mode="down" if side == "buy" else "up")
        try:
            res = self.client.trade(
                pair=pair,
                side=side,
                order_type="limit",
                price=px,
                coin_amount=qty,
                client_order_id=client_order_id,
                time_in_force="MOC",  # maker-or-cancel: rejected rather than crossing
            )
            return PlacedOrder(client_order_id, str(res.order_id) if res.order_id else None, True)
        except Exception as exc:  # noqa: BLE001
            log.warning("post-only %s %s failed: %s", side, pair, exc)
            return PlacedOrder(client_order_id, None, False, str(exc))

    def place_market(
        self, *, pair: str, side: str, client_order_id: str,
        qty: float | None = None, idr: float | None = None,
    ) -> PlacedOrder:
        try:
            res = self.client.trade(
                pair=pair,
                side=side,
                order_type="market",
                coin_amount=qty if side == "sell" else None,
                idr_amount=idr if side == "buy" else None,
                client_order_id=client_order_id,
            )
            return PlacedOrder(client_order_id, str(res.order_id) if res.order_id else None, True)
        except Exception as exc:  # noqa: BLE001
            log.error("market %s %s failed: %s", side, pair, exc)
            return PlacedOrder(client_order_id, None, False, str(exc))

    def cancel(self, *, pair: str, order_id: str, side: str) -> bool:
        try:
            self.client.cancel_order(pair=pair, order_id=order_id, side=side)
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("cancel %s %s failed: %s", pair, order_id, exc)
            return False

    def status(self, *, pair: str, order_id: str, client_order_id: str) -> OrderStatus:
        try:
            o = (
                self.client.get_order(pair, order_id)
                if order_id
                else self.client.get_order_by_client_order_id(client_order_id)
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("status %s %s failed: %s", pair, order_id, exc)
            return OrderStatus("unknown")

        if not o:
            return OrderStatus("unknown")
        raw = str(o.get("status", "")).lower()
        base = base_currency(pair)
        filled = float(o.get(f"{base}_filled") or o.get("filled") or 0.0)
        price = float(o.get("price") or 0.0)
        mapping = {"open": "open", "filled": "filled", "cancelled": "cancelled",
                   "canceled": "cancelled", "rejected": "rejected"}
        return OrderStatus(mapping.get(raw, "unknown"), filled, price)


@dataclass
class _PaperOrder:
    client_order_id: str
    pair: str
    side: str
    qty: float
    price: float
    post_only: bool
    created: float = field(default_factory=time.time)
    status: str = "open"
    filled_qty: float = 0.0
    avg_price: float = 0.0
    fee: float = 0.0


class PaperBroker:
    """Simulated fills against the live book. Same interface as LiveBroker.

    Fill rule for a resting post-only order: it fills only once the market
    actually trades through it (best ask <= our bid, or best bid >= our ask).
    Assuming an instant fill at the touch is the most common way paper trading
    flatters itself.
    """

    def __init__(
        self,
        public: IndodaxPublicClient,
        costs: CostsCfg,
        *,
        starting_idr: float,
        state: dict | None = None,
    ) -> None:
        self.public = public
        self.costs = costs
        self.bal: dict[str, float] = state.get("balances") if state else {"idr": starting_idr}
        self.bal.setdefault("idr", starting_idr)
        self.orders: dict[str, _PaperOrder] = {}
        if state:
            for d in state.get("orders", []):
                self.orders[d["client_order_id"]] = _PaperOrder(**d)

    def export_state(self) -> dict:
        return {
            "balances": self.bal,
            "orders": [o.__dict__ for o in self.orders.values() if o.status == "open"],
        }

    def balances(self) -> tuple[dict[str, float], dict[str, float]]:
        held: dict[str, float] = {}
        for o in self.orders.values():
            if o.status != "open":
                continue
            if o.side == "buy":
                held["idr"] = held.get("idr", 0.0) + o.qty * o.price
            else:
                b = base_currency(o.pair)
                held[b] = held.get(b, 0.0) + o.qty
        return dict(self.bal), held

    def place_post_only(
        self, *, pair: str, side: str, qty: float, price: float, client_order_id: str
    ) -> PlacedOrder:
        tick = self.public.price_increment(pair)
        px = round_to_increment(price, tick, mode="down" if side == "buy" else "up")
        book = self.public.book_top(pair)
        # MOC semantics: an order that would cross is rejected, not filled.
        if (side == "buy" and px >= book.ask) or (side == "sell" and px <= book.bid):
            return PlacedOrder(client_order_id, None, False, "would cross book (MOC reject)")
        if side == "buy" and qty * px > self.bal.get("idr", 0.0):
            return PlacedOrder(client_order_id, None, False, "insufficient IDR")
        if side == "sell" and qty > self.bal.get(base_currency(pair), 0.0):
            return PlacedOrder(client_order_id, None, False, "insufficient coin")
        self.orders[client_order_id] = _PaperOrder(
            client_order_id, pair, side, qty, px, post_only=True
        )
        return PlacedOrder(client_order_id, f"paper-{client_order_id}", True)

    def place_market(
        self, *, pair: str, side: str, client_order_id: str,
        qty: float | None = None, idr: float | None = None,
    ) -> PlacedOrder:
        book = self.public.book_top(pair)
        px = book.ask if side == "buy" else book.bid
        if side == "buy":
            spend = idr if idr is not None else (qty or 0) * px
            qty_filled = spend / px
        else:
            qty_filled = qty or 0.0
        o = _PaperOrder(client_order_id, pair, side, qty_filled, px, post_only=False)
        self._fill(o, px, qty_filled, taker=True)
        self.orders[client_order_id] = o
        return PlacedOrder(client_order_id, f"paper-{client_order_id}", True)

    def cancel(self, *, pair: str, order_id: str, side: str) -> bool:
        for o in self.orders.values():
            if o.status == "open" and (o.client_order_id == order_id or order_id.endswith(o.client_order_id)):
                o.status = "cancelled"
                return True
        return False

    def status(self, *, pair: str, order_id: str, client_order_id: str) -> OrderStatus:
        o = self.orders.get(client_order_id)
        if o is None:
            return OrderStatus("unknown")
        return OrderStatus(o.status, o.filled_qty, o.avg_price, o.fee)

    def poll_fills(self) -> list[_PaperOrder]:
        """Advance resting orders against the current book. Call once per loop."""
        newly: list[_PaperOrder] = []
        for o in self.orders.values():
            if o.status != "open":
                continue
            try:
                book = self.public.book_top(o.pair)
            except Exception:  # noqa: BLE001
                continue
            crossed = (o.side == "buy" and book.ask <= o.price) or (
                o.side == "sell" and book.bid >= o.price
            )
            if crossed:
                self._fill(o, o.price, o.qty, taker=False)
                newly.append(o)
        return newly

    def _fill(self, o: _PaperOrder, price: float, qty: float, *, taker: bool) -> None:
        base = base_currency(o.pair)
        notional = price * qty
        if o.side == "buy":
            fee = notional * self.costs.buy_cost_pct(taker=taker)
            self.bal["idr"] = self.bal.get("idr", 0.0) - notional - fee
            self.bal[base] = self.bal.get(base, 0.0) + qty
        else:
            fee = notional * self.costs.sell_cost_pct(taker=taker)
            self.bal[base] = self.bal.get(base, 0.0) - qty
            self.bal["idr"] = self.bal.get("idr", 0.0) + notional - fee
        o.status = "filled"
        o.filled_qty = qty
        o.avg_price = price
        o.fee = fee
