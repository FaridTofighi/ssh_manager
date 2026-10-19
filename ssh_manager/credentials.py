"""Credential manager for SSH usernames/passwords independent of host configs."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from .secure_store import SecureStore, SecureStoreError
from .settings import _default_config_dir
from .persistence import write_json_atomic


@dataclass
class Credential:
    id: str
    name: str
    username: str


def _default_credentials_path() -> Path:
    return _default_config_dir() / "credentials.json"


class CredentialStore:
    """Persist credentials (metadata) and bind them to hosts, with secrets in SecureStore."""

    def __init__(self, secure_store: SecureStore, path: Optional[Path] = None) -> None:
        self._secure_store = secure_store
        self._path = path or _default_credentials_path()
        self._credentials: Dict[str, Credential] = {}
        self._bindings: Dict[str, str] = {}  # host_key -> credential_id
        self._load()

    # ------------------------------------------------------------------ helpers
    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(data, dict):
            return
        creds = data.get("credentials", [])
        bindings = data.get("bindings", {})
        if isinstance(creds, list):
            for entry in creds:
                if not isinstance(entry, dict):
                    continue
                cid = entry.get("id")
                name = entry.get("name")
                username = entry.get("username")
                if not isinstance(cid, str) or not isinstance(name, str) or not isinstance(username, str):
                    continue
                if cid and name and username:
                    self._credentials[cid] = Credential(id=cid, name=name, username=username)
        if isinstance(bindings, dict):
            for host_key, cred_id in bindings.items():
                if isinstance(host_key, str) and isinstance(cred_id, str) and cred_id in self._credentials:
                    self._bindings[host_key] = cred_id

    def _save(self) -> None:
        payload = {
            "credentials": [
                {"id": cred.id, "name": cred.name, "username": cred.username}
                for cred in self._credentials.values()
            ],
            "bindings": dict(self._bindings),
        }
        try:
            write_json_atomic(self._path, payload, mode=0o600)
        except OSError as exc:
            raise SecureStoreError(f"Failed to save credentials metadata: {exc}") from exc

    def _cred_key(self, cred_id: str) -> str:
        return f"credential:{cred_id}"

    # ------------------------------------------------------------------- api
    def list(self) -> List[Credential]:
        return sorted(self._credentials.values(), key=lambda c: c.name.lower())

    def get(self, cred_id: str) -> Optional[Credential]:
        return self._credentials.get(cred_id)

    def add(self, name: str, username: str, password: str) -> Credential:
        cred_id = uuid.uuid4().hex
        credential = Credential(id=cred_id, name=name, username=username)
        self.set_password(cred_id, password)
        self._credentials[cred_id] = credential
        try:
            self._save()
        except SecureStoreError:
            self._credentials.pop(cred_id, None)
            self.delete_password(cred_id)
            raise
        return credential

    def update(self, cred_id: str, name: str, username: str, password: Optional[str] = None) -> bool:
        cred = self._credentials.get(cred_id)
        if not cred:
            return False
        old_name = cred.name
        old_username = cred.username
        old_password = self.get_password(cred_id) if password is not None else None
        if password is not None:
            self.set_password(cred_id, password)
        cred.name = name
        cred.username = username
        try:
            self._save()
        except SecureStoreError:
            cred.name = old_name
            cred.username = old_username
            if password is not None:
                if old_password is None:
                    self.delete_password(cred_id)
                else:
                    self.set_password(cred_id, old_password)
            raise
        return True

    def delete(self, cred_id: str) -> None:
        old_credential = self._credentials.get(cred_id)
        old_bindings = dict(self._bindings)
        if cred_id in self._credentials:
            del self._credentials[cred_id]
        bindings_to_remove = [key for key, cid in self._bindings.items() if cid == cred_id]
        for key in bindings_to_remove:
            self._bindings.pop(key, None)
        try:
            self._save()
        except SecureStoreError:
            if old_credential:
                self._credentials[cred_id] = old_credential
            self._bindings = old_bindings
            raise
        self.delete_password(cred_id)

    def set_password(self, cred_id: str, password: str) -> None:
        try:
            self._secure_store.backend.set_password(self._secure_store.service_name, self._cred_key(cred_id), password)
        except Exception as exc:  # pragma: no cover - backend-specific
            raise SecureStoreError(str(exc)) from exc

    def get_password(self, cred_id: str) -> Optional[str]:
        try:
            return self._secure_store.backend.get_password(self._secure_store.service_name, self._cred_key(cred_id))
        except Exception as exc:  # pragma: no cover - backend-specific
            raise SecureStoreError(str(exc)) from exc

    def delete_password(self, cred_id: str) -> None:
        try:
            self._secure_store.backend.delete_password(self._secure_store.service_name, self._cred_key(cred_id))
        except Exception:  # pragma: no cover - best effort
            pass

    def bind_host(self, host_key: str, cred_id: Optional[str]) -> None:
        old_bindings = dict(self._bindings)
        if cred_id and cred_id in self._credentials:
            self._bindings[host_key] = cred_id
        else:
            self._bindings.pop(host_key, None)
        try:
            self._save()
        except SecureStoreError:
            self._bindings = old_bindings
            raise

    def bind_many(self, mapping: Dict[str, Optional[str]]) -> None:
        """Bind (credential id) or unbind (``None``) many hosts and save once.

        Unknown credential ids raise ``KeyError`` before anything changes; a
        failed save restores the previous bindings and re-raises.
        """

        unknown = sorted({cid for cid in mapping.values() if cid and cid not in self._credentials})
        if unknown:
            raise KeyError(f"Unknown credential id(s): {', '.join(unknown)}")
        if not mapping:
            return
        old_bindings = dict(self._bindings)
        for host_key, cred_id in mapping.items():
            if cred_id:
                self._bindings[host_key] = cred_id
            else:
                self._bindings.pop(host_key, None)
        try:
            self._save()
        except SecureStoreError:
            self._bindings = old_bindings
            raise

    def prune_bindings(self, valid_host_ids: set) -> int:
        """Drop bindings of app-store hosts that no longer exist (the cascade for deleted hosts).

        Only id-style keys are considered; legacy ``parent::child::alias`` keys of
        folder inventories are left alone. The credentials themselves are kept.
        """

        stale = [key for key in self._bindings if "::" not in key and key not in valid_host_ids]
        if not stale:
            return 0
        old_bindings = dict(self._bindings)
        for key in stale:
            self._bindings.pop(key, None)
        try:
            self._save()
        except SecureStoreError:
            self._bindings = old_bindings
            raise
        return len(stale)

    def legacy_bindings(self) -> Dict[str, str]:
        """``{parent::child::alias: credential id}`` left from folder inventories (for migration)."""

        return {key: cid for key, cid in self._bindings.items() if "::" in key}

    def rebind_host(self, old_host_key: str, new_host_key: str) -> None:
        old_bindings = dict(self._bindings)
        cred_id = self._bindings.pop(old_host_key, None)
        if cred_id and cred_id in self._credentials:
            self._bindings[new_host_key] = cred_id
            try:
                self._save()
            except SecureStoreError:
                self._bindings = old_bindings
                raise

    def remap_bindings_prefix(self, old_prefix: str, new_prefix: str) -> None:
        old_bindings = dict(self._bindings)
        updates: Dict[str, str] = {}
        for host_key, cred_id in list(self._bindings.items()):
            if host_key.startswith(old_prefix):
                updates[new_prefix + host_key[len(old_prefix):]] = cred_id
                self._bindings.pop(host_key, None)
        if updates:
            self._bindings.update(updates)
            try:
                self._save()
            except SecureStoreError:
                self._bindings = old_bindings
                raise

    def unbind_prefix(self, prefix: str) -> None:
        old_bindings = dict(self._bindings)
        changed = False
        for host_key in list(self._bindings):
            if host_key.startswith(prefix):
                self._bindings.pop(host_key, None)
                changed = True
        if changed:
            try:
                self._save()
            except SecureStoreError:
                self._bindings = old_bindings
                raise

    def credential_for_host(self, host_key: str) -> Optional[Credential]:
        cred_id = self._bindings.get(host_key)
        if cred_id:
            return self._credentials.get(cred_id)
        return None

    def binding_for_host(self, host_key: str) -> Optional[str]:
        return self._bindings.get(host_key)
