"""Editable settings, and the onboarding that fills them in the first time.

Two stores, deliberately kept apart:

* **holdings.yaml** -- your salary, spending, targets, allocation. Edited through
  a round-trip YAML writer so the comments survive. Those comments carry the
  reasoning behind the numbers ("savings and cash are separate: savings earns"),
  and a settings screen that silently strips them makes the file worse every
  time it is used.
* **.env** -- API keys and tokens. Write-only from the outside: values go in,
  nothing ever comes back out. The API reports whether a secret is *set*, never
  what it is, so a compromised dashboard session cannot read the Indodax key
  back out of the machine it is running on.

Both are allowlisted by key. A settings endpoint that writes whatever it is
handed is an arbitrary-config-write endpoint, and config drives order sizing.
"""

from __future__ import annotations

import io
import logging
import os
import re
import stat
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from ..config import PROJECT_ROOT

log = logging.getLogger(__name__)

_yaml = YAML()
_yaml.preserve_quotes = True
_yaml.indent(mapping=2, sequence=4, offset=2)


# -- what the UI is allowed to change ---------------------------------------
# path -> (label, kind, minimum, maximum). Anything absent is not editable, and
# is not editable by accident either: unknown keys are rejected, not ignored.
# Grouping for the settings screen. A flat list of thirteen inputs makes
# "monthly income" and "target: crypto" look equally consequential, which they
# are not -- one drives every projection, the other nudges a bar chart.
GROUPS = [
    ("Money in and out", "What arrives and what leaves each month. Everything "
                         "else is derived from these two.",
     ["cashflow.income_monthly", "cashflow.spending_monthly", "cashflow.payday_day"]),
    ("What you are aiming for", "",
     ["goals.monthly_income_target", "goals.subscription_idr"]),
    ("Safety net", "How many months of spending must stay liquid. Cash, savings "
                   "and gold all count.",
     ["emergency_fund_months", "cash_idr"]),
    ("Target allocation", "Shares of net worth. These must add up to 100%.",
     ["target_allocation.stocks", "target_allocation.bonds",
      "target_allocation.gold", "target_allocation.savings",
      "target_allocation.cash", "target_allocation.crypto"]),
]

FIELDS: dict[str, tuple[str, str, float, float]] = {
    "cashflow.income_monthly": ("Monthly income", "idr", 0, 10_000_000_000),
    "cashflow.spending_monthly": ("Monthly spending", "idr", 0, 10_000_000_000),
    "cashflow.payday_day": ("Payday (day of month, 0 = off)", "int", 0, 31),
    "goals.monthly_income_target": ("Passive income target", "idr", 0, 10_000_000_000),
    "goals.subscription_idr": ("Subscription to cover", "idr", 0, 100_000_000),
    "emergency_fund_months": ("Emergency fund (months)", "int", 0, 60),
    "cash_idr": ("Cash on hand", "idr", 0, 10_000_000_000),
    "target_allocation.stocks": ("Target: stocks", "pct", 0, 1),
    "target_allocation.bonds": ("Target: bonds", "pct", 0, 1),
    "target_allocation.gold": ("Target: gold", "pct", 0, 1),
    "target_allocation.savings": ("Target: savings", "pct", 0, 1),
    "target_allocation.cash": ("Target: cash", "pct", 0, 1),
    "target_allocation.crypto": ("Target: crypto", "pct", 0, 1),
}

# .env keys the onboarding screen may set. Note what is NOT here: nothing that
# would let a browser session point the engine at a different exchange or turn
# live trading on. TA_MODE stays a deliberate, local decision.
ENV_FIELDS: dict[str, tuple[str, bool]] = {
    "INDODAX_KEY": ("Indodax API key", True),
    "INDODAX_SECRET": ("Indodax API secret", True),
    "DASHBOARD_TOKEN": ("Dashboard token", True),
}


def _number(value: Any) -> float:
    """Parse a rupiah figure written the way people actually write it.

    Accepts "7.401", "7,401" and "7401" alike -- Indonesian and English
    thousands separators both turn up, and rejecting a paste is a worse
    experience than accepting both.

    Also accepts "5jt" and "500rb", because that is how the amounts are actually
    typed -- in the chat and in the settings box alike. One parser understanding
    every spelling beats three callers each accepting a slightly different set.

    The word boundary matters. Without it "1.2345" loses its decimal point and
    becomes twelve thousand, and an average price silently off by 1000x is the
    kind of error that only surfaces as a nonsensical P&L days later.

    A trailing "%" is stripped, because rates are typed the way they are read
    ("6.5%"). Anything that is not a number raises SettingsError with a message
    meant for a person -- the raw float() text ("could not convert string to
    float") reads like a stack trace, not an answer.
    """
    t = str(value).strip().lower().replace(" ", "")
    mult = 1
    if t.endswith("jt"):
        mult, t = 1_000_000, t[:-2]
    elif t.endswith("rb"):
        mult, t = 1_000, t[:-2]
    if t.endswith("%"):
        t = t[:-1]
    cleaned = re.sub(r"[.,](?=\d{3}\b)", "", t)
    if mult > 1:
        # "1,5jt" and "1.5jt" both mean one and a half million: with a
        # multiplier attached the separator is a decimal point, not thousands.
        cleaned = cleaned.replace(",", ".")
    try:
        return float(cleaned) * mult
    except ValueError:
        raise SettingsError(f"could not read {value!r} as a number") from None


class SettingsError(ValueError):
    """Rejected input. The message is safe to show the user."""


def _holdings_path() -> Path:
    return PROJECT_ROOT / "config" / "holdings.yaml"


def _env_path() -> Path:
    return PROJECT_ROOT / ".env"


def _get(doc: Any, dotted: str) -> Any:
    node = doc
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _set(doc: Any, dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    node = doc
    for part in parts[:-1]:
        if part not in node or not isinstance(node[part], dict):
            node[part] = {}
        node = node[part]
    node[parts[-1]] = value


def read_settings() -> dict[str, Any]:
    """Current values plus the metadata the UI needs to render inputs."""
    path = _holdings_path()
    with open(path, encoding="utf-8") as fh:
        doc = _yaml.load(fh) or {}

    out: dict[str, Any] = {}
    for key, (label, kind, lo, hi) in FIELDS.items():
        raw = _get(doc, key)
        out[key] = {
            "label": label,
            "kind": kind,
            "min": lo,
            "max": hi,
            "value": float(raw) if isinstance(raw, (int, float)) else None,
        }
    return out


def _coerce(key: str, value: Any) -> float | int:
    label, kind, lo, hi = FIELDS[key]
    if value is None or value == "":
        raise SettingsError(f"{label} cannot be blank")
    try:
        # Accept "17.000.000" and "17,000,000" -- Indonesian and English
        # separators both appear in practice, and a rejected paste is a worse
        # experience than accepting both.
        num = _number(value)
    except ValueError:
        raise SettingsError(f"{label}: not a number") from None
    if num < lo or num > hi:
        raise SettingsError(f"{label}: must be between {lo:g} and {hi:g}")
    return int(num) if kind == "int" else num


def write_settings(updates: dict[str, Any]) -> dict[str, Any]:
    """Validate everything, then write once. Never a partial application."""
    unknown = sorted(set(updates) - set(FIELDS))
    if unknown:
        raise SettingsError(f"not editable: {', '.join(unknown)}")

    clean = {k: _coerce(k, v) for k, v in updates.items()}

    path = _holdings_path()
    with open(path, encoding="utf-8") as fh:
        doc = _yaml.load(fh) or {}

    # Allocation is checked against the merged result, not the submitted subset:
    # editing one bucket in isolation still has to leave a coherent whole.
    merged = {
        k: clean.get(k, _get(doc, k) or 0.0)
        for k in FIELDS
        if k.startswith("target_allocation.")
    }
    total = sum(float(v) for v in merged.values())
    if total > 1.02:
        raise SettingsError(
            f"target allocation adds up to {total * 100:.0f}%. It cannot exceed 100%."
        )

    for key, value in clean.items():
        _set(doc, key, value)

    # Render to a buffer first. A failure mid-dump would otherwise leave a
    # truncated holdings.yaml, and that file is the balance sheet.
    buf = io.StringIO()
    _yaml.dump(doc, buf)
    text = buf.getvalue()
    if len(text) < 100:
        raise SettingsError("refusing to write a suspiciously small config")

    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    log.info("settings updated: %s", ", ".join(sorted(clean)))
    return {"written": sorted(clean), "allocation_total": total}


# -- holdings ---------------------------------------------------------------
# Adding a stock is not a settings tweak: it changes what gets priced, what the
# concentration rule measures, and which tickers the morning research asks
# about. Kept as its own writer with its own validation rather than squeezed
# into the flat FIELDS allowlist.
MAX_HOLDINGS = 30


def read_holdings() -> list[dict[str, Any]]:
    path = _holdings_path()
    with open(path, encoding="utf-8") as fh:
        doc = _yaml.load(fh) or {}
    out = []
    for h in doc.get("stocks", []) or []:
        out.append({
            "ticker": str(h.get("ticker", "")),
            "lots": int(h.get("lots", 0) or 0),
            "avg_price": float(h.get("avg_price", 0) or 0),
            "take_profit": float(h.get("take_profit", 0) or 0),
            "cut_loss": float(h.get("cut_loss", 0) or 0),
            "note": str(h.get("note", "")),
        })
    return out


def _clean_ticker(raw: str) -> str:
    """Normalise to the Yahoo Finance form the price fetcher needs.

    People type "BBRI" because that is what Stockbit shows. The suffix is an
    implementation detail of where prices come from, so it is added here rather
    than demanded of whoever is typing.
    """
    t = str(raw or "").strip().upper().replace(" ", "")
    if not t:
        raise SettingsError("ticker cannot be blank")
    if not t.replace(".", "").isalnum():
        raise SettingsError(f"{t!r} is not a valid ticker")
    return t if t.endswith(".JK") else f"{t}.JK"


def write_holding(entry: dict[str, Any]) -> dict[str, Any]:
    """Add or update one stock. `lots: 0` removes it."""
    ticker = _clean_ticker(entry.get("ticker", ""))
    try:
        lots = int(float(str(entry.get("lots", 0)).replace(",", "")))
        avg = _number(entry.get("avg_price", 0) or 0)
    except ValueError:
        raise SettingsError("lots and average price must be numbers") from None

    if lots < 0:
        raise SettingsError("lots cannot be negative")
    if lots > 0 and avg <= 0:
        raise SettingsError("average price must be above zero")

    def _opt(key: str) -> float:
        v = entry.get(key)
        if v in (None, "", 0, "0"):
            return 0.0
        try:
            return _number(v)
        except ValueError:
            raise SettingsError(f"{key} must be a number") from None

    tp, cl = _opt("take_profit"), _opt("cut_loss")
    # Caught here rather than at read time: levels the wrong way round would
    # silently mean "already hit" on both, and the report would look broken
    # rather than misconfigured.
    if tp and cl and cl >= tp:
        raise SettingsError("cut loss must be below take profit")
    if lots > 0 and avg > 0:
        if tp and tp <= avg:
            raise SettingsError("take profit must be above your average price")
        if cl and cl >= avg:
            raise SettingsError("cut loss must be below your average price")

    path = _holdings_path()
    with open(path, encoding="utf-8") as fh:
        doc = _yaml.load(fh) or {}
    stocks = doc.get("stocks") or []

    idx = next(
        (i for i, h in enumerate(stocks)
         if str(h.get("ticker", "")).upper() == ticker), None
    )

    if lots == 0:
        if idx is None:
            raise SettingsError(f"{ticker} is not in your holdings")
        stocks.pop(idx)
        action = "removed"
    else:
        record = {"ticker": ticker, "lots": lots, "avg_price": avg}
        if tp:
            record["take_profit"] = tp
        if cl:
            record["cut_loss"] = cl
        if entry.get("note"):
            record["note"] = str(entry["note"])[:120]
        if idx is None:
            if len(stocks) >= MAX_HOLDINGS:
                raise SettingsError(f"at most {MAX_HOLDINGS} holdings")
            stocks.append(record)
            action = "added"
        else:
            stocks[idx] = record
            action = "updated"

    doc["stocks"] = stocks
    buf = io.StringIO()
    _yaml.dump(doc, buf)
    text = buf.getvalue()
    if len(text) < 100:
        raise SettingsError("refusing to write a suspiciously small config")
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    log.info("holding %s: %s", action, ticker)
    return {"action": action, "ticker": ticker, "holdings": len(stocks)}


# -- other assets -------------------------------------------------------------
ASSET_KINDS = ("gold", "savings", "bonds", "crypto", "other")
MAX_ASSETS = 40


def read_assets() -> list[dict[str, Any]]:
    with open(_holdings_path(), encoding="utf-8") as fh:
        doc = _yaml.load(fh) or {}
    return [
        {
            "name": str(a.get("name", "")),
            "kind": str(a.get("kind", "other")),
            "value_idr": float(a.get("value_idr", 0) or 0),
            "rate": float(a.get("rate", 0) or 0),
            "note": str(a.get("note", "")),
        }
        for a in doc.get("other_assets", []) or []
    ]


def _find_asset(assets: list, needle: str) -> int | None:
    """Match on a prefix, so "SR025" finds "SR025 (Sukuk Ritel)"."""
    n = needle.strip().casefold()
    exact = [i for i, a in enumerate(assets)
             if str(a.get("name", "")).casefold() == n]
    if exact:
        return exact[0]
    partial = [i for i, a in enumerate(assets)
               if n in str(a.get("name", "")).casefold()]
    if len(partial) == 1:
        return partial[0]
    if len(partial) > 1:
        names = ", ".join(str(assets[i].get("name")) for i in partial)
        raise SettingsError(f"{needle!r} matches more than one: {names}")
    return None


def _write_doc(doc) -> None:
    path = _holdings_path()
    buf = io.StringIO()
    _yaml.dump(doc, buf)
    text = buf.getvalue()
    if len(text) < 100:
        raise SettingsError("refusing to write a suspiciously small config")
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def write_asset(name: str, kind: str = "", value: Any = None,
                rate: Any = None, note: str = "") -> dict[str, Any]:
    """Add, update or remove one entry in `other_assets`.

    Setting the value to 0 removes it. Note that this is a *revaluation*, not a
    sale: gold falling in price and gold being sold look identical here, and they
    are not the same event -- one changes what you own, the other moves money
    into your account. Use `sell_asset` when cash actually arrived.
    """
    name = str(name or "").strip()
    if not name:
        raise SettingsError("name cannot be blank")

    with open(_holdings_path(), encoding="utf-8") as fh:
        doc = _yaml.load(fh) or {}
    assets = doc.get("other_assets") or []
    idx = _find_asset(assets, name)

    if value is None and idx is None:
        raise SettingsError(f"{name!r} is not in your assets, so it needs a value")

    if idx is None:
        if kind and kind not in ASSET_KINDS:
            raise SettingsError(f"kind must be one of: {', '.join(ASSET_KINDS)}")
        if len(assets) >= MAX_ASSETS:
            raise SettingsError(f"at most {MAX_ASSETS} assets")
        val = _number(value)
        if val <= 0:
            raise SettingsError("a new asset needs a value above zero")
        entry = {"name": name, "kind": kind or "other", "value_idr": val}
        if rate not in (None, ""):
            entry["rate"] = _number(rate) / (100 if _number(rate) > 1 else 1)
        if note:
            entry["note"] = str(note)[:120]
        assets.append(entry)
        action = "added"
    else:
        entry = assets[idx]
        if value is not None:
            val = _number(value)
            if val <= 0:
                assets.pop(idx)
                doc["other_assets"] = assets
                _write_doc(doc)
                log.info("asset removed: %s", entry.get("name"))
                return {"action": "removed", "name": str(entry.get("name")),
                        "assets": len(assets)}
            entry["value_idr"] = val
        if kind:
            if kind not in ASSET_KINDS:
                raise SettingsError(f"kind must be one of: {', '.join(ASSET_KINDS)}")
            entry["kind"] = kind
        if rate not in (None, ""):
            r = _number(rate)
            entry["rate"] = r / 100 if r > 1 else r
        if note:
            entry["note"] = str(note)[:120]
        action = "updated"

    doc["other_assets"] = assets
    _write_doc(doc)
    log.info("asset %s: %s", action, name)
    return {"action": action, "name": str(entry.get("name")), "assets": len(assets)}


def rename_asset(old: str, new: str) -> dict[str, Any]:
    """Rename in place, keeping value, kind and rate."""
    new = str(new or "").strip()
    if not new:
        raise SettingsError("new name cannot be blank")
    with open(_holdings_path(), encoding="utf-8") as fh:
        doc = _yaml.load(fh) or {}
    assets = doc.get("other_assets") or []
    idx = _find_asset(assets, old)
    if idx is None:
        raise SettingsError(f"{old!r} is not in your assets")
    was = str(assets[idx].get("name"))
    assets[idx]["name"] = new
    doc["other_assets"] = assets
    _write_doc(doc)
    log.info("asset renamed: %s -> %s", was, new)
    return {"action": "renamed", "was": was, "name": new}


def sell_asset(name: str, amount: Any = None) -> dict[str, Any]:
    """Sell some or all of an asset and move the proceeds into cash.

    Kept separate from `write_asset` on purpose. Lowering a value is ambiguous --
    gold falling in price and gold being sold produce the same new number, and
    only one of them puts money in your account. Saying "sell" says which.
    """
    with open(_holdings_path(), encoding="utf-8") as fh:
        doc = _yaml.load(fh) or {}
    assets = doc.get("other_assets") or []
    idx = _find_asset(assets, name)
    if idx is None:
        raise SettingsError(f"{name!r} is not in your assets")

    entry = assets[idx]
    held = float(entry.get("value_idr", 0) or 0)
    sold = held if amount in (None, "") else _number(amount)
    if sold <= 0:
        raise SettingsError("amount must be above zero")
    if sold > held:
        raise SettingsError(
            f"you hold Rp {held:,.0f} of {entry.get('name')}, cannot sell Rp {sold:,.0f}"
        )

    remaining = held - sold
    if remaining <= 0:
        assets.pop(idx)
    else:
        entry["value_idr"] = remaining
    doc["other_assets"] = assets

    cash_before = float(_get(doc, "cash_idr") or 0.0)
    _set(doc, "cash_idr", cash_before + sold)
    _write_doc(doc)
    log.info("sold %s of %s into cash", sold, name)
    return {
        "action": "sold", "name": str(entry.get("name")), "sold": sold,
        "remaining": remaining, "cash": cash_before + sold,
    }


def add_to_cash(amount: float) -> float:
    """Increase `cash_idr` by `amount` and return the new balance.

    Money that arrives lands in a bank account before it lands anywhere else, so
    recording income without moving cash leaves the balance sheet wrong until
    someone remembers to edit it by hand -- and nobody does. Deploying it later
    moves it out of cash into whichever bucket actually received it.

    Uses the same round-trip writer as the settings screen, so the comments in
    holdings.yaml survive.
    """
    path = _holdings_path()
    with open(path, encoding="utf-8") as fh:
        doc = _yaml.load(fh) or {}
    current = float(_get(doc, "cash_idr") or 0.0)
    # Negative amounts are spending. Floored at zero rather than allowed to go
    # negative: a negative cash balance is not a fact about the world, it means
    # something was paid from an account this file does not track, and a wrong
    # number that looks impossible is better than a wrong number that looks fine.
    new_total = max(0.0, current + float(amount))
    _set(doc, "cash_idr", new_total)

    buf = io.StringIO()
    _yaml.dump(doc, buf)
    text = buf.getvalue()
    if len(text) < 100:
        raise SettingsError("refusing to write a suspiciously small config")
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    log.info("cash_idr %s -> %s", current, new_total)
    return new_total


def sync_spending_from_actuals(conn, *, months: int = 3, min_months: int = 1,
                               max_change: float = 0.5) -> dict[str, Any]:
    """Rewrite `cashflow.spending_monthly` from what was actually spent.

    A typed budget drifts away from reality quietly, and every number built on it
    -- surplus, emergency target, years-to-goal -- drifts with it. The recorded
    average is the honest figure, so it becomes the stored one.

    Three guards, because this writes to the balance sheet unattended:

    * **Complete months only.** `spending_profile` already excludes the month in
      progress; a month that is ten days old would halve the average and inflate
      the surplus.
    * **`min_months`** before it will act at all. One logged month is a sample of
      one, and spending is lumpy -- a month with a wedding is not a trend.
    * **`max_change`** caps how far a single sync can move the figure. A month of
      forgotten logging looks exactly like a month of frugality, and should not
      be allowed to silently rewrite the plan.
    """
    from ..spending import spending_profile

    path = _holdings_path()
    with open(path, encoding="utf-8") as fh:
        doc = _yaml.load(fh) or {}
    current = float(_get(doc, "cashflow.spending_monthly") or 0.0)

    prof = spending_profile(conn, current, months=months)
    tracked = len(prof.tracked_months)
    avg = prof.average

    if tracked < min_months or avg <= 0:
        return {"changed": False, "reason": f"only {tracked} complete month(s) logged",
                "spending_monthly": current, "months_used": tracked}

    if current > 0:
        move = abs(avg - current) / current
        if move > max_change:
            return {
                "changed": False,
                "reason": (
                    f"actual average {avg:,.0f} differs from {current:,.0f} by "
                    f"{move * 100:.0f}% — too large to apply automatically, "
                    "check for unlogged months"
                ),
                "spending_monthly": current, "actual_average": avg,
                "months_used": tracked,
            }

    _set(doc, "cashflow.spending_monthly", round(avg))
    buf = io.StringIO()
    _yaml.dump(doc, buf)
    text = buf.getvalue()
    if len(text) < 100:
        raise SettingsError("refusing to write a suspiciously small config")
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    log.info("spending_monthly %s -> %s (%d months)", current, round(avg), tracked)
    return {"changed": True, "was": current, "spending_monthly": round(avg),
            "months_used": tracked}


# -- secrets ----------------------------------------------------------------
def env_status() -> dict[str, Any]:
    """Which secrets are set. Never their values."""
    from ..config import PLACEHOLDERS

    path = _env_path()
    present: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            present[k.strip()] = v.strip().strip("\"'")

    out = {}
    for key, (label, is_secret) in ENV_FIELDS.items():
        val = present.get(key, "")
        configured = bool(val) and val not in PLACEHOLDERS
        out[key] = {
            "label": label,
            "secret": is_secret,
            "configured": configured,
            # Non-secret values are safe to echo; secrets never are, not even
            # masked -- a partial key is still a head start.
            "value": "" if is_secret else val,
        }
    return out


def write_env(updates: dict[str, str]) -> dict[str, Any]:
    """Set .env keys in place, preserving comments and unrelated lines."""
    unknown = sorted(set(updates) - set(ENV_FIELDS))
    if unknown:
        raise SettingsError(f"not settable: {', '.join(unknown)}")

    clean = {k: str(v).strip() for k, v in updates.items() if str(v).strip()}
    if not clean:
        raise SettingsError("nothing to save")

    for key, value in clean.items():
        if "\n" in value or "\r" in value:
            raise SettingsError(f"{key}: value cannot contain a line break")

    path = _env_path()
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    remaining = dict(clean)
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in remaining:
            lines[i] = f"{key}={remaining.pop(key)}"
    for key, value in remaining.items():
        lines.append(f"{key}={value}")

    tmp = path.with_suffix(".env.tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    try:
        # Owner-only. Best effort: on Windows this is advisory rather than
        # enforced, but it costs nothing and matters if the tree is ever synced
        # to a POSIX box.
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass

    # Log the names only. Logging a value here would put an API secret into a
    # plaintext file that is not treated as a secret.
    log.info("secrets updated: %s", ", ".join(sorted(clean)))
    return {"written": sorted(clean), "restart_required": True}


# -- wishes (commitments that bind nothing) ------------------------------------
MAX_WISHES = 40


def read_wishes() -> list[dict[str, Any]]:
    """The considering list: commitments with kind=wish, priced wants that
    never bind the safety net."""
    with open(_holdings_path(), encoding="utf-8") as fh:
        doc = _yaml.load(fh) or {}
    return [
        {
            "name": str(c.get("name", "")),
            "amount_idr": float(c.get("amount_idr", 0) or 0),
            "note": str(c.get("note", "")),
        }
        for c in doc.get("commitments", []) or []
        if str(c.get("kind", "obligation")).lower() == "wish"
    ]


def _find_wish(commitments: list, needle: str) -> int | None:
    """Prefix match, over WISHES ONLY -- updating an obligation by accident
    would turn a binding promise into a daydream."""
    n = needle.strip().casefold()
    wish_idx = [i for i, c in enumerate(commitments)
                if str(c.get("kind", "obligation")).lower() == "wish"]
    exact = [i for i in wish_idx
             if str(commitments[i].get("name", "")).casefold() == n]
    if exact:
        return exact[0]
    partial = [i for i in wish_idx
               if n in str(commitments[i].get("name", "")).casefold()]
    if len(partial) == 1:
        return partial[0]
    if len(partial) > 1:
        names = ", ".join(str(commitments[i].get("name")) for i in partial)
        raise SettingsError(f"{needle!r} matches more than one: {names}")
    return None


def write_wish(name: str, amount: Any = None, note: str = "",
               remove: bool = False) -> dict[str, Any]:
    """Add, update or remove one wish in `commitments`.

    A wish is listed and priced, never binding (networth.py): it shows in
    CONSIDERING and the buy-date simulation, and it never shrinks the safety
    net. Promoting it to a real obligation is a deliberate edit elsewhere.
    """
    name = str(name or "").strip()
    if not name:
        raise SettingsError("name cannot be blank")

    with open(_holdings_path(), encoding="utf-8") as fh:
        doc = _yaml.load(fh) or {}
    commits = doc.get("commitments") or []
    idx = _find_wish(commits, name)

    if remove:
        if idx is None:
            raise SettingsError(f"{name!r} is not in your considering list")
        gone = commits.pop(idx)
        _write_doc(doc)
        log.info("wish removed: %s", gone.get("name"))
        n = sum(1 for c in commits
                if str(c.get("kind", "obligation")).lower() == "wish")
        return {"action": "removed", "name": str(gone.get("name")), "wishes": n}

    val = _number(amount)
    if val <= 0:
        raise SettingsError("a wish needs an amount above zero, e.g. 730rb")

    if idx is None:
        wishes = [c for c in commits
                  if str(c.get("kind", "obligation")).lower() == "wish"]
        if len(wishes) >= MAX_WISHES:
            raise SettingsError(f"at most {MAX_WISHES} wishes")
        entry = {"name": name, "amount_idr": val, "note": note, "kind": "wish"}
        commits.append(entry)
        action = "added"
    else:
        commits[idx]["amount_idr"] = val
        commits[idx]["kind"] = "wish"
        if note:
            commits[idx]["note"] = note
        action = "updated"

    doc["commitments"] = commits
    _write_doc(doc)
    log.info("wish %s: %s at %s", action, name, val)
    return {"action": action, "name": name, "amount_idr": val}
