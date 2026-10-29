"""One process owns video-import processing per data directory.

The web server and the ``deeptutor video-import`` CLI share the same job
files and the same GPU. Whichever process first claims ``worker.lock`` (an OS
file lock, released automatically when the process exits) runs the worker and
is the only one allowed to change jobs; another process gets
:class:`WorkerBusyError`. The server claims it at startup and keeps it for its
lifetime; a CLI run claims it only when no server holds it.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys
import threading
from typing import BinaryIO

_held: dict[Path, BinaryIO] = {}
_guard = threading.Lock()


class WorkerBusyError(ValueError):
    code = "worker_busy"


def _pid_path(lock_path: Path) -> Path:
    return lock_path.with_name(lock_path.name + ".pid")


def _lock(handle: BinaryIO) -> None:
    if sys.platform == "win32":
        import msvcrt

        handle.seek(0)
        if not handle.read(1):
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def claim(lock_path: Path) -> bool:
    """Take the lock for this process; True when this process holds it."""
    path = Path(lock_path).resolve()
    with _guard:
        if path in _held:
            return True
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("a+b")
        try:
            _lock(handle)
        except OSError:
            handle.close()
            return False
        _held[path] = handle
        _pid_path(path).write_text(str(os.getpid()), encoding="utf-8")
        return True


def release(lock_path: Path) -> None:
    """Give the lock up (process exit does this too)."""
    path = Path(lock_path).resolve()
    with _guard:
        handle = _held.pop(path, None)
        if handle is not None:
            handle.close()  # closing the handle drops the OS lock


def busy_error(lock_path: Path) -> WorkerBusyError:
    try:
        pid = _pid_path(Path(lock_path).resolve()).read_text(encoding="utf-8").strip()
    except OSError:
        pid = "?"
    return WorkerBusyError(
        f"Another DeepTutor process (pid {pid}) owns video imports. If it is the server, "
        "use the web app or the /api/video-import API; if it is a CLI run, wait for it to finish."
    )
