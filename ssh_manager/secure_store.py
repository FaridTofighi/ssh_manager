"""Secure password storage helper built on top of the system keyring.

This module provides a thin abstraction that can be swapped with an in-memory
backend for unit testing environments that do not expose a keyring service.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol
from pathlib import Path
import base64
import json
import logging
import os
import threading

from .persistence import write_json_atomic

try:
    import keyring  # type: ignore
    from keyring.errors import KeyringError  # type: ignore
except ImportError:  # pragma: no cover - optional dependency for CI
    keyring = None

    class KeyringError(Exception):
        """Fallback KeyringError when keyring cannot be imported."""

try:
    from cryptography.fernet import Fernet  # type: ignore
    from cryptography.hazmat.primitives import hashes  # type: ignore
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC  # type: ignore

    CRYPTO_AVAILABLE = True
except Exception:  # pragma: no cover - cryptography optional
    CRYPTO_AVAILABLE = False

LOGGER = logging.getLogger(__name__)


def _system_keyring() -> Optional[object]:
    """Return the system keyring backend, or None when it is missing or unusable."""

    if keyring is None:
        return None
    try:
        backend = keyring.get_keyring()
    except Exception:  # pragma: no cover - backend discovery is environment specific
        return None
    try:
        from keyring.backends import fail  # type: ignore

        if isinstance(backend, fail.Keyring):
            return None
    except Exception:  # pragma: no cover
        pass
    return backend


class KeyringBackend(Protocol):
    """Protocol describing the subset of keyring's API used by SecureStore."""

    def get_password(self, service_name: str, username: str) -> Optional[str]:
        ...

    def set_password(self, service_name: str, username: str, password: str) -> None:
        ...

    def delete_password(self, service_name: str, username: str) -> None:
        ...


class SecureStoreError(Exception):
    """Raised when the secure storage backend encounters an error."""


def _default_store_path() -> Path:
    home = os.environ.get("XDG_CONFIG_HOME")
    base = Path(home).expanduser() if home else Path.home() / ".config"
    return base / "ssh-manager" / "passwords.json"


class FileKeyring:
    """File-based keyring fallback when system keyring is unavailable.

    When a master password is supplied (and ``cryptography`` is installed) the
    file is encrypted with a key derived from it. Without one, secrets are kept
    in a private (0600) plain JSON file, matching the legacy format.
    """

    FORMAT_VERSION = 2
    KDF_ITERATIONS = 200_000

    def __init__(self, path: Optional[Path] = None, master_password: Optional[str] = None) -> None:
        self._path = path or _default_store_path()
        self._lock = threading.Lock()
        self._data: dict[tuple[str, str], str] = {}
        self._master_password = master_password if master_password and CRYPTO_AVAILABLE else None
        self._salt: Optional[bytes] = None
        self._locked = False
        self._load()

    # ------------------------------------------------------------ properties
    @property
    def encrypted(self) -> bool:
        return self._master_password is not None

    @property
    def locked(self) -> bool:
        """True when the file is encrypted but could not be decrypted."""

        return self._locked

    # --------------------------------------------------------------- keying
    def _fernet(self, password: str, salt: bytes) -> "Fernet":
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=self.KDF_ITERATIONS,
        )
        return Fernet(base64.urlsafe_b64encode(kdf.derive(password.encode("utf-8"))))

    # -------------------------------------------------------------- loading
    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(raw, dict):
            return
        if raw.get("version") == self.FORMAT_VERSION and "data" in raw:
            self._load_encrypted(raw)
            return
        self._data = self._entries_from_payload(raw)
        if self.encrypted and self._data:
            # Migrate a legacy plain-text file to the encrypted format.
            try:
                self._save()
            except KeyringError as exc:
                LOGGER.warning("Could not encrypt password file %s: %s", self._path, exc)

    def _load_encrypted(self, raw: dict) -> None:
        if not self.encrypted:
            LOGGER.warning("Password file %s is encrypted but no master password is available.", self._path)
            self._locked = True
            return
        try:
            salt = base64.b64decode(raw.get("salt", ""))
            token = base64.b64decode(raw.get("data", ""))
            plaintext = self._fernet(self._master_password, salt).decrypt(token)
            payload = json.loads(plaintext.decode("utf-8"))
        except Exception:
            LOGGER.warning("Password file %s could not be decrypted with the master password.", self._path)
            self._locked = True
            return
        self._salt = salt
        self._data = self._entries_from_payload(payload if isinstance(payload, dict) else {})

    @staticmethod
    def _entries_from_payload(raw: dict) -> dict[tuple[str, str], str]:
        data: dict[tuple[str, str], str] = {}
        for svc, entries in raw.items():
            if isinstance(svc, str) and isinstance(entries, dict):
                for user, pwd in entries.items():
                    if isinstance(user, str) and isinstance(pwd, str):
                        data[(svc, user)] = pwd
        return data

    # --------------------------------------------------------------- saving
    def _save(self) -> None:
        if self._locked:
            raise KeyringError("Password file is locked; refusing to overwrite it.")
        payload: dict[str, dict[str, str]] = {}
        for (svc, user), pwd in self._data.items():
            payload.setdefault(svc, {})[user] = pwd
        document: object = payload
        if self.encrypted:
            if self._salt is None:
                self._salt = os.urandom(16)
            token = self._fernet(self._master_password, self._salt).encrypt(
                json.dumps(payload).encode("utf-8")
            )
            document = {
                "version": self.FORMAT_VERSION,
                "kdf": "pbkdf2_sha256",
                "iterations": self.KDF_ITERATIONS,
                "salt": base64.b64encode(self._salt).decode("ascii"),
                "data": base64.b64encode(token).decode("ascii"),
            }
        try:
            write_json_atomic(self._path, document, mode=0o600)
        except OSError as exc:
            raise KeyringError(str(exc)) from exc

    def rekey(self, master_password: Optional[str]) -> None:
        """Re-save the file under a new master password (``None`` stores plain text)."""

        with self._lock:
            if self._locked:
                raise KeyringError("Password file is locked; cannot change its key.")
            old_state = (self._master_password, self._salt)
            self._master_password = master_password if master_password and CRYPTO_AVAILABLE else None
            self._salt = None
            try:
                self._save()
            except KeyringError:
                self._master_password, self._salt = old_state
                raise

    # ------------------------------------------------------------------ api
    def get_password(self, service_name: str, username: str) -> Optional[str]:
        with self._lock:
            return self._data.get((service_name, username))

    def set_password(self, service_name: str, username: str, password: str) -> None:
        with self._lock:
            key = (service_name, username)
            old_value = self._data.get(key)
            self._data[(service_name, username)] = password
            try:
                self._save()
            except KeyringError:
                if old_value is None:
                    self._data.pop(key, None)
                else:
                    self._data[key] = old_value
                raise

    def delete_password(self, service_name: str, username: str) -> None:
        with self._lock:
            key = (service_name, username)
            if key in self._data:
                old_value = self._data.pop(key)
                try:
                    self._save()
                except KeyringError:
                    self._data[key] = old_value
                    raise


@dataclass
class SecureStore:
    """Persist SSH passwords securely using the system keyring."""

    backend: KeyringBackend
    service_name: str = "ssh-manager"

    @classmethod
    def with_default_backend(cls, master_password: Optional[str] = None) -> "SecureStore":
        """Construct a SecureStore using the best available backend.

        The system keyring is preferred. When it is missing or unusable, secrets
        go to a local file encrypted with ``master_password`` when one is given.
        """
        backend = _system_keyring()
        if backend is None:
            return cls(FileKeyring(master_password=master_password))
        return cls(backend)  # type: ignore[arg-type]

    @classmethod
    def for_keybox(cls, keybox, path: Optional[Path] = None) -> "SecureStore":
        """The app's store: ``passwords.json`` v3 encrypted with the keybox (the system keyring is not used)."""

        return cls(EncryptedFileKeyring(keybox, path))

    @property
    def uses_file_backend(self) -> bool:
        return isinstance(self.backend, (FileKeyring, EncryptedFileKeyring))

    @property
    def saving_disabled(self) -> bool:
        return isinstance(self.backend, _DisabledBackend)

    @staticmethod
    def key(host: str, port: int, username: str) -> str:
        return f"{host}:{port}:{username}"

    def get(self, host: str, port: int, username: str) -> Optional[str]:
        key = self.key(host, port, username)
        try:
            return self.backend.get_password(self.service_name, key)
        except KeyringError as exc:  # pragma: no cover - depends on backend
            raise SecureStoreError(str(exc)) from exc

    def set(self, host: str, port: int, username: str, secret: str) -> None:
        key = self.key(host, port, username)
        try:
            self.backend.set_password(self.service_name, key, secret)
        except KeyringError as exc:  # pragma: no cover - depends on backend
            raise SecureStoreError(str(exc)) from exc

    def delete(self, host: str, port: int, username: str) -> None:
        key = self.key(host, port, username)
        try:
            self.backend.delete_password(self.service_name, key)
        except KeyringError as exc:  # pragma: no cover - depends on backend
            raise SecureStoreError(str(exc)) from exc


class InMemoryKeyring:
    """Simple in-memory keyring backend for tests."""

    def __init__(self) -> None:
        self._store: dict[tuple[str, str], str] = {}

    def get_password(self, service_name: str, username: str) -> Optional[str]:
        return self._store.get((service_name, username))

    def set_password(self, service_name: str, username: str, password: str) -> None:
        self._store[(service_name, username)] = password

    def delete_password(self, service_name: str, username: str) -> None:
        self._store.pop((service_name, username), None)


# --------------------------------------------------------------- keybox store
SECRETS_FORMAT = "ssh-manager-secrets"
SECRETS_VERSION = 3


class EncryptedFileKeyring:
    """``passwords.json`` version 3: every secret encrypted with the keybox's ``secrets`` subkey.

    ``{"format": "ssh-manager-secrets", "version": 3, "keybox_id": …, "nonce": …, "data": …}``; the header
    (format, version, keybox_id) is the associated data of AES-256-GCM. A file bound to another keybox,
    a newer version or a damaged file is never read or overwritten (:attr:`locked`, :attr:`error`).
    Writes re-read the file under an advisory lock (``passwords.json.lock``), apply the one change and write
    atomically (0600, the previous file kept as ``.bak``): two app instances never lose each other's changes.
    """

    def __init__(self, keybox, path: Optional[Path] = None) -> None:
        self._path = path or _default_store_path()
        self._key = keybox.subkey("secrets")
        self._keybox_id = keybox.id
        self._lock = threading.RLock()
        self._data: dict[tuple[str, str], str] = {}
        self._stamp: Optional[tuple] = None
        self.error = ""
        with self._lock:
            self._reload(force=True)

    # ------------------------------------------------------------ properties
    @property
    def locked(self) -> bool:
        return bool(self.error)

    @property
    def encrypted(self) -> bool:
        return True

    @property
    def path(self) -> Path:
        return self._path

    def count(self) -> int:
        with self._lock:
            self._reload()
            return len(self._data)

    def items(self) -> dict:
        """A copy of every ``(service, key) → secret`` (migration verification only)."""

        with self._lock:
            self._reload()
            return dict(self._data)

    # ---------------------------------------------------------------- file
    def _header(self) -> dict:
        return {"format": SECRETS_FORMAT, "version": SECRETS_VERSION, "keybox_id": self._keybox_id}

    def _file_stamp(self) -> Optional[tuple]:
        try:
            stat = self._path.stat()
        except FileNotFoundError:
            return None
        return stat.st_mtime_ns, stat.st_size, stat.st_ino

    def _reload(self, force: bool = False) -> None:
        stamp = self._file_stamp()
        if not force and stamp == self._stamp:
            return
        self._stamp = stamp
        if stamp is None:
            self._data, self.error = {}, ""
            return
        try:
            self._data = self._decode(self._path.read_bytes())
            self.error = ""
        except KeyringError as exc:
            self._data = {}
            self.error = str(exc)
            LOGGER.warning("Password file %s cannot be opened: %s", self._path.name, exc)

    def _decode(self, raw: bytes) -> dict:
        from .aead import AeadError, open_sealed

        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise KeyringError("The password file is damaged.") from None
        if not isinstance(document, dict) or document.get("format") != SECRETS_FORMAT:
            raise KeyringError("The password file is not in the expected format.")
        version = document.get("version")
        if not isinstance(version, int) or isinstance(version, bool) or version < SECRETS_VERSION:
            raise KeyringError("The password file is not in the expected format.")
        if version > SECRETS_VERSION:
            raise KeyringError("The password file was written by a newer SSH Manager.")
        if document.get("keybox_id") != self._keybox_id:
            raise KeyringError("This file belongs to a different key.")
        try:
            plain = open_sealed(self._key, document.get("nonce"), document.get("data"), self._header())
        except AeadError:
            raise KeyringError("The password file is damaged or was changed.") from None
        try:
            payload = json.loads(plain.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise KeyringError("The password file is damaged.") from None
        return FileKeyring._entries_from_payload(payload.get("secrets", {}) if isinstance(payload, dict) else {})

    def _encode(self, data: dict) -> bytes:
        from .aead import seal

        payload: dict[str, dict[str, str]] = {}
        for (svc, user), pwd in data.items():
            payload.setdefault(svc, {})[user] = pwd
        nonce, sealed = seal(self._key, json.dumps({"secrets": payload}).encode("utf-8"), self._header())
        return (json.dumps(dict(self._header(), nonce=nonce, data=sealed), indent=1) + "\n").encode("utf-8")

    def _change(self, apply) -> None:
        import contextlib
        import fcntl

        from .keybox import write_private_atomic

        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            lock_path = self._path.with_name(self._path.name + ".lock")
            try:
                handle = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            except OSError as exc:
                raise KeyringError(f"The password file cannot be locked ({exc.strerror or exc}).") from None
            try:
                with contextlib.suppress(OSError):
                    fcntl.flock(handle, fcntl.LOCK_EX)
                self._reload(force=True)  # what another instance wrote meanwhile
                if self.error:
                    raise KeyringError("Password file is locked; refusing to overwrite it.")
                data = dict(self._data)
                apply(data)
                try:
                    write_private_atomic(self._path, self._encode(data))
                except OSError as exc:
                    raise KeyringError(str(exc.strerror or exc)) from None
                self._data = data
                self._stamp = self._file_stamp()
            finally:
                os.close(handle)

    def replace_all(self, data: dict) -> None:
        """Write exactly ``data`` (migration: the collected legacy secrets)."""

        def apply(current: dict) -> None:
            current.clear()
            current.update(data)

        self._change(apply)

    # ------------------------------------------------------------------ api
    def get_password(self, service_name: str, username: str) -> Optional[str]:
        with self._lock:
            self._reload()
            if self.error:
                raise KeyringError(self.error)
            return self._data.get((service_name, username))

    def set_password(self, service_name: str, username: str, password: str) -> None:
        self._change(lambda data: data.__setitem__((service_name, username), password))

    def delete_password(self, service_name: str, username: str) -> None:
        with self._lock:
            self._reload()
            if (service_name, username) not in self._data and not self.error:
                return
        self._change(lambda data: data.pop((service_name, username), None))


class _DisabledBackend:
    """"Don't save passwords": nothing is read, writing says so."""

    def get_password(self, service_name: str, username: str) -> Optional[str]:
        return None

    def set_password(self, service_name: str, username: str, password: str) -> None:
        raise KeyringError("Saving passwords is turned off")

    def delete_password(self, service_name: str, username: str) -> None:
        raise KeyringError("Saving passwords is turned off")


class DisabledSecureStore(SecureStore):
    """Passwords are not saved: ``get`` → ``None``; ``set`` / ``delete`` raise :class:`SecureStoreError`."""

    def __init__(self) -> None:
        SecureStore.__init__(self, _DisabledBackend())
