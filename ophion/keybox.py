"""The keybox: one random data key (DEK) that encrypts every secret, wrapped by the master password (no GTK).

``keybox.json`` (config folder, 0600 in a 0700 folder, written atomically, the
previous version kept as ``keybox.json.bak``)::

    {"format": "ssh-manager-keybox", "version": 1, "id": "<16 bytes, base64>", "created": "<UTC ISO>",
     "unlockers": [{"type": "passphrase", "kdf": {...}, "nonce": "...", "wrapped": "..."},
                   {"type": "recovery", "kdf": {...}, "nonce": "...", "wrapped": "..."}],
     "check": {"nonce": "...", "data": "..."}}

* The **DEK** (32 random bytes) is never stored in clear; it lives in a
  ``bytearray`` while unlocked and :meth:`Keybox.forget` zeroes it (best effort).
* Each unlocker wraps it with AES-256-GCM under a key derived from a secret
  (the master password, or the recovery code) with scrypt (or Argon2id when
  ``cryptography`` has it); ``format``, ``version``, ``id``, the unlocker's
  ``type`` and ``kdf`` are the associated data. A wrong password fails the tag:
  that is the verification. Weak or unknown parameters are refused on read.
* ``check`` is ``b"ssh-manager-keybox-check"`` encrypted with the DEK: it
  verifies a DEK that comes from elsewhere (the OS keyring, "Remember").
* Stores get **subkeys**: HKDF-SHA256(DEK, salt = the keybox id, info =
  ``ssh-manager/<store>/v1``), so one store's format never hands out the DEK.
* Changing the master password only re-wraps the DEK (one atomic write); the
  stores are untouched. The password and the recovery code are never stored.
"""

from __future__ import annotations

import base64
import hmac
import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from . import aead

FORMAT = "ssh-manager-keybox"
VERSION = 1
FILE_NAME = "keybox.json"
CHECK = b"ssh-manager-keybox-check"
KEYBOX_SCRYPT_N = 2 ** 17  # ~0.3 s on the development machine (scrypt n=2^15 took ~75 ms there)
RECOVERY_BYTES = 20  # 160 bits
STORES = ("secrets", "macros", "export")


class KeyboxError(Exception):
    """``code``: damaged, unsafe, unsupported, newer, missing, locked, write. Never carries a secret."""

    def __init__(self, code: str, message: str) -> None:
        Exception.__init__(self, message)
        self.code = code
        self.message = message


def default_keybox_path() -> Path:
    from .settings import _default_config_dir

    return _default_config_dir() / FILE_NAME


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def write_private_atomic(path: Path, data: bytes, *, backup: bool = True) -> None:
    """``data`` to ``path``: the folder 0700, a 0600 temporary file in it, fsync, rename, fsync of the folder.

    With ``backup`` the previous file is kept as ``<name>.bak`` (also written atomically) first.
    """

    from .persistence import write_bytes_private

    folder = path.parent
    created = not folder.exists()
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    if created:
        os.chmod(folder, 0o700)
    if backup and path.exists():
        write_bytes_private(path.with_name(path.name + ".bak"), path.read_bytes())
    write_bytes_private(path, data)
    try:
        fd = os.open(folder, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def format_recovery_code(raw: bytes) -> str:
    text = base64.b32encode(raw).decode("ascii")  # 32 characters for 20 bytes
    return "-".join(text[i:i + 4] for i in range(0, len(text), 4))


def normalize_recovery_code(code: str) -> str:
    return "".join(ch for ch in code.upper() if ch.isalnum()).replace("0", "O").replace("1", "I").replace("8", "B")


class Keybox:
    """Load or create with :meth:`load` / :meth:`create`; then :meth:`unlock` (all methods are thread-safe)."""

    def __init__(self, path: Path, document: Dict[str, Any]) -> None:
        self.path = Path(path)
        self._document = document
        self._dek: Optional[bytearray] = None
        self._lock = threading.RLock()

    # ------------------------------------------------------------- creating
    @classmethod
    def create(cls, password: str, path: Optional[Path] = None, *, scrypt_n: int = KEYBOX_SCRYPT_N) -> "Keybox":
        """A new keybox (refuses to replace an existing file), unlocked."""

        path = Path(path or default_keybox_path())
        if path.exists():
            raise KeyboxError("exists", "A key file already exists; it is never replaced.")
        if not password:
            raise KeyboxError("locked", "A master password is needed.")
        dek = bytearray(os.urandom(32))
        document: Dict[str, Any] = {"format": FORMAT, "version": VERSION, "id": aead.b64(os.urandom(16)),
                                    "created": _now(), "unlockers": []}
        box = cls(path, document)
        box._scrypt_n = scrypt_n
        box._dek = dek
        document["unlockers"].append(box._wrap("passphrase", password))
        nonce, data = aead.seal(bytes(dek), CHECK, box._check_header())
        document["check"] = {"nonce": nonce, "data": data}
        box._save()
        return box

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "Keybox":
        path = Path(path or default_keybox_path())
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            raise KeyboxError("missing", "The key file keybox.json is missing.") from None
        except OSError as exc:
            raise KeyboxError("damaged", f"The key file cannot be read ({exc.strerror or exc}).") from None
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise KeyboxError("damaged", "The key file is damaged.") from None
        cls._validate(document)
        return cls(path, document)

    @staticmethod
    def _validate(document: Any) -> None:
        if not isinstance(document, dict) or document.get("format") != FORMAT:
            raise KeyboxError("damaged", "The key file is damaged or unsafe.")
        version = document.get("version")
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise KeyboxError("damaged", "The key file is damaged or unsafe.")
        if version > VERSION:
            raise KeyboxError("newer", "The key file was written by a newer Ophion.")
        try:
            if len(base64.b64decode(document.get("id", ""), validate=True)) != 16:
                raise ValueError
        except (ValueError, TypeError):
            raise KeyboxError("damaged", "The key file is damaged or unsafe.") from None
        unlockers = document.get("unlockers")
        check = document.get("check")
        if not isinstance(unlockers, list) or not unlockers or not isinstance(check, dict):
            raise KeyboxError("damaged", "The key file is damaged or unsafe.")
        types = [u.get("type") if isinstance(u, dict) else None for u in unlockers]
        if "passphrase" not in types or any(t not in ("passphrase", "recovery") for t in types) \
                or len(set(types)) != len(types):
            raise KeyboxError("damaged", "The key file is damaged or unsafe.")
        for unlocker in unlockers:
            try:
                aead.check_kdf(unlocker.get("kdf"))
            except aead.AeadError as exc:
                if exc.code == "unsupported":
                    raise KeyboxError("unsupported", exc.message) from None
                raise KeyboxError("unsafe", "The key file is damaged or unsafe.") from None
            if not isinstance(unlocker.get("nonce"), str) or not isinstance(unlocker.get("wrapped"), str):
                raise KeyboxError("damaged", "The key file is damaged or unsafe.")

    # --------------------------------------------------------------- state
    @property
    def id(self) -> str:
        return self._document["id"]

    @property
    def locked(self) -> bool:
        return self._dek is None

    @property
    def has_recovery(self) -> bool:
        return any(u.get("type") == "recovery" for u in self._document["unlockers"])

    @property
    def migrated(self) -> Optional[Mapping[str, Any]]:
        value = self._document.get("migrated")
        return value if isinstance(value, dict) else None

    # ----------------------------------------------------------- unlocking
    def _unlocker_header(self, unlocker: Mapping[str, Any]) -> Dict[str, Any]:
        return {"format": FORMAT, "version": self._document["version"], "id": self.id,
                "type": unlocker["type"], "kdf": unlocker["kdf"]}

    def _check_header(self) -> Dict[str, Any]:
        return {"format": FORMAT, "version": self._document["version"], "id": self.id, "type": "check"}

    def _wrap(self, kind: str, secret: str) -> Dict[str, Any]:
        assert self._dek is not None
        kdf = aead.new_kdf(scrypt_n=getattr(self, "_scrypt_n", KEYBOX_SCRYPT_N), prefer_argon2=True)
        unlocker: Dict[str, Any] = {"type": kind, "kdf": kdf}
        nonce, wrapped = aead.seal(aead.derive(secret, kdf), bytes(self._dek), self._unlocker_header(unlocker))
        unlocker.update(nonce=nonce, wrapped=wrapped)
        return unlocker

    def _try(self, kind: str, secret: str) -> Optional[bytearray]:
        for unlocker in self._document["unlockers"]:
            if unlocker["type"] != kind:
                continue
            try:
                key = aead.derive(secret, unlocker["kdf"])
                dek = aead.open_sealed(key, unlocker["nonce"], unlocker["wrapped"], self._unlocker_header(unlocker))
            except aead.AeadError as exc:
                if exc.code == "auth":
                    return None
                raise KeyboxError("damaged" if exc.code == "damaged" else exc.code, exc.message) from None
            candidate = bytearray(dek)
            return candidate if self._verify(candidate) else None
        return None

    def _verify(self, dek: bytearray) -> bool:
        check = self._document["check"]
        try:
            return hmac.compare_digest(aead.open_sealed(bytes(dek), check.get("nonce"), check.get("data"),
                                                        self._check_header()), CHECK)
        except aead.AeadError:
            return False

    def unlock(self, password: str) -> bool:
        """``True`` when ``password`` opens the keybox (a few hundred ms: call it off the GTK thread)."""

        dek = self._try("passphrase", password)
        with self._lock:
            if dek is not None:
                self._dek = dek
            return dek is not None

    def unlock_with_dek(self, dek: bytes) -> bool:
        candidate = bytearray(dek)
        if len(candidate) != 32 or not self._verify(candidate):
            return False
        with self._lock:
            self._dek = candidate
        return True

    def unlock_with_recovery(self, code: str) -> bool:
        dek = self._try("recovery", normalize_recovery_code(code))
        with self._lock:
            if dek is not None:
                self._dek = dek
            return dek is not None

    def dek(self) -> bytes:
        """The DEK (only to hand to the OS keyring for "Remember on this computer")."""

        with self._lock:
            if self._dek is None:
                raise KeyboxError("locked", "The keybox is locked.")
            return bytes(self._dek)

    def subkey(self, name: str) -> bytes:
        from cryptography.hazmat.primitives import hashes  # type: ignore
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF  # type: ignore

        with self._lock:
            if self._dek is None:
                raise KeyboxError("locked", "The keybox is locked.")
            return HKDF(algorithm=hashes.SHA256(), length=32, salt=base64.b64decode(self.id),
                        info=f"ssh-manager/{name}/v1".encode("ascii")).derive(bytes(self._dek))

    def forget(self) -> None:
        with self._lock:
            if self._dek is not None:
                for index in range(len(self._dek)):
                    self._dek[index] = 0
            self._dek = None

    # ------------------------------------------------------------- changes
    def _replace_unlocker(self, unlocker: Dict[str, Any], *, remove: str = "") -> None:
        kinds = {unlocker["type"]} if unlocker else set()
        if remove:
            kinds.add(remove)
        old = self._document
        new = dict(old, unlockers=[u for u in old["unlockers"] if u["type"] not in kinds] +
                   ([unlocker] if unlocker else []))
        self._document = new
        try:
            self._save()
        except BaseException:
            self._document = old
            raise

    def change_password(self, old: str, new: str) -> bool:
        """Re-wrap the DEK for ``new`` after verifying ``old``; ``False`` when ``old`` is wrong. Atomic."""

        if not new:
            raise KeyboxError("locked", "A master password is needed.")
        dek = self._try("passphrase", old)
        if dek is None:
            return False
        with self._lock:
            self._dek = dek
            self._replace_unlocker(self._wrap("passphrase", new))
        return True

    def reset_password(self, new: str) -> None:
        """A new master password for an unlocked keybox (after :meth:`unlock_with_recovery`)."""

        if not new:
            raise KeyboxError("locked", "A master password is needed.")
        with self._lock:
            if self._dek is None:
                raise KeyboxError("locked", "The keybox is locked.")
            self._replace_unlocker(self._wrap("passphrase", new))

    def add_recovery(self) -> str:
        """A new recovery code (shown once; the previous one stops working)."""

        with self._lock:
            if self._dek is None:
                raise KeyboxError("locked", "The keybox is locked.")
            code = format_recovery_code(secrets.token_bytes(RECOVERY_BYTES))
            self._replace_unlocker(self._wrap("recovery", normalize_recovery_code(code)))
            return code

    def remove_recovery(self) -> None:
        with self._lock:
            self._replace_unlocker({}, remove="recovery")

    def record_migration(self, info: Mapping[str, Any]) -> None:
        """Informational only (nothing depends on it)."""

        with self._lock:
            old = self._document
            self._document = dict(old, migrated=dict(info, at=_now()))
            try:
                self._save()
            except BaseException:
                self._document = old
                raise

    def _save(self) -> None:
        try:
            write_private_atomic(self.path, (json.dumps(self._document, indent=1) + "\n").encode("utf-8"))
        except OSError as exc:
            raise KeyboxError("write", f"The key file could not be written ({exc.strerror or exc}).") from None


def unlocker_names(document: Mapping[str, Any]) -> List[str]:
    return [u.get("type", "") for u in document.get("unlockers", [])]


__all__ = ["Keybox", "KeyboxError", "default_keybox_path", "write_private_atomic", "format_recovery_code",
           "normalize_recovery_code", "STORES", "FILE_NAME"]
