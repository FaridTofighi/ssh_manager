"""Encrypted export / import of shared credentials (``*.sshcred``) — no GTK.

Moves credentials between installations. The file is always encrypted with an
export passphrase (never the master password implicitly):

``{"format": "ssh-manager-credentials-export", "version": 1, "kdf": {"name": "scrypt", "n": 32768, "r": 8,
"p": 1, "salt": …}, "nonce": …, "data": <AES-256-GCM>}`` — the header (format, version, kdf) is the associated
data. The plaintext is ``{"credentials": [{"name", "username", "password"}]}``; host-specific (private)
credentials are not exported. Nothing here logs a password.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Sequence

from .aead import AeadError, derive, new_kdf, open_sealed, seal

FORMAT = "ssh-manager-credentials-export"
VERSION = 1
EXTENSION = ".sshcred"
MAX_BYTES = 10 * 1024 * 1024
POLICIES = ("skip", "rename", "reuse")


class CredentialExportError(Exception):
    """``code``: wrong_passphrase, damaged, newer, not_export, unsafe, unsupported, unreadable."""

    def __init__(self, code: str, message: str) -> None:
        Exception.__init__(self, message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ExportedCredential:
    name: str
    username: str
    password: str

    def __repr__(self) -> str:  # never print the secret
        return f"ExportedCredential(name={self.name!r}, username={self.username!r}, password=<hidden>)"


def _header(kdf: Dict[str, Any]) -> Dict[str, Any]:
    return {"format": FORMAT, "version": VERSION, "kdf": kdf}


def export_credentials(credential_store, passphrase: str, path: Path) -> int:
    """Write the shared credentials (with their passwords) encrypted with ``passphrase``; returns how many."""

    from .persistence import write_bytes_private

    items = []
    for cred in credential_store.list():
        password = credential_store.get_password(cred.id)
        items.append({"name": cred.name, "username": cred.username, "password": password or ""})
    kdf = new_kdf()
    header = _header(kdf)
    nonce, data = seal(derive(passphrase, kdf), json.dumps({"credentials": items}).encode("utf-8"), header)
    document = dict(header, nonce=nonce, data=data)
    write_bytes_private(Path(path), (json.dumps(document, indent=1) + "\n").encode("utf-8"))
    return len(items)


def read_credentials_export(path: Path, passphrase: str) -> List[ExportedCredential]:
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise CredentialExportError("unreadable", f"The file could not be read ({exc.strerror or exc}).") from exc
    if len(raw) > MAX_BYTES:
        raise CredentialExportError("damaged", "The file is too large for a credentials export.")
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise CredentialExportError("not_export", "This is not an Ophion credentials export.") from None
    if not isinstance(document, dict) or document.get("format") != FORMAT:
        raise CredentialExportError("not_export", "This is not an Ophion credentials export.")
    version = document.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise CredentialExportError("damaged", "The credentials export is damaged.")
    if version > VERSION:
        raise CredentialExportError("newer", "This file was created by a newer Ophion.")
    kdf = document.get("kdf")
    try:
        key = derive(passphrase, kdf)  # checks the parameters first
        plain = open_sealed(key, document.get("nonce"), document.get("data"), _header(kdf))
    except AeadError as exc:
        code = "wrong_passphrase" if exc.code == "auth" else exc.code
        raise CredentialExportError(code, exc.message) from None
    try:
        payload = json.loads(plain.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise CredentialExportError("damaged", "The credentials export is damaged.") from None
    items: List[ExportedCredential] = []
    for item in payload.get("credentials", []) if isinstance(payload, dict) else []:
        if isinstance(item, dict) and all(isinstance(item.get(k), str) for k in ("name", "username", "password")) \
                and item["name"].strip():
            items.append(ExportedCredential(item["name"].strip(), item["username"], item["password"]))
    return items


@dataclass
class ImportResult:
    created: List[str] = field(default_factory=list)
    renamed: List[str] = field(default_factory=list)  # stored names that got " (2)" …
    reused: List[str] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)


def import_credentials(credential_store, items: Sequence[ExportedCredential], policy: str = "rename") -> ImportResult:
    """Create the credentials. On a name already used (any case): ``skip``, ``rename`` (" (2)" …) or ``reuse``
    (nothing to do when the existing one has the same username and password; otherwise renamed)."""

    if policy not in POLICIES:
        raise ValueError(policy)
    result = ImportResult()
    for item in items:
        existing = {c.name.casefold(): c for c in credential_store.list(include_private=True)}
        clash = existing.get(item.name.casefold())
        name = item.name
        if clash is not None:
            if policy == "skip":
                result.skipped.append(item.name)
                continue
            if policy == "reuse" and not clash.private and clash.username == item.username and \
                    credential_store.get_password(clash.id) == item.password:
                result.reused.append(clash.name)
                continue
            number = 2
            while f"{item.name} ({number})".casefold() in existing:
                number += 1
            name = f"{item.name} ({number})"
            result.renamed.append(name)
        credential_store.add(name, item.username, item.password)
        result.created.append(name)
    return result


__all__ = ["export_credentials", "read_credentials_export", "import_credentials", "ExportedCredential",
           "ImportResult", "CredentialExportError", "EXTENSION", "FORMAT", "POLICIES"]
