"""Public IP tracking for the Indodax API whitelist.

Indodax Trade API V2 binds a key to one IP. Home connections get a *dynamic*
public IP, so the whitelist silently goes stale after a router reboot or a lease
renewal, and every signed call starts failing with what looks like a bad key.

This module detects that specific situation and says so plainly, rather than
letting you debug a "broken" API key that is actually fine.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

# Queried over forced IPv4: the whitelist holds an IPv4 address, so that is the
# one that must be checked, even on a dual-stack connection.
SERVICES = (
    "https://api.ipify.org",
    "https://ipv4.icanhazip.com",
    "https://checkip.amazonaws.com",
)


@dataclass
class IPStatus:
    current: str | None
    whitelisted: str | None
    changed_at: int | None = None

    @property
    def ok(self) -> bool:
        return bool(self.current and self.whitelisted and self.current == self.whitelisted)

    @property
    def unknown(self) -> bool:
        return self.current is None

    @property
    def not_configured(self) -> bool:
        return not self.whitelisted


def public_ipv4(*, timeout: float = 8.0) -> str | None:
    """Best-effort public IPv4, forcing an IPv4 socket."""
    transport = httpx.HTTPTransport(local_address="0.0.0.0")
    with httpx.Client(timeout=timeout, transport=transport) as c:
        for url in SERVICES:
            try:
                ip = c.get(url).text.strip()
                # Reject an IPv6 answer -- the whitelist cannot use it.
                if ip and ":" not in ip and ip.count(".") == 3:
                    return ip
            except Exception:  # noqa: BLE001
                continue
    return None


def check(state_path: str | Path, whitelisted: str | None) -> IPStatus:
    """Compare the live IP against the one recorded in the key's whitelist."""
    current = public_ipv4()
    st = IPStatus(current=current, whitelisted=(whitelisted or "").strip() or None)

    p = Path(state_path)
    prev = {}
    if p.exists():
        try:
            prev = json.loads(p.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            prev = {}

    if current and prev.get("last_ip") and prev["last_ip"] != current:
        st.changed_at = int(time.time())

    if current:
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(
                json.dumps({"last_ip": current, "seen_at": int(time.time())}),
                encoding="utf-8",
            )
        except OSError:
            pass
    return st
