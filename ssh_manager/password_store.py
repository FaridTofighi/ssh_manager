"""Simple password storage for SSH manager."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Optional

from .config import HostEntry
from .persistence import write_json_atomic


class PasswordStore:
    """Persist and retrieve per-host passwords."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._passwords: Dict[str, str] = {}
        self._load()

    # --------------------------------------------------------------------- util
    def _key_for_host(self, host: HostEntry) -> str:
        return f"{host.parent_group}::{host.child_group}::{host.alias}"

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if isinstance(data, dict):
            self._passwords = {
                key: value
                for key, value in data.items()
                if isinstance(key, str) and isinstance(value, str)
            }

    def _save(self) -> None:
        write_json_atomic(self._path, self._passwords, mode=0o600)

    # --------------------------------------------------------------------- api
    def get(self, host: HostEntry) -> Optional[str]:
        return self._passwords.get(self._key_for_host(host))

    def set(self, host: HostEntry, password: str) -> None:
        key = self._key_for_host(host)
        old_value = self._passwords.get(key)
        self._passwords[key] = password
        try:
            self._save()
        except OSError:
            if old_value is None:
                self._passwords.pop(key, None)
            else:
                self._passwords[key] = old_value
            raise

    def delete(self, host: HostEntry) -> None:
        key = self._key_for_host(host)
        if key in self._passwords:
            old_value = self._passwords.pop(key)
            try:
                self._save()
            except OSError:
                self._passwords[key] = old_value
                raise
