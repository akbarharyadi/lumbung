"""The always-on answerer: questions in, Lumbung's voice out.

Until now a free-form question waited for a Claude Code session to be open --
which meant most of the time it waited. This worker replaces that: one
OpenCode 2 (GLM) session server-side, one Python loop here, both running for
as long as the PC is on. The queues and the answer channel are exactly the
ones the in-app chat already reads; nothing on the frontend changes.

The boundary that keeps this honest is the same one the project already holds:
**rules decide, the LLM explains.** Every number the model sees was computed
by deterministic code moments before and handed to it in the prompt. The model
cannot read files (see opencode_backend), cannot record spending, cannot touch
config, and its answer is prose into `answers.jsonl` -- the same file engine
alerts land in, and no more powerful than that.

Receipts (`[FILE]` lines) are the one place the model would need eyes. OCR
runs locally through the same vision endpoint `lumbung spend` already uses;
without it the worker says so plainly rather than guessing.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path

from httpx import HTTPError

from .chat import build_commands, follow, read_answers
from .config import Config, get_secrets, load_config
from .opencode_backend import AgentSettings, OpenCodeServer
from .research import (
    Finding,
    deliver_findings,
    pending_questions,
    question_key,
)
from .singleton import InstanceLock

log = logging.getLogger(__name__)

ANSWER_SOURCE = "agent-worker"
ASK_POLL_SEC = 45
RESEARCH_POLL_SEC = 120
_CONTEXT_CHARS = 1500          # per section; the prompt is not a database dump

# Everything, for research -- web questions can be about any of it.
RESEARCH_SECTIONS = (
    ("NET WORTH", "networth"),
    ("WHAT TO DO NOW (ranked)", "todo"),
    ("BOT STATUS", "status"),
    ("OPEN POSITIONS", "positions"),
    ("BOT PnL", "pnl"),
)

# Intent -> which computed sections the model even sees. Rules decide, the
# model explains: a greeting must not drag in the whole portfolio, and the
# ranked checklist only appears when the question is about what to do.
CHAT_SECTIONS: dict[str, tuple[tuple[str, str], ...]] = {
    "smalltalk": (),
    "record": (),
    "bot": (("BOT STATUS", "status"), ("OPEN POSITIONS", "positions"),
            ("BOT PnL", "pnl")),
    "spending": (("SPENDING (LAST 30 DAYS)", "expenses"),
                 ("PAYDAY PLAN", "payday")),
    "action": (("WHAT TO DO NOW (ranked)", "todo"), ("NET WORTH", "networth")),
    "portfolio": (("NET WORTH", "networth"), ("PAYDAY PLAN", "payday")),
    "general": (("NET WORTH", "networth"), ("BOT STATUS", "status"),
                ("OPEN POSITIONS", "positions"), ("BOT PnL", "pnl")),
}

# First deterministic cut at what the message is. The model sees the verdict
# and the matching numbers -- it never has to guess which numbers matter.
_INTENT_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("smalltalk", (
        r"^(hi|hello|hey|hai|halo|pagi|siang|sore|malam|ok(ay)?|oke|mantap"
        r"|makasih|terima ?kasih|thanks|thx|test|tes|wow|keren)\b",
        r"\b(who are you|siapa (kamu|namamu)|apa kabar|how are you)\b",
    )),
    ("record", (
        r"\b(masuk\w*|tambah\w*|add|catat\w*|record|simpan|update|hapus"
        r"|remove|ganti|considering|wishlist)\b",
    )),
    ("bot", (
        r"\b(bot|engine|crypto|krypto|trading|trade|posisi|position|pnl"
        r"|untung|rugi|halt|indodax|bitcoin|btc|altcoin|token|coin)\b",
    )),
    ("spending", (
        r"\b(spend|spent|spending|expense|pengeluaran|belanja|jajan|budget"
        r"|boros|hemat)\b",
    )),
    ("action", (
        r"\b(what to do|todo|checklist|payday|gajian|rekomend\w*|recommend\w*"
        r"|prioritas|priority)\b",
    )),
    ("portfolio", (
        r"\b(net ?worth|kekayaan|harta|portfolio|portofolio|alokasi|allocation"
        r"|target|passive ?income|dividen\w*|sbn|obligasi|bond\w*|emas|gold"
        r"|tabungan|saving\w*|goal|bbc\w*)\b",
    )),
)


def capture_intent(question: str) -> str:
    """One chat message -> one intent class. Cheap, deterministic, and the
    reason a 'Hello' does not cost a portfolio computation."""
    q = question.lower().strip()
    for name, patterns in _INTENT_RULES:
        if any(re.search(p, q) for p in patterns):
            return name
    return "general"

# [FILE] <path> — <instruction>: an upload from the app. The instruction text
# is written for the answerer, not for him.
# Upload rows arrive in two shapes: the old single-file "[FILE] <path> —
# instruction" and the app's current "[FILES N] <instruction>\n<path>\n<path>".
# Both must route to the attachment reader, or the message is answered as if
# it were plain text and the upload is silently wasted.
_UPLOAD_ROW = re.compile(r"^\s*\[FILES?\s*\d*\]")


def _is_upload_row(text: str) -> bool:
    return bool(_UPLOAD_ROW.match(text))


def _looks_like_path(ln: str) -> bool:
    """POSIX absolute, a Windows drive path, or a slash-y relative path
    ('data/uploads/x.png'). A caption with spaces never matches, so his
    words stay words."""
    if ln.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", ln):
        return True
    return "/" in ln and " " not in ln


def _parse_upload_row(text: str) -> tuple[list[Path], str]:
    """Attachment rows -> (paths, his instruction). Paths are the lines that
    start with '/'; everything else is what he wrote about them. The old
    single-file shape kept 'path — instruction' on ONE line."""
    body = _UPLOAD_ROW.sub("", text, count=1).strip()
    paths: list[Path] = []
    words: list[str] = []
    for ln in body.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        if _looks_like_path(ln):
            # old shape: "/path/to file.png — caption" on one line
            path_part, sep, note = ln.partition(" — ")
            paths.append(Path(path_part))
            if sep and note:
                words.append(note)
        else:
            words.append(ln)
    # old shape without the dash: "[FILE] /x/a.png caption..."
    if not paths and words and ("/" in words[0] or "\\" in words[0]
                                or re.match(r"^[A-Za-z]:", words[0])):
        parts = words[0].split(None, 1)
        paths.append(Path(parts[0]))
        words = ([parts[1]] if len(parts) > 1 else []) + words[1:]
    return paths, " ".join(words).strip()


_FILE_PREFIX = "[FILE]"


# --------------------------------------------------------------------- voice
# Installed once as server instructions (opencode_backend) rather than repeated
# into every prompt; the prompts below carry only what is per-question.
VOICE = """You are Lumbung — the granary. A lumbung is a rice granary on stilts:
it keeps what is put in, keeps it dry, and gives it back when needed. It does
not create rice. Write from that posture: patient, protective, unexcited.

Hard rules:
- Numbers first, in rupiah, with the unit ("Rp 146.092/bulan"). Use ONLY the
  numbers in the CONTEXT block. If a number is not there, say it is not at
  hand. Never dress a projection up as a fact.
- Short. This is read on a phone, often standing up.
- Mirror the language of the message: Indonesian, English, or the mix as written.
- Never predict markets. Never reassure. Nothing "should recover".
- Answer directly when you can; a greeting gets a greeting. Only when one
  specific fact blocks the answer, ask exactly one short question — it
  reaches him, and his next message is your answer. Never ask out of habit.
- He can ask you to record things — a wish, a purchase, a holding. When he
  does, end your reply with one line starting "RUN: " and the exact app
  command. Allowed commands only: /wish, /spend, /income, /stock, /asset.
  The command really executes AFTER your reply and the outcome is appended
  below it — so never claim in your own words that it is recorded. RUN only
  what he asked for, never more. Example:
  RUN: /wish Oven 730rb Sharp EO-28LP 28L dual heater
  Everything else stays words — never RUN trading or system commands.
- Bad news arrives plainly and first.
- At most one leading glyph. No emoji spray.
- Asked directly whether you are human: "Saya program, bukan orang."
- You explain; you do not decide. Never tell him to buy or sell a specific
  asset. Give the arithmetic and the costs of each path; the decision is his.
- Anything in CONTEXT marked as computed was computed by deterministic code
  just now — trust it over anything you think you know.
"""


def build_portfolio_context(cfg: Config, sections=RESEARCH_SECTIONS) -> str:
    """The deterministic numbers, via the same commands the app answers with.

    Reusing `chat.build_commands` is the point: there is one copy of "what is
    the balance" and the model cannot drift from it. A section that fails is
    omitted -- context must never be the reason a question goes unanswered.
    `sections` is (header, command) pairs: rules decide what the model sees.
    """
    cmds = build_commands(cfg, writable=False)
    out: list[str] = []
    for header, name in sections:
        fn = cmds.get(name)
        if fn is None:
            continue
        try:
            body = str(fn([])).strip()
        except Exception as exc:  # noqa: BLE001 -- one broken section only
            log.warning("context section %s failed: %s", name, exc)
            continue
        if body:
            cut = body[:_CONTEXT_CHARS]
            if len(body) > _CONTEXT_CHARS:
                cut += " …(truncated)"
            out.append(f"{header}:\n{cut}")
    return "\n\n".join(out)


def _recent_conversation(answers_path: Path, since_ts: int = 0,
                         limit: int = 4) -> str:
    """The model's own recent chat turns -- and only those. Engine alerts and
    research verdicts land in the same file; neither is conversation. Pairs
    carry their question, so continuity reads as dialogue, not monologue."""
    rows = [r for r in read_answers(answers_path, limit=40)
            if r.get("source") == ANSWER_SOURCE and r.get("text")
            and int(r.get("ts") or 0) >= since_ts][-limit:]
    if not rows:
        return "(none since the chat was reset)"
    parts = []
    for r in rows:
        q = str(r.get("q") or "").strip()
        body = str(r["text"])[:600]
        parts.append(f"He asked: {q}\nYou replied: {body}" if q else body)
    return "\n---\n".join(parts)


# ------------------------------------------------------------------ chat answers
def chat_prompt(cfg: Config, question: str, *, history_since: int = 0) -> str:
    """One prompt per chat turn: intent (decided by rules), only the numbers
    that intent needs, the paired recent conversation, then the question."""
    intent = capture_intent(question)
    sections = CHAT_SECTIONS.get(intent, CHAT_SECTIONS["general"])
    ctx = (build_portfolio_context(cfg, sections) if sections
           else "(none -- a greeting needs no numbers)")
    record_note = ""
    if intent == "record":
        record_note = (
            "HE WANTS SOMETHING RECORDED. You CAN do this: end your reply "
            "with a RUN: line carrying the exact command. Allowed: /wish, "
            "/spend, /income, /stock, /asset. Command shape is strict -- "
            "/wish <one-word-name> <amount> [note words free], e.g. "
            "RUN: /wish Oven 730rb Sharp EO-28LP 28L dual heater. "
            "Never claim it is recorded -- your RUN: line is executed AFTER "
            "your reply and the real outcome is appended below it.\n\n"
        )
    return (
        f"INTENT (decided by rules, trust it): {intent}\n\n"
        + (record_note)
        + "CONTEXT (computed just now from the journal and the holdings):\n"
        f"{ctx}\n\n"
        "RECENT CONVERSATION (oldest first):\n"
        f"{_recent_conversation(cfg.data_dir / 'answers.jsonl', since_ts=history_since)}\n\n"
        f"QUESTION:\n{question}\n\n"
        "Answer as Lumbung. Answer the question only."
    )


def answer_chat_question(
    server: OpenCodeServer,
    cfg: Config,
    row: dict,
    *,
    session_dir: Path,
    session_id: str,
    on_form=None,
    history_since: int = 0,
) -> tuple[str, str]:
    """Answer one queued chat question. Returns (answer, session_id); an empty
    answer with no form delivered means the model produced nothing usable."""
    answer, session_id = server.ask(
        chat_prompt(cfg, str(row.get("text", "")), history_since=history_since),
        session_dir=session_dir,
        session_id=session_id,
        on_form=on_form,
    )
    return answer, session_id


def write_chat_answer(data_dir: Path, text: str,
                      question: str | None = None) -> None:
    row: dict = {"ts": int(time.time()), "text": text, "source": ANSWER_SOURCE}
    if question:
        row["q"] = question[:400]
    with open(data_dir / "answers.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


# The one write path the model gets: a strict subset of the app's own
# /commands, run through the same deterministic dispatch the dashboard uses.
# The model composes; Python validates and executes. The trading controls
# (/pause /resume /flat /kill) are not on the list no matter what it says.
_RUN_ALLOWED = re.compile(r"^/(wish|spend|income|stock|asset)\b")
_RUN_LIMIT = 3


def execute_run_lines(cfg: Config, answer: str) -> str:
    from .chat import dispatch

    out: list[str] = []
    budget = _RUN_LIMIT
    for line in answer.splitlines():
        m = re.match(r"\s*RUN:\s*(/\S.*)$", line)
        if not m:
            out.append(line)
            continue
        if budget <= 0:
            out.append("(not run: at most "
                       f"{_RUN_LIMIT} commands per reply)")
            continue
        cmd = m.group(1).strip()
        if not _RUN_ALLOWED.match(cmd):
            out.append(f"(not run: {cmd.split()[0]} is not an allowed "
                       "command)")
            continue
        budget -= 1
        try:
            res = dispatch(cfg, cmd, writable=False)
            reply = str(res.get("reply", "")).strip() or "done"
            out.append(f"✅ {cmd.split()[0]} — {reply.splitlines()[0]}")
        except Exception as exc:  # noqa: BLE001 -- a bad command must not eat the answer
            out.append(f"⚠️ {cmd.split()[0]} gagal — {exc}")
    return "\n".join(out)


_TX_LINE = re.compile(
    r"^\s*(\d{4}-\d{2}-\d{2})\s*\|\s*(-?[\d.,]+)\s*\|\s*([^|]+?)\s*(?:\|\s*(\w+)\s*)?$"
)


def execute_transactions_block(cfg: Config, answer: str, note: str) -> str:
    """The TRANSACTIONS block is the statement write path: the model only
    transcribes what the statement shows; deterministic code validates,
    dedupes against the ledger and records what is genuinely new."""
    from .spending import CATEGORIES, connect, reconcile_statement
    from .web.settings import _number

    if "TRANSACTIONS:" not in answer:
        return answer
    rows: list[dict] = []
    out: list[str] = []
    in_block = False
    for line in answer.splitlines():
        t = line.strip()
        if t == "TRANSACTIONS:":
            in_block = True
            continue
        if in_block:
            m = _TX_LINE.match(line)
            if m:
                date_s, amount_s, item, cat = m.groups()
                try:
                    amount = float(_number(amount_s))
                except Exception:  # noqa: BLE001
                    amount = 0.0
                category = (cat or "other").lower()
                if category not in CATEGORIES:
                    category = "other"
                rows.append({"date": date_s, "amount": amount,
                             "item": item, "category": category})
                continue
            if not t:
                continue
            in_block = False
            out.append(line)
            continue
        out.append(line)
    if not rows:
        return answer
    note = note.strip()[:120] or "statement"
    try:
        res = reconcile_statement(connect(cfg.db_path), rows, note=note)
    except Exception as exc:  # noqa: BLE001 -- a failed batch must not eat the answer
        log.warning("reconcile failed: %s", exc)
        return answer + "\n\n⚠️ Rekonsiliasi gagal: " + str(exc)
    total = f"Rp {res['recorded_total']:,.0f}".replace(",", ".")
    parts = [f"✅ Rekonsiliasi: {len(res['recorded'])} transaksi dicatat "
             f"(total {total})"]
    if res["skipped"]:
        parts.append(f"{len(res['skipped'])} sudah ada di buku — dilewati: "
                     + ", ".join(s["item"] for s in res["skipped"][:5]))
    if res["failed"]:
        parts.append(f"{len(res['failed'])} baris gagal dibaca")
    out.append("\n".join(parts))
    return "\n".join(out)


# ----------------------------------------------------------------- receipts
def _render_pdf_pages(pdf: Path, out_dir: Path, max_pages: int = 8) -> list[Path]:
    """PDFs cannot reach the model directly -- the provider drops the
    attachment -- but page images read fine. Rendered deterministically,
    capped, into uploads/_render/."""
    try:
        import pypdfium2 as pdfium
    except ImportError:
        log.warning("pypdfium2 missing: cannot render %s", pdf.name)
        return []
    out_dir.mkdir(parents=True, exist_ok=True)
    pages: list[Path] = []
    try:
        doc = pdfium.PdfDocument(str(pdf))
        for i in range(min(len(doc), max_pages)):
            img = doc[i].render(scale=2.0).to_pil()
            out = out_dir / f"{pdf.stem}-p{i + 1}.png"
            img.save(out)
            pages.append(out)
    except Exception as exc:  # noqa: BLE001 -- a broken pdf must not kill the turn
        log.warning("pdf render failed for %s: %s", pdf.name, exc)
        return []
    return pages


def answer_file_upload(
    server: OpenCodeServer,
    cfg: Config,
    row: dict,
    *,
    session_dir: Path,
    session_id: str,
    on_form=None,
    history_since: int = 0,
) -> tuple[str, str]:
    """One or more files from the app. The chat session runs inside
    data/uploads, so the model's Read tool can open the attachments itself
    -- GLM reads images -- and nothing outside that folder is reachable. No
    OCR service, no LOCAL_LLM: the one model both sees and explains."""
    paths, instruction = _parse_upload_row(str(row.get("text", "")))
    existing = [p for p in paths if p.exists()]
    if not existing:
        if paths:
            return ("Lampiran tidak ditemukan di penyimpanan. Kirim ulang, "
                    "atau tulis angkanya sebagai teks."), session_id
        return ("Lampiran tidak jelas. Kirim ulang filenya, atau tulis "
                "maksudnya sebagai teks."), session_id

    # PDFs: the provider drops the attachment, so pages are pre-rendered to
    # PNG (which GLM reads) before anything is listed.
    from .spending import CATEGORIES

    readable: list[Path] = []
    for p in existing:
        if p.suffix.lower() == ".pdf":
            pages = _render_pdf_pages(p, p.parent / "_render")
            readable.extend(pages or [p])   # fall back to the raw path: the
        else:                               # honest failure is still an answer
            readable.append(p)

    listing = "\n".join(f"- {p.name}" for p in readable)
    prompt = (
        f"ATTACHMENT{'S' if len(readable) > 1 else ''}: he sent "
        f"{len(readable)} file(s) from the app. They are in the current "
        "directory -- open each with the Read tool:\n"
        f"{listing}\n"
        + (f"HIS INSTRUCTION: {instruction}\n" if instruction else
           "Say what each shows: merchant, amount, date, whatever is "
           "legible.\n")
        + "If a file is an account statement and he asked to record or "
          "reconcile it: after reading, end with a block that starts with "
          "TRANSACTIONS: and then one line per transaction, exactly:\n"
          "YYYY-MM-DD | amount | merchant | category\n"
          "amount NEGATIVE for money out (debits), POSITIVE for money in "
          f"(credits). Categories allowed: {', '.join(CATEGORIES)}. The "
          "system records them, skips what is already logged, and appends "
          "the real outcome -- never claim they are recorded yourself.\n"
        + "Then answer as Lumbung, short. Do not record anything yourself "
          "outside that block."
    )
    answer, session_id = server.ask(
        prompt, session_dir=session_dir, session_id=session_id,
        on_form=on_form,
    )
    return answer, session_id


# ------------------------------------------------------------- research queue
def research_prompt(cfg: Config, q: dict) -> str:
    return (
        "You are answering a research question. Use the web tools to verify "
        "dates, quotas, rates and news; cite the source and its date inline. "
        "Answer in English. Numbers with units.\n"
        "Nobody can answer you mid-research: never ask questions, never "
        "request confirmation -- decide with what you find and answer.\n\n"
        f"CONTEXT (computed just now):\n"
        f"{build_portfolio_context(cfg) or '(unavailable)'}\n\n"
        f"TOPIC: {q.get('topic', '?')} (urgency: {q.get('urgency', 'normal')})\n"
        f"WHY ASKED: {q.get('why', '')}\n"
        f"QUESTION: {q.get('text', '')}\n\n"
        "If the web does not settle it, say exactly what remains unknown "
        "rather than filling the gap."
    )


def process_research(
    server: OpenCodeServer,
    cfg: Config,
    *,
    session_dir: Path,
    limit: int = 0,
) -> int:
    """Answer pending research questions and deliver through the ledger,
    so `--pending` stays truthful. Returns how many were answered. Each
    question gets a fresh session -- research turns stay isolated.
    `limit` caps how many per pass: a backlog must drain over successive
    polls, not hold the chat loop hostage for an hour."""
    d = cfg.data_dir
    open_q = pending_questions(d / "research_queue.jsonl",
                               d / "research_answers.jsonl")
    if limit > 0:
        open_q = open_q[:limit]
    d = cfg.data_dir
    open_q = pending_questions(d / "research_queue.jsonl",
                               d / "research_answers.jsonl")
    answered = 0
    for q in open_q:
        try:
            text, _ = server.ask(research_prompt(cfg, q), session_dir=session_dir)
        except (RuntimeError, HTTPError) as exc:
            log.warning("research ask failed for %s: %s", q.get("topic"), exc)
            continue
        if not text:
            continue
        deliver_findings(
            [Finding(topic=str(q.get("topic", "?")), text=text,
                     question=str(q.get("text", "")),
                     key=question_key(q))],
            data_dir=d,
        )
        answered += 1
    return answered


# ------------------------------------------------------------------ the loop
def run_worker(*, once: bool = False, cfg: Config | None = None,
               settings: AgentSettings | None = None,
               ask_poll_sec: int = ASK_POLL_SEC,
               research_poll_sec: int = RESEARCH_POLL_SEC) -> None:
    """Serve both queues until stopped. One instance per profile (PID lock) --
    two workers would answer every question twice."""
    cfg = cfg or load_config()
    s = settings or AgentSettings.from_secrets(get_secrets())
    if s is None:
        raise SystemExit(
            "agent disabled: set OPENCODE_MODEL (e.g. zai-coding-plan/glm-5.3) "
            "in .env, and AGENT_WORKER=1 to run the worker"
        )
    if not s.worker_enabled and not once:
        raise SystemExit("agent worker disabled: set AGENT_WORKER=1 in .env")

    d = cfg.data_dir
    state_dir = d / "agent"
    scratch = state_dir / "scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    # Chat sessions run inside data/uploads: the model's Read tool can open
    # his attachments (GLM reads images) and that folder holds nothing but
    # the files he sent -- .env, the db and the source stay out of reach.
    uploads_dir = d / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)
    instructions = state_dir / "instructions.md"
    instructions.parent.mkdir(parents=True, exist_ok=True)
    instructions.write_text(VOICE, encoding="utf-8")

    lock = InstanceLock(state_dir / "worker.pid")
    lock.acquire()
    server = OpenCodeServer(
        s.bin_path, s.port, s.model, state_dir, instructions=instructions
    )

    # Byte offsets, never line counts (pruning would replay the backlog), and
    # starting at EOF on boot so months-old questions stay months old.
    offsets: dict[str, int] = {}

    def _offset(path: Path, *, init: bool = False) -> int:
        key = str(path)
        if init and key not in offsets:
            try:
                offsets[key] = path.stat().st_size
            except OSError:
                offsets[key] = 0
        return offsets.get(key, 0)

    chat_sid = ""
    failed: set[str] = set()
    state_path = d / "chat_state.json"
    try:
        last_reset = int(json.loads(
            state_path.read_text(encoding="utf-8") or "{}").get("chat_reset_ts") or 0)
    except (OSError, ValueError):
        last_reset = 0
    # Checkpoint of the newest queued question already handled. Missing on a
    # first-ever run: anchor to now, and PERSIST the anchor immediately --
    # an anchor that only appears after the first processed row leaves every
    # boot before that first row swallowing in-flight messages.
    try:
        last_q_ts = int((state_dir / "last_q_ts").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        last_q_ts = int(time.time())
        try:
            (state_dir / "last_q_ts").write_text(str(last_q_ts),
                                                 encoding="utf-8")
        except OSError:
            pass

    def process_chat_row(row: dict) -> None:
        """One queued question, end to end. Idempotent enough: the ts of the
        last row seen is persisted, so a restart replays only what is newer
        than what was actually handled."""
        nonlocal chat_sid, last_q_ts
        ident = question_key(row)
        if ident in failed:
            return
        asked: list[str] = []

        def on_form(q: str) -> None:
            # The model asked instead of answering. Deliver its question --
            # his next message lands in this same session as the reply.
            asked.append(q)
            write_chat_answer(d, q, question=str(row.get("text", "")))

        try:
            if _is_upload_row(str(row.get("text", ""))):
                answer, chat_sid = answer_file_upload(
                    server, cfg, row,
                    session_dir=uploads_dir, session_id=chat_sid,
                    on_form=on_form)
            else:
                answer, chat_sid = answer_chat_question(
                    server, cfg, row,
                    session_dir=uploads_dir, session_id=chat_sid,
                    on_form=on_form, history_since=last_reset)
            if not answer:
                if asked:
                    pass           # the turn WAS the clarifying question
                else:
                    # Silent drop hid a whole failure class: GLM asking a
                    # question back, the form being cancelled, and the turn
                    # ending with no text. Say so, or the queue eats messages
                    # invisibly.
                    log.warning("chat ask returned nothing for %r (forms=%d)",
                                str(row.get("text", ""))[:60], len(asked))
                    failed.add(ident)
                    return
            log.info("chat answered %r -> %d chars",
                     str(row.get("text", ""))[:40], len(answer))
            if _is_upload_row(str(row.get("text", ""))):
                answer = execute_transactions_block(
                    cfg, answer, note=str(row.get("text", "")))
            answer = execute_run_lines(cfg, answer)
            write_chat_answer(d, answer, question=str(row.get("text", "")))
        except Exception as exc:  # noqa: BLE001 -- one bad question only
            log.warning("chat answer failed: %s", exc)
            failed.add(ident)
        finally:
            # Persisted even for failed rows: a poison question must not
            # replay on every restart.
            last_q_ts = max(last_q_ts, int(row.get("ts") or 0))
            try:
                (state_dir / "last_q_ts").write_text(str(last_q_ts),
                                                     encoding="utf-8")
            except OSError:
                pass

    def drain_chat() -> None:
        nonlocal chat_sid, last_reset
        # The app's Reset clears the view via /api/chat/reset; here is where
        # the memory follows: fresh model session, pre-reset turns dropped
        # from every later prompt.
        try:
            rst = int(json.loads(
                state_path.read_text(encoding="utf-8") or "{}").get("chat_reset_ts") or 0)
        except (OSError, ValueError):
            rst = last_reset
        if rst > last_reset:
            last_reset = rst
            chat_sid = ""
            log.info("chat reset seen: fresh session, history before %d dropped", rst)
        q_path = d / "ask_queue.jsonl"
        _cur = q_path.stat().st_size if q_path.exists() else 0
        rows, consumed = follow(q_path, _offset(q_path, init=True))
        offsets[str(q_path)] = consumed       # follow's offset, not a re-stat:
        log.info("drain: offset=%d size_now=%d rows=%d",
                 _offset(q_path), _cur, len(rows))
        for line in rows:                     # a trailing fragment stays live
            try:
                row = json.loads(line)
            except ValueError:
                log.warning("chat queue: unparsable line %r", line[:80])
                continue
            process_chat_row(row)

    def replay_downtime() -> None:
        """Answer questions that arrived while the worker was down.

        The byte-follow starts at EOF, which is right for a running system
        and exactly wrong across a restart: a question sent during a deploy
        would wait forever. The ts checkpoint closes that gap.
        """
        q_path = d / "ask_queue.jsonl"
        try:
            tail = q_path.read_bytes()[-65536:]
        except OSError:
            return
        for line in tail.decode("utf-8", "replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if int(row.get("ts") or 0) > last_q_ts:
                log.info("replay: %r arrived while the worker was down",
                         str(row.get("text", ""))[:40])
                process_chat_row(row)

    try:
        replay_downtime()
        if once:
            drain_chat()
            n = process_research(server, cfg, session_dir=scratch)
            log.info("once: research answered=%d", n)
            return
        log.info("agent worker up (model=%s, port=%d)", s.model, s.port)

        # Research runs on its own thread. A single research turn takes
        # minutes -- on the chat loop it made every "Hello" wait behind it,
        # which read as the bot being stuck. Chat polls stay on the main
        # thread and never block on research again.
        research_lock = threading.Lock()

        def research_loop() -> None:
            while True:
                time.sleep(research_poll_sec)
                if not research_lock.acquire(blocking=False):
                    continue      # previous pass still running -- skip
                try:
                    process_research(server, cfg, session_dir=scratch, limit=1)
                except Exception as exc:  # noqa: BLE001
                    log.warning("research pass failed: %s", exc)
                finally:
                    research_lock.release()

        threading.Thread(target=research_loop, daemon=True,
                         name="research").start()

        while True:
            drain_chat()
            time.sleep(ask_poll_sec)
    finally:
        server.close()
        lock.release()
