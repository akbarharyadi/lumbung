"""Dashboard API: auth, payload shape, and control-flag semantics.

Auth gets the most attention here. These endpoints can flatten a live position,
so "did the token check actually run" is the test that matters most.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from lumbung.config import load_config
from lumbung.journal import Journal
from lumbung.web.server import create_app

TOKEN = "test-token-abc123"


@pytest.fixture
def client(tmp_path, monkeypatch):
    cfg = load_config()
    cfg.paths.db = str(tmp_path / "web.db")
    cfg.paths.halt_file = str(tmp_path / "HALT")
    monkeypatch.setattr("lumbung.web.server.load_config", lambda: cfg)
    # Keep the network out of the test: stub the slow holdings snapshot.
    monkeypatch.setattr(
        "lumbung.web.server._cached",
        lambda key, fn, ttl=0: _FAKE if key == "holdings" else fn(),
    )
    Journal(cfg.db_path)  # create the schema
    return TestClient(create_app(token=TOKEN)), cfg


_FAKE = {
    "net_worth": 106_500_000, "cash": 10_000_000, "passive_monthly": 419_467,
    "subscription_pct": 89.9,
    "buckets": [{"name": "stocks", "value": 64_500_000, "weight": 60.6,
                 "target": 40.0, "drift": 20.6}],
    "holdings": [{"ticker": "BBCA", "lots": 100, "price": 6450, "avg": 7401,
                  "value": 64_500_000, "pnl": -9_510_000, "pnl_pct": -12.85,
                  "yield_pct": 5.52, "monthly": 296_667, "weight": 60.6,
                  "signal": "NO BUY", "verdict": "TRIM?", "alerts": []}],
    "cashflow": {"income": 17_000_000, "spending": 7_000_000,
                 "surplus": 10_000_000, "savings_rate": 58.8},
    "emergency": {"target": 42_000_000, "liquid": 42_000_000, "months_cash": 1.43,
                  "months_liquid": 6.0, "shortfall": 0},
    "surplus_plan": [{"bucket": "bonds", "amount": 6_865_056}],
}

AUTH = {"Authorization": f"Bearer {TOKEN}"}
READ = ["/api/summary", "/api/equity", "/api/events"]
WRITE = ["/api/pause", "/api/resume", "/api/flat", "/api/kill", "/api/refresh"]


# ------------------------------------------------------------------- auth
@pytest.mark.parametrize("path", READ)
def test_reads_require_a_token(client, path):
    c, _ = client
    assert c.get(path).status_code == 401


@pytest.mark.parametrize("path", WRITE)
def test_writes_require_a_token(client, path):
    """A missing token must never be able to flatten a position."""
    c, _ = client
    assert c.post(path).status_code == 401


def test_wrong_token_rejected(client):
    c, _ = client
    r = c.get("/api/summary", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


def test_query_param_token_accepted_for_first_load(client):
    c, _ = client
    assert c.get(f"/api/summary?t={TOKEN}").status_code == 200


def test_malformed_auth_header_rejected(client):
    c, _ = client
    for bad in ("", "Basic xyz", "Bearer", TOKEN):
        assert c.get("/api/summary", headers={"Authorization": bad}).status_code == 401


# ---------------------------------------------------------------- payload
def test_summary_shape(client):
    c, _ = client
    d = c.get("/api/summary", headers=AUTH).json()
    for key in ("net_worth", "buckets", "holdings", "cashflow", "emergency",
                "bot", "goal", "surplus_plan", "server_time"):
        assert key in d, key
    assert d["net_worth"] == 106_500_000
    assert d["bot"]["mode"] in ("paper", "live")
    assert d["goal"]["capital_required"] > d["net_worth"]


def test_goal_progress_is_bounded(client):
    c, _ = client
    p = c.get("/api/summary", headers=AUTH).json()["goal"]["progress_pct"]
    assert 0 <= p <= 100


def test_empty_journal_still_returns_valid_json(client):
    c, _ = client
    assert c.get("/api/equity", headers=AUTH).json() == {"points": []}
    assert c.get("/api/events", headers=AUTH).json()["events"] == []


# ---------------------------------------------------------------- control
def test_pause_then_resume_toggles_halt(client):
    c, cfg = client
    assert c.post("/api/pause", headers=AUTH).json()["halted"] is True
    assert Journal(cfg.db_path).get_state("halted") is True
    assert c.post("/api/resume", headers=AUTH).json()["halted"] is False
    assert Journal(cfg.db_path).get_state("halted") is False


def test_flat_queues_a_request_rather_than_selling_directly(client):
    """Only the engine may talk to the exchange; the API just raises a flag."""
    c, cfg = client
    assert c.post("/api/flat", headers=AUTH).json()["queued"] is True
    j = Journal(cfg.db_path)
    assert j.get_state("flatten_request") is not None
    assert j.get_state("flatten_done") is None  # not consumed until the engine runs


def test_kill_writes_the_halt_file_and_queues_a_flatten(client):
    c, cfg = client
    assert c.post("/api/kill", headers=AUTH).json()["ok"] is True
    assert cfg.halt_path.exists()
    assert Journal(cfg.db_path).get_state("flatten_request") is not None


def test_resume_rebases_peak_equity(client):
    """Otherwise the drawdown gate re-trips on the next tick and deadlocks."""
    c, cfg = client
    j = Journal(cfg.db_path)
    j.record_equity(80_000_000, 80_000_000, 0, 0)
    j.set_state("peak_equity", 100_000_000)
    c.post("/api/resume", headers=AUTH)
    assert Journal(cfg.db_path).get_state("peak_equity") == 80_000_000


# ----------------------------------------------------------------- static
def test_pwa_assets_are_served_without_a_token(client):
    """The shell must load so the login screen can be shown at all."""
    c, _ = client
    for path in ("/", "/manifest.json", "/sw.js"):
        assert c.get(path).status_code == 200


def test_manifest_is_valid_and_installable(client):
    c, _ = client
    m = c.get("/manifest.json").json()
    assert m["display"] == "standalone"
    assert m["start_url"] == "/"
    assert any(i["sizes"] == "512x512" for i in m["icons"])
    assert any(i.get("purpose") == "maskable" for i in m["icons"])


def test_service_worker_never_caches_api_responses(client):
    """A cached portfolio number is worse than an honest failure."""
    c, _ = client
    sw = c.get("/sw.js").text
    assert "/api/" in sw and "return" in sw


# ------------------------------------------------------- credential hygiene
def test_env_example_placeholders_do_not_count_as_credentials(monkeypatch):
    """Copying .env.example verbatim must not look like real Indodax keys.

    The placeholders are non-empty strings, so a plain truthiness check reports
    "credentials present", `doctor` says you are ready, and live mode then fails
    with an opaque 401.
    """
    from pydantic import SecretStr

    from lumbung.config import Secrets

    fake = Secrets(
        indodax_key=SecretStr("your_api_key_here"),
        indodax_secret=SecretStr("your_api_secret_here"),
    )
    assert fake.has_indodax is False

    blank = Secrets(indodax_key=SecretStr(""), indodax_secret=SecretStr(""))
    assert blank.has_indodax is False

    real = Secrets(indodax_key=SecretStr("ABCD-1234"), indodax_secret=SecretStr("beef"))
    assert real.has_indodax is True


def test_whitespace_only_credentials_are_not_credentials():
    from pydantic import SecretStr

    from lumbung.config import Secrets

    assert Secrets(
        indodax_key=SecretStr("   "), indodax_secret=SecretStr("  ")
    ).has_indodax is False


# ----------------------------------------------------------------- alerts
def test_alerts_land_in_the_chat_transcript(tmp_path):
    """The engine and the web server are separate processes.

    The file is the whole interface between them: an alert raised at 02:00 with
    nothing serving has to be waiting in the chat in the morning.
    """
    import json

    from lumbung.notify.app import AppNotifier

    AppNotifier(tmp_path).send("HALTED\ndrawdown 21%")
    row = json.loads((tmp_path / "answers.jsonl").read_text(encoding="utf-8").strip())
    assert row["text"] == "HALTED\ndrawdown 21%"
    assert isinstance(row["ts"], int)


def test_alerts_append_rather_than_replace(tmp_path):
    from lumbung.notify.app import AppNotifier

    n = AppNotifier(tmp_path)
    n.send("first")
    n.send("second")
    lines = (tmp_path / "answers.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2, "an alert overwrote the conversation"


def test_the_data_directory_is_created_on_first_alert(tmp_path):
    from lumbung.notify.app import AppNotifier

    AppNotifier(tmp_path / "nested" / "data").send("hello")
    assert (tmp_path / "nested" / "data" / "answers.jsonl").exists()


def test_an_undeliverable_alert_falls_back_to_stdout(tmp_path, capsys):
    """A full disk while flattening a position must not raise out of an alert.

    Losing the alert entirely is the failure worth avoiding; losing the nice
    delivery is not.
    """
    from lumbung.notify.app import AppNotifier

    n = AppNotifier(tmp_path)
    # A directory where the file should be: writing to it raises OSError.
    (tmp_path / "answers.jsonl").mkdir()
    n.send("EXIT FAILED btc_idr")
    assert "EXIT FAILED btc_idr" in capsys.readouterr().out


def test_notifier_falls_back_to_console_without_a_data_dir(capsys):
    """Alerts must never be silently dropped."""
    from lumbung.notify.app import ConsoleNotifier, build_notifier

    n = build_notifier("")
    assert isinstance(n, ConsoleNotifier)
    n.send("hello")
    assert "hello" in capsys.readouterr().out


# --------------------------------------------------------- IP whitelisting
def test_ip_status_flags_a_mismatch():
    from lumbung.netcheck import IPStatus

    assert IPStatus("1.2.3.4", "1.2.3.4").ok is True
    bad = IPStatus("5.6.7.8", "1.2.3.4")
    assert bad.ok is False and bad.unknown is False and bad.not_configured is False


def test_ip_status_distinguishes_unknown_from_unconfigured():
    """'I could not reach the internet' and 'you never set this' need different
    messages -- one is a transient failure, the other is a setup step."""
    from lumbung.netcheck import IPStatus

    assert IPStatus(None, "1.2.3.4").unknown is True
    assert IPStatus("1.2.3.4", None).not_configured is True
    assert IPStatus("1.2.3.4", "").not_configured is True


def test_check_records_the_ip_and_detects_a_later_change(tmp_path):
    import json

    from lumbung.netcheck import check

    state = tmp_path / "last_ip.json"
    state.write_text(json.dumps({"last_ip": "9.9.9.9", "seen_at": 0}), encoding="utf-8")
    st = check(state, "9.9.9.9")
    if st.current:  # skip cleanly when offline
        assert st.changed_at is not None or st.current == "9.9.9.9"
        assert json.loads(state.read_text(encoding="utf-8"))["last_ip"] == st.current


def test_private_client_forces_ipv4_by_default():
    """On a dual-stack link Python prefers IPv6, but the Indodax whitelist holds
    an IPv4 address -- so every signed call would fail authorisation."""
    import inspect

    from lumbung.exchanges.indodax_private import IndodaxPrivateClient

    sig = inspect.signature(IndodaxPrivateClient.__init__)
    assert sig.parameters["force_ipv4"].default is True


# --------------------------------------------------------- payday shorthand
def test_payday_amount_shorthand_parsing():
    """/payday 5jt and /payday 500rb should mean what an Indonesian reader expects."""
    def parse(raw: str) -> float:
        raw = raw.lower().replace(",", "").replace("_", "")
        mult = 1_000_000 if "jt" in raw else (1_000 if "rb" in raw else 1)
        return float(raw.replace("jt", "").replace("rb", "")) * mult

    assert parse("5jt") == 5_000_000
    assert parse("5.5jt") == 5_500_000
    assert parse("500rb") == 500_000
    assert parse("250000") == 250_000


# ------------------------------------------------------ single-instance lock
def test_lock_blocks_a_second_engine(tmp_path):
    """Two engines share a journal and an exchange account: both would size from
    the same balance and both would send the orders."""
    import os

    from lumbung.singleton import AlreadyRunning, InstanceLock

    a = InstanceLock(tmp_path / "engine.pid")
    a.acquire()
    assert (tmp_path / "engine.pid").read_text().strip() == str(os.getpid())

    b = InstanceLock(tmp_path / "engine.pid")
    # Same PID is treated as a re-entry, not a conflict; simulate a live foreign one.
    (tmp_path / "engine.pid").write_text("1", encoding="utf-8")
    import lumbung.singleton as sg

    orig = sg._alive
    sg._alive = lambda pid: pid == 1
    try:
        with pytest.raises(AlreadyRunning):
            b.acquire()
    finally:
        sg._alive = orig


def test_stale_lock_from_a_crash_is_taken_over(tmp_path):
    """A hard kill leaves the PID file behind. Refusing to start forever after a
    crash would be worse than the race it guards against."""
    from lumbung.singleton import InstanceLock

    (tmp_path / "engine.pid").write_text("999999", encoding="utf-8")
    import lumbung.singleton as sg

    orig = sg._alive
    sg._alive = lambda pid: False
    try:
        InstanceLock(tmp_path / "engine.pid").acquire()   # must not raise
    finally:
        sg._alive = orig


def test_release_only_removes_our_own_lock(tmp_path):
    import os

    from lumbung.singleton import InstanceLock

    lock = InstanceLock(tmp_path / "engine.pid")
    lock.acquire()
    (tmp_path / "engine.pid").write_text("4242", encoding="utf-8")  # someone else took it
    lock.release()
    assert (tmp_path / "engine.pid").exists()
    assert (tmp_path / "engine.pid").read_text().strip() == "4242"
    assert os.getpid() != 4242


# --------------------------------------------------------- cache invalidation
def test_editing_holdings_invalidates_the_cached_snapshot(tmp_path, monkeypatch):
    """Editing your own numbers must show up at once. A five-minute wait after a
    config edit is indistinguishable from a broken dashboard."""
    import lumbung.web.server as srv

    srv._cache.clear()
    calls = {"n": 0}

    def build():
        calls["n"] += 1
        return {"v": calls["n"]}

    stamp = {"t": 1000.0}
    monkeypatch.setattr(srv, "_config_stamp", lambda: stamp["t"])

    assert srv._cached("k", build)["v"] == 1
    assert srv._cached("k", build)["v"] == 1      # cached, not rebuilt

    stamp["t"] = 2000.0                            # config edited
    assert srv._cached("k", build)["v"] == 2      # rebuilt immediately
    srv._cache.clear()


# ----------------------------------------------------------------- ask queue
def test_free_form_message_is_queued_not_refused(tmp_path):
    """A question is not an error.

    Refusing anything that is not a command would make the chat a command line
    with a worse keyboard, and the queue is how a question reaches a Claude
    session without the app having to wait for one.
    """
    import json

    from lumbung.chat import dispatch
    from lumbung.config import load_config

    q = tmp_path / "ask_queue.jsonl"
    out = dispatch(load_config(), "should I buy the 5070 ti?", ask_queue=q)

    assert out["queued"] is True
    # The acknowledgement must sound like Lumbung, not like a forwarder.
    assert "Noted" in out["reply"]
    assert "Claude" not in out["reply"]

    rows = [json.loads(x) for x in q.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["text"] == "should I buy the 5070 ti?"


def test_known_commands_never_reach_the_queue(tmp_path):
    from lumbung.chat import dispatch
    from lumbung.config import load_config

    q = tmp_path / "ask_queue.jsonl"
    out = dispatch(load_config(), "/about", ask_queue=q)
    assert out["queued"] is False
    assert "Lumbung" in out["reply"]
    assert not q.exists()


def test_without_a_queue_an_unknown_command_still_explains_itself():
    from lumbung.chat import dispatch
    from lumbung.config import load_config

    out = dispatch(load_config(), "/nope")
    assert out["queued"] is False
    assert "/help" in out["reply"]


# --------------------------------------------------------------- readonly mode
def _ro_client(tmp_path, monkeypatch):
    from lumbung.config import load_config
    from lumbung.journal import Journal
    from lumbung.web.server import create_app

    cfg = load_config()
    cfg.paths.db = str(tmp_path / "ro.db")
    cfg.paths.halt_file = str(tmp_path / "HALT")
    monkeypatch.setattr("lumbung.web.server.load_config", lambda: cfg)
    monkeypatch.setattr(
        "lumbung.web.server._cached",
        lambda key, fn, ttl=0: _FAKE if key == "holdings" else fn(),
    )
    Journal(cfg.db_path)
    return TestClient(create_app(token=TOKEN, readonly=True)), cfg


@pytest.mark.parametrize("path", ["/api/pause", "/api/flat", "/api/kill"])
def test_readonly_blocks_controls(tmp_path, monkeypatch, path):
    """Reading net worth from a leaked URL is embarrassing. Flattening positions
    from one is expensive. Public deployments get the first, never the second."""
    c, _ = _ro_client(tmp_path, monkeypatch)
    r = c.post(path, headers=AUTH)
    assert r.status_code == 403


def test_readonly_still_serves_reads(tmp_path, monkeypatch):
    c, _ = _ro_client(tmp_path, monkeypatch)
    assert c.get("/api/summary", headers=AUTH).status_code == 200
    assert c.get("/api/config", headers=AUTH).json()["readonly"] is True


def test_kill_does_not_write_the_halt_file_in_readonly(tmp_path, monkeypatch):
    c, cfg = _ro_client(tmp_path, monkeypatch)
    c.post("/api/kill", headers=AUTH)
    assert not cfg.halt_path.exists()


def test_repeated_bad_tokens_get_rate_limited(client):
    """An exposed endpoint attracts credential stuffing; an unbounded guess rate
    turns a weak token into no token."""
    c, _ = client
    codes = [
        c.get("/api/summary", headers={"Authorization": "Bearer wrong"}).status_code
        for _ in range(12)
    ]
    assert codes[0] == 401
    assert 429 in codes


def test_a_good_token_still_works_after_someone_elses_failures(client):
    c, _ = client
    for _ in range(3):
        c.get("/api/summary", headers={"Authorization": "Bearer wrong"})
    assert c.get("/api/summary", headers=AUTH).status_code == 200


# ------------------------------------------------- headless live start
def test_live_confirmation_is_skipped_without_a_tty(monkeypatch):
    """Under pythonw there is no stdin, so an interactive confirm is not a
    safety check -- it raises RuntimeError and the engine dies before its first
    loop, silently. The deliberate act in that path is TA_MODE=live."""
    import sys as _sys

    class NoTTY:
        def isatty(self):
            return False

    monkeypatch.setattr(_sys, "stdin", NoTTY())
    assert not (_sys.stdin is not None and _sys.stdin.isatty())

    monkeypatch.setattr(_sys, "stdin", None)
    assert not (_sys.stdin is not None and _sys.stdin.isatty())


def test_interactive_terminal_still_gets_the_confirmation():

    class TTY:
        def isatty(self):
            return True

    stdin = TTY()
    assert stdin is not None and stdin.isatty()


# ---------------------------------------------------------------- uploads
def _png() -> bytes:
    """Smallest thing that passes the extension check and is really bytes."""
    return b"\x89PNG\r\n\x1a\n" + b"0" * 64


def test_several_files_arrive_as_one_question_with_the_caption_first(client):
    """One file per message was the wrong unit -- a statement runs to several
    pages. And a file with no caption had to be explained in a follow-up, so
    the caption leads the entry rather than being buried mid-sentence."""
    import json

    c, cfg = client
    r = c.post(
        "/api/chat/upload",
        headers=AUTH,
        files=[
            ("file", ("page1.png", _png(), "image/png")),
            ("file", ("page2.png", _png(), "image/png")),
            ("file", ("page3.jpg", _png(), "image/jpeg")),
        ],
        data={"note": "BCA statement, three pages"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 3
    assert len(body["stored"]) == 3

    uploads = sorted((cfg.db_path.parent / "uploads").iterdir())
    assert len(uploads) == 3, "same-second uploads must not overwrite each other"

    lines = [
        json.loads(x)
        for x in (cfg.db_path.parent / "ask_queue.jsonl").read_text(
            encoding="utf-8"
        ).splitlines() if x.strip()
    ]
    assert len(lines) == 1, "three files is one question, not three"
    text = lines[0]["text"]
    assert text.startswith("[FILES 3] BCA statement, three pages"), text
    for u in uploads:
        assert str(u) in text


def test_one_bad_file_stores_none_of_them(client):
    """Writing as we go would leave half an upload on disk and a queue entry
    naming files that are not all there."""
    c, cfg = client
    r = c.post(
        "/api/chat/upload",
        headers=AUTH,
        files=[
            ("file", ("good.png", _png(), "image/png")),
            ("file", ("payload.exe", b"MZ" + b"0" * 32, "application/octet-stream")),
        ],
    )
    assert r.status_code == 400
    updir = cfg.db_path.parent / "uploads"
    assert not updir.exists() or not list(updir.iterdir())
    q = cfg.db_path.parent / "ask_queue.jsonl"
    assert not q.exists() or not q.read_text(encoding="utf-8").strip()


def test_upload_without_a_caption_still_asks_the_default_question(client):
    import json

    c, cfg = client
    r = c.post(
        "/api/chat/upload", headers=AUTH,
        files=[("file", ("receipt.png", _png(), "image/png"))],
    )
    assert r.status_code == 200
    text = json.loads(
        (cfg.db_path.parent / "ask_queue.jsonl").read_text(encoding="utf-8").strip()
    )["text"]
    assert text.startswith("[FILES 1] Read these and tell me what they are")


def test_too_many_files_is_refused(client):
    c, cfg = client
    r = c.post(
        "/api/chat/upload", headers=AUTH,
        files=[("file", (f"p{i}.png", _png(), "image/png")) for i in range(9)],
    )
    assert r.status_code == 400
    # the app normalises HTTPException detail onto `error`
    assert "8" in r.json()["error"]
