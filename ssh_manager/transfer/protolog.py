"""The connection log of a file-browser tab, optionally mirrored to a rotating file (pure Python).

Backends call the log from their worker threads; the tab reads it on the GTK
thread, so the buffer is locked. Every line is masked twice: the backend
masks ``PASS`` and its known secrets, and :meth:`ConnectionLog.append` masks
the secrets it was given (the password of the current attempt) again, so a
password never reaches the pane, the clipboard or the file.

The file (Preferences → File transfer → "Keep a protocol log file", off by
default) is ``$XDG_STATE_HOME/ssh-manager/protocol.log``, mode 0600, rotated
at :data:`MAX_FILE_BYTES` with :data:`BACKUPS` old copies.
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable, Deque, Dict, List, Optional

from .backend import redact

MAX_LINES = 2000
MAX_LINE_CHARS = 2000
MAX_FILE_BYTES = 1024 * 1024
BACKUPS = 3


def default_log_path() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "ssh-manager" / "protocol.log"


def _printable(text: str) -> str:
    """One line: control characters (and bidi controls from the server) shown as ``?``."""

    out = []
    for ch in text[:MAX_LINE_CHARS]:
        code = ord(ch)
        if code < 32 or code == 127 or 0x202A <= code <= 0x202E or 0x2066 <= code <= 0x2069:
            out.append("?" if ch not in "\t" else " ")
        else:
            out.append(ch)
    return "".join(out)


class RotatingLogFile:
    """Append-only text file, 0600, rotated by size (``protocol.log`` → ``protocol.log.1`` …)."""

    def __init__(self, path: Path, max_bytes: int = MAX_FILE_BYTES, backups: int = BACKUPS) -> None:
        self.path = Path(path)
        self.max_bytes = max_bytes
        self.backups = backups
        self._lock = threading.Lock()

    def write(self, line: str) -> None:
        data = (line + "\n").encode("utf-8", "replace")
        with self._lock:
            try:
                self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                try:
                    size = self.path.stat().st_size
                except FileNotFoundError:
                    size = 0
                if size and size + len(data) > self.max_bytes:
                    self._rotate()
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                try:
                    os.write(fd, data)
                finally:
                    os.close(fd)
            except OSError:
                pass  # diagnostics never break a transfer

    def _rotate(self) -> None:
        for index in range(self.backups, 0, -1):
            older = self.path.with_name(f"{self.path.name}.{index}")
            newer = self.path.with_name(f"{self.path.name}.{index - 1}") if index > 1 else self.path
            if index == self.backups and older.exists():
                older.unlink()
            if newer.exists():
                os.replace(newer, older)


class ConnectionLog:
    """Thread-safe ring buffer of timestamped, masked lines."""

    def __init__(self, label: str = "", *, file: Optional[RotatingLogFile] = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.label = label
        self.file = file
        self._clock = clock
        self._lines: Deque[str] = deque(maxlen=MAX_LINES)
        self._secrets: Dict[str, str] = {}
        self._lock = threading.Lock()
        self.listeners: List[Callable[[str], None]] = []  # called on the logging thread

    def set_secret(self, name: str, value: str) -> None:
        with self._lock:
            if value:
                self._secrets[name] = value
            else:
                self._secrets.pop(name, None)

    def append(self, text: str) -> str:
        with self._lock:
            secrets = dict(self._secrets)
        stamp = time.strftime("%H:%M:%S", time.localtime(self._clock()))
        line = f"{stamp} {_printable(redact(str(text), secrets))}"
        with self._lock:
            self._lines.append(line)
        if self.file is not None:
            date = time.strftime("%Y-%m-%d", time.localtime(self._clock()))
            self.file.write(f"{date} {line} [{self.label}]" if self.label else f"{date} {line}")
        for listener in list(self.listeners):
            listener(line)
        return line

    def __call__(self, text: str) -> None:  # usable as a backend's ``log=`` callable
        self.append(text)

    def lines(self) -> List[str]:
        with self._lock:
            return list(self._lines)

    def text(self) -> str:
        return "\n".join(self.lines()) + ("\n" if self._lines else "")

    def clear(self) -> None:
        with self._lock:
            self._lines.clear()
