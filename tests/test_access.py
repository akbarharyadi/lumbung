"""Tests for trusting Cloudflare Access identity.

Signed-identity auth fails in quiet ways. A verifier that skips the audience
check still lets the right person in, so the happy path passes and the hole
stays open until someone with a token for a *different* app walks through it.
These assert the refusals.
"""

from __future__ import annotations

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from lumbung.web.access import AccessAuth
from lumbung.web.server import create_app

TEAM = "test-team.cloudflareaccess.com"
AUD = "a" * 64
EMAIL = "akbar@example.com"
TOKEN = "test-token"
H = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture(scope="module")
def keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key, key.public_key()


def make_token(keypair, **over) -> str:
    private, _ = keypair
    now = int(time.time())
    claims = {
        "aud": AUD,
        "iss": f"https://{TEAM}",
        "email": EMAIL,
        "iat": now,
        "exp": now + 3600,
    }
    claims.update(over)
    for k, v in list(claims.items()):
        if v is None:
            del claims[k]
    return jwt.encode(claims, private, algorithm="RS256")


@pytest.fixture
def auth(keypair, monkeypatch):
    """AccessAuth wired to a local key instead of Cloudflare's endpoint."""
    _, public = keypair
    a = AccessAuth(TEAM, AUD, [EMAIL])

    class FakeJWK:
        def get_signing_key_from_jwt(self, _token):
            return type("K", (), {"key": public})()

    monkeypatch.setattr(a, "_client", lambda: FakeJWK())
    return a


# -- the happy path ---------------------------------------------------------
def test_valid_token_yields_the_email(auth, keypair):
    assert auth.email_for(make_token(keypair)) == EMAIL


def test_email_comparison_is_case_insensitive(auth, keypair):
    """Identity providers are inconsistent about case; the same person must not
    become a different one because of it."""
    assert auth.email_for(make_token(keypair, email="AkBar@Example.COM")) == EMAIL


# -- the refusals that matter ------------------------------------------------
def test_token_for_another_application_is_rejected(auth, keypair):
    """The check people skip. A JWT signed for a different app in the same
    account is otherwise perfectly valid."""
    assert auth.email_for(make_token(keypair, aud="b" * 64)) is None


def test_token_from_another_team_is_rejected(auth, keypair):
    assert auth.email_for(make_token(keypair, iss="https://someone-else.cloudflareaccess.com")) is None


def test_expired_token_is_rejected(auth, keypair):
    now = int(time.time())
    assert auth.email_for(make_token(keypair, iat=now - 7200, exp=now - 3600)) is None


def test_token_signed_by_a_different_key_is_rejected(auth):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = int(time.time())
    forged = jwt.encode(
        {"aud": AUD, "iss": f"https://{TEAM}", "email": EMAIL,
         "iat": now, "exp": now + 3600},
        other, algorithm="RS256",
    )
    assert auth.email_for(forged) is None


def test_unsigned_token_is_rejected(auth):
    """`alg: none` is the oldest JWT attack and must not work."""
    now = int(time.time())
    none_token = jwt.encode(
        {"aud": AUD, "iss": f"https://{TEAM}", "email": EMAIL,
         "iat": now, "exp": now + 3600},
        key="", algorithm="none",
    )
    assert auth.email_for(none_token) is None


def test_email_outside_the_allowlist_is_rejected(auth, keypair):
    """Access let them in; we do not. Two lists have to agree."""
    assert auth.email_for(make_token(keypair, email="stranger@example.com")) is None


def test_garbage_and_empty_input_never_raise(auth):
    for bad in ("", "not-a-jwt", "a.b.c", "..", "null"):
        assert auth.email_for(bad) is None


def test_missing_claims_are_rejected(auth, keypair):
    assert auth.email_for(make_token(keypair, exp=None)) is None
    assert auth.email_for(make_token(keypair, email=None)) is None


# -- team domain normalisation ----------------------------------------------
def test_bare_team_name_is_expanded():
    """Typing the bare team name is the obvious mistake; it should just work."""
    a = AccessAuth("your-team", AUD, [])
    assert a.team_domain == "your-team.cloudflareaccess.com"
    assert a.issuer == "https://your-team.cloudflareaccess.com"


def test_https_prefix_is_tolerated():
    a = AccessAuth("https://your-team.cloudflareaccess.com/", AUD, [])
    assert a.team_domain == "your-team.cloudflareaccess.com"


# -- endpoint wiring ---------------------------------------------------------
def test_access_identity_replaces_the_token(auth, keypair):
    """The whole point: no bearer token, and still in."""
    c = TestClient(create_app(token=TOKEN, readonly=True, access=auth))
    r = c.get("/api/actions",
              headers={"Cf-Access-Jwt-Assertion": make_token(keypair)})
    assert r.status_code == 200


def test_forged_email_header_alone_is_not_enough(auth):
    """`Cf-Access-Authenticated-User-Email` is a plain string. Anything that can
    reach the origin can set it, so it must count for nothing."""
    c = TestClient(create_app(token=TOKEN, readonly=True, access=auth))
    r = c.get("/api/actions",
              headers={"Cf-Access-Authenticated-User-Email": EMAIL})
    assert r.status_code == 401


def test_token_still_works_when_access_is_configured(auth):
    """Local access over 127.0.0.1 has no Access header and must not be locked out."""
    c = TestClient(create_app(token=TOKEN, readonly=True, access=auth))
    assert c.get("/api/actions", headers=H).status_code == 200


def test_whoami_reports_the_verified_email(auth, keypair):
    c = TestClient(create_app(token=TOKEN, readonly=True, access=auth))
    body = c.get("/api/whoami",
                 headers={"Cf-Access-Jwt-Assertion": make_token(keypair)}).json()
    assert body == {"access": True, "email": EMAIL}


def test_whoami_reports_no_email_for_token_auth(auth):
    c = TestClient(create_app(token=TOKEN, readonly=True, access=auth))
    assert c.get("/api/whoami", headers=H).json() == {"access": True, "email": ""}
