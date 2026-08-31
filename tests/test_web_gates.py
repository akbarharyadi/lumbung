"""Endpoint-level tests for who is allowed through which gate.

This proves the *wiring* -- that the endpoints actually depend on the gates.
That is a different failure from a broken gate: a perfect check that no route
consults protects nothing, and its own unit tests would still be green.

Two gates remain in front of the origin. `auth` wants the bearer token, or a
signed Cloudflare Access identity in its place. `writable` wants that plus a
deployment that is not read-only. The Telegram login code that used to sit
between them is gone; Access, with Google sign-in and a one-time PIN, is what
now stands between someone holding the link and someone getting in.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from lumbung.web.server import create_app

TOKEN = "test-token"
H = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """A client whose data directory is NOT the real one.

    `create_app` calls `load_config()` itself, so a plain TestClient writes into
    `data/` -- and a chat test then queues questions nobody asked into the live
    ask-queue, which the running app announces. This has bitten before.
    """
    from lumbung.config import load_config
    from lumbung.journal import Journal

    cfg = load_config()
    cfg.paths.db = str(tmp_path / "gates.db")
    cfg.paths.halt_file = str(tmp_path / "HALT")
    monkeypatch.setattr("lumbung.web.server.load_config", lambda: cfg)
    Journal(cfg.db_path)

    def build(*, readonly: bool):
        return TestClient(create_app(token=TOKEN, readonly=readonly))

    return build

# Everything that exposes the balance sheet or accepts a change.
PROTECTED = ["/api/summary", "/api/positions", "/api/expenses", "/api/actions",
             "/api/settings"]


# -- the token gate ----------------------------------------------------------
@pytest.mark.parametrize("path", PROTECTED)
def test_the_token_opens_every_protected_route(path):
    c = TestClient(create_app(token=TOKEN, readonly=True))
    assert c.get(path, headers=H).status_code == 200, path


@pytest.mark.parametrize("path", PROTECTED)
def test_a_bad_token_is_refused_everywhere(path):
    """One route left ungated is the whole gate."""
    c = TestClient(create_app(token=TOKEN, readonly=True))
    r = c.get(path, headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401, path


@pytest.mark.parametrize("path", PROTECTED)
def test_no_token_at_all_is_refused(path):
    c = TestClient(create_app(token=TOKEN, readonly=True))
    assert c.get(path).status_code == 401, path


# -- read-only is about the exchange, not about your own notes ---------------
def test_readonly_blocks_trading_controls():
    c = TestClient(create_app(token=TOKEN, readonly=True))
    assert c.post("/api/flat", headers=H).status_code == 403
    assert c.post("/api/pause", headers=H).status_code == 403


@pytest.mark.parametrize("cmd", ["/kill", "/flat", "/pause", "/resume"])
def test_readonly_blocks_the_controls_in_chat_too(isolated, tmp_path, cmd):
    """The chat is a second door to the same levers.

    Hiding a button while leaving `/kill` answerable would be a read-only
    deployment that can still flatten the account.
    """
    c = isolated(readonly=True)
    r = c.post("/api/chat", headers=H, json={"text": cmd})
    assert r.status_code == 200
    assert r.json()["queued"] is True, f"{cmd} was executed on a read-only deployment"
    assert not (tmp_path / "HALT").exists(), "/kill wrote the halt file anyway"


def test_the_controls_work_on_a_writable_deployment(isolated):
    c = isolated(readonly=False)
    r = c.post("/api/chat", headers=H, json={"text": "/pause"})
    assert r.json()["queued"] is False
    assert "Paused" in r.json()["reply"]


def test_a_writable_deployment_still_answers_the_read_only_commands(isolated):
    c = isolated(readonly=False)
    r = c.post("/api/chat", headers=H, json={"text": "/pnl"})
    assert r.json()["queued"] is False
    assert "Realized P&L" in r.json()["reply"]


# -- signing out -------------------------------------------------------------
def test_there_is_no_server_side_session_to_forge():
    """Sessions live in Cloudflare now, not here.

    A session endpoint left behind after the code that issued it was removed
    would be an unauthenticated way in, so assert it is actually gone rather
    than trusting that nothing calls it.
    """
    c = TestClient(create_app(token=TOKEN, readonly=True))
    for path in ("/api/2fa/status", "/api/2fa/request", "/api/2fa/verify",
                 "/api/2fa/logout"):
        assert c.get(path, headers=H).status_code == 404, path
        assert c.post(path, headers=H, json={}).status_code == 404, path


# -- secrets never travel outward -------------------------------------------
def test_settings_never_returns_secret_values():
    c = TestClient(create_app(token=TOKEN, readonly=True))
    secrets = c.get("/api/settings", headers=H).json()["secrets"]
    for key, meta in secrets.items():
        if meta["secret"]:
            assert meta["value"] == "", f"{key} leaked a value"


def test_settings_rejects_keys_outside_the_allowlist():
    c = TestClient(create_app(token=TOKEN, readonly=True))
    r = c.post("/api/settings", headers=H,
               json={"fields": {"capital.sleeve_idr": 999_000_000}})
    assert r.status_code == 400
    assert "not editable" in r.json()["error"]


def test_secret_writes_reject_keys_outside_the_allowlist():
    c = TestClient(create_app(token=TOKEN, readonly=True))
    r = c.post("/api/settings/secrets", headers=H, json={"secrets": {"TA_MODE": "live"}})
    assert r.status_code == 400
    assert "not settable" in r.json()["error"]


# -- income must mean received, never projected ------------------------------
def test_passive_income_is_dividends_plus_interest():
    """Two bugs lived here at once and cancelled into a plausible number: the
    screener's projected income from stocks NOT owned was added in, and real
    savings/bond interest was left out. Wrong in both directions still looks
    reasonable, which is why it survived.
    """
    c = TestClient(create_app(token=TOKEN, readonly=True))
    d = c.get("/api/summary", headers=H).json()

    assert "passive_monthly" in d, "field must name what it contains"
    assert "dividend_monthly" not in d, "the misleading name must be gone"
    assert d["passive_monthly"] == pytest.approx(
        d["stock_dividends"] + d["interest_monthly"]
    ), "total must be exactly what is received, with nothing projected added"


def test_passive_income_excludes_unowned_stocks():
    """Whatever the screener suggests buying must never inflate income."""
    c = TestClient(create_app(token=TOKEN, readonly=True))
    d = c.get("/api/summary", headers=H).json()
    # stock_dividends comes only from holdings.yaml; the screener cannot touch it.
    assert d["stock_dividends"] >= 0
    assert d["passive_monthly"] >= d["stock_dividends"]
