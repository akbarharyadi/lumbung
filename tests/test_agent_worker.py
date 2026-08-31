"""Tests for the opencode2 backend plumbing and the always-on answerer.

The worker answers real questions with a real model, so what is worth testing
here is everything AROUND the model: the config gate, the prompt shape, the
answer files, the research ledger, and the fail-closed event handling. The
model itself is always a stub -- a test that calls GLM is a test you cannot
run in the suite.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import respx

from lumbung.agent_worker import (
    ANSWER_SOURCE,
    answer_file_upload,
    build_portfolio_context,
    capture_intent,
    chat_prompt,
    process_research,
    research_prompt,
    run_worker,
    write_chat_answer,
)
from lumbung.opencode_backend import (
    AgentSettings,
    OpenCodeServer,
    _consume_event,
    _sse_payload,
    server_permission_config,
)


@pytest.fixture
def cfg(tmp_path):
    """A cfg whose data_dir is tmp. Only `data_dir` is read by the worker --
    the deterministic command answers arrive through the mocked
    build_commands, exactly so no test ever touches the real data directory.
    """
    return SimpleNamespace(data_dir=tmp_path)


@pytest.fixture
def fake_cmds(monkeypatch):
    """Deterministic command answers in place of the real ones. A section may
    be a callable, which is expected to raise -- to test omission."""
    def _install(**sections):
        monkeypatch.setattr(
            "lumbung.agent_worker.build_commands",
            lambda cfg, *, writable=False: {
                name: (lambda args, _t=text: _t(args) if callable(_t) else _t)
                for name, text in sections.items()
            },
        )
    return _install


def _settings(**over) -> AgentSettings:
    base = dict(bin_path="opencode2", port=42799, model="zai-coding-plan/glm-5.3",
                worker_enabled=True)
    base.update(over)
    return AgentSettings(**base)


# ------------------------------------------------------------------- config


def test_settings_disabled_without_model():
    secrets = SimpleNamespace(opencode_bin="", opencode_port=42778,
                              opencode_model="", agent_worker="1")
    assert AgentSettings.from_secrets(secrets) is None


def test_settings_from_secrets():
    secrets = SimpleNamespace(opencode_bin="", opencode_port=0,
                              opencode_model="zai-coding-plan/glm-5.3",
                              agent_worker="1")
    s = AgentSettings.from_secrets(secrets)
    assert s is not None
    assert s.bin_path == "opencode2"        # empty -> PATH default
    assert s.port == 42778                  # empty -> default, not 0
    assert s.worker_enabled is True


def test_permission_gate_is_fail_closed():
    p = server_permission_config()
    for denied in ("bash", "edit", "write", "patch", "external_directory"):
        assert p[denied] == "deny", denied
    assert p["webfetch"] == "allow"         # research needs the web
    assert p["websearch"] == "allow"


def test_run_worker_refuses_without_model(monkeypatch):
    """Gate on the injected path -- never on whatever the real .env happens
    to contain today."""
    monkeypatch.setattr("lumbung.agent_worker.get_secrets", lambda:
                        SimpleNamespace(opencode_bin="", opencode_port=0,
                                        opencode_model="", agent_worker=""))
    with pytest.raises(SystemExit, match="OPENCODE_MODEL"):
        run_worker(once=True, cfg=SimpleNamespace(data_dir=Path(".")),
                   settings=None)


def test_run_worker_refuses_when_worker_disabled():
    with pytest.raises(SystemExit, match="AGENT_WORKER"):
        run_worker(cfg=SimpleNamespace(data_dir=Path(".")),
                   settings=_settings(worker_enabled=False))


# ------------------------------------------------------------------ context


def test_context_uses_command_answers_and_marks_truncation(cfg, fake_cmds):
    fake_cmds(
        networth="Rp 134.500.000",
        todo="x" * 3000,
        status=lambda *a: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    ctx = build_portfolio_context(cfg)
    assert "Rp 134.500.000" in ctx
    assert "…(truncated)" in ctx
    assert "BOT STATUS" not in ctx          # raised -> omitted, never fatal


def test_chat_prompt_carries_question_context_and_history(cfg, fake_cmds):
    fake_cmds(networth="Rp 134.500.000")
    (cfg.data_dir / "answers.jsonl").write_text(
        json.dumps({"ts": 1, "text": "sebelumnya", "source": ANSWER_SOURCE,
                    "q": "saldo berapa?"}) + "\n",
        encoding="utf-8",
    )
    p = chat_prompt(cfg, "berapa saldo saya?", history_since=0)
    assert "berapa saldo saya?" in p
    assert "Rp 134.500.000" in p
    assert "sebelumnya" in p
    assert "He asked: saldo berapa?" in p      # paired, not a bare monologue
    assert "INTENT (decided by rules" in p


def test_chat_prompt_intent_trims_context(cfg, fake_cmds):
    fake_cmds(networth="Rp 134.500.000", todo="CHECKLIST-MARKER")
    # A greeting drags in no numbers at all.
    p = chat_prompt(cfg, "Hello")
    assert "INTENT (decided by rules, trust it): smalltalk" in p
    assert "Rp 134.500.000" not in p
    assert "CHECKLIST-MARKER" not in p
    # The ranked checklist appears only for action-intent questions.
    p = chat_prompt(cfg, "apa rekomendasinya?")
    assert "CHECKLIST-MARKER" in p


def test_capture_intent_rules():
    assert capture_intent("Hello") == "smalltalk"
    assert capture_intent("siapa kamu?") == "smalltalk"
    assert capture_intent("berapa pnl bot hari ini?") == "bot"
    assert capture_intent("pengeluaran bulan ini apa saja?") == "spending"
    assert capture_intent("what to do next?") == "action"
    assert capture_intent("berapa net worth saya?") == "portfolio"
    assert capture_intent("cuaca besok hujan?") == "general"


def test_execute_run_lines_runs_allowed_and_blocks_rest(cfg, monkeypatch):
    from lumbung.agent_worker import execute_run_lines

    calls = []

    def fake_dispatch(cfg_, text, *, ask_queue=None, writable=False):
        calls.append(text)
        assert writable is False            # the trading controls stay out
        return {"reply": "Oven (Rp 730.000) added to considering.",
                "queued": False}

    monkeypatch.setattr("lumbung.chat.dispatch", fake_dispatch)
    out = execute_run_lines(
        cfg,
        "Masukin ya.\n"
        "RUN: /wish Oven 730rb Sharp EO-28LP\n"
        "RUN: /kill\n"
        "RUN: /asset\n"
        "RUN: /asset\n"
        "RUN: /asset\n",
    )
    # /kill is refused without spending the budget; the cap is 3 runs
    assert calls == ["/wish Oven 730rb Sharp EO-28LP", "/asset", "/asset"]
    assert "✅ /wish" in out
    assert "not an allowed command" in out
    assert out.count("✅ /asset") == 2
    assert "RUN:" not in out               # executed lines leave no trace


def test_research_prompt_demands_verification(cfg, fake_cmds):
    fake_cmds()
    p = research_prompt(cfg, {"topic": "bonds", "text": "SBN apa terbuka?",
                              "why": "config stale", "urgency": "high"})
    assert "SBN apa terbuka?" in p
    assert "cite the source" in p


# ------------------------------------------------------------------ answers


def test_write_chat_answer_shape(tmp_path):
    write_chat_answer(tmp_path, "Rp 36.800/bulan, modelled.")
    row = json.loads(
        (tmp_path / "answers.jsonl").read_text(encoding="utf-8").splitlines()[-1]
    )
    assert row["text"] == "Rp 36.800/bulan, modelled."
    assert row["source"] == ANSWER_SOURCE
    assert isinstance(row["ts"], int)


def test_file_upload_missing_path_answers_honestly(cfg):
    server = SimpleNamespace()
    answer, sid = answer_file_upload(
        server, cfg, {"text": "[FILE] data/uploads/ghost.png — baca"},
        session_dir=cfg.data_dir, session_id="",
    )
    assert "tidak ditemukan" in answer
    assert sid == ""                        # never asked the model


def test_file_upload_asks_model_to_read_attachment(cfg):
    p = cfg.data_dir / "uploads" / "r.png"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"png")
    seen = {}

    class _S:
        def ask(self, prompt, *, session_dir, session_id="", on_form=None,
                **kw):
            seen["prompt"] = prompt
            seen["dir"] = session_dir
            return "Ini struk Rp 25.000.", "s1"

    answer, sid = answer_file_upload(
        _S(), cfg, {"text": f"[FILE] {p} — baca ini"},
        session_dir=cfg.data_dir / "uploads", session_id="",
    )
    assert "Ini struk" in answer
    assert "r.png" in seen["prompt"]          # the model is told to Read it
    assert seen["dir"] == cfg.data_dir / "uploads"
    assert sid == "s1"


# ----------------------------------------------------------------- research


class _FakeServer:
    """What the worker needs from OpenCodeServer, minus the model."""

    def __init__(self, replies: dict[str, str]):
        self.replies = replies
        self.prompts: list[str] = []

    def ask(self, prompt, *, session_dir, session_id="", overall_timeout=900.0):
        self.prompts.append(prompt)
        return self.replies.get("default", "Jawaban riset."), "sid-1"


def _seed_queue(tmp_path: Path, text: str) -> None:
    with open(tmp_path / "research_queue.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "ts": 1720000000, "topic": "bonds", "text": text,
            "why": "test", "urgency": "normal", "source": "morning-research",
        }) + "\n")


def test_research_answers_once_through_the_ledger(cfg):
    _seed_queue(cfg.data_dir, "SBN ritel mana yang terbuka?")
    server = _FakeServer({})
    assert process_research(server, cfg, session_dir=cfg.data_dir) == 1
    ledger = [json.loads(row) for row in
              (cfg.data_dir / "research_answers.jsonl").read_text(
                  encoding="utf-8").splitlines()]
    chat = [json.loads(row) for row in
            (cfg.data_dir / "answers.jsonl").read_text(
                encoding="utf-8").splitlines()]
    assert len(ledger) == 1 and ledger[0]["question"].startswith("SBN")
    assert chat[0]["text"] == "Jawaban riset."
    # Second pass: the ledger marks it answered, so nothing is re-asked.
    assert process_research(server, cfg, session_dir=cfg.data_dir) == 0
    assert len(server.prompts) == 1


def test_research_model_failure_is_skipped_not_fatal(cfg):
    _seed_queue(cfg.data_dir, "Berapa BI rate sekarang?")

    class _Broken(_FakeServer):
        def ask(self, *a, **kw):
            raise RuntimeError("server down")

    assert process_research(_Broken({}), cfg, session_dir=cfg.data_dir) == 0
    assert not (cfg.data_dir / "answers.jsonl").exists()


# ------------------------------------------------------------ event stream


def test_sse_payload_handles_both_framings():
    assert _sse_payload('data: {"type":"a"}') == {"type": "a"}
    assert _sse_payload('{"type":"b"}') == {"type": "b"}
    assert _sse_payload('{"payload":{"type":"c"}}') == {"type": "c"}
    assert _sse_payload(": keepalive") is None
    assert _sse_payload("data: not-json") is None


def test_consume_event_accumulates_and_finishes():
    texts: dict[str, list[str]] = {}
    order: list[str] = []
    with httpx.Client(base_url="http://test") as client:
        done, err = _consume_event(
            {"type": "session.text.delta",
             "data": {"sessionID": "s1", "assistantMessageID": "m1",
                      "delta": "Rp "}}, "s1", texts, order, client)
        assert not done and not err
        done, err = _consume_event(
            {"type": "session.text.delta",
             "data": {"sessionID": "s1", "assistantMessageID": "m1",
                      "delta": "36.800"}}, "s1", texts, order, client)
        assert not done
        done, err = _consume_event(
            {"type": "session.execution.succeeded",
             "data": {"sessionID": "s1"}}, "s1", texts, order, client)
        assert done and not err
    assert "".join(texts[order[-1]]) == "Rp 36.800"


def test_consume_event_ignores_other_sessions():
    texts: dict[str, list[str]] = {}
    order: list[str] = []
    with httpx.Client(base_url="http://test") as client:
        done, _ = _consume_event(
            {"type": "session.execution.succeeded",
             "data": {"sessionID": "other"}}, "s1", texts, order, client)
    assert not done                      # another session's end must not end ours


@respx.mock
def test_permission_ask_is_rejected_headless():
    """A permission ask during a headless run must get an explicit reject --
    without it the tool call sits pending forever and hangs the worker."""
    route = respx.post("http://test/api/session/s1/permission/p1/reply").mock(
        return_value=httpx.Response(200))
    with httpx.Client(base_url="http://test") as client:
        done, err = _consume_event(
            {"type": "permission.asked",
             "data": {"sessionID": "s1", "id": "p1", "action": "bash"}},
            "s1", {}, [], client)
    assert not done and not err
    assert route.called
    body = json.loads(route.calls.last.request.content.decode())
    assert body["reply"] == "reject"


def test_model_ids_wire_shape():
    server = OpenCodeServer("opencode2", 42799, "zai-coding-plan/glm-5.3",
                            SimpleNamespace(),
                            instructions=SimpleNamespace())
    assert server._model_ids() == {"providerID": "zai-coding-plan",
                                   "id": "glm-5.3"}
