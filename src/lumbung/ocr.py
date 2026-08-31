"""Receipt scanning via a local vision model.

Manual expense logging fails for one reason: friction. Photographing a receipt
is something you will actually do; typing amounts into a CLI is not. So this
turns a photo into a draft expense you confirm, rather than data you enter.

Talks to any **OpenAI-compatible** `/v1/chat/completions` endpoint, which covers
vLLM, Ollama, LM Studio and llama.cpp — so a self-hosted Qwen works the same as
a hosted model, with no data leaving your machine.

The model must be **vision-capable** (a `-VL` variant). A text-only model will
either error or hallucinate a plausible receipt, which is worse than failing, so
`extract_receipt` treats a response with no usable JSON as a failure rather than
guessing.

Nothing here is trusted: the output is a *draft* shown for confirmation before
anything is recorded, because a misread digit in an amount is silent corruption.
"""

from __future__ import annotations

import base64
import json
import logging
import mimetypes
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

CATEGORY_HINT = (
    "tech, food, transport, home, health, family, fun, fees, gift, other"
)

PROMPT = f"""Read this Indonesian receipt or payment screenshot and extract the purchase.

Return ONLY a JSON object, no markdown fence, no commentary:
{{"amount": <total paid as a number, no separators>,
  "merchant": "<shop or app name>",
  "date": "<YYYY-MM-DD, or null if not visible>",
  "category": "<one of: {CATEGORY_HINT}>",
  "method": "<cash, credit, debit, qris, transfer, or unknown>",
  "items": ["<up to 5 line items>"],
  "confidence": <0.0 to 1.0>}}

Rules:
- `amount` is the FINAL total actually paid, after discounts and including tax.
- Indonesian receipts write thousands as "150.000" meaning 150000. Never read a
  dot as a decimal point. "Rp 1.234.567" is 1234567.
- If the total is unreadable, set amount to null and confidence low. Do not guess.
- Reply with the JSON object only."""


@dataclass
class Receipt:
    amount: float | None
    merchant: str
    date: str | None
    category: str
    method: str
    items: list[str]
    confidence: float
    raw: str = ""

    @property
    def usable(self) -> bool:
        return self.amount is not None and self.amount > 0

    @property
    def needs_review(self) -> bool:
        """Low confidence, or an amount odd enough to be a misread."""
        return self.confidence < 0.7 or (self.amount or 0) <= 0


def encode_image(path: str | Path) -> tuple[str, str]:
    """(data-uri, mime) for an image file."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"no such image: {p}")
    mime = mimetypes.guess_type(p.name)[0] or "image/jpeg"
    if not mime.startswith("image/"):
        raise ValueError(f"{p.name} is not an image ({mime})")
    b64 = base64.b64encode(p.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{b64}", mime


def extract_receipt(
    image: str | Path,
    *,
    base_url: str,
    model: str,
    api_key: str = "",
    timeout: float = 180.0,
    extra_body: dict | None = None,
) -> Receipt | None:
    """Send an image to a local vision model and parse the draft expense."""
    data_uri, _ = encode_image(image)
    url = base_url.rstrip("/")
    if not url.endswith("/chat/completions"):
        url = url + ("/chat/completions" if url.endswith("/v1") else "/v1/chat/completions")

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        r = httpx.post(
            url,
            headers=headers,
            json={
                "model": model,
                "temperature": 0,
                "max_tokens": 600,
                # Qwen servers accept chat_template_kwargs here; disabling
                # thinking keeps the reply to the JSON we asked for.
                **(extra_body or {}),
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": PROMPT},
                            {"type": "image_url", "image_url": {"url": data_uri}},
                        ],
                    }
                ],
            },
            timeout=timeout,
        )
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"]
    except httpx.HTTPStatusError as exc:
        body = exc.response.text[:200]
        if "image" in body.lower() or "vision" in body.lower() or "content" in body.lower():
            log.error(
                "The model rejected the image. %s is probably text-only — "
                "a vision (-VL) model is required.", model
            )
        else:
            log.error("OCR request failed: HTTP %s %s", exc.response.status_code, body)
        return None
    except Exception as exc:  # noqa: BLE001
        log.error("OCR request failed: %s", exc)
        return None

    data = _json_from(content)
    if data is None:
        log.error("Model returned no usable JSON: %s", str(content)[:160])
        return None

    return Receipt(
        amount=_number(data.get("amount")),
        merchant=str(data.get("merchant") or "unknown")[:60],
        date=_valid_date(data.get("date")),
        category=str(data.get("category") or "other").lower()[:20],
        method=str(data.get("method") or "unknown").lower()[:12],
        items=[str(i)[:60] for i in (data.get("items") or [])][:5],
        confidence=float(data.get("confidence") or 0.0),
        raw=str(content)[:800],
    )


def _json_from(text: str) -> dict | None:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S).strip()
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except ValueError:
        pass
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else None
    except ValueError:
        return None


def _number(v) -> float | None:
    """Parse an amount, defending against Indonesian thousands separators.

    '150.000' means 150000 here, never 150.0 -- a model that hands back the
    string form must not silently become a thousand-times-too-small expense.
    """
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v) if v > 0 else None
    s = str(v).strip().replace("Rp", "").replace(" ", "")
    s = re.sub(r"[^0-9.,]", "", s)
    if not s:
        return None

    has_dot, has_comma = "." in s, "," in s
    if has_dot and has_comma:
        # Indonesian convention: dots group thousands, comma is the decimal.
        # "1.234,50" -> 1234.50
        s = s.replace(".", "").replace(",", ".")
    elif has_dot:
        # A trailing group of exactly three digits is a thousands separator,
        # so "150.000" is 150000 and never 150.0.
        parts = s.split(".")
        s = "".join(parts) if len(parts[-1]) == 3 else s
    elif has_comma:
        s = s.replace(",", ".")

    try:
        n = float(s)
    except ValueError:
        return None
    return n if n > 0 else None


def _valid_date(v) -> str | None:
    if not v:
        return None
    s = str(v)[:10]
    try:
        date.fromisoformat(s)
        return s
    except ValueError:
        return None


# ------------------------------------------------------------ connectivity
TINY_PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAAAgAAAAICAYAAADED76LAAAAHElEQVQoz2NgGAWjYBSMglEw"
    "CkbBKBgFo2AUAAAHkgABfN2gwQAAAABJRU5ErkJggg=="
)


def probe(base_url: str, model: str, api_key: str = "", timeout: float = 30.0) -> dict:
    """Check reachability, whether `model` is served, and whether it takes images.

    A text-only model is the trap here: it will not refuse an image so much as
    respond about nothing, so the vision check sends a real (if tiny) one and
    treats an error as the answer.
    """
    url = base_url.rstrip("/")
    root = url[:-3].rstrip("/") if url.endswith("/v1") else url
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    out: dict = {"reachable": False, "models": [], "model_present": False,
                 "vision": None, "error": ""}
    try:
        r = httpx.get(f"{root}/v1/models", headers=headers, timeout=timeout)
        r.raise_for_status()
        out["reachable"] = True
        out["models"] = [m.get("id", "?") for m in r.json().get("data", [])]
        out["model_present"] = model in out["models"]
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out

    try:
        r = httpx.post(
            f"{root}/v1/chat/completions",
            headers=headers,
            json={
                "model": model, "max_tokens": 8, "temperature": 0,
                "messages": [{"role": "user", "content": [
                    {"type": "text", "text": "Reply with the single word: ok"},
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/png;base64,{TINY_PNG}"}},
                ]}],
            },
            timeout=timeout,
        )
        out["vision"] = r.status_code == 200
        if r.status_code != 200:
            out["error"] = f"vision check: HTTP {r.status_code} {r.text[:160]}"
    except Exception as exc:  # noqa: BLE001
        out["vision"] = False
        out["error"] = f"vision check: {type(exc).__name__}: {exc}"
    return out
