"""Indodax private Trade API (TAPI) client.

Reference: https://github.com/btcid/indodax-official-api-docs (Private-RestAPI.md)

Every call is ``POST https://indodax.com/tapi`` with a form-encoded body.
Auth is two headers:
    Key  = the API key
    Sign = HMAC-SHA512(secret, <the exact urlencoded body string>) as hex

The signature covers the *literal bytes we send*, so the body string is built
once and reused for both signing and transmission -- never re-encoded.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx

log = logging.getLogger(__name__)

TAPI_URL = "https://indodax.com/tapi"
SERVER_TIME_URL = "https://indodax.com/api/server_time"

# Accepted values, per the official docs.
ORDER_TYPES = {"limit", "market"}
SIDES = {"buy", "sell"}
TIME_IN_FORCE = {"GTC", "MOC"}  # MOC = maker-or-cancel (post-only) -> 0% maker fee


class IndodaxError(RuntimeError):
    """The exchange returned success=0."""

    def __init__(self, message: str, code: str | None = None, payload: dict | None = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.payload = payload or {}


class IndodaxTransportError(RuntimeError):
    """Network / HTTP-level failure. Safe to retry for reads; NOT for writes."""


@dataclass(frozen=True)
class OrderResult:
    order_id: int | None
    client_order_id: str | None
    raw: dict[str, Any]


def sign_body(secret: str, body: str) -> str:
    """HMAC-SHA512 hex digest of the urlencoded body, keyed by the API secret."""
    return hmac.new(secret.encode("utf-8"), body.encode("utf-8"), hashlib.sha512).hexdigest()


class IndodaxPrivateClient:
    """Thin, explicit client. One method per TAPI call, no magic."""

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        *,
        recv_window: int = 5000,
        timeout: float = 15.0,
        min_interval_sec: float = 0.15,
        client: httpx.Client | None = None,
        force_ipv4: bool = True,
        force_ipv4_bind: str = "0.0.0.0",
    ) -> None:
        if not api_key or not api_secret:
            raise ValueError("Indodax API key/secret missing. Fill .env from .env.example.")
        self._key = api_key
        self._secret = api_secret
        self._recv_window = recv_window
        # Force IPv4. Indodax Trade API V2 whitelists a single IP, and on a
        # dual-stack connection Python prefers IPv6 -- so the exchange would see
        # your IPv6 address while the whitelist holds your IPv4, and every
        # signed call fails with an authorisation error that looks like a bad key.
        self._client = client or httpx.Client(
            timeout=timeout,
            transport=httpx.HTTPTransport(local_address=force_ipv4_bind) if force_ipv4 else None,
        )
        self._lock = threading.Lock()
        self._min_interval = min_interval_sec
        self._last_call = 0.0
        self._time_offset_ms = 0

    # -- internals ---------------------------------------------------------
    def _throttle(self) -> None:
        """Stay well under the documented 20 trade/s + 30 cancel/s limits."""
        elapsed = time.monotonic() - self._last_call
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_call = time.monotonic()

    def _now_ms(self) -> int:
        return int(time.time() * 1000) + self._time_offset_ms

    def sync_time(self) -> int:
        """Align our clock with the exchange so `timestamp` is never rejected."""
        try:
            resp = self._client.get(SERVER_TIME_URL)
            resp.raise_for_status()
            # /api/server_time returns MILLIseconds (verified live), unlike the
            # `server_time` field inside a ticker payload, which is seconds.
            server_ms = int(resp.json()["server_time"])
            self._time_offset_ms = server_ms - int(time.time() * 1000)
            log.info("Indodax clock offset: %+d ms", self._time_offset_ms)
        except Exception as exc:  # non-fatal: the local clock is usually fine
            log.warning("server_time sync failed (%s); using local clock", exc)
        return self._time_offset_ms

    def _call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"method": method}
        for k, v in (params or {}).items():
            if v is not None:
                payload[k] = v
        payload["timestamp"] = self._now_ms()
        payload["recvWindow"] = self._recv_window

        body = urlencode(payload)
        headers = {
            "Key": self._key,
            "Sign": sign_body(self._secret, body),
            "Content-Type": "application/x-www-form-urlencoded",
        }

        with self._lock:
            self._throttle()
            try:
                resp = self._client.post(TAPI_URL, content=body, headers=headers)
            except httpx.HTTPError as exc:
                raise IndodaxTransportError(f"{method}: {exc}") from exc

        if resp.status_code == 429:
            raise IndodaxTransportError(f"{method}: rate limited (429)")
        if resp.status_code >= 400:
            raise IndodaxTransportError(f"{method}: HTTP {resp.status_code}: {resp.text[:300]}")

        try:
            data = resp.json()
        except ValueError as exc:
            raise IndodaxTransportError(f"{method}: non-JSON response: {resp.text[:300]}") from exc

        if data.get("success") != 1:
            raise IndodaxError(data.get("error", "unknown error"), data.get("error_code"), data)
        return data.get("return", {})

    # -- read methods ------------------------------------------------------
    def get_info(self) -> dict[str, Any]:
        """Balances, held balances, server time. The safe call to test credentials."""
        return self._call("getInfo")

    def balances(self) -> tuple[dict[str, float], dict[str, float]]:
        """(available, on_hold) as float maps, e.g. idr -> 250000.0, btc -> 0.0012."""
        info = self.get_info()
        avail = {k: float(v) for k, v in info.get("balance", {}).items()}
        hold = {k: float(v) for k, v in info.get("balance_hold", {}).items()}
        return avail, hold

    def open_orders(self, pair: str | None = None) -> list[dict[str, Any]]:
        result = self._call("openOrders", {"pair": pair})
        orders = result.get("orders", [])
        # With no pair, Indodax returns a {pair: [orders]} map instead of a flat list.
        if isinstance(orders, dict):
            flat: list[dict[str, Any]] = []
            for pair_key, lst in orders.items():
                for o in lst or []:
                    o.setdefault("pair", pair_key)
                    flat.append(o)
            return flat
        return orders or []

    def get_order(self, pair: str, order_id: int | str) -> dict[str, Any]:
        return self._call("getOrder", {"pair": pair, "order_id": order_id}).get("order", {})

    def get_order_by_client_order_id(self, client_order_id: str) -> dict[str, Any]:
        return self._call("getOrderByClientOrderId", {"client_order_id": client_order_id}).get(
            "order", {}
        )

    def trade_history(
        self, pair: str, *, count: int | None = None, since: int | None = None
    ) -> list[dict[str, Any]]:
        result = self._call("tradeHistory", {"pair": pair, "count": count, "since": since})
        return result.get("trades", []) or []

    # -- write methods -----------------------------------------------------
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
        """Place an order.

        Indodax amount rules, enforced here so a bad payload never leaves the process:
          * limit buy   -> coin_amount + price     (passing the IDR amount is REJECTED)
          * market buy  -> idr_amount, no price
          * sell (both) -> coin_amount; price only for limit
        """
        side = side.lower()
        order_type = order_type.lower()
        if side not in SIDES:
            raise ValueError(f"side must be one of {SIDES}, got {side!r}")
        if order_type not in ORDER_TYPES:
            raise ValueError(f"order_type must be one of {ORDER_TYPES}, got {order_type!r}")
        if time_in_force is not None and time_in_force not in TIME_IN_FORCE:
            raise ValueError(f"time_in_force must be one of {TIME_IN_FORCE}, got {time_in_force!r}")
        if client_order_id is not None and len(client_order_id) > 36:
            raise ValueError("client_order_id must be <= 36 chars")

        params: dict[str, Any] = {"pair": pair, "type": side, "order_type": order_type}

        if order_type == "limit":
            if price is None:
                raise ValueError("limit order requires a price")
            if coin_amount is None:
                raise ValueError("limit order requires coin_amount (the IDR amount is rejected)")
            if idr_amount is not None:
                raise ValueError(
                    "Indodax rejects a limit order carrying both the IDR amount and order_type=limit"
                )
            params["price"] = _fmt(price)
            params[base_currency(pair)] = _fmt(coin_amount)
            if time_in_force:
                params["time_in_force"] = time_in_force
        else:  # market
            if price is not None:
                raise ValueError("market order must not carry a price")
            if side == "buy":
                if idr_amount is None:
                    raise ValueError("market buy requires idr_amount")
                params["idr"] = _fmt(idr_amount)
            else:
                if coin_amount is None:
                    raise ValueError("market sell requires coin_amount")
                params[base_currency(pair)] = _fmt(coin_amount)

        if client_order_id:
            params["client_order_id"] = client_order_id

        result = self._call("trade", params)
        raw_id = result.get("order_id")
        return OrderResult(
            order_id=int(raw_id) if raw_id not in (None, "", 0, "0") else None,
            client_order_id=result.get("client_order_id") or client_order_id,
            raw=result,
        )

    def cancel_order(self, *, pair: str, order_id: int | str, side: str) -> dict[str, Any]:
        """Cancel by exchange order id. Indodax requires the side, so the journal stores it."""
        side = side.lower()
        if side not in SIDES:
            raise ValueError(f"side must be one of {SIDES}, got {side!r}")
        return self._call("cancelOrder", {"pair": pair, "order_id": order_id, "type": side})

    def cancel_by_client_order_id(self, client_order_id: str) -> dict[str, Any]:
        return self._call("cancelByClientOrderId", {"client_order_id": client_order_id})

    def close(self) -> None:
        self._client.close()


class DryRunIndodaxClient(IndodaxPrivateClient):
    """Logs what it *would* send and returns a synthetic ack. Sends no writes.

    Reads (getInfo, openOrders, ...) still hit the real API so balances are truthful.
    """

    def trade(self, **kwargs: Any) -> OrderResult:  # type: ignore[override]
        log.warning("[DRY-RUN] trade %s", kwargs)
        return OrderResult(order_id=None, client_order_id=kwargs.get("client_order_id"), raw=kwargs)

    def cancel_order(self, **kwargs: Any) -> dict[str, Any]:  # type: ignore[override]
        log.warning("[DRY-RUN] cancelOrder %s", kwargs)
        return {"dry_run": True, **kwargs}

    def cancel_by_client_order_id(self, client_order_id: str) -> dict[str, Any]:  # type: ignore[override]
        log.warning("[DRY-RUN] cancelByClientOrderId %s", client_order_id)
        return {"dry_run": True, "client_order_id": client_order_id}


def base_currency(pair: str) -> str:
    """btc_idr -> btc. The amount field for a pair is named after its base coin."""
    return pair.split("_")[0].lower()


def _fmt(value: float) -> str:
    """8-dp fixed notation. Avoids scientific notation (1e-05), which the API rejects."""
    return f"{value:.8f}".rstrip("0").rstrip(".") or "0"
