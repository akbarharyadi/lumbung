"""Editing holdings.yaml without destroying it.

The file is more comment than data -- 116 lines of them explaining why each
target is what it is. `yaml.safe_dump` round-trips the data and silently drops
every one, which turns "record a deposit" into "lose your notes".
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

# Buckets whose balance lives somewhere a command may move it.
MOVABLE = ("cash", "savings", "gold", "bonds", "crypto")


def _rt():
    from ruamel.yaml import YAML

    y = YAML()
    y.preserve_quotes = True
    # holdings.yaml is hand-written and hand-indented; keep it that way.
    y.width = 4096
    return y


def edit(path: str | Path, mutate: Callable[[dict], None]) -> None:
    """Apply `mutate` to the parsed document and write it back with comments."""
    p = Path(path)
    y = _rt()
    with p.open(encoding="utf-8") as fh:
        doc = y.load(fh)
    mutate(doc)
    with p.open("w", encoding="utf-8") as fh:
        y.dump(doc, fh)


def bucket_balance(doc: dict, bucket: str) -> float:
    if bucket == "cash":
        return float(doc.get("cash_idr", 0) or 0)
    for o in doc.get("other_assets") or []:
        if str(o.get("kind", "")).lower() == bucket:
            return float(o.get("value_idr", 0) or 0)
    return 0.0


def adjust(doc: dict, bucket: str, delta: float) -> float:
    """Move a bucket by `delta` and return the new balance.

    Raises ValueError if the move would take it below zero -- that almost always
    means the wrong bucket was named, and a clamped-to-zero balance is a lie
    that looks like an answer.
    """
    if bucket not in MOVABLE:
        raise ValueError(f"unknown bucket {bucket!r}; expected one of {', '.join(MOVABLE)}")
    current = bucket_balance(doc, bucket)
    new = current + delta
    if new < 0:
        raise ValueError(
            f"{bucket} holds Rp {current:,.0f}; that would take it to Rp {new:,.0f}"
        )
    if bucket == "cash":
        doc["cash_idr"] = new
        return new
    others = doc.get("other_assets") or []
    for o in others:
        if str(o.get("kind", "")).lower() == bucket:
            o["value_idr"] = new
            return new
    others.append({"name": bucket.title(), "kind": bucket, "value_idr": new})
    doc["other_assets"] = others
    return new
