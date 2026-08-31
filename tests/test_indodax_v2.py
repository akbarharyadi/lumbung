"""Trade API v2: signing, payload shape, and the v1 differences that bite.

Every fact asserted here was verified against the live API, because the published
v2 doc disagrees with itself in two places (see indodax_v2 module docstring).
"""

from __future__ import annotations

import hashlib
import hmac
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from lumbung.exchanges.indodax_private import IndodaxError
from lumbung.exchanges.indodax_v2 import IndodaxV2Client, sign_v2, to_symbol

SECRET = "test-secret"


def test_signature_is_sha256_not_sha512():
    """The doc's worked example is 128 hex chars (SHA512) but SHA256 is what
    actually authenticates -- confirmed against the live account endpoint."""
    qs = "omitZeroBalances=false&timestamp=1578304294000&recvWindow=5000"
    assert sign_v2(SECRET, qs) == hmac.new(SECRET.encode(), qs.encode(), hashlib.sha256).hexdigest()
    assert len(sign_v2(SECRET, qs)) == 64


def test_symbol_drops_the_underscore():
    assert to_symbol("btc_idr") == "btcidr"
    assert to_symbol("PEPE_IDR") == "pepeidr"


def _client(capture: dict, response: dict | None = None) -> IndodaxV2Client:
    def handler(request: httpx.Request) -> httpx.Response:
        capture["method"] = request.method
        capture["path"] = urlparse(str(request.url)).path
        capture["headers"] = dict(request.headers)
        raw = request.content.decode() or urlparse(str(request.url)).query
        capture["params"] = {k: v[0] for k, v in parse_qs(raw).items()}
        return httpx.Response(200, json=response or {"orderId": 42, "clientOrderId": "x"})

    return IndodaxV2Client(
        "KEY", SECRET, client=httpx.Client(transport=httpx.MockTransport(handler))
    )


def test_uses_the_x_apikey_header_not_key():
    cap: dict = {}
    _client(cap, {"balances": []}).get_info()
    assert cap["headers"]["x-apikey"] == "KEY"
    assert "key" not in {k.lower() for k in cap["headers"]} - {"x-apikey"}


def test_signature_covers_the_exact_query_string():
    cap: dict = {}
    _client(cap, {"balances": []}).get_info()
    sig = cap["params"].pop("signature")
    from urllib.parse import urlencode

    assert sig == sign_v2(SECRET, urlencode(cap["params"]))


def test_limit_order_uses_quantity_and_uppercase_enums():
    cap: dict = {}
    _client(cap).trade(
        pair="btc_idr", side="buy", order_type="limit",
        price=1_335_000_000, coin_amount=0.0001,
        client_order_id="ent-1", time_in_force="MOC",
    )
    assert cap["method"] == "POST" and cap["path"] == "/api/v2/order"
    assert cap["params"]["symbol"] == "btcidr"
    assert cap["params"]["side"] == "BUY"        # v1 used lowercase
    assert cap["params"]["type"] == "LIMIT"
    assert cap["params"]["quantity"] == "0.0001"  # v1 named this field 'btc'
    assert cap["params"]["timeInForce"] == "MOC"  # post-only -> maker fee
    assert cap["params"]["clientOrderId"] == "ent-1"


def test_market_sell_sends_quantity_and_no_price():
    cap: dict = {}
    _client(cap).trade(pair="sol_idr", side="sell", order_type="market", coin_amount=1.25)
    assert cap["params"]["type"] == "MARKET"
    assert cap["params"]["quantity"] == "1.25"
    assert "price" not in cap["params"]


def test_market_buy_by_rupiah_uses_quote_order_qty():
    cap: dict = {}
    _client(cap).trade(pair="btc_idr", side="buy", order_type="market", idr_amount=50_000)
    assert cap["params"]["quoteOrderQty"] == "50000"


def test_cancel_is_a_delete_with_order_id():
    cap: dict = {}
    _client(cap, {"orderId": 7}).cancel_order(pair="btc_idr", order_id=7, side="buy")
    assert cap["method"] == "DELETE" and cap["path"] == "/api/v2/order"
    assert cap["params"]["orderId"] == "7" and cap["params"]["symbol"] == "btcidr"


def test_rejects_limit_maker_type_because_the_api_does():
    """Probed live: type accepts only LIMIT or MARKET."""
    with pytest.raises(ValueError):
        _client({}).trade(pair="btc_idr", side="buy", order_type="LIMIT_MAKER",
                          price=1, coin_amount=1)


def test_rejects_unknown_time_in_force():
    with pytest.raises(ValueError):
        _client({}).trade(pair="btc_idr", side="buy", order_type="limit",
                          price=1, coin_amount=1, time_in_force="IOC")


def test_tiny_quantities_never_use_scientific_notation():
    cap: dict = {}
    _client(cap).trade(pair="pepe_idr", side="sell", order_type="limit",
                       price=0.000001, coin_amount=1e-08)
    assert "e" not in cap["params"]["quantity"].lower()
    assert "e" not in cap["params"]["price"].lower()


def test_get_info_maps_v2_balances_to_the_v1_shape():
    resp = {"canTrade": True, "canWithdraw": False, "accountType": "individual", "uid": 1,
            "balances": [{"asset": "IDR", "free": "500000", "locked": "0"},
                         {"asset": "BTC", "free": "0.5", "locked": "0.1"}]}
    avail, held = _client({}, resp).balances()
    assert avail["idr"] == 500_000 and avail["btc"] == 0.5
    assert held["btc"] == 0.1


def test_negative_code_raises_even_on_http_200():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": -2010, "msg": "Insufficient balance"})

    c = IndodaxV2Client("K", SECRET, client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(IndodaxError) as e:
        c.get_info()
    assert "Insufficient balance" in str(e.value)
