"""Trusted TLS certificate fingerprints ("Trust this certificate") for FTPS hosts.

One JSON file, ``$XDG_DATA_HOME/ssh-manager/tls-pins.json`` (mode 0600 in a
0700 folder): ``{"<host id>|<hostname>:<port>": {"sha256": "AB:CD:…",
"subject": "…", "added": <epoch>}}``. A pin is keyed by host id *and*
address, so moving a host to another server never reuses an old pin.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Dict, Optional

from ..persistence import write_json_atomic


def default_pins_path() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "ssh-manager" / "tls-pins.json"


def fingerprint(der: bytes) -> str:
    """``AB:CD:…`` SHA-256 of a DER certificate."""

    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))


def normalize_fingerprint(text: str) -> str:
    hexdigits = "".join(ch for ch in (text or "").upper() if ch in "0123456789ABCDEF")
    if len(hexdigits) != 64:
        return ""
    return ":".join(hexdigits[i:i + 2] for i in range(0, 64, 2))


def pin_key(host_id: str, hostname: str, port: int) -> str:
    return f"{host_id or '-'}|{hostname.lower()}:{int(port)}"


class PinStore:
    """Load, query and save trusted fingerprints (thread-safe, small file)."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path is not None else default_pins_path()
        self._lock = threading.Lock()

    def _load(self) -> Dict[str, dict]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return {k: v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}

    def get(self, key: str) -> str:
        with self._lock:
            return normalize_fingerprint(str(self._load().get(key, {}).get("sha256", "")))

    def set(self, key: str, sha256: str, subject: str = "") -> None:
        value = normalize_fingerprint(sha256)
        if not value:
            raise ValueError("not a SHA-256 fingerprint")
        with self._lock:
            data = self._load()
            data[key] = {"sha256": value, "subject": subject[:256], "added": int(time.time())}
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            write_json_atomic(self.path, data, mode=0o600)

    def forget(self, key: str) -> bool:
        with self._lock:
            data = self._load()
            if data.pop(key, None) is None:
                return False
            write_json_atomic(self.path, data, mode=0o600)
            return True
