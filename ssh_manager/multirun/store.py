"""Saved commands and the command history of Run Command, encrypted like macros (no GTK).

``commands.enc`` in the configuration folder, the same mechanism as macros
version 2: the keybox's own subkey (``commands``), AES-256-GCM with the header
(``format``, ``version``, ``keybox_id``, ``cipher``) authenticated, written
atomically (0600, the previous file kept as ``.bak``). A command can hold a
token or a password, so nothing is ever stored in plain text: without a keybox
("Don't save passwords"), saving is refused and the history lives in memory
only — the same rule as macros. A file that cannot be opened is reported and
never overwritten.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

FORMAT = "ssh-manager-commands"
VERSION = 1
CIPHER = "aes-256-gcm"
HISTORY_SIZE = 50
FILE_NAME = "commands.enc"


class CommandFileError(Exception):
    """``kind``: damaged, unsupported, foreign, tampered, unavailable."""

    def __init__(self, kind: str) -> None:
        Exception.__init__(self, kind)
        self.kind = kind


@dataclass
class SavedCommand:
    name: str
    text: str
    shell: str = "sh"
    options: Dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "text": self.text, "shell": self.shell, "options": self.options}

    @classmethod
    def from_dict(cls, data: Any) -> Optional["SavedCommand"]:
        if not isinstance(data, dict) or not isinstance(data.get("text"), str):
            return None
        options = data.get("options")
        return cls(name=str(data.get("name") or ""), text=data["text"], shell=str(data.get("shell") or "sh"),
                   options=options if isinstance(options, dict) else {}, id=str(data.get("id") or uuid.uuid4().hex))


def default_path() -> Path:
    from ..settings import _default_config_dir

    return _default_config_dir() / FILE_NAME


class CommandStore:
    """``path=None``: memory only (no key). :meth:`unlock` opens (or prepares) the encrypted file."""

    def __init__(self, path: Optional[Path]) -> None:
        self.path = Path(path) if path is not None else None
        self._key: Optional[bytes] = None
        self._keybox_id = ""
        self._commands: List[SavedCommand] = []
        self._history: List[str] = []

    @property
    def persistent(self) -> bool:
        return self.path is not None and self._key is not None

    def _header(self) -> dict:
        return {"format": FORMAT, "version": VERSION, "keybox_id": self._keybox_id, "cipher": CIPHER}

    def unlock(self, key: bytes, keybox_id: str) -> None:
        """Read the file (raises :class:`CommandFileError` and leaves it untouched when it cannot)."""

        from ..aead import AeadError, open_sealed

        self._key, self._keybox_id = bytes(key), keybox_id
        if self.path is None or not self.path.exists():
            return
        try:
            document = json.loads(self.path.read_bytes().decode("utf-8"))
        except (UnicodeDecodeError, ValueError, OSError):
            self._key = None
            raise CommandFileError("damaged") from None
        try:
            if not isinstance(document, dict) or document.get("format") != FORMAT:
                raise CommandFileError("damaged")
            version = document.get("version")
            if version != VERSION:
                raise CommandFileError("unsupported" if isinstance(version, int) and version > VERSION else "damaged")
            if document.get("cipher") != CIPHER:
                raise CommandFileError("unsupported")
            if document.get("keybox_id") != keybox_id:
                raise CommandFileError("foreign")
            try:
                plain = open_sealed(self._key, document.get("nonce"), document.get("data"), self._header())
            except AeadError:
                raise CommandFileError("tampered") from None
            try:
                payload = json.loads(plain.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                raise CommandFileError("damaged") from None
        except CommandFileError:
            self._key = None  # never written over
            raise
        payload = payload if isinstance(payload, dict) else {}
        self._commands = [c for c in (SavedCommand.from_dict(item) for item in payload.get("commands", [])) if c]
        self._history = [t for t in payload.get("history", []) if isinstance(t, str)][:HISTORY_SIZE]

    def _save(self) -> None:
        if not self.persistent:
            return
        from ..aead import seal
        from ..keybox import write_private_atomic

        plain = json.dumps({"commands": [c.to_dict() for c in self._commands], "history": self._history},
                           separators=(",", ":")).encode("utf-8")
        nonce, data = seal(self._key, plain, self._header())
        write_private_atomic(self.path, (json.dumps(dict(self._header(), nonce=nonce, data=data), indent=1)
                                         + "\n").encode("utf-8"))

    # ---------------------------------------------------------- commands
    def commands(self) -> List[SavedCommand]:
        return sorted(self._commands, key=lambda c: c.name.casefold())

    def save_command(self, name: str, text: str, *, shell: str = "sh", options: Optional[dict] = None) -> SavedCommand:
        if not self.persistent:
            raise CommandFileError("unavailable")
        existing = next((c for c in self._commands if c.name.casefold() == name.strip().casefold()), None)
        command = SavedCommand(name=name.strip(), text=text, shell=shell, options=dict(options or {}),
                               id=existing.id if existing else uuid.uuid4().hex)
        self._commands = [c for c in self._commands if c is not existing] + [command]
        self._save()
        return command

    def delete_command(self, command_id: str) -> None:
        self._commands = [c for c in self._commands if c.id != command_id]
        self._save()

    # ----------------------------------------------------------- history
    def history(self) -> List[str]:
        return list(self._history)

    def add_history(self, text: str) -> None:
        if not text.strip():
            return
        self._history = [text] + [t for t in self._history if t != text]
        del self._history[HISTORY_SIZE:]
        self._save()

    def clear_history(self) -> None:
        self._history = []
        self._save()


__all__ = ["CommandStore", "SavedCommand", "CommandFileError", "default_path", "HISTORY_SIZE"]
