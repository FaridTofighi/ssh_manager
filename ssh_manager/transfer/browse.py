"""Pure helpers of the file browser: sorting, filtering, sizes, paths, breadcrumbs, URLs (no GTK).

Paths in a pane are POSIX-style for remote servers (``/srv/data``) and may
be Windows-style for some FTP servers (``C:/Inetpub`` or ``/C:/Inetpub``);
local paths are the local OS's. :func:`normalize_remote` collapses ``.``,
``..``, duplicate slashes and trailing slashes without ever going above the
root.
"""

from __future__ import annotations

import posixpath
import re
import time
from dataclasses import dataclass
from typing import Callable, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import quote

from .backend import EntryKind, RemoteEntry

_NATURAL = re.compile(r"(\d+)")
_DRIVE = re.compile(r"^/?([A-Za-z]):(/|$)")


# ------------------------------------------------------------------ sorting
def natural_key(name: str) -> Tuple:
    """Case-insensitive natural order: ``file2`` before ``file10``."""

    parts = _NATURAL.split(name.casefold())
    return tuple((0, int(part)) if part.isdigit() else (1, part) for part in parts)


SORT_COLUMNS = ("name", "size", "mtime", "perms", "owner")


def sort_entries(entries: Iterable[RemoteEntry], column: str = "name", *, descending: bool = False,
                 folders_first: bool = True) -> List[RemoteEntry]:
    """Stable sort by ``column``; folders stay first in both directions when ``folders_first``."""

    def value(entry: RemoteEntry):
        if column == "size":
            return (entry.size if entry.size is not None else -1, natural_key(entry.name))
        if column == "mtime":
            return (entry.mtime if entry.mtime is not None else float("-inf"), natural_key(entry.name))
        if column == "perms":
            return (entry.perms, natural_key(entry.name))
        if column == "owner":
            return (entry.owner.casefold(), natural_key(entry.name))
        return natural_key(entry.name)

    items = sorted(entries, key=value, reverse=descending)
    if folders_first:
        items.sort(key=lambda e: 0 if e.kind is EntryKind.DIR else 1)  # stable: keeps the order above
    return items


def filter_entries(entries: Iterable[RemoteEntry], text: str = "", *, show_hidden: bool = True) -> List[RemoteEntry]:
    """Live filter by name (case-insensitive substring; ``*`` / ``?`` wildcards) and hidden files."""

    needle = (text or "").strip().casefold()
    matcher: Callable[[str], bool]
    if any(ch in needle for ch in "*?"):
        pattern = re.compile("^" + re.escape(needle).replace(r"\*", ".*").replace(r"\?", ".") + "$")
        matcher = lambda name: bool(pattern.match(name.casefold()))  # noqa: E731
    else:
        matcher = lambda name: needle in name.casefold()  # noqa: E731
    return [e for e in entries if (show_hidden or not e.is_hidden) and (not needle or matcher(e.name))]


# ------------------------------------------------------------------ display
def human_size(size: Optional[int]) -> str:
    """``1.4 MB`` (decimal units, like file managers); ``""`` for unknown sizes."""

    if size is None or size < 0:
        return ""
    if size < 1000:
        return f"{size} B"
    value = float(size)
    for unit in ("kB", "MB", "GB", "TB", "PB"):
        value /= 1000.0
        if value < 1000 or unit == "PB":
            return f"{value:.1f} {unit}" if value < 100 else f"{value:.0f} {unit}"
    return f"{size} B"


def format_mtime(mtime: Optional[float], *, now: Optional[float] = None) -> Tuple[str, str]:
    """``(short local text, full ISO tooltip)``."""

    if mtime is None:
        return "", ""
    now = time.time() if now is None else now
    local = time.localtime(mtime)
    iso = time.strftime("%Y-%m-%d %H:%M:%S %Z", local)
    if abs(now - mtime) < 180 * 86400:
        return time.strftime("%b %d %H:%M", local), iso
    return time.strftime("%b %d %Y", local), iso


def format_perms(entry: RemoteEntry) -> str:
    """``rwxr-xr-x (755)`` when the mode is known, else the server's text."""

    if entry.mode is not None:
        bits = "".join(char if entry.mode & (1 << (8 - i)) else "-" for i, char in enumerate("rwxrwxrwx"))
        return f"{bits} ({entry.mode & 0o7777:o})"
    return entry.perms


def isolate(name: str) -> str:
    """Wrap a name in Unicode first-strong isolates for display (RTL names never reorder the UI)."""

    return "\u2068" + name + "\u2069"


# --------------------------------------------------------------------- paths
def windows_style(path: str) -> bool:
    return bool(_DRIVE.match(path or ""))


def normalize_remote(path: str, cwd: str = "/") -> str:
    """Absolute, clean remote path: ``.``/``..`` resolved, duplicate and trailing slashes removed.

    ``..`` never goes above ``/`` (or a drive root ``/C:``). Windows-style
    paths keep their drive and use ``/`` separators.
    """

    text = (path or "").replace("\\", "/").strip()
    if not text:
        text = cwd or "/"
    if not text.startswith("/") and not _DRIVE.match(text):
        text = posixpath.join(cwd or "/", text)
    drive = ""
    match = _DRIVE.match(text)
    if match:
        drive = match.group(1).upper() + ":"
        text = text[match.end() - (1 if match.group(2) else 0):] or "/"
    parts: List[str] = []
    for part in text.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    body = "/" + "/".join(parts)
    return f"/{drive}{body}" if drive and body != "/" else (f"/{drive}" if drive else body)


def parent_of(path: str) -> str:
    clean = normalize_remote(path)
    if clean == "/" or re.fullmatch(r"/[A-Z]:", clean):
        return clean
    return normalize_remote(clean + "/..")


@dataclass(frozen=True)
class Crumb:
    label: str
    path: str


def breadcrumbs(path: str) -> List[Crumb]:
    """Path bar segments: ``/`` → ``srv`` → ``data`` (each with the path it jumps to)."""

    clean = normalize_remote(path)
    crumbs = [Crumb("/", "/")]
    current = ""
    for part in [p for p in clean.split("/") if p]:
        current += "/" + part
        crumbs.append(Crumb(part, current))
    return crumbs


def local_breadcrumbs(path: str) -> List[Crumb]:
    import os

    clean = os.path.normpath(os.path.abspath(path or os.sep))
    crumbs = [Crumb(os.sep, os.sep)]
    current = ""
    for part in [p for p in clean.split(os.sep) if p]:
        current += os.sep + part
        crumbs.append(Crumb(part, current))
    return crumbs


def complete(prefix: str, names: Sequence[str]) -> List[str]:
    """Path-bar completion: loaded folder names that start with the typed last segment."""

    head, _, tail = prefix.rpartition("/")
    needle = tail.casefold()
    base = head + "/" if prefix.count("/") else ""
    return [base + name for name in names if name.casefold().startswith(needle)]


def remote_url(protocol: str, hostname: str, port: int, default_port: int, user: str = "", path: str = "/") -> str:
    """``sftp://alice@host:2222/path`` — never with a password."""

    netloc = hostname if ":" not in hostname else f"[{hostname}]"
    if user:
        netloc = f"{quote(user, safe='')}@{netloc}"
    if port and port != default_port:
        netloc += f":{port}"
    return f"{protocol}://{netloc}{quote(normalize_remote(path), safe='/')}"


# ------------------------------------------------------------------- history
class History:
    """Back / forward and the recent folders (newest first, at most ``limit``)."""

    def __init__(self, limit: int = 20) -> None:
        self.limit = limit
        self._back: List[str] = []
        self._forward: List[str] = []
        self.current: Optional[str] = None
        self.recent: List[str] = []

    def visit(self, path: str) -> None:
        if path == self.current:
            return
        if self.current is not None:
            self._back.append(self.current)
        self._forward.clear()
        self.current = path
        self._remember(path)

    def _remember(self, path: str) -> None:
        if path in self.recent:
            self.recent.remove(path)
        self.recent.insert(0, path)
        del self.recent[self.limit:]

    def can_back(self) -> bool:
        return bool(self._back)

    def can_forward(self) -> bool:
        return bool(self._forward)

    def back(self) -> Optional[str]:
        if not self._back:
            return None
        if self.current is not None:
            self._forward.append(self.current)
        self.current = self._back.pop()
        return self.current

    def forward(self) -> Optional[str]:
        if not self._forward:
            return None
        if self.current is not None:
            self._back.append(self.current)
        self.current = self._forward.pop()
        return self.current
