"""Alerts, delivered into the in-app chat.

There used to be a Telegram bot here. It is gone: the dashboard's own chat is
the one conversation Lumbung has, so an alert that arrived anywhere else was a
second inbox to remember to check.

The transport is a file. `data/answers.jsonl` is what the chat already streams
over SSE and replays in `/api/chat/history`, so anything appended to it shows up
as a Lumbung message in the app -- on the phone as well, since the dashboard is a
PWA. That also means the engine does not have to know the web server exists, or
be running at the same time as it: an alert raised at 02:00 with no browser open
is waiting in the chat in the morning.

Delivery must never take the engine down with it. A full disk while flattening a
position is a bad moment to raise an OSError out of an alert call, so every
failure degrades to stdout and is logged, never re-raised.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Protocol

log = logging.getLogger(__name__)

ANSWERS_FILE = "answers.jsonl"


class Notifier(Protocol):
    def send(self, message: str) -> None: ...


class ConsoleNotifier:
    """Fallback when there is no data directory to write into.

    Never silently drops a message: an alert nobody sees is worse than a noisy
    one, because it reads exactly like nothing having gone wrong.
    """

    def send(self, message: str) -> None:
        print(f"[notify] {message}")


class AppNotifier:
    """Append alerts to the chat transcript the dashboard reads."""

    def __init__(self, data_dir: str | Path, *, filename: str = ANSWERS_FILE) -> None:
        self.path = Path(data_dir) / filename

    def send(self, message: str) -> None:
        text = str(message).strip()
        if not text:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {"ts": int(time.time()), "text": text},
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        except OSError as exc:
            # Losing the alert entirely is the failure worth avoiding; losing the
            # nice delivery is not.
            log.warning("could not write alert to the chat: %s", exc)
            print(f"[notify-fallback] {text}")


def build_notifier(data_dir: str | Path | None) -> Notifier:
    if data_dir:
        return AppNotifier(data_dir)
    log.warning("no data directory -- alerts go to the console only")
    return ConsoleNotifier()
