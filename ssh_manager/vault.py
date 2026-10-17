"""Encrypted credential vault (optional, additive)."""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, List, Optional
import hashlib
import hmac

from .persistence import write_json_atomic
from .settings import _default_config_dir

try:
    from cryptography.fernet import Fernet  # type: ignore
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC  # type: ignore
    from cryptography.hazmat.primitives import hashes  # type: ignore

    CRYPTO_AVAILABLE = True
except Exception:  # pragma: no cover - cryptography optional
    CRYPTO_AVAILABLE = False


def default_vault_path() -> Path:
    """Return the vault location, honoring XDG_CONFIG_HOME.

    Older versions always used ~/.config/ssh-manager/vault.json; an existing
    vault there keeps being used so it is not silently orphaned.
    """

    preferred = _default_config_dir() / "vault.json"
    legacy = Path.home() / ".config" / "ssh-manager" / "vault.json"
    if not preferred.exists() and legacy.exists():
        return legacy
    return preferred


@dataclass
class VaultEntry:
    id: str
    label: str
    username: str
    password: str


class VaultError(Exception):
    pass


class VaultLockedError(VaultError):
    pass


class CredentialVault:
    """Minimal encrypted vault. Optional; no effect if unavailable."""

    VERSION = 1

    def __init__(self, path: Path) -> None:
        self._path = path
        self._salt: Optional[bytes] = None
        self._master_hash: Optional[bytes] = None
        self._entries: Dict[str, VaultEntry] = {}
        self._key: Optional[bytes] = None
        self._load_header()

    # ----------------------------------------------------------------- keying
    def _derive_key(self, password: str, salt: bytes) -> bytes:
        if not CRYPTO_AVAILABLE:
            raise VaultError("Encryption support is unavailable (cryptography not installed).")
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=200_000,
        )
        return base64.urlsafe_b64encode(kdf.derive(password.encode("utf-8")))

    def _load_header(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(data, dict):
            return
        salt_b64 = data.get("salt")
        master = data.get("master_hash")
        if isinstance(salt_b64, str):
            try:
                self._salt = base64.b64decode(salt_b64)
            except Exception:
                self._salt = None
        if isinstance(master, str):
            try:
                self._master_hash = base64.b64decode(master)
            except Exception:
                self._master_hash = None

    # ----------------------------------------------------------------- public
    @property
    def available(self) -> bool:
        return CRYPTO_AVAILABLE

    @property
    def unlocked(self) -> bool:
        return self._key is not None

    @property
    def initialized(self) -> bool:
        return self._salt is not None and self._master_hash is not None

    def create_master(self, password: str) -> None:
        if not CRYPTO_AVAILABLE:
            raise VaultError("Encryption support is unavailable.")
        old_state = (self._salt, self._master_hash, dict(self._entries), self._key)
        salt = os.urandom(16)
        key = self._derive_key(password, salt)
        verifier = hmac.new(key, b"vault", hashlib.sha256).digest()
        self._salt = salt
        self._master_hash = verifier
        self._entries = {}
        self._key = key
        try:
            self._save()
        except VaultError:
            self._salt, self._master_hash, self._entries, self._key = old_state
            raise

    def unlock(self, password: str) -> bool:
        if not self.initialized:
            raise VaultError("Vault not initialized.")
        if self._salt is None or self._master_hash is None:
            return False
        key = self._derive_key(password, self._salt)
        verifier = hmac.new(key, b"vault", hashlib.sha256).digest()
        if not hmac.compare_digest(verifier, self._master_hash):
            return False
        self._key = key
        self._load_entries()
        return True

    def change_master(self, old_password: str, new_password: str) -> bool:
        if not self.unlock(old_password):
            return False
        old_state = (self._salt, self._master_hash, dict(self._entries), self._key)
        salt = os.urandom(16)
        key = self._derive_key(new_password, salt)
        verifier = hmac.new(key, b"vault", hashlib.sha256).digest()
        self._salt = salt
        self._master_hash = verifier
        self._key = key
        try:
            self._save()
        except VaultError:
            self._salt, self._master_hash, self._entries, self._key = old_state
            raise
        return True

    def entries(self) -> List[VaultEntry]:
        return sorted(self._entries.values(), key=lambda e: e.label.lower())

    def add_entry(self, entry: VaultEntry) -> None:
        if not self.unlocked:
            raise VaultLockedError("Vault locked.")
        old_entry = self._entries.get(entry.id)
        self._entries[entry.id] = entry
        try:
            self._save()
        except VaultError:
            if old_entry is None:
                self._entries.pop(entry.id, None)
            else:
                self._entries[entry.id] = old_entry
            raise

    def update_entry(self, entry: VaultEntry) -> None:
        if not self.unlocked:
            raise VaultLockedError("Vault locked.")
        old_entry = self._entries.get(entry.id)
        self._entries[entry.id] = entry
        try:
            self._save()
        except VaultError:
            if old_entry is None:
                self._entries.pop(entry.id, None)
            else:
                self._entries[entry.id] = old_entry
            raise

    def delete_entry(self, entry_id: str) -> None:
        if not self.unlocked:
            raise VaultLockedError("Vault locked.")
        old_entry = self._entries.pop(entry_id, None)
        try:
            self._save()
        except VaultError:
            if old_entry is not None:
                self._entries[entry_id] = old_entry
            raise

    def get_entry(self, entry_id: str) -> Optional[VaultEntry]:
        return self._entries.get(entry_id)

    def export_encrypted(self, target: Path, password: str) -> None:
        if not self.unlocked:
            raise VaultLockedError("Vault locked.")
        if not CRYPTO_AVAILABLE:
            raise VaultError("Encryption support unavailable.")
        salt = os.urandom(16)
        key = self._derive_key(password, salt)
        data = [asdict(e) for e in self._entries.values()]
        token = Fernet(key).encrypt(json.dumps(data).encode("utf-8"))
        payload = {
            "version": self.VERSION,
            "salt": base64.b64encode(salt).decode("utf-8"),
            "data": base64.b64encode(token).decode("utf-8"),
        }
        write_json_atomic(target, payload, indent=None, mode=0o600)

    def import_encrypted(self, source: Path, password: str) -> bool:
        if not CRYPTO_AVAILABLE:
            raise VaultError("Encryption support unavailable.")
        try:
            raw = json.loads(source.read_text(encoding="utf-8"))
        except Exception:
            return False
        if not isinstance(raw, dict):
            return False
        salt_b64 = raw.get("salt")
        data_b64 = raw.get("data")
        if not isinstance(salt_b64, str) or not isinstance(data_b64, str):
            return False
        try:
            salt = base64.b64decode(salt_b64)
            token = base64.b64decode(data_b64)
            key = self._derive_key(password, salt)
            plaintext = Fernet(key).decrypt(token)
            entries = json.loads(plaintext.decode("utf-8"))
        except Exception:
            return False
        if not isinstance(entries, list):
            return False
        if not self.unlocked:
            return False
        old_entries = dict(self._entries)
        for entry in entries:
            if isinstance(entry, dict):
                vid = entry.get("id")
                label = entry.get("label")
                username = entry.get("username")
                password_val = entry.get("password")
                if not all(isinstance(value, str) for value in (vid, label, username, password_val)):
                    continue
                if vid and label and username:
                    self._entries[vid] = VaultEntry(vid, label, username, password_val)
        try:
            self._save()
        except VaultError:
            self._entries = old_entries
            raise
        return True

    # ----------------------------------------------------------------- internal
    def _load_entries(self) -> None:
        if not self.initialized or not self.unlocked:
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(raw, dict):
            return
        data_b64 = raw.get("data")
        if not isinstance(data_b64, str):
            return
        try:
            token = base64.b64decode(data_b64)
            plaintext = Fernet(self._key).decrypt(token)
            entries = json.loads(plaintext.decode("utf-8"))
        except Exception:
            return
        if not isinstance(entries, list):
            return
        self._entries = {}
        for entry in entries:
            if isinstance(entry, dict):
                vid = entry.get("id")
                label = entry.get("label")
                username = entry.get("username")
                password = entry.get("password")
                if not all(isinstance(value, str) for value in (vid, label, username, password)):
                    continue
                if vid and label and username:
                    self._entries[vid] = VaultEntry(vid, label, username, password)

    def _save(self) -> None:
        if not self.unlocked or self._salt is None or self._master_hash is None:
            return
        data = [asdict(e) for e in self._entries.values()]
        token = Fernet(self._key).encrypt(json.dumps(data).encode("utf-8"))
        payload = {
            "version": self.VERSION,
            "salt": base64.b64encode(self._salt).decode("utf-8"),
            "master_hash": base64.b64encode(self._master_hash).decode("utf-8"),
            "data": base64.b64encode(token).decode("utf-8"),
        }
        try:
            write_json_atomic(self._path, payload, mode=0o600)
        except OSError as exc:
            raise VaultError(f"Failed to save vault: {exc}") from exc
