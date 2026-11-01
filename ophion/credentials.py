"""Credential manager for SSH usernames/passwords independent of host configs.

A **private** credential (``Credential.private``) is a host-specific password:
an ordinary credential bound to exactly one host. It is hidden from every
credential list (:meth:`CredentialStore.list`), and it never outlives its
binding: whenever a change of bindings leaves a private credential bound to no
host (host deleted, re-bound, unbound, group deleted, pruned), the credential
and its secret are deleted in the same save.
"""

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
    private: bool = False  # a host-specific password, bound to one host and hidden from lists
    based_on: Optional[str] = None  # private: the shared credential it replaced (duplicates bind that one)


@dataclass(frozen=True)
class PasswordChoice:
    """The host dialog's password decision (applied by ``CredentialController.apply_host_credentials``).

    ``host_specific=False``: use the bound credential's password. ``True``: a
    password specific to this host — ``password`` replaces it (``None`` keeps
    the saved one), ``clear`` removes it ("not set": asked at connect).
    """

    host_specific: bool = False
    password: Optional[str] = None
    clear: bool = False

    def __repr__(self) -> str:  # never print the secret
        return (f"PasswordChoice(host_specific={self.host_specific}, password=<{'set' if self.password else 'none'}>, "
                f"clear={self.clear})")

    __str__ = __repr__


KEEP = object()  # update_private: leave this field as it is


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
                based_on = entry.get("based_on")
                private = entry.get("private") is True
                if cid and name and (username or private):  # a host-specific one may leave User to ssh
                    self._credentials[cid] = Credential(
                        id=cid, name=name, username=username, private=private,
                        based_on=based_on if isinstance(based_on, str) and based_on else None,
                    )
        if isinstance(bindings, dict):
            for host_key, cred_id in bindings.items():
                if isinstance(host_key, str) and isinstance(cred_id, str) and cred_id in self._credentials:
                    self._bindings[host_key] = cred_id

    def _save(self) -> None:
        payload = {
            "credentials": [self._entry(cred) for cred in self._credentials.values()],
            "bindings": dict(self._bindings),
        }
        try:
            write_json_atomic(self._path, payload, mode=0o600)
        except OSError as exc:
            raise SecureStoreError(f"Failed to save credentials metadata: {exc}") from exc

    @staticmethod
    def _entry(cred: Credential) -> Dict[str, object]:
        entry: Dict[str, object] = {"id": cred.id, "name": cred.name, "username": cred.username}
        if cred.private:
            entry["private"] = True
            if cred.based_on:
                entry["based_on"] = cred.based_on
        return entry

    def _cred_key(self, cred_id: str) -> str:
        return f"credential:{cred_id}"

    def _unbound_private(self) -> List[str]:
        bound = set(self._bindings.values())
        return [cid for cid, cred in self._credentials.items() if cred.private and cid not in bound]

    def _commit(self, old_bindings: Dict[str, str], old_credentials: Optional[Dict[str, Credential]] = None) -> None:
        """Save after a change of bindings, dropping private credentials no host uses any more.

        A failed save restores bindings and credentials and re-raises; the
        dropped secrets are deleted only after the save succeeded.
        """

        old_credentials = dict(self._credentials) if old_credentials is None else old_credentials
        orphans = self._unbound_private()
        for cid in orphans:
            self._credentials.pop(cid, None)
        try:
            self._save()
        except SecureStoreError:
            self._bindings = old_bindings
            self._credentials = old_credentials
            raise
        for cid in orphans:
            self.delete_password(cid)

    # ------------------------------------------------------------------- api
    def list(self, include_private: bool = False) -> List[Credential]:
        """Credentials sorted by name; host-specific (private) ones only with ``include_private``."""

        return sorted((c for c in self._credentials.values() if include_private or not c.private),
                      key=lambda c: c.name.lower())

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
        old_credentials = dict(self._credentials)
        old_bindings = dict(self._bindings)
        if cred_id in self._credentials:
            del self._credentials[cred_id]
        bindings_to_remove = [key for key, cid in self._bindings.items() if cid == cred_id]
        for key in bindings_to_remove:
            self._bindings.pop(key, None)
        self._commit(old_bindings, old_credentials)
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
        self._commit(old_bindings)

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
        self._commit(old_bindings)

    def prune_bindings(self, valid_host_ids: set) -> int:
        """Drop bindings of app-store hosts that no longer exist (the cascade for deleted hosts).

        Only id-style keys are considered; legacy ``parent::child::alias`` keys of
        folder inventories are left alone. Shared credentials are kept; the
        private (host-specific) credentials of those hosts are deleted.
        """

        stale = [key for key in self._bindings if "::" not in key and key not in valid_host_ids]
        if not stale:
            return 0
        old_bindings = dict(self._bindings)
        for key in stale:
            self._bindings.pop(key, None)
        self._commit(old_bindings)
        return len(stale)

    def legacy_bindings(self) -> Dict[str, str]:
        """``{parent::child::alias: credential id}`` left from folder inventories (for migration)."""

        return {key: cid for key, cid in self._bindings.items() if "::" in key}

    def rebind_host(self, old_host_key: str, new_host_key: str) -> None:
        old_bindings = dict(self._bindings)
        cred_id = self._bindings.pop(old_host_key, None)
        if cred_id and cred_id in self._credentials:
            self._bindings[new_host_key] = cred_id
            self._commit(old_bindings)  # a private credential bound to the target before is dropped

    def remap_bindings_prefix(self, old_prefix: str, new_prefix: str) -> None:
        old_bindings = dict(self._bindings)
        updates: Dict[str, str] = {}
        for host_key, cred_id in list(self._bindings.items()):
            if host_key.startswith(old_prefix):
                updates[new_prefix + host_key[len(old_prefix):]] = cred_id
                self._bindings.pop(host_key, None)
        if updates:
            self._bindings.update(updates)
            self._commit(old_bindings)

    def unbind_prefix(self, prefix: str) -> None:
        old_bindings = dict(self._bindings)
        changed = False
        for host_key in list(self._bindings):
            if host_key.startswith(prefix):
                self._bindings.pop(host_key, None)
                changed = True
        if changed:
            self._commit(old_bindings)

    def credential_for_host(self, host_key: str) -> Optional[Credential]:
        cred_id = self._bindings.get(host_key)
        if cred_id:
            return self._credentials.get(cred_id)
        return None

    def binding_for_host(self, host_key: str) -> Optional[str]:
        return self._bindings.get(host_key)

    # ------------------------------------------------------- host-specific
    def private_for_host(self, host_key: str) -> Optional[Credential]:
        """The host's private (host-specific) credential, if it is bound to one."""

        cred = self.credential_for_host(host_key)
        return cred if cred and cred.private else None

    def is_private(self, cred_id: Optional[str]) -> bool:
        cred = self._credentials.get(cred_id or "")
        return bool(cred and cred.private)

    def has_password(self, cred_id: str) -> bool:
        """Whether a secret is stored for ``cred_id`` (raises :class:`SecureStoreError` when locked)."""

        return bool(self.get_password(cred_id))

    def create_private(self, host_key: str, username: str, password: Optional[str], *,
                       label: str = "", based_on: Optional[str] = None) -> Credential:
        """Bind ``host_key`` to a new private credential holding ``password`` — one save.

        A host that already has a private credential gets its password (and
        username) updated instead. ``password=None`` creates it without a
        secret ("not set": the user is asked at connect). ``based_on`` is the
        shared credential it replaces. On :class:`SecureStoreError` nothing is
        changed (secret, credential and binding are rolled back).
        """

        existing = self.private_for_host(host_key)
        if existing:
            self.set_private_password(host_key, password, username=username)
            return existing
        cred_id = uuid.uuid4().hex
        credential = Credential(id=cred_id, name=f"host:{label or host_key}", username=username, private=True,
                                based_on=based_on if based_on in self._credentials else None)
        if password:
            self.set_password(cred_id, password)
        old_credentials = dict(self._credentials)
        old_bindings = dict(self._bindings)
        self._credentials[cred_id] = credential
        self._bindings[host_key] = cred_id
        try:
            self._commit(old_bindings, old_credentials)
        except SecureStoreError:
            if password:
                self.delete_password(cred_id)
            raise
        return credential

    def set_private_password(self, host_key: str, password: Optional[str], *, username: Optional[str] = None) -> bool:
        """Replace (or with ``None`` clear) the secret of the host's private credential."""

        return self.update_private(host_key, password=password, username=username)

    def update_private(self, host_key: str, *, password: object = KEEP, username: Optional[str] = None,
                       based_on: object = KEEP) -> bool:
        """Change the host's private credential: secret (``None`` clears), username, ``based_on``.

        Returns ``False`` when the host has no private credential. On
        :class:`SecureStoreError` the previous secret and metadata are restored.
        """

        cred = self.private_for_host(host_key)
        if not cred:
            return False
        old_meta = (cred.username, cred.based_on)
        old_password = self.get_password(cred.id) if password is not KEEP else None
        if password is not KEEP:
            if password:
                self.set_password(cred.id, str(password))
            else:
                self.delete_password(cred.id)
        if username is not None:
            cred.username = username
        if based_on is not KEEP:
            cred.based_on = based_on if isinstance(based_on, str) and based_on in self._credentials else None
        if (cred.username, cred.based_on) != old_meta:
            try:
                self._save()
            except SecureStoreError:
                cred.username, cred.based_on = old_meta
                if password is not KEEP:
                    if old_password:
                        self.set_password(cred.id, old_password)
                    else:
                        self.delete_password(cred.id)
                raise
        return True

    def shared_for_copy(self, host_key: str) -> Optional[str]:
        """Credential id a copy of the host is bound to: its shared credential, never a private one."""

        cred = self.credential_for_host(host_key)
        if not cred:
            return None
        if not cred.private:
            return cred.id
        return cred.based_on if cred.based_on in self._credentials else None
