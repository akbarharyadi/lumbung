"""OpenCode 2 as a local service: one `opencode2 serve`, text in, text out.

The pattern is proven in ~/AppWork/claude-code-telegram (opencode_runner.py);
this is that, reduced to what Lumbung needs: no Telegram, no approval buttons,
no streaming UI. The beta is driven through the same local HTTP API its TUI
uses -- a subprocess run headlessly cannot answer a permission ask, so the
server is the only way in.

The agent created here is deliberately powerless:

* its session directory is an empty scratch folder under data/, so the Read
  tool has nothing to expose. Portfolio numbers reach the model inside the
  prompt, computed by deterministic code -- the model never reads files, which
  keeps .env and source out of reach no matter what a web page it fetched
  tells it to do;
* bash / edit / write / patch are denied in the server config, and a permission
  ask that slips through anyway is auto-rejected: headless, nobody is home;
* webfetch / websearch stay allowed -- research questions are exactly the ones
  that need the web (ex-dividend dates, SBN quotas, BI/LPS changes).

Everything stateful -- the queues, the answers, the journal -- is touched by
Python, never by the model.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

_SERVER_START_TIMEOUT = 60.0
# Per-chunk read timeout on the event stream: a silent stream is a dead
# stream, but a thinking model can legitimately pause long between deltas.
_STREAM_READ_TIMEOUT = 120.0


@dataclass(frozen=True)
class AgentSettings:
    """Everything the agent backends need, from .env. Empty model = disabled."""

    bin_path: str          # "" resolves to "opencode2" on PATH
    port: int
    model: str             # "provider/model", e.g. zai-coding-plan/glm-5.3
    worker_enabled: bool

    @classmethod
    def from_secrets(cls, secrets) -> AgentSettings | None:
        model = (secrets.opencode_model or "").strip()
        if not model:
            return None
        return cls(
            bin_path=(secrets.opencode_bin or "").strip() or "opencode2",
            port=int(secrets.opencode_port or 42778),
            model=model,
            worker_enabled=(secrets.agent_worker or "").strip() == "1",
        )


def server_permission_config() -> dict:
    """Fail-closed permission gate for the server, mirroring the belt on the
    Telegram bot: nothing that changes state, no file reads outside an empty
    scratch dir (there is nothing inside it either), web lookups allowed."""
    return {
        "bash": "deny",
        "edit": "deny",
        "write": "deny",
        "patch": "deny",
        "webfetch": "allow",
        "websearch": "allow",
        "external_directory": "deny",
        "doom_loop": "allow",       # refusing it would only spam the log
        # Note: the model's question tool is deliberately NOT denied here --
        # this beta ignores unknown permission keys anyway, and a clarifying
        # question is now delivered to the chat instead of cancelled into
        # silence (agent_worker passes on_form).
    }


class OpenCodeServer:
    """One `opencode2 serve` per port, adopted across restarts.

    The server prints its credentials to stdout (`server password <pw>`); they
    are persisted so a restarted worker adopts the healthy server it spawned
    before instead of piling processes onto the same port.
    """

    def __init__(
        self,
        bin_path: str,
        port: int,
        model: str,
        state_dir: Path,
        *,
        instructions: Path,
    ) -> None:
        self.bin_path = bin_path
        self.port = int(port)
        self.model = model
        self.state_dir = state_dir
        self.instructions = instructions
        self._proc: subprocess.Popen | None = None
        self._password = ""
        self._config_fingerprint = ""

    # ── lifecycle ────────────────────────────────────────────────────────

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _config_content(self) -> str:
        """Inline server config (OPENCODE_CONFIG_CONTENT): the permission gate
        plus the Lumbung voice as instructions. Server-scoped, so it is rebuilt
        only when something that affects it changes."""
        return json.dumps({
            "permission": server_permission_config(),
            "instructions": [str(self.instructions)],
        })

    def _credentials_file(self) -> Path:
        return self.state_dir / "opencode-server.json"

    def _bin_command(self) -> list[str]:
        """npm/pnpm shims on Windows are .cmd files, which only cmd.exe runs."""
        if self.bin_path.lower().endswith((".cmd", ".bat")):
            return ["cmd", "/c", self.bin_path]
        return [self.bin_path]

    def ensure(self) -> str:
        """Make sure a healthy server we can authenticate against is running.
        Returns the password."""
        fingerprint = self._config_content()
        stored = self._read_credentials()

        if stored and self._healthy(stored["password"]):
            if stored.get("config") == fingerprint:
                self._password = stored["password"]
                self._config_fingerprint = fingerprint
                return self._password
            # The running server still gates with the old config; replace it.
            self._kill_stored_pid(stored)

        if self._proc is not None and self._proc.poll() is None:
            self._kill()
        self._wait_port_free()

        env = dict(os.environ)
        env["OPENCODE_CONFIG_CONTENT"] = fingerprint
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        password = ""
        for attempt in range(2):
            self._proc = subprocess.Popen(
                self._popen_args(),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=env,
                creationflags=flags,
            )
            password = self._read_password(_SERVER_START_TIMEOUT)
            if password:
                break
            if attempt == 0:
                # Usually the port was still held for a moment by the server
                # we just replaced; give the socket a beat and try once more.
                time.sleep(2.0)
                self._wait_port_free()

        if not password:
            self._kill()
            raise RuntimeError(
                "opencode2 serve exited before printing a password. "
                "Run `opencode2 serve` by hand to see why."
            )

        self._password = password
        self._config_fingerprint = fingerprint
        self._write_credentials(password, fingerprint)

        deadline = time.monotonic() + _SERVER_START_TIMEOUT
        while time.monotonic() < deadline:
            if self._healthy(password):
                return password
            time.sleep(0.4)

        self._kill()
        raise RuntimeError(
            f"opencode2 server did not come up on port {self.port} "
            f"within {int(_SERVER_START_TIMEOUT)}s."
        )

    def _popen_args(self) -> list[str]:
        return [*self._bin_command(), "serve", "--hostname", "127.0.0.1",
                "--port", str(self.port)]

    def _read_password(self, timeout: float) -> str:
        """Read stdout until the server names its password, with a deadline.
        A reader thread keeps a silent server from hanging the worker."""
        q: list[str] = []
        assert self._proc is not None and self._proc.stdout is not None

        def _reader() -> None:
            try:
                for raw in self._proc.stdout:          # type: ignore[union-attr]
                    text = raw.decode("utf-8", "replace").strip()
                    if text.startswith("server password "):
                        q.append(text.removeprefix("server password ").strip())
                        return
            except (OSError, ValueError):
                pass

        t = threading.Thread(target=_reader, daemon=True)
        t.start()
        t.join(timeout)
        return q[0] if q else ""

    def _read_credentials(self) -> dict | None:
        try:
            data = json.loads(self._credentials_file().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict) or not data.get("password"):
            return None
        return data

    def _write_credentials(self, password: str, fingerprint: str) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        pid = self._proc.pid if self._proc is not None else None
        self._credentials_file().write_text(
            json.dumps({"url": self.base_url, "password": password,
                        "pid": pid, "config": fingerprint}),
            encoding="utf-8",
        )

    def _kill_stored_pid(self, stored: dict) -> None:
        pid = stored.get("pid")
        if not isinstance(pid, int) or pid <= 0:
            return
        try:
            os.kill(pid, 9)
        except OSError:
            pass

    def _healthy(self, password: str) -> bool:
        try:
            with httpx.Client(timeout=2.0, auth=("opencode", password)) as client:
                return client.get(f"{self.base_url}/api/project").status_code == 200
        except httpx.HTTPError:
            return False

    def _kill(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            try:
                self._proc.kill()
            except OSError:
                pass
        self._proc = None

    def _wait_port_free(self, timeout: float = 8.0) -> None:
        """Block until the port accepts a bind, so a just-replaced server's
        lingering socket cannot fail the fresh spawn."""
        import socket

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    sock.bind(("127.0.0.1", self.port))
                    return
                except OSError:
                    pass
            time.sleep(0.4)

    def close(self) -> None:
        self._kill()

    # ── the API surface ──────────────────────────────────────────────────

    def _model_ids(self) -> dict:
        """`provider/model` -> the wire shape the beta wants: {providerID, id}."""
        provider, _, model_id = self.model.partition("/")
        return {"providerID": provider, "id": model_id}

    def _create_session(self, client: httpx.Client, directory: Path) -> str:
        body: dict = {
            "title": "lumbung-agent",
            "location": {"directory": str(directory)},
            "model": self._model_ids(),
        }
        r = client.post(f"{self.base_url}/api/session", json=body)
        if r.status_code in {400, 422}:
            # Older beta: location was a plain directory string.
            body["location"] = str(directory)
            r = client.post(f"{self.base_url}/api/session", json=body)
        r.raise_for_status()
        data = r.json()
        sid = str(((data.get("data") or data) or {}).get("id") or "")
        if not sid:
            raise RuntimeError(f"opencode2 returned no session id: {data}")
        return sid

    def ask(
        self,
        prompt: str,
        *,
        session_dir: Path,
        session_id: str = "",
        overall_timeout: float = 900.0,
        on_form=None,
    ) -> tuple[str, str]:
        """One turn: post the prompt, ride the event stream to the end.

        Returns (text, session_id). `session_id` resumes a conversation; a
        stale id (session gone after a server restart) is recreated silently.
        A permission ask is rejected and a question form cancelled -- nothing
        headless can answer them, so they must never hang the worker. When
        `on_form` is given, each question the model asks is handed to it as
        the form is cancelled, so the caller can show it to a human.
        """
        password = self.ensure()
        timeout = httpx.Timeout(connect=10.0, read=_STREAM_READ_TIMEOUT,
                                write=30.0, pool=30.0)
        auth = ("opencode", password)
        deadline = time.monotonic() + overall_timeout

        with httpx.Client(timeout=timeout, auth=auth) as client:
            with client.stream("GET", f"{self.base_url}/api/event") as stream:
                sid = session_id
                fresh = False
                if not sid:
                    sid = self._create_session(client, session_dir)
                    fresh = True
                try:
                    r = client.post(f"{self.base_url}/api/session/{sid}/prompt",
                                    json={"text": prompt})
                    if r.status_code == 404 and not fresh:
                        # Session from before a server restart -- start over.
                        sid = self._create_session(client, session_dir)
                        r = client.post(f"{self.base_url}/api/session/{sid}/prompt",
                                        json={"text": prompt})
                    r.raise_for_status()
                except httpx.HTTPError:
                    if fresh:
                        self._abort(sid)
                    raise

                texts: dict[str, list[str]] = {}
                order: list[str] = []
                forms: list[str] = []
                error = ""
                try:
                    for raw in stream.iter_lines():
                        if time.monotonic() > deadline:
                            error = (f"timed out after {int(overall_timeout)}s "
                                     "waiting for the model")
                            break
                        payload = _sse_payload(raw)
                        if not payload:
                            continue
                        done, err = _consume_event(
                            payload, sid, texts, order, client, forms)
                        if forms and on_form is not None:
                            for q in forms:
                                try:
                                    on_form(q)
                                except Exception:  # noqa: BLE001
                                    log.warning("on_form callback failed",
                                                exc_info=True)
                            forms.clear()
                        if done:
                            break
                        if err:
                            error = err
                            break
                except httpx.HTTPError as exc:
                    error = f"opencode2 server error: {exc}"

            if error:
                self._abort(sid)
                raise RuntimeError(error)

        final = ""
        if order:
            final = "".join(texts[order[-1]]).strip()
        return final, sid

    def _abort(self, session_id: str) -> None:
        if not session_id or not self._password:
            return
        try:
            with httpx.Client(timeout=10.0,
                              auth=("opencode", self._password)) as client:
                client.post(f"{self.base_url}/api/session/{session_id}/interrupt")
        except httpx.HTTPError:
            pass


# ── event-stream helpers (module-level so tests can drive them) ──────────


def _sse_payload(line: str) -> dict | None:
    """One SSE line -> the event object. Handles `data: {json}` framing and
    bare JSON lines, whichever the beta emits; dev-branch events nest under
    `payload`, the beta sends them flat."""
    text = line[5:].strip() if line.startswith("data:") else line.strip()
    if not text.startswith("{"):
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    inner = parsed.get("payload")
    return inner if isinstance(inner, dict) else parsed


def _event_data(payload: dict) -> dict:
    data = payload.get("data") or payload.get("properties") or {}
    return data if isinstance(data, dict) else {}


def _form_text(form: dict) -> str:
    """Best-effort question text from a form payload. The beta's shape is not
    documented; log the whole thing once so the real shape surfaces."""
    for key in ("question", "message", "title", "prompt"):
        v = form.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, list):
            parts = [str(x.get("question") or x.get("message") or "")
                     for x in v if isinstance(x, dict)]
            parts = [p for p in parts if p]
            if parts:
                return " ".join(parts)
    qs = form.get("questions")
    if isinstance(qs, list):
        out = []
        for q in qs:
            if isinstance(q, dict):
                out.append(str(q.get("question") or q.get("message") or ""))
        out = [o for o in out if o]
        if out:
            return " ".join(out)
    log.info("opencode2 form shape (unrecognized): %s",
             json.dumps(form)[:600])
    return ""


def _consume_event(
    payload: dict,
    session_id: str,
    texts: dict[str, list[str]],
    order: list[str],
    client: httpx.Client,
    forms: list[str] | None = None,
) -> tuple[bool, str]:
    """Fold one event into `texts`. Returns (finished, error). Events from
    other sessions are ignored; a permission ask is rejected, a question form
    is captured for the caller (who may show it to the user) and then
    cancelled -- the run must never hang waiting for a headless answer."""
    etype = str(payload.get("type") or "")
    data = _event_data(payload)
    data_session = str(data.get("sessionID") or "")

    if etype.startswith("session."):
        if data_session and data_session != session_id:
            return False, ""
        if etype == "session.text.delta":
            mid = str(data.get("assistantMessageID") or "")
            if mid not in texts:
                texts[mid] = []
                order.append(mid)
            texts[mid].append(str(data.get("delta") or ""))
        elif etype == "session.text.ended":
            mid = str(data.get("assistantMessageID") or "")
            text = str(data.get("text") or "")
            if mid and text:
                texts[mid] = [text]
        elif etype == "session.execution.succeeded":
            return True, ""
        elif etype == "session.execution.failed":
            error = data.get("error")
            message = ""
            if isinstance(error, dict):
                inner = error.get("data") or error
                message = str(inner.get("message") or "") if isinstance(inner, dict) else ""
            return True, (message or "the run failed")
        elif etype == "session.execution.interrupted":
            # opencode2 ends the turn when an approval is declined.
            return True, "the run was stopped (a tool call was declined)"
    elif etype == "permission.asked" and data_session == session_id:
        _reject_permission(client, session_id, str(data.get("id") or ""))
    elif etype == "form.created":
        form = data.get("form") if isinstance(data.get("form"), dict) else data
        if str(form.get("sessionID") or data_session) == session_id:
            q = _form_text(form)
            if q and forms is not None:
                forms.append(q)
            _cancel_form(client, session_id, str(form.get("id") or ""))
    return False, ""


def _reject_permission(client: httpx.Client, sid: str, permission_id: str) -> None:
    """Without a reply the tool call sits pending forever, so the default here
    is reject -- the fail-closed direction."""
    log.warning("opencode2 asked permission (headless): rejected")
    try:
        client.post(
            f"{client.base_url}/api/session/{sid}/permission/{permission_id}/reply",
            json={"reply": "reject",
                  "message": "No operator is attached to answer this."},
        )
    except httpx.HTTPError:
        pass


def _cancel_form(client: httpx.Client, sid: str, form_id: str) -> None:
    log.warning("opencode2 asked a question (headless): cancelled")
    try:
        client.post(f"{client.base_url}/api/session/{sid}/form/{form_id}/cancel")
    except httpx.HTTPError:
        pass
