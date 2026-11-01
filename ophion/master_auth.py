"""Master password hashing and verification helpers."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os

PBKDF2_ALGORITHM = "pbkdf2_sha256"
PBKDF2_ITERATIONS = 200_000


def hash_master_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        PBKDF2_ITERATIONS,
    )
    return "$".join(
        [
            PBKDF2_ALGORITHM,
            str(PBKDF2_ITERATIONS),
            base64.b64encode(salt).decode("ascii"),
            base64.b64encode(digest).decode("ascii"),
        ]
    )


def verify_master_password(password: str, stored_hash: str) -> bool:
    if not stored_hash:
        return False
    if stored_hash.startswith(f"{PBKDF2_ALGORITHM}$"):
        return _verify_pbkdf2(password, stored_hash)
    return _verify_legacy_sha256(password, stored_hash)


def master_hash_needs_upgrade(stored_hash: str) -> bool:
    return bool(stored_hash) and not stored_hash.startswith(f"{PBKDF2_ALGORITHM}$")


def _verify_pbkdf2(password: str, stored_hash: str) -> bool:
    try:
        algorithm, iterations_text, salt_text, digest_text = stored_hash.split("$", 3)
        if algorithm != PBKDF2_ALGORITHM:
            return False
        iterations = int(iterations_text)
        salt = base64.b64decode(salt_text)
        expected = base64.b64decode(digest_text)
    except (ValueError, TypeError):
        return False
    actual = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        iterations,
    )
    return hmac.compare_digest(actual, expected)


def _verify_legacy_sha256(password: str, stored_hash: str) -> bool:
    if len(stored_hash) != 64:
        return False
    try:
        int(stored_hash, 16)
    except ValueError:
        return False
    digest = hashlib.sha256(password.encode("utf-8")).hexdigest()
    return hmac.compare_digest(digest, stored_hash)
