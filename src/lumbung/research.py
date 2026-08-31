"""Questions worth asking a research agent each morning.

The rules in this project answer everything that can be computed from what is
already known. This covers the other half -- the things that only change when
something happens in the world, and that no amount of arithmetic will surface:

* a new SBN series opening, which `bonds.yaml` cannot know about because there
  is no public calendar API and the file is maintained by hand;
* an ex-dividend date approaching on something owned or watched;
* news that changes the thesis on a holding rather than merely its price.

**Nothing here decides anything.** It writes questions to a queue for a research
agent to answer in prose, and those answers are read by a person. Keeping the
decision boundary at "rules decide, research explains" is deliberate: a fluent,
confident answer about which stock will rise is exactly the failure mode that
makes a bad recommendation persuasive.

Questions are generated from **real state**, never from a fixed list. A question
that fires every morning regardless of circumstances is one that gets skimmed,
and the whole point is that these are rare enough to be read.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

log = logging.getLogger(__name__)

# A bond file older than this is probably missing a series that has since opened.
BONDS_STALE_DAYS = 45
# Ask about an offering only once it is close enough to matter.
OFFER_SOON_DAYS = 30


@dataclass
class Question:
    topic: str
    text: str
    why: str
    urgency: str = "normal"          # normal | high
    tags: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "ts": int(time.time()),
            "topic": self.topic,
            "text": self.text,
            "why": self.why,
            "urgency": self.urgency,
            "tags": self.tags,
            "source": "morning-research",
        }


def build_questions(
    *,
    offerings=None,
    reports=None,
    bonds_file_age_days: float = 0.0,
    today: date | None = None,
) -> list[Question]:
    """Turn the current state into things worth looking up. Pure and testable."""
    today = today or date.today()
    out: list[Question] = []

    # 1. The bond calendar. This is the single most valuable one, because it is
    #    the only part of the balance sheet that cannot self-update: there is no
    #    API, so a series can open and close without the tool ever noticing.
    open_now = [o for o in (offerings or []) if o.is_open(today)]
    if bonds_file_age_days >= BONDS_STALE_DAYS or not open_now:
        out.append(Question(
            topic="bonds",
            text=(
                "Which Indonesian retail government bonds (SBN Ritel: ORI, SR, ST, "
                "SBR) are open for subscription right now or announced for the next "
                "two months? For each: series code, coupon, tenor, minimum, opening "
                "and closing dates, and whether it is tradeable on the secondary "
                "market or early-redemption only."
            ),
            why=(
                f"config/bonds.yaml is {bonds_file_age_days:.0f} days old"
                if bonds_file_age_days >= BONDS_STALE_DAYS
                else "no offering in the file is currently open"
            ),
            urgency="high" if not open_now else "normal",
            tags=["sbn", "config-update"],
        ))

    for o in open_now:
        left = o.days_left(today)
        if 0 <= left <= OFFER_SOON_DAYS:
            out.append(Question(
                topic="bonds",
                text=(
                    f"{o.series} closes in {left} days at {o.coupon * 100:.2f}% gross. "
                    "Has the quota been raised or filled, and has a successor series "
                    "been announced that would pay more?"
                ),
                why=f"{o.series} closes {o.closes}",
                urgency="high" if left <= 7 else "normal",
                tags=["sbn", o.series],
            ))

    # 2. Dividends, on what is actually owned. Asked as a date question, not as
    #    "is it a good buy" -- the date is a fact that can be looked up; the
    #    judgement is not, and `trade_math` already prices the trade properly.
    for r in reports or []:
        ticker = r.holding.ticker.replace(".JK", "")
        out.append(Question(
            topic="dividend",
            text=(
                f"When is the next ex-dividend date for {ticker} on the IDX, and what "
                "is the declared amount per share? Has the company guided on the "
                "payout ratio for this year?"
            ),
            why=f"you hold {r.holding.lots} lots of {ticker}",
            tags=["dividend", ticker],
        ))
        out.append(Question(
            topic="news",
            text=(
                f"Any material news on {ticker} in the last 24 hours -- earnings, "
                "dividend changes, regulatory action, management changes? Material "
                "means it changes the reason to hold, not that the price moved."
            ),
            why=f"{ticker} is {r.holding.lots} lots of your portfolio",
            tags=["news", ticker],
        ))

    # 3. Rates. These move the comparison between every option in the file.
    out.append(Question(
        topic="rates",
        text=(
            "Has Bank Indonesia changed the BI Rate, or the LPS changed its "
            "guaranteed-rate cap (tingkat bunga penjaminan)? Both are currently "
            "recorded as BI 5.75% and LPS 3.75%."
        ),
        why="deposit safety and every net-rate comparison depend on these",
        tags=["rates", "config-update"],
    ))

    return out


def queue_questions(path: str | Path, questions: list[Question]) -> int:
    """Append questions to the research queue. Returns how many were written."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as fh:
        for q in questions:
            fh.write(json.dumps(q.as_dict(), ensure_ascii=False) + "\n")
    log.info("queued %d research question(s)", len(questions))
    return len(questions)


def file_age_days(path: str | Path) -> float:
    try:
        return (time.time() - Path(path).stat().st_mtime) / 86_400
    except OSError:
        return 0.0


# ------------------------------------------------------------------ answers
# A question with no answer path is a question nobody answers. The morning job
# has been queueing into this file since it was written; nothing read it back.
#
# There is one channel now. Findings are appended to `answers.jsonl`, which the
# in-app chat streams -- so writing the file *is* the delivery, and there is no
# second send that can fail after the fact. `research_answers.jsonl` is the
# separate ledger that keeps `pending_questions` honest; it is not a second
# inbox.


@dataclass
class Finding:
    """An answer to one queued question, in prose, written by a research agent."""

    topic: str
    text: str
    question: str = ""
    key: str = ""               # links back to the question it answers
    tags: list[str] = field(default_factory=list)

    def as_answer(self) -> dict:
        return {
            "ts": int(time.time()),
            "text": self.text,
            "topic": self.topic,
            "key": self.key,
            "source": "morning-research",
        }


def question_key(row: dict) -> str:
    """Stable identity for one queued question.

    Not the timestamp alone -- a single `lumbung research` run queues every
    question with the same `ts`, so that would mark all five answered the moment
    one was. Not the text alone either: "any material news in the last 24 hours"
    is *meant* to recur, and hashing only the text would suppress it forever
    after the first answer. The pair is what makes each asking distinct.
    """
    body = f"{row.get('topic', '')}\n{row.get('text', '')}".encode()
    return f"{row.get('ts', 0)}:{hashlib.sha1(body).hexdigest()[:12]}"


def pending_questions(queue_path: str | Path, answers_path: str | Path) -> list[dict]:
    """Queued questions that have no answer yet, oldest first.

    The monitor deliberately starts from the end of the file, so a session that
    opens after the morning job ran is never notified about what it missed.
    That is the right behaviour for notifications and the wrong one for the
    backlog, which is what this exists to surface.
    """
    rows = _read_jsonl(queue_path)
    done = {r.get("key") for r in _read_jsonl(answers_path) if r.get("key")}
    return [r for r in rows if question_key(r) not in done]


def _read_jsonl(path: str | Path) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    out: list[dict] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:      # a half-written line must not lose the rest
            continue
    return out


def deliver_findings(
    findings: list[Finding],
    *,
    data_dir: str | Path,
) -> dict:
    """Write answers into the chat and mark them answered.

    Two files, two jobs. `answers.jsonl` is the conversation the app shows.
    `research_answers.jsonl` is the record `pending_questions` reads, so a
    question is only ever answered once -- and it carries the question text,
    which the chat transcript deliberately does not.

    The chat is written first. If the ledger write fails the answer is still
    delivered and the question simply looks unanswered, which is the harmless
    direction for that failure to fall.
    """
    d = Path(data_dir)
    d.mkdir(parents=True, exist_ok=True)
    if not findings:
        return {"written": 0}

    with open(d / "answers.jsonl", "a", encoding="utf-8") as fh:
        for f in findings:
            fh.write(json.dumps(f.as_answer(), ensure_ascii=False) + "\n")
    with open(d / "research_answers.jsonl", "a", encoding="utf-8") as fh:
        for f in findings:
            row = f.as_answer()
            row["question"] = f.question
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    return {"written": len(findings)}
