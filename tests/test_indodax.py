"""Signing and payload construction.

A malformed order payload is the most expensive kind of bug here, so the rules
Indodax enforces server-side are asserted client-side too.
"""

from __future__ import annotations

import hashlib
import hmac
from urllib.parse import parse_qs

import httpx
import pytest

from lumbung.exchanges.indodax_private import (
    IndodaxError,
    IndodaxPrivateClient,
    base_currency,
    sign_body,
)
from lumbung.exchanges.indodax_public import round_to_increment

SECRET = "test-secret"


def test_sign_body_matches_hmac_sha512():
    body = "method=getInfo&timestamp=1578304294000&recvWindow=5000"
    assert sign_body(SECRET, body) == hmac.new(
        SECRET.encode(), body.encode(), hashlib.sha512
    ).hexdigest()


def test_signature_covers_exact_bytes_sent():
    """The Sign header must be computed over the literal body, or auth fails."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content.decode()
        seen["sign"] = request.headers["Sign"]
        seen["key"] = request.headers["Key"]
        return httpx.Response(200, json={"success": 1, "return": {"balance": {"idr": 5}}})

    c = IndodaxPrivateClient(
        "k", SECRET, client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    c.get_info()
    assert seen["key"] == "k"
    assert seen["sign"] == sign_body(SECRET, seen["body"])
    assert "method=getInfo" in seen["body"]


def _client(capture: dict) -> IndodaxPrivateClient:
    def handler(request: httpx.Request) -> httpx.Response:
        capture.update({k: v[0] for k, v in parse_qs(request.content.decode()).items()})
        return httpx.Response(200, json={"success": 1, "return": {"order_id": 123}})

    return IndodaxPrivateClient(
        "k", SECRET, client=httpx.Client(transport=httpx.MockTransport(handler))
    )


def test_limit_buy_sends_coin_amount_and_moc():
    cap: dict = {}
    _client(cap).trade(
        pair="btc_idr", side="buy", order_type="limit", price=1_338_000_000,
        coin_amount=0.0001, client_order_id="ent-1", time_in_force="MOC",
    )
    assert cap["pair"] == "btc_idr"
    assert cap["type"] == "buy"
    assert cap["btc"] == "0.0001"          # named after the base currency
    assert cap["time_in_force"] == "MOC"   # post-only -> maker fee
    assert "idr" not in cap                # would be rejected by the exchange


def test_limit_order_with_idr_amount_is_refused_locally():
    """Indodax rejects this combination; catch it before it costs a round trip."""
    with pytest.raises(ValueError, match="both"):
        _client({}).trade(
            pair="btc_idr", side="buy", order_type="limit",
            price=1_000_000, coin_amount=0.1, idr_amount=100_000,
        )


def test_market_buy_uses_idr_and_no_price():
    cap: dict = {}
    _client(cap).trade(pair="eth_idr", side="buy", order_type="market", idr_amount=50_000)
    assert cap["idr"] == "50000"
    assert "price" not in cap


def test_market_order_rejects_a_price():
    with pytest.raises(ValueError, match="must not carry a price"):
        _client({}).trade(pair="btc_idr", side="buy", order_type="market",
                          idr_amount=50_000, price=123)


def test_market_sell_uses_coin_amount():
    cap: dict = {}
    _client(cap).trade(pair="sol_idr", side="sell", order_type="market", coin_amount=1.25)
    assert cap["sol"] == "1.25"


def test_cancel_requires_side():
    cap: dict = {}
    _client(cap).cancel_order(pair="btc_idr", order_id=99, side="buy")
    assert cap["type"] == "buy" and cap["order_id"] == "99"
    with pytest.raises(ValueError):
        _client({}).cancel_order(pair="btc_idr", order_id=99, side="long")


def test_tiny_amounts_never_use_scientific_notation():
    """f-string %g would emit '1e-08', which the API rejects."""
    cap: dict = {}
    _client(cap).trade(
        pair="pepe_idr", side="sell", order_type="limit", price=0.000001, coin_amount=1e-08
    )
    assert "e" not in cap["pepe"].lower()
    assert "e" not in cap["price"].lower()


def test_rejects_bad_enum_values():
    for kw in (
        {"side": "long"},
        {"order_type": "stop"},
        {"time_in_force": "IOC"},
    ):
        with pytest.raises(ValueError):
            _client({}).trade(
                pair="btc_idr", order_type=kw.pop("order_type", "limit"),
                side=kw.pop("side", "buy"), price=1, coin_amount=1, **kw
            )


def test_client_order_id_length_capped():
    with pytest.raises(ValueError, match="36"):
        _client({}).trade(pair="btc_idr", side="buy", price=1, coin_amount=1,
                          client_order_id="x" * 37)


def test_error_response_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": 0, "error": "Invalid credentials",
                                         "error_code": "invalid_credentials"})

    c = IndodaxPrivateClient("k", SECRET, client=httpx.Client(
        transport=httpx.MockTransport(handler)))
    with pytest.raises(IndodaxError) as e:
        c.get_info()
    assert e.value.code == "invalid_credentials"


def test_base_currency():
    assert base_currency("btc_idr") == "btc"
    assert base_currency("PEPE_IDR") == "pepe"


def test_post_only_rounding_stays_passive():
    """Buys round DOWN and sells round UP, so a rounded order never crosses."""
    assert round_to_increment(1_338_429_500, 1000, mode="down") == 1_338_429_000
    assert round_to_increment(1_338_429_500, 1000, mode="up") == 1_338_430_000
