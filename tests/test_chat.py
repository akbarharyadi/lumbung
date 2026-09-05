"""Tests for the shared chat dispatcher and the upload endpoint.

The point of `chat.py` is that every command Lumbung answers lives in one place,
so the app and the CLI cannot drift apart. What is worth testing is that a
command really is answered locally, that anything else becomes a queued question
rather than an error, and that the upload endpoint cannot be talked into writing
outside its own directory.
"""

from __future__ import annotations

import io
import json

import pytest
from fastapi.testclient import TestClient

from lumbung.chat import dispatch, queue_question, read_answers
from lumbung.config import load_config
from lumbung.web.server import create_app

TOKEN = "test-token"
H = {"Authorization": f"Bearer {TOKEN}"}
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 128


@pytest.fixture
def cfg():
    return load_config()


@pytest.fixture(autouse=True)
def _isolate_uploads(tmp_path, monkeypatch):
    """Keep the upload tests out of the real data directory.

    Without this they wrote real files into data/uploads and real entries into
    the live ask_queue, so running the suite made the running app announce
    questions nobody asked. A test that changes production state is a test you
    stop trusting.
    """
    import lumbung.web.server as srv

    real = srv.load_config()

    class _Cfg:
        def __getattr__(self, name):
            return getattr(real, name)

        @property
        def db_path(self):
            return tmp_path / "test.db"

        @property
        def data_dir(self):
            # Must be overridden too, not inherited. Delegating it sent the
            # queue entry to the real data/ while the files went to tmp -- so
            # the running app announced questions nobody asked, which is the
            # exact failure this fixture exists to prevent.
            return tmp_path

    monkeypatch.setattr(srv, "load_config", lambda *a, **k: _Cfg())
    return tmp_path


# -- dispatch ----------------------------------------------------------------
def test_known_command_is_answered_locally(cfg, tmp_path):
    r = dispatch(cfg, "/about", ask_queue=tmp_path / "q.jsonl")
    assert not r["queued"]
    assert "Lumbung" in r["reply"]
    assert not (tmp_path / "q.jsonl").exists(), "a command must not be queued"


def test_command_aliases_reach_the_same_answer(cfg, tmp_path):
    a = dispatch(cfg, "/todo", ask_queue=tmp_path / "q.jsonl")["reply"]
    b = dispatch(cfg, "/recomendation", ask_queue=tmp_path / "q.jsonl")["reply"]
    assert a == b, "the misspelling people actually type must work identically"


def test_free_form_is_queued_not_refused(cfg, tmp_path):
    q = tmp_path / "q.jsonl"
    r = dispatch(cfg, "should I buy more gold?", ask_queue=q)
    assert r["queued"]
    rows = [json.loads(x) for x in q.read_text(encoding="utf-8").splitlines()]
    assert rows[-1]["text"] == "should I buy more gold?"


def test_unknown_slash_command_is_treated_as_a_question(cfg, tmp_path):
    """A typo should be answerable, not rejected."""
    q = tmp_path / "q.jsonl"
    r = dispatch(cfg, "/wat do i do about bbca", ask_queue=q)
    assert r["queued"]


def test_empty_message_does_nothing(cfg, tmp_path):
    r = dispatch(cfg, "   ", ask_queue=tmp_path / "q.jsonl")
    assert r["reply"] == ""
    assert not r["queued"]


def test_a_failing_command_does_not_kill_the_chat(cfg, tmp_path):
    """A bad argument returns a message, never an exception."""
    r = dispatch(cfg, "/spend notanumber", ask_queue=tmp_path / "q.jsonl")
    assert isinstance(r["reply"], str) and r["reply"]


def test_queue_and_read_round_trip(tmp_path):
    p = tmp_path / "q.jsonl"
    assert queue_question(p, "hello", source="app")
    rows = read_answers(p)
    assert rows[-1]["text"] == "hello"
    assert rows[-1]["source"] == "app"


def test_reading_a_missing_file_is_empty_not_an_error(tmp_path):
    assert read_answers(tmp_path / "nope.jsonl") == []


def test_corrupt_lines_are_skipped(tmp_path):
    p = tmp_path / "q.jsonl"
    p.write_text('{"ts":1,"text":"good"}\nnot json\n', encoding="utf-8")
    rows = read_answers(p)
    assert len(rows) == 1


# -- /asset -------------------------------------------------------------------
@pytest.fixture
def _holdings(tmp_path, monkeypatch):
    """Isolate the holdings file, the way the settings tests do."""
    import shutil

    import lumbung.web.settings as st
    from lumbung.config import PROJECT_ROOT

    dst = tmp_path / "holdings.yaml"
    shutil.copy(PROJECT_ROOT / "config" / "holdings.yaml", dst)
    monkeypatch.setattr(st, "_holdings_path", lambda: dst)
    return st


def test_asset_tolerates_prose_after_the_value(cfg, tmp_path, _holdings):
    """The command that failed in the chat: trailing words are a note, not a
    rate. Nothing about "saldo Tahapan 5245046607" should block the save."""
    r = dispatch(cfg, "/asset Cash savings 21.4jt saldo Tahapan 5245046607",
                 ask_queue=tmp_path / "q.jsonl")
    assert "added" in r["reply"]
    row = [a for a in _holdings.read_assets() if a["name"] == "Cash"][0]
    assert row["value_idr"] == 21_400_000
    assert row["rate"] == 0.0, "an account number must never land in rate"
    assert "saldo Tahapan 5245046607" in row["note"]


def test_asset_still_takes_a_plain_rate(cfg, tmp_path, _holdings):
    r = dispatch(cfg, "/asset TestDep savings 15jt 6.5",
                 ask_queue=tmp_path / "q.jsonl")
    assert "added" in r["reply"]
    row = [a for a in _holdings.read_assets() if a["name"] == "TestDep"][0]
    assert row["value_idr"] == 15_000_000
    assert row["rate"] == pytest.approx(0.065)


def test_asset_pct_rate_updates_rate_without_touching_value(cfg, tmp_path, _holdings):
    """A bare "4.25%" is a rate, never a Rp 4 revaluation."""
    r = dispatch(cfg, "/asset Superbank savings 4.25%",
                 ask_queue=tmp_path / "q.jsonl")
    assert "updated" in r["reply"]
    row = [a for a in _holdings.read_assets() if a["name"] == "Superbank"][0]
    assert row["value_idr"] == 10_000_000
    assert row["rate"] == pytest.approx(0.0425)


def test_asset_prose_error_is_readable(cfg, tmp_path, _holdings):
    """No numbers at all on a new asset: a sentence, not a traceback."""
    r = dispatch(cfg, "/asset Cash savings saldo aja",
                 ask_queue=tmp_path / "q.jsonl")
    assert "Could not save" in r["reply"]
    assert "float" not in r["reply"]


# -- uploads -----------------------------------------------------------------
def test_upload_accepts_an_image():
    c = TestClient(create_app(token=TOKEN, readonly=True))
    r = c.post("/api/chat/upload", headers=H,
               files={"file": ("receipt.png", io.BytesIO(PNG), "image/png")})
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_upload_rejects_an_executable():
    c = TestClient(create_app(token=TOKEN, readonly=True))
    r = c.post("/api/chat/upload", headers=H,
               files={"file": ("evil.exe", io.BytesIO(b"MZ"), "application/octet-stream")})
    assert r.status_code == 400


def test_upload_neutralises_a_traversal_filename():
    """A crafted name must not escape the uploads directory."""
    c = TestClient(create_app(token=TOKEN, readonly=True))
    r = c.post("/api/chat/upload", headers=H,
               files={"file": ("../../etc/passwd.png", io.BytesIO(PNG), "image/png")})
    assert r.status_code == 200
    stored = r.json()["stored"]
    assert ".." not in stored
    assert "/" not in stored and "\\" not in stored


def test_upload_rejects_an_oversized_file():
    c = TestClient(create_app(token=TOKEN, readonly=True))
    big = b"\x89PNG\r\n\x1a\n" + b"\0" * (13 * 1024 * 1024)
    r = c.post("/api/chat/upload", headers=H,
               files={"file": ("big.png", io.BytesIO(big), "image/png")})
    assert r.status_code == 400


def test_upload_with_no_file_is_refused():
    c = TestClient(create_app(token=TOKEN, readonly=True))
    assert c.post("/api/chat/upload", headers=H, data={}).status_code == 400
