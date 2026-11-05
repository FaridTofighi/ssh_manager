"""The Ophion export file (``*.ophion``; ``*.sshm`` before): constants, canonical JSON, integrity, encryption (no GTK).

A plain export is one UTF-8 JSON document (``format: ssh-manager-export``); the
exporter (:mod:`ophion.exporters.native`) builds it and the importer
(:mod:`ophion.importers.native`) reads it. ``integrity.sha256`` is the
SHA-256 of the canonical compact JSON (sorted keys, ``(",", ":")``) of every
top-level key except ``exported_at`` and ``integrity``.

With a passphrase the document is encrypted with AES-256-GCM; the key comes
from PBKDF2-HMAC-SHA256 (:data:`KDF_ITERATIONS`, a random 16-byte salt) and the
header (format, version, kdf, cipher, nonce) is the associated data, so it
cannot be changed without the decryption failing. See ``docs/export-import.md``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from typing import Any, Dict, Optional

# Internal identifiers are frozen for compatibility; only the file name extension is branded. The format ids,
# versions and the encryption header are authenticated (AES-GCM associated data) and hashed: renaming them
# would make every existing export unreadable. An .ophion file renamed to .sshm opens in older versions.
FORMAT = "ssh-manager-export"
ENCRYPTED_FORMAT = "ssh-manager-export-encrypted"
VERSION = 1
ENCRYPTED_VERSION = 1
EXTENSION = ".ophion"
LEGACY_EXTENSIONS = (".sshm", ".json")  # earlier exports: still read (the content decides, not the name)
ACCEPTED_EXTENSIONS = (EXTENSION,) + LEGACY_EXTENSIONS
MIME_TYPE = "application/x-ophion+json"
LEGACY_MIME_TYPE = "application/x-ssh-manager+json"  # files typed by SSH Manager still open
LEGACY_MIME_TYPES = (LEGACY_MIME_TYPE,)
MIME_TYPES = (MIME_TYPE,) + LEGACY_MIME_TYPES
KDF = "pbkdf2-sha256"
KDF_ITERATIONS = 600_000
MIN_KDF_ITERATIONS = 600_000
CIPHER = "aes-256-gcm"
SALT_BYTES = 16
NONCE_BYTES = 12
NOT_HASHED = ("exported_at", "integrity")


class NativeFormatError(Exception):
    """A file-level problem; ``code`` names it, ``message`` is shown to the user (never file content)."""

    def __init__(self, code: str, message: str) -> None:
        Exception.__init__(self, message)
        self.code = code
        self.message = message


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def integrity_hash(document: Dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json({k: v for k, v in document.items() if k not in NOT_HASHED})).hexdigest()


def check_integrity(document: Dict[str, Any]) -> str:
    """``"ok"``, ``"missing"`` (accepted) or ``"mismatch"`` (edited after the export)."""

    integrity = document.get("integrity")
    if not isinstance(integrity, dict) or not isinstance(integrity.get("sha256"), str):
        return "missing"
    return "ok" if integrity["sha256"] == integrity_hash(document) else "mismatch"


def dump_document(document: Dict[str, Any]) -> bytes:
    """UTF-8, no BOM, LF, one-space indent, keys in the builder's (stable) order."""

    return (json.dumps(document, ensure_ascii=False, indent=1) + "\n").encode("utf-8")


# ---------------------------------------------------------------- encryption
def _key(passphrase: str, salt: bytes, iterations: int) -> bytes:
    from cryptography.hazmat.primitives import hashes  # type: ignore
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC  # type: ignore

    return PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt,
                      iterations=iterations).derive(passphrase.encode("utf-8"))


def _header_aad(header: Dict[str, Any]) -> bytes:
    return canonical_json({k: header[k] for k in ("format", "version", "kdf", "cipher", "nonce")})


def encrypt_document(plain: bytes, passphrase: str, *, iterations: int = KDF_ITERATIONS) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # type: ignore

    salt, nonce = os.urandom(SALT_BYTES), os.urandom(NONCE_BYTES)
    header: Dict[str, Any] = {
        "format": ENCRYPTED_FORMAT,
        "version": ENCRYPTED_VERSION,
        "kdf": {"name": KDF, "iterations": iterations, "salt": base64.b64encode(salt).decode("ascii")},
        "cipher": CIPHER,
        "nonce": base64.b64encode(nonce).decode("ascii"),
    }
    data = AESGCM(_key(passphrase, salt, iterations)).encrypt(nonce, plain, _header_aad(header))
    header["data"] = base64.b64encode(data).decode("ascii")
    return (json.dumps(header, indent=1) + "\n").encode("utf-8")


def is_encrypted(document: Any) -> bool:
    return isinstance(document, dict) and document.get("format") == ENCRYPTED_FORMAT


def decrypt_document(header: Dict[str, Any], passphrase: str) -> bytes:
    """The plain document's bytes; :class:`NativeFormatError` ``wrong_passphrase`` / ``unsupported`` / ``damaged``."""

    from cryptography.exceptions import InvalidTag  # type: ignore
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # type: ignore

    version = header.get("version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise NativeFormatError("damaged", "The encrypted export is damaged.")
    if version > ENCRYPTED_VERSION:
        raise NativeFormatError("newer", "This file was created by a newer Ophion.")
    kdf = header.get("kdf")
    if header.get("cipher") != CIPHER or not isinstance(kdf, dict) or kdf.get("name") != KDF:
        raise NativeFormatError("unsupported", "This encrypted export uses a method Ophion does not know.")
    iterations = kdf.get("iterations")
    if not isinstance(iterations, int) or isinstance(iterations, bool) or iterations < MIN_KDF_ITERATIONS \
            or iterations > 50_000_000:
        raise NativeFormatError("damaged", "The encrypted export is damaged (key settings).")
    try:
        salt = base64.b64decode(str(kdf.get("salt", "")), validate=True)
        nonce = base64.b64decode(str(header.get("nonce", "")), validate=True)
        data = base64.b64decode(str(header.get("data", "")), validate=True)
    except ValueError:
        raise NativeFormatError("damaged", "The encrypted export is damaged.") from None
    if len(salt) < SALT_BYTES or len(nonce) != NONCE_BYTES or not data:
        raise NativeFormatError("damaged", "The encrypted export is damaged.")
    try:
        return AESGCM(_key(passphrase, salt, iterations)).decrypt(nonce, data, _header_aad(header))
    except InvalidTag:
        raise NativeFormatError("wrong_passphrase",
                                "The passphrase is wrong, or the file was changed.") from None


def default_file_name(now: Optional[float] = None) -> str:
    import time

    from .identity import APP_SLUG

    return time.strftime(f"{APP_SLUG}-%Y%m%d-%H%M", time.localtime(now)) + EXTENSION


__all__ = [
    "FORMAT", "ENCRYPTED_FORMAT", "VERSION", "EXTENSION", "MIME_TYPE", "LEGACY_MIME_TYPE", "MIME_TYPES", "NativeFormatError", "canonical_json",
    "integrity_hash", "check_integrity", "dump_document", "encrypt_document", "decrypt_document", "is_encrypted",
    "default_file_name",
]
