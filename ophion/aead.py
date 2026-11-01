"""Passphrase-based encryption helpers: KDF parameters, AES-256-GCM with an authenticated header (no GTK).

Used by the credentials export (``*.sshcred``) and the keybox. Keys come from a
passphrase through scrypt (or Argon2id when the installed ``cryptography``
has it, or PBKDF2-HMAC-SHA256 for reading older files); the parameters are
stored with the data and checked against minimums before use, so a file
cannot ask for a weak derivation. AES-256-GCM uses a fresh 96-bit nonce per
encryption, and the header is the associated data: changing it makes the
decryption fail. Nothing here logs a passphrase, a key or a plaintext.
"""

from __future__ import annotations

import base64
import json
import os
from typing import Any, Dict, Mapping, Tuple, Union

SALT_BYTES = 16
NONCE_BYTES = 12
MIN_SCRYPT_N = 2 ** 15
MIN_PBKDF2_ITERATIONS = 600_000
MIN_ARGON2_MEMORY_KIB = 32 * 1024


class AeadError(Exception):
    """``code``: ``auth`` (wrong passphrase or changed data), ``unsafe`` / ``unsupported`` (KDF), ``damaged``."""

    def __init__(self, code: str, message: str) -> None:
        Exception.__init__(self, message)
        self.code = code
        self.message = message


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def unb64(text: Any, what: str = "field") -> bytes:
    try:
        return base64.b64decode(str(text), validate=True)
    except ValueError:
        raise AeadError("damaged", f"The {what} is not valid base64.") from None


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def argon2_available() -> bool:
    try:
        from cryptography.hazmat.primitives.kdf.argon2 import Argon2id  # type: ignore # noqa: F401
    except ImportError:
        return False
    return True


def new_kdf(*, scrypt_n: int = MIN_SCRYPT_N, prefer_argon2: bool = False) -> Dict[str, Any]:
    """Fresh KDF parameters with a random salt (Argon2id only when asked for and available)."""

    salt = b64(os.urandom(SALT_BYTES))
    if prefer_argon2 and argon2_available():
        return {"name": "argon2id", "time_cost": 3, "memory_kib": 64 * 1024, "parallelism": 4, "salt": salt}
    return {"name": "scrypt", "n": scrypt_n, "r": 8, "p": 1, "salt": salt}


def check_kdf(kdf: Any) -> None:
    """:class:`AeadError` for unknown, malformed or too weak parameters."""

    if not isinstance(kdf, Mapping):
        raise AeadError("damaged", "The key settings are missing.")
    name = kdf.get("name")

    def number(key: str, low: int, high: int) -> int:
        value = kdf.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
            raise AeadError("unsafe", "The key settings are damaged or unsafe.")
        return value

    if name == "scrypt":
        n = number("n", MIN_SCRYPT_N, 2 ** 22)
        if n & (n - 1):
            raise AeadError("unsafe", "The key settings are damaged or unsafe.")
        number("r", 8, 32)
        number("p", 1, 16)
    elif name == "pbkdf2-sha256":
        number("iterations", MIN_PBKDF2_ITERATIONS, 50_000_000)
    elif name == "argon2id":
        number("time_cost", 1, 100)
        number("memory_kib", MIN_ARGON2_MEMORY_KIB, 4 * 1024 * 1024)
        number("parallelism", 1, 64)
        if not argon2_available():
            raise AeadError("unsupported", "This file needs Argon2id, which the installed 'cryptography' package "
                                           "does not provide; update it.")
    else:
        raise AeadError("unsupported", "This file uses a key method Ophion does not know.")
    if len(unb64(kdf.get("salt"), "salt")) < SALT_BYTES:
        raise AeadError("unsafe", "The key settings are damaged or unsafe.")


def derive(secret: Union[str, bytes], kdf: Mapping[str, Any]) -> bytes:
    """A 32-byte key from ``secret`` with the (checked) parameters ``kdf``."""

    check_kdf(kdf)
    data = secret.encode("utf-8") if isinstance(secret, str) else bytes(secret)
    salt = unb64(kdf["salt"], "salt")
    name = kdf["name"]
    if name == "scrypt":
        from cryptography.hazmat.primitives.kdf.scrypt import Scrypt  # type: ignore

        return Scrypt(salt=salt, length=32, n=kdf["n"], r=kdf["r"], p=kdf["p"]).derive(data)
    if name == "pbkdf2-sha256":
        from cryptography.hazmat.primitives import hashes  # type: ignore
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC  # type: ignore

        return PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=kdf["iterations"]).derive(data)
    from cryptography.hazmat.primitives.kdf.argon2 import Argon2id  # type: ignore

    return Argon2id(salt=salt, length=32, iterations=kdf["time_cost"], lanes=kdf["parallelism"],
                    memory_cost=kdf["memory_kib"]).derive(data)


def seal(key: bytes, plaintext: bytes, header: Mapping[str, Any]) -> Tuple[str, str]:
    """``(nonce, data)`` in base64: AES-256-GCM of ``plaintext`` with ``header`` authenticated."""

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # type: ignore

    nonce = os.urandom(NONCE_BYTES)
    return b64(nonce), b64(AESGCM(bytes(key)).encrypt(nonce, plaintext, canonical(dict(header))))


def open_sealed(key: bytes, nonce: Any, data: Any, header: Mapping[str, Any]) -> bytes:
    """The plaintext; :class:`AeadError` ``auth`` when the key is wrong or anything was changed."""

    from cryptography.exceptions import InvalidTag  # type: ignore
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # type: ignore

    nonce_bytes = unb64(nonce, "nonce")
    if len(nonce_bytes) != NONCE_BYTES:
        raise AeadError("damaged", "The file is damaged (nonce).")
    try:
        return AESGCM(bytes(key)).decrypt(nonce_bytes, unb64(data, "data"), canonical(dict(header)))
    except InvalidTag:
        raise AeadError("auth", "The passphrase is wrong, or the file was changed.") from None


__all__ = ["AeadError", "new_kdf", "check_kdf", "derive", "seal", "open_sealed", "canonical", "b64", "unb64",
           "argon2_available"]
