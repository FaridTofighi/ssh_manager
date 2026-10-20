"""Per-host file-browser memory: bookmarks, the last local / remote folder, column widths (no GTK).

One JSON file, ``$XDG_DATA_HOME/ssh-manager/file-browser.json`` (0600), keyed
by the host's id (or its group path for folder inventories). Remembering the
last folders can be turned off in Preferences; bookmarks are always kept.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..persistence import write_json_atomic

MAX_BOOKMARKS = 50


def default_state_path() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "ssh-manager" / "file-browser.json"


def host_state_key(host: Any) -> str:
    host_id = getattr(host, "id", "")
    if host_id:
        return host_id
    return f"{host.parent_group}::{host.child_group}::{host.alias}"


class BrowserState:
    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path is not None else default_state_path()
        self._lock = threading.Lock()

    def _load(self) -> Dict[str, Dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return {k: v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}

    def _save(self, data: Dict[str, Dict[str, Any]]) -> None:
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            write_json_atomic(self.path, data, mode=0o600)
        except OSError:
            pass  # memory only; browsing works without it

    def get(self, key: str) -> Dict[str, Any]:
        with self._lock:
            return dict(self._load().get(key, {}))

    def update(self, key: str, **values: Any) -> None:
        with self._lock:
            data = self._load()
            entry = data.setdefault(key, {})
            entry.update(values)
            self._save(data)

    def bookmarks(self, key: str) -> List[Dict[str, str]]:
        items = self.get(key).get("bookmarks", [])
        return [b for b in items if isinstance(b, dict) and isinstance(b.get("path"), str)
                and b.get("side") in ("local", "remote")]

    def add_bookmark(self, key: str, side: str, path: str) -> None:
        items = [b for b in self.bookmarks(key) if not (b["side"] == side and b["path"] == path)]
        items.append({"side": side, "path": path})
        self.update(key, bookmarks=items[-MAX_BOOKMARKS:])

    def remove_bookmark(self, key: str, side: str, path: str) -> None:
        self.update(key, bookmarks=[b for b in self.bookmarks(key) if not (b["side"] == side and b["path"] == path)])

    def forget(self, key: str) -> None:
        with self._lock:
            data = self._load()
            if data.pop(key, None) is not None:
                self._save(data)
