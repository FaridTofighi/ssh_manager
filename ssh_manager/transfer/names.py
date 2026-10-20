"""Make untrusted remote names safe on the local file system (pure Python).

A server controls every name it lists. Before a downloaded name touches the
disk it goes through :func:`safe_local_name`, and the resulting path through
:func:`contained_path`, which proves it stays inside the chosen destination
(symlink-aware). Nothing here ever follows a link.

Rules for one name:

* path separators (``/`` and ``\\``), NUL and control characters are replaced
  with ``_``; ``.`` and ``..`` become ``_``; a drive prefix (``C:``) loses
  its colon;
* Unicode bidi controls (``U+202A``–``U+202E``, ``U+2066``–``U+2069``,
  ``U+200E``/``U+200F``) are removed — they can disguise ``gpj.exe`` as
  ``exe.jpg``;
* trailing dots and spaces are stripped (Windows would strip them later);
* names longer than 255 bytes are shortened, keeping the extension, with a
  hash so different long names stay different;
* Windows reserved names (``CON``, ``NUL``, ``COM1`` …) are allowed on Linux
  but reported, since the file may be moved to Windows later.
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import List, Optional, Tuple

MAX_NAME_BYTES = 255
_BIDI = dict.fromkeys(map(ord, "‪‫‬‭‮⁦⁧⁨⁩‎‏؜"), None)
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


class UnsafePath(ValueError):
    """A computed local path would leave the destination folder."""


def safe_local_name(name: str) -> Tuple[str, List[str]]:
    """``(safe name, warnings)`` for one remote name (never empty, never a path)."""

    warnings: List[str] = []
    original = name
    text = (name or "").translate(_BIDI)
    if text != (name or ""):
        warnings.append("bidi control characters removed")
    text = "".join("_" if (ch in "/\\" or ord(ch) < 32 or ord(ch) == 127) else ch for ch in text)
    text = re.sub(r"^([A-Za-z]):", r"\1_", text)  # a drive prefix never becomes absolute anywhere
    stripped = text.rstrip(" .")
    if stripped != text and stripped:
        warnings.append("trailing dots or spaces removed")
    text = stripped
    if text in ("", ".", ".."):
        text = "_"
    if len(text.encode("utf-8", "surrogateescape")) > MAX_NAME_BYTES:
        text = _shorten(text)
        warnings.append("name shortened")
    if text.split(".", 1)[0].lower() in _RESERVED:
        warnings.append("Windows reserved name")
    if text != original and "renamed" not in warnings:
        warnings.insert(0, "renamed")
    return text, warnings


def _shorten(name: str) -> str:
    digest = hashlib.sha256(name.encode("utf-8", "surrogateescape")).hexdigest()[:10]
    stem, dot, extension = name.rpartition(".")
    if not dot or len(extension.encode("utf-8", "surrogateescape")) > 20:
        stem, extension = name, ""
    suffix = f"~{digest}" + (f".{extension}" if extension else "")
    budget = MAX_NAME_BYTES - len(suffix.encode("utf-8", "surrogateescape"))
    encoded = stem.encode("utf-8", "surrogateescape")[:budget]
    return encoded.decode("utf-8", "ignore") + suffix


def contained_path(destination: Path, *parts: str) -> Path:
    """``destination / parts…`` after making every part safe; raises :class:`UnsafePath` if it would escape.

    The check resolves symlinks of the *existing* part of the path, so a
    folder inside the destination that is a link to elsewhere is caught too.
    """

    base = Path(destination).resolve()
    target = base
    for part in parts:
        safe, _warnings = safe_local_name(part)
        target = target / safe
    resolved = _resolve_existing(target)
    if resolved != base and base not in resolved.parents:
        raise UnsafePath(f"{target} is outside {base}")
    return target


def _resolve_existing(path: Path) -> Path:
    """Resolve the longest existing prefix (following links), then append the rest unresolved."""

    existing = path
    rest: List[str] = []
    while not existing.exists() and existing != existing.parent:
        rest.insert(0, existing.name)
        existing = existing.parent
    resolved = existing.resolve()
    for part in rest:
        resolved = resolved / part
    return resolved


def unique_name(directory: Path, name: str, taken: Optional[set] = None) -> str:
    """``name (1).ext``, ``name (2).ext`` … for the conflict policy *Rename*."""

    stem, dot, extension = name.rpartition(".")
    if not dot or not stem:
        stem, extension, dot = name, "", ""
    counter = 1
    while True:
        candidate = f"{stem} ({counter}){dot}{extension}"
        if not (Path(directory) / candidate).exists() and (taken is None or candidate not in taken):
            return candidate
        counter += 1


def local_path_ok(path: str) -> bool:
    return os.path.isabs(path)
