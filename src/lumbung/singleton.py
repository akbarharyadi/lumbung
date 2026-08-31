"""Single-instance guard for the trading engine.

Now that the engine autostarts at logon, it is easy to end up with a second one:
double-click the launcher, or start a live run while the paper run is already up.
Two engines share one journal and one exchange account, so both would size
positions from the same balance and both would place the orders -- doubling
size, and racing each other on exits.

A PID file plus a liveness check is enough here. It is not a distributed lock,
but the failure it prevents is a human starting the same program twice on one
machine, which this catches reliably.
"""

from __future__ import annotations

import os
from pathlib import Path


class AlreadyRunning(RuntimeError):
    def __init__(self, pid: int, path: Path):
        super().__init__(f"another engine is already running (pid {pid})")
        self.pid = pid
        self.path = path


def _alive(pid: int) -> bool:
    """Is this PID a live process?"""
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            import subprocess

            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, timeout=10,
            ).stdout
            return str(pid) in out
        os.kill(pid, 0)          # POSIX: signal 0 just tests existence
        return True
    except (OSError, ValueError):
        # No subprocess.SubprocessError here: the import lives in the Windows
        # branch above, so naming it in this tuple is an UnboundLocalError on
        # POSIX -- a stale PID file would crash-loop instead of being taken
        # over. The generic handler below already covers subprocess errors.
        return False
    except Exception:  # noqa: BLE001
        return False


class InstanceLock:
    """Context manager holding a PID file for the lifetime of the process."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def acquire(self) -> None:
        if self.path.exists():
            try:
                pid = int(self.path.read_text(encoding="utf-8").strip() or 0)
            except (ValueError, OSError):
                pid = 0
            if pid and pid != os.getpid() and _alive(pid):
                raise AlreadyRunning(pid, self.path)
            # Stale file from a crash or a hard kill -- safe to take over.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(str(os.getpid()), encoding="utf-8")

    def release(self) -> None:
        try:
            if self.path.exists():
                pid = int(self.path.read_text(encoding="utf-8").strip() or 0)
                if pid == os.getpid():
                    self.path.unlink()
        except (ValueError, OSError):
            pass

    def __enter__(self) -> InstanceLock:
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()
