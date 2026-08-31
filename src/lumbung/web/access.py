"""Trust Cloudflare Access's identity instead of asking for a token.

Access has already authenticated the visitor by the time the request reaches
this process -- Google sign-in, or a one-time PIN -- and it forwards proof in the
`Cf-Access-Jwt-Assertion` header. Reading that is strictly better than a shared
bearer token: it names *who* is here rather than *what secret they hold*, it
cannot be copied out of a bookmark, and it expires on Cloudflare's schedule
without anything here having to track it.

The one thing that must not be done casually is trusting it.

* **The signature is verified**, against Cloudflare's published keys for this
  team. The convenient-looking `Cf-Access-Authenticated-User-Email` header is
  *not* used, because it is a plain string: anything that can reach the origin
  can set it and become anyone. The signed assertion is the only honest source.
* **The audience is checked.** A JWT signed for a *different* application in the
  same Cloudflare account is otherwise perfectly valid, and would let someone
  with access to any other app walk into this one. `aud` is what stops that, and
  it is the check people most often skip.
* **The email is still checked against an allowlist here.** Access has its own
  policy, but a misconfiguration there should not silently become full access to
  a trading dashboard. Two independent lists have to agree.

Keys are cached, because fetching them per request would put a network round
trip in front of every page load and make Cloudflare's availability our own.
"""

from __future__ import annotations

import logging
import threading
import time

import httpx
import jwt
from jwt import PyJWKClient

log = logging.getLogger(__name__)

CERTS_TTL = 3600.0        # keys rotate rarely; an hour is Cloudflare's own guidance
CLOCK_SKEW = 30           # seconds of leeway on exp/iat


class AccessAuth:
    """Verifies Cloudflare Access JWTs for one application."""

    def __init__(self, team_domain: str, aud: str, emails: list[str]) -> None:
        # Accept "your-team" or the full hostname; typing the bare team
        # name is the obvious mistake and it produces a confusing 404 later.
        team = team_domain.strip().rstrip("/")
        team = team.removeprefix("https://").removeprefix("http://")
        if not team.endswith(".cloudflareaccess.com"):
            team = f"{team}.cloudflareaccess.com"
        self.team_domain = team
        self.aud = aud.strip()
        # Compared casefolded: identity providers are inconsistent about case,
        # and "Akbar@..." must not be a different person from "akbar@...".
        self.emails = {e.strip().casefold() for e in emails if e.strip()}
        self.issuer = f"https://{self.team_domain}"
        self.certs_url = f"{self.issuer}/cdn-cgi/access/certs"
        self._jwk: PyJWKClient | None = None
        self._jwk_at = 0.0
        self._lock = threading.Lock()

    # -- keys ---------------------------------------------------------------
    def _client(self) -> PyJWKClient:
        now = time.time()
        with self._lock:
            if self._jwk is None or now - self._jwk_at > CERTS_TTL:
                self._jwk = PyJWKClient(self.certs_url, cache_keys=True)
                self._jwk_at = now
            return self._jwk

    def preflight(self) -> tuple[bool, str]:
        """Check the team domain resolves and serves keys, before serving traffic.

        Worth doing at startup: a typo here otherwise surfaces as "nobody can log
        in", at the moment you are trying to log in.
        """
        try:
            r = httpx.get(self.certs_url, timeout=10.0)
            r.raise_for_status()
            keys = r.json().get("keys", [])
        except Exception as exc:  # noqa: BLE001
            return False, f"cannot reach {self.certs_url}: {exc}"
        if not keys:
            return False, f"{self.certs_url} returned no keys"
        return True, f"{len(keys)} signing key(s) from {self.team_domain}"

    # -- verification -------------------------------------------------------
    def email_for(self, token: str) -> str | None:
        """Return the verified email, or None. Never raises on bad input."""
        if not token:
            return None
        try:
            key = self._client().get_signing_key_from_jwt(token).key
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256", "ES256"],
                audience=self.aud,
                issuer=self.issuer,
                leeway=CLOCK_SKEW,
                options={"require": ["exp", "iat", "aud", "iss"]},
            )
        except Exception as exc:  # noqa: BLE001 -- any failure is simply "no"
            log.debug("access jwt rejected: %s", exc)
            return None

        email = str(claims.get("email", "")).casefold()
        if not email:
            return None
        if self.emails and email not in self.emails:
            # Logged at warning: Access let them through, we did not. That gap is
            # either a policy mistake or someone probing, and both deserve a line.
            log.warning("access jwt for %s is not on the allowlist", email)
            return None
        return email
