"""Indodax Trade API **v2** client.

Keys generated from the current Indodax UI are v2 keys, and they are rejected by
the old ``/tapi`` endpoint with "Access denied for this API key version". v2 is a
different API in almost every respect:

|              | v1 (`/tapi`)                | v2 (`api.indodax.com`)          |
|--------------|-----------------------------|---------------------------------|
| host         | indodax.com                 | api.indodax.com                 |
| shape        | one POST, `method=` in body | REST paths + verbs              |
| key header   | `Key`                       | `X-APIKEY`                      |
| signature    | HMAC-SHA512                 | **HMAC-SHA256**                 |
| pair         | `btc_idr`                   | `btcidr`                        |
| amount field | named after the coin(`btc`) | `quantity`                      |
| side / type  | lowercase                   | UPPERCASE                       |

The published v2 doc says SHA256 in prose but its worked example is 128 hex
characters (SHA512). SHA256 is the one that actually authenticates -- verified
against the live account endpoint, so the example is simply stale.

Order enums were probed against the live API rather than trusted from the doc:
``type`` accepts only LIMIT and MARKET (there is no LIMIT_MAKER), while
``timeInForce`` accepts GTC and **MOC** -- so post-only, and therefore the maker
fee, is still available.

The public method names match `IndodaxPrivateClient` so `LiveBroker` works
against either without changes.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import threading
import time
from typing import Any
from urllib.parse import urlencode

import httpx

from .indodax_private import IndodaxError, IndodaxTransportError, OrderResult, base_currency

log = logging.getLogger(__name__)

BASE_URL = "https://api.indodax.com"
SIDES = {"buy", "sell"}
ORDER_TYPES = {"limit", "market"}
TIME_IN_FORCE = {"GTC", "MOC"}  # MOC = maker-or-cancel (post-only)


def sign_v2(secret: str, payload: str) -> str:
    """HMAC-SHA256 hex of the query string / body, keyed by the API secret."""
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def to_symbol(pair: str) -> str:
    """`btc_idr` -> `btcidr`. v2 uses the compact form."""
    return pair.replace("_", "").lower()


class IndodaxV2Client:
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        *,
        recv_window: int = 5000,
        timeout: float = 20.0,
        min_interval_sec: float = 0.15,
        client: httpx.Client | None = None,
        force_ipv4: bool = True,
    ) -> None:
        if not api_key or not api_secret:
            raise ValueError("Indodax API key/secret missing. Fill .env from .env.example.")
        self._key = api_key
        self._secret = api_secret
        self._recv_window = recv_window
        # v2 keys are IP-whitelisted. On a dual-stack link Python prefers IPv6,
        # so the exchange would see an address that is not on the whitelist.
        self._client = client or httpx.Client(
            timeout=timeout,
            transport=httpx.HTTPTransport(local_address="0.0.0.0") if force_ipv4 else None,
        )
        self._lock = threading.Lock()
        self._min_interval = min_interval_sec
        self._last_call = 0.0
        self._time_offset_ms = 0

    # ------------------------------------------------------------ internals
    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_call
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_call = time.monotonic()

    def _now_ms(self) -> int:
        return int(time.time() * 1000) + self._time_offset_ms

    def sync_time(self) -> int:
        try:
            r = self._client.get("https://indodax.com/api/server_time")
            r.raise_for_status()
            server_ms = int(r.json()["server_time"])  # already milliseconds
            self._time_offset_ms = server_ms - int(time.time() * 1000)
            log.info("Indodax clock offset: %+d ms", self._time_offset_ms)
        except Exception as exc:  # noqa: BLE001  -- local clock is usually fine
            log.warning("server_time sync failed (%s); using local clock", exc)
        return self._time_offset_ms

    def _request(self, method: str, path: str, params: dict[str, Any] | None = None) -> Any:
        payload: dict[str, Any] = {k: v for k, v in (params or {}).items() if v is not None}
        payload["timestamp"] = self._now_ms()
        payload["recvWindow"] = self._recv_window

        qs = urlencode(payload)
        signed = qs + "&signature=" + sign_v2(self._secret, qs)
        headers = {"Accept": "application/json", "X-APIKEY": self._key}

        with self._lock:
            self._throttle()
            try:
                if method == "POST":
                    headers["Content-Type"] = "application/x-www-form-urlencoded"
                    resp = self._client.post(BASE_URL + path, content=signed, headers=headers)
                else:
                    resp = self._client.request(
                        method, f"{BASE_URL}{path}?{signed}", headers=headers
                    )
            except httpx.HTTPError as exc:
                raise IndodaxTransportError(f"{method} {path}: {exc}") from exc

        try:
            data = resp.json()
        except ValueError as exc:
            raise IndodaxTransportError(
                f"{method} {path}: non-JSON response: {resp.text[:200]}"
            ) from exc

        if resp.status_code >= 400 or (isinstance(data, dict) and data.get("code", 0) < 0):
            msg = data.get("msg", resp.text[:200]) if isinstance(data, dict) else resp.text[:200]
            code = str(data.get("code")) if isinstance(data, dict) else str(resp.status_code)
            if resp.status_code == 429:
                raise IndodaxTransportError(f"{method} {path}: rate limited")
            raise IndodaxError(msg, code, data if isinstance(data, dict) else {})
        return data

    # ---------------------------------------------------------------- reads
    def get_info(self) -> dict[str, Any]:
        """Account snapshot. Shaped like v1's getInfo so callers need no change."""
        acct = self._request("GET", "/api/v2/account", {"omitZeroBalances": "false"})
        balance: dict[str, str] = {}
        hold: dict[str, str] = {}
        for b in acct.get("balances", []):
            asset = str(b.get("asset", "")).lower()
            balance[asset] = b.get("free", "0")
            hold[asset] = b.get("locked", "0")
        return {
            "balance": balance,
            "balance_hold": hold,
            "user_id": acct.get("uid"),
            "email": acct.get("email", ""),
            # v1 used 1/0; expose the v2 boolean the same way.
            "withdraw_status": 1 if acct.get("canWithdraw") else 0,
            "can_trade": bool(acct.get("canTrade")),
            "account_type": acct.get("accountType"),
            "server_time": self._now_ms(),
        }

    def balances(self) -> tuple[dict[str, float], dict[str, float]]:
        info = self.get_info()
        avail = {k: float(v or 0) for k, v in info["balance"].items()}
        held = {k: float(v or 0) for k, v in info["balance_hold"].items()}
        return avail, held

    def open_orders(self, pair: str | None = None) -> list[dict[str, Any]]:
        params = {"symbol": to_symbol(pair)} if pair else None
        data = self._request("GET", "/api/v2/openOrders", params)
        orders = data if isinstance(data, list) else data.get("orders", data.get("data", []))
        return list(orders or [])

    def get_order(self, pair: str, order_id: int | str) -> dict[str, Any]:
        """v2 has no single-order lookup, so search open orders then history."""
        for o in self.open_orders(pair):
            if str(o.get("orderId")) == str(order_id):
                return o
        try:
            hist = self._request(
                "GET", "/api/v2/order/histories", {"symbol": to_symbol(pair), "limit": 100}
            )
            rows = hist if isinstance(hist, list) else hist.get("data", [])
            for o in rows or []:
                if str(o.get("orderId")) == str(order_id):
                    return o
        except IndodaxError as exc:
            log.debug("order history lookup failed: %s", exc)
        return {}

    def get_order_by_client_order_id(self, client_order_id: str) -> dict[str, Any]:
        for o in self.open_orders():
            if str(o.get("clientOrderId")) == str(client_order_id):
                return o
        return {}

    def trade_history(
        self, pair: str, *, count: int | None = None, since: int | None = None
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"symbol": to_symbol(pair), "limit": count or 100}
        if since:
            params["startTime"] = int(since) * 1000
        data = self._request("GET", "/api/v2/myTrades", params)
        return list((data if isinstance(data, list) else data.get("data", [])) or [])

    # --------------------------------------------------------------- writes
    def trade(
        self,
        *,
        pair: str,
        side: str,
        order_type: str = "limit",
        price: float | None = None,
        idr_amount: float | None = None,
        coin_amount: float | None = None,
        client_order_id: str | None = None,
        time_in_force: str | None = None,
    ) -> OrderResult:
        side_l, type_l = side.lower(), order_type.lower()
        if side_l not in SIDES:
            raise ValueError(f"side must be one of {SIDES}, got {side!r}")
        if type_l not in ORDER_TYPES:
            raise ValueError(f"order_type must be one of {ORDER_TYPES}, got {order_type!r}")
        if time_in_force is not None and time_in_force not in TIME_IN_FORCE:
            raise ValueError(f"time_in_force must be one of {TIME_IN_FORCE}, got {time_in_force!r}")
        if client_order_id is not None and len(client_order_id) > 36:
            raise ValueError("client_order_id must be <= 36 chars")

        body: dict[str, Any] = {
            "symbol": to_symbol(pair),
            "side": side_l.upper(),
            "type": type_l.upper(),
        }

        if type_l == "limit":
            if price is None:
                raise ValueError("limit order requires a price")
            if coin_amount is None:
                raise ValueError("limit order requires coin_amount")
            body["price"] = _fmt(price)
            body["quantity"] = _fmt(coin_amount)
            if time_in_force:
                body["timeInForce"] = time_in_force
        else:
            if price is not None:
                raise ValueError("market order must not carry a price")
            if coin_amount is not None:
                body["quantity"] = _fmt(coin_amount)
            elif idr_amount is not None:
                # Spend a rupiah amount rather than a coin amount.
                body["quoteOrderQty"] = _fmt(idr_amount)
            else:
                raise ValueError("market order requires coin_amount or idr_amount")

        if client_order_id:
            body["clientOrderId"] = client_order_id

        res = self._request("POST", "/api/v2/order", body)
        raw_id = res.get("orderId") if isinstance(res, dict) else None
        return OrderResult(
            order_id=int(raw_id) if raw_id not in (None, "", 0, "0") else None,
            client_order_id=(res.get("clientOrderId") if isinstance(res, dict) else None)
            or client_order_id,
            raw=res if isinstance(res, dict) else {"raw": res},
        )

    def cancel_order(self, *, pair: str, order_id: int | str, side: str) -> dict[str, Any]:
        """`side` is unused by v2 but kept so the call site matches v1."""
        return self._request(
            "DELETE", "/api/v2/order", {"symbol": to_symbol(pair), "orderId": order_id}
        )

    def cancel_by_client_order_id(self, client_order_id: str) -> dict[str, Any]:
        o = self.get_order_by_client_order_id(client_order_id)
        if not o:
            raise IndodaxError(f"no open order with clientOrderId {client_order_id}")
        return self.cancel_order(
            pair=str(o.get("symbol", "")), order_id=o["orderId"], side=str(o.get("side", "buy"))
        )

    def close(self) -> None:
        self._client.close()


class DryRunV2Client(IndodaxV2Client):
    """Logs writes instead of sending them. Reads still hit the live API."""

    def trade(self, **kwargs: Any) -> OrderResult:  # type: ignore[override]
        log.warning("[DRY-RUN] order %s", kwargs)
        return OrderResult(None, kwargs.get("client_order_id"), kwargs)

    def cancel_order(self, **kwargs: Any) -> dict[str, Any]:  # type: ignore[override]
        log.warning("[DRY-RUN] cancel %s", kwargs)
        return {"dry_run": True, **kwargs}


def _fmt(value: float) -> str:
    """Fixed notation; scientific ('1e-05') is rejected by the API."""
    return f"{value:.8f}".rstrip("0").rstrip(".") or "0"


__all__ = ["IndodaxV2Client", "DryRunV2Client", "sign_v2", "to_symbol", "base_currency"]
