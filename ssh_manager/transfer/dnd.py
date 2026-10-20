"""Drag and drop rules for the file browser (pure Python, no GTK).

Targets:

* ``application/x-sshmanager-entries`` — between panes and tabs of this app.
  A small JSON payload (version, the source pane's token, side, paths). It is
  *validated on drop*: the token must name a live pane and every path must
  be listed in that pane right now; anything else is rejected.
* ``text/uri-list`` — local files from / to the desktop's file manager.
  Only ``file://`` URIs of this machine (``file:///…`` or
  ``file://localhost/…``) naming existing, readable files are accepted.

:func:`resolve_drop` decides the action from where the drag started and
where it ends.
"""

from __future__ import annotations

import json
import os
import posixpath
from dataclasses import dataclass
from enum import Enum
from typing import List, Optional, Sequence, Tuple
from urllib.parse import unquote, urlsplit

PAYLOAD_TARGET = "application/x-sshmanager-entries"
URI_TARGET = "text/uri-list"
TEXT_TARGET = "text/plain"
PAYLOAD_VERSION = 1
MAX_PAYLOAD_BYTES = 4 * 1024 * 1024
MAX_ITEMS = 100_000


class DropAction(str, Enum):
    UPLOAD = "upload"
    DOWNLOAD = "download"
    MOVE = "move"
    COPY = "copy"
    RELAY_COPY = "relay_copy"  # server to server (another tab / host), streamed through this computer
    REJECT = "reject"


@dataclass(frozen=True)
class Payload:
    token: str  # the source pane
    side: str  # "local" | "remote"
    paths: Tuple[str, ...]


def encode_payload(token: str, side: str, paths: Sequence[str]) -> bytes:
    return json.dumps({"v": PAYLOAD_VERSION, "token": token, "side": side, "paths": list(paths)}).encode("utf-8")


def decode_payload(data: bytes) -> Optional[Payload]:
    """The payload, or ``None`` for anything malformed (never trusted blindly)."""

    if not data or len(data) > MAX_PAYLOAD_BYTES:
        return None
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(value, dict) or value.get("v") != PAYLOAD_VERSION:
        return None
    token, side, paths = value.get("token"), value.get("side"), value.get("paths")
    if not isinstance(token, str) or side not in ("local", "remote") or not isinstance(paths, list):
        return None
    if not paths or len(paths) > MAX_ITEMS or not all(isinstance(p, str) and p and "\0" not in p for p in paths):
        return None
    return Payload(token, side, tuple(paths))


def validate_payload(payload: Payload, listed_paths: Sequence[str]) -> bool:
    """Every path must be listed in the source pane right now."""

    listed = set(listed_paths)
    return all(path in listed for path in payload.paths)


def parse_uri_list(text: str, *, check_files: bool = True) -> List[str]:
    """Local absolute paths from a ``text/uri-list`` (comments, CRLF, percent-encoding, localhost)."""

    paths: List[str] = []
    for line in (text or "").replace("\r\n", "\n").split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = urlsplit(line)
        if parts.scheme.lower() != "file" or parts.netloc not in ("", "localhost"):
            continue  # http://, sftp://, smb://, another machine: not a local file
        path = unquote(parts.path)
        if not path.startswith("/") or "\0" in path:
            continue
        path = os.path.normpath(path)
        if check_files and not (os.path.exists(path) and os.access(path, os.R_OK)):
            continue
        paths.append(path)
    return paths


def uri_list(paths: Sequence[str]) -> str:
    from urllib.parse import quote

    return "".join(f"file://{quote(os.path.abspath(p))}\r\n" for p in paths)


def resolve_drop(source_side: str, target_side: str, *, same_pane: bool, same_server: bool, copy_modifier: bool,
                 target_is_file: bool, read_only_target: bool = False) -> DropAction:
    """What a drop does.

    ``same_pane``: dragged within one pane (onto one of its folders);
    ``same_server``: two remote panes of the same connection (same tab).
    """

    if target_is_file or read_only_target and target_side == "remote":
        return DropAction.REJECT
    if source_side == "local" and target_side == "remote":
        return DropAction.UPLOAD
    if source_side == "remote" and target_side == "local":
        return DropAction.DOWNLOAD
    if source_side == "remote" and target_side == "remote":
        if same_pane or same_server:
            return DropAction.COPY if copy_modifier else DropAction.MOVE
        return DropAction.RELAY_COPY
    return DropAction.COPY if copy_modifier or not same_pane else DropAction.MOVE  # local → local


def into_itself(sources: Sequence[str], target_dir: str) -> bool:
    """Dropping a folder into itself or one of its descendants."""

    target = posixpath.normpath(target_dir)
    for source in sources:
        parent = posixpath.normpath(source)
        if target == parent or target.startswith(parent.rstrip("/") + "/"):
            return True
    return False
