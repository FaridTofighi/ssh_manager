"""Moving the removed Credential Vault's entries into Credentials (migration only; no GTK).

The Credential Vault (``vault.json``: its own password, PBKDF2-HMAC-SHA256 with
200 000 iterations, Fernet, an HMAC "verifier") was removed: with one master
key for every secret it only duplicated Credentials. This module is the
**only** place that still knows its file format, and it only *reads* it. It
exists for the migration and may be deleted in a later release.

:func:`convert_vault` moves every entry into a shared credential (reusing one
with the same username **and** password), rebinds the hosts that used it
(``metadata["vault_id"]``), verifies everything by reading it back and only
then removes ``vault_id`` from the hosts. Deleting ``vault.json`` is a separate,
explicit step (:func:`secure_delete_vault_files`). Re-running after an
interruption reuses what was created and finishes; running twice changes
nothing. Nothing here logs or reports a password.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

LOGGER = logging.getLogger(__name__)
FILE_NAME = "vault.json"
ITERATIONS = 200_000
VAULT_ID = "vault_id"
# Host metadata the removed feature wrote: kept as it is until the conversion, never exported or used.
LEGACY_METADATA_KEYS = (VAULT_ID,)


class LegacyVaultError(Exception):
    """The old file cannot be read (missing cryptography, damaged file). Never carries a secret."""


@dataclass(frozen=True)
class LegacyEntry:
    id: str
    label: str
    username: str
    password: str

    def __repr__(self) -> str:  # never print the secret
        return f"LegacyEntry(id={self.id!r}, label={self.label!r}, username={self.username!r}, password=<hidden>)"


def legacy_vault_path() -> Path:
    """``<config>/vault.json``, or the old fixed ``~/.config/ssh-manager/vault.json`` when only that one exists."""

    from .settings import _default_config_dir

    preferred = _default_config_dir() / FILE_NAME
    legacy = Path.home() / ".config" / "ssh-manager" / FILE_NAME
    if not preferred.exists() and legacy.exists():
        return legacy
    return preferred


def vault_files(path: Path) -> List[Path]:
    """The file and the copies next to it (``vault.json.bak`` …) that removal deletes."""

    return [p for p in sorted(path.parent.glob(path.name + "*")) if p.is_file()]


def read_legacy_vault(path: Path, password: str) -> Optional[List[LegacyEntry]]:
    """The entries, or ``None`` for a wrong password. :class:`LegacyVaultError` when the file cannot be read."""

    try:
        from cryptography.fernet import Fernet, InvalidToken  # type: ignore
        from cryptography.hazmat.primitives import hashes  # type: ignore
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC  # type: ignore
    except ImportError as exc:  # pragma: no cover - cryptography is a dependency
        raise LegacyVaultError("The Python package 'cryptography' is missing; the old vault cannot be read.") from exc
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        salt = base64.b64decode(data["salt"])
        verifier = base64.b64decode(data["master_hash"])
        token = base64.b64decode(data["data"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise LegacyVaultError("The old vault file is damaged or unreadable.") from exc
    key = base64.urlsafe_b64encode(PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt,
                                              iterations=ITERATIONS).derive(password.encode("utf-8")))
    if not hmac.compare_digest(hmac.new(key, b"vault", hashlib.sha256).digest(), verifier):
        return None
    try:
        items = json.loads(Fernet(key).decrypt(token).decode("utf-8"))
    except (InvalidToken, ValueError) as exc:
        raise LegacyVaultError("The old vault file is damaged.") from exc
    entries: List[LegacyEntry] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        values = [item.get(name) for name in ("id", "label", "username", "password")]
        if all(isinstance(v, str) for v in values) and values[0] and values[1] and values[2]:
            entries.append(LegacyEntry(*values))  # type: ignore[arg-type]
    return entries


# ------------------------------------------------------------------ conversion
@dataclass
class ConversionReport:
    entries: int = 0
    created: List[str] = field(default_factory=list)  # credential names created
    reused: List[str] = field(default_factory=list)  # credential names reused (same username and password)
    hosts_rebound: int = 0
    hosts_kept_private: List[str] = field(default_factory=list)  # host aliases: their host-specific password stays
    dangling: List[str] = field(default_factory=list)  # host aliases whose vault_id pointed to no entry
    warnings: List[str] = field(default_factory=list)
    verified: bool = False
    hosts_cleaned: int = 0

    def summary(self) -> str:
        lines = [f"Entries: {self.entries}", f"Credentials created: {len(self.created)}",
                 f"Credentials reused: {len(self.reused)}", f"Hosts moved to a credential: {self.hosts_rebound}"]
        if self.hosts_kept_private:
            lines.append("Host-specific password kept for: " + ", ".join(self.hosts_kept_private))
        if self.dangling:
            lines.append("Pointed to a missing entry (link removed): " + ", ".join(self.dangling))
        lines += self.warnings
        return "\n".join(lines)


def _unique_name(label: str, taken: Iterable[str]) -> str:
    used = {name.casefold() for name in taken}
    if label.casefold() not in used:
        return label
    number = 2
    while f"{label} ({number})".casefold() in used:
        number += 1
    return f"{label} ({number})"


def _hosts_with_ids(manager) -> List[object]:
    return [host for children in manager.tree.values() for group in children.values() for host in group.hosts
            if isinstance((host.metadata or {}).get(VAULT_ID), str) and host.metadata.get(VAULT_ID)]


def binding_key(host) -> str:
    """The key ``CredentialStore`` binds a host by: its id (inventory store), else ``parent::child::alias``."""

    from .host_utils import host_config_key

    return getattr(host, "id", "") or host_config_key(host)


def convert_vault(
    entries: Sequence[LegacyEntry],
    credential_store,
    manager,
    *,
    resolve: Optional[Callable[[object], Tuple[Optional[str], str]]] = None,
) -> ConversionReport:
    """Move ``entries`` into shared credentials and rebind the hosts (see the module docstring).

    ``resolve(host) -> (password, source)`` is the app's password resolution, used to verify the hosts
    afterwards. Raises :class:`LegacyVaultError` when the verification fails: then no ``vault_id`` has
    been removed and ``vault.json`` is untouched.
    """

    report = ConversionReport(entries=len(entries))
    shared = credential_store.list()
    existing: Dict[Tuple[str, str], object] = {}
    for cred in shared:
        try:
            secret = credential_store.get_password(cred.id)
        except Exception:  # a locked store: nothing to reuse
            secret = None
        if secret is not None:
            existing.setdefault((cred.username, secret), cred)
    credential_for_entry: Dict[str, str] = {}
    for entry in entries:
        found = existing.get((entry.username, entry.password))
        if found is not None:
            credential_for_entry[entry.id] = found.id  # type: ignore[attr-defined]
            if found.name not in report.reused:  # type: ignore[attr-defined]
                report.reused.append(found.name)  # type: ignore[attr-defined]
            continue
        name = _unique_name(entry.label, [c.name for c in credential_store.list(include_private=True)])
        created = credential_store.add(name, entry.username, entry.password)
        existing[(entry.username, entry.password)] = created
        credential_for_entry[entry.id] = created.id
        report.created.append(created.name)

    hosts = _hosts_with_ids(manager)
    bindings: Dict[str, Optional[str]] = {}
    to_clean = []
    for host in hosts:
        vault_id = host.metadata[VAULT_ID]
        key = binding_key(host)
        if vault_id not in credential_for_entry:
            report.dangling.append(host.alias)
            to_clean.append(host)
            continue
        current = credential_store.credential_for_host(key)
        if current is not None and getattr(current, "private", False):
            report.hosts_kept_private.append(host.alias)  # its own password stays (never deleted)
            to_clean.append(host)
            continue
        bindings[key] = credential_for_entry[vault_id]
        to_clean.append(host)
    if bindings:
        credential_store.bind_many(bindings)
    report.hosts_rebound = len(bindings)

    # Verify by reading back, before anything in the old data is touched.
    passwords = {entry.id: entry.password for entry in entries}
    for entry_id, cred_id in credential_for_entry.items():
        stored = credential_store.get_password(cred_id)
        if stored is None or not hmac.compare_digest(stored.encode(), passwords[entry_id].encode()):
            raise LegacyVaultError("A moved password could not be read back; nothing was removed.")
    for host in hosts:
        key = binding_key(host)
        if key in bindings and credential_store.binding_for_host(key) != bindings[key]:
            raise LegacyVaultError("A host could not be bound to its new credential; nothing was removed.")
        if resolve is not None and key in bindings:
            password, source = resolve(host)
            expected = passwords.get(host.metadata[VAULT_ID], "")
            if source != "credential" or password is None or not hmac.compare_digest(password.encode(),
                                                                                       expected.encode()):
                raise LegacyVaultError("A host does not resolve its password from its new credential; "
                                       "nothing was removed.")
    report.verified = True
    report.hosts_cleaned = _remove_links(manager, to_clean)
    return report


def _remove_links(manager, hosts: Sequence[object]) -> int:
    """Drop ``vault_id`` from the hosts' metadata: one transaction (inventory store, after a backup) or per host."""

    if not hosts:
        return 0
    store = getattr(manager, "store", None)
    if store is not None:
        manager.backup_before("vault-removal")
        with store.transaction():
            for host in hosts:
                row = store.get_host(host.id) if host.id else None
                if row is None or VAULT_ID not in row.metadata:
                    continue
                store.update_host(row.id, metadata={k: v for k, v in row.metadata.items() if k != VAULT_ID})
        manager.refresh()
        return len(hosts)
    cleaned = 0
    for host in hosts:
        host.metadata = {k: v for k, v in host.metadata.items() if k != VAULT_ID}
        ok, _message = manager.update_host(host.parent_group, host.child_group, host.alias, host)
        cleaned += 1 if ok else 0
    return cleaned


def secure_delete(path: Path) -> bool:
    """Overwrite with zeros, fsync, unlink (best effort: SSDs and journaling file systems may keep copies)."""

    try:
        size = path.stat().st_size
        with open(path, "r+b") as handle:
            handle.write(b"\0" * size)
            handle.flush()
            os.fsync(handle.fileno())
        path.unlink()
        return True
    except FileNotFoundError:
        return True
    except OSError as exc:
        LOGGER.warning("old vault: %s could not be removed (%s)", path.name, type(exc).__name__)
        return False


def secure_delete_vault_files(path: Path) -> bool:
    return all([secure_delete(p) for p in vault_files(path)])


__all__ = ["LEGACY_METADATA_KEYS", "LegacyEntry", "LegacyVaultError", "ConversionReport", "legacy_vault_path", "read_legacy_vault",
           "convert_vault", "secure_delete", "secure_delete_vault_files", "vault_files", "binding_key"]
