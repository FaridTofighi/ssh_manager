"""Moving the data of older versions onto the keybox (no GTK; the dialogs are in ``gtkui/migration_flow.py``).

Older versions kept saved passwords in the OS keyring, or in ``passwords.json`` (version 1: plain JSON;
version 2: encrypted with a key derived from the master password), and macros in ``macros.enc`` version 1
(its own key from the master password). :class:`Migration` moves them to ``passwords.json`` version 3 and
``macros.enc`` version 2, both encrypted with the keybox. Every step can run again (an interrupted move
resumes), is verified by reading it back, and nothing old is removed before the new copy was written,
fsynced and verified:

1. a master password (the dialogs) and 2. the keybox — with the marker ``migration.pre-keybox`` written
   *before* the keybox, so a crash later is detected as "resume" (the marker holds no secret);
3. :meth:`Migration.collect` — the old secrets, into memory only: ``passwords.json`` v1/v2 (or the copy
   ``passwords.json.pre-keybox`` kept by an interrupted run), the OS keyring by every key the app can
   know (``credential:<id>`` and ``host:port:user`` of every inventory host), and what a previous run
   already wrote to the new store (the newer value wins);
4. the new store is written to ``passwords.json.keybox-new``, verified value by value, the old file is
   copied to ``passwords.json.pre-keybox`` (verified), then the new file replaces ``passwords.json`` and
   is verified again;
5. ``macros.enc`` v1 → v2 (``MacroStore.adopt_key``; the old file kept as ``macros.enc.pre-keybox``);
6. the counts are recorded in ``keybox.json`` and the marker is removed;
7. :meth:`Migration.cleanup` — only after all of that: the ``*.pre-keybox`` copies and old backups are
   overwritten with zeros and removed (best effort on SSDs and journaling file systems), and — when the
   user agrees — the old copies in the OS keyring are deleted.

Any failure raises :class:`MigrationError` (a code, never a secret) with nothing removed.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from .unlock import json_version

LOGGER = logging.getLogger(__name__)

SERVICE = "ssh-manager"
MARKER = "migration.pre-keybox"
NEW_SUFFIX = ".keybox-new"
KEEP_SUFFIX = ".pre-keybox"
STEPS = ("collect", "write", "keep_old", "switch", "macros", "record")

Secrets = Dict[Tuple[str, str], str]


class MigrationError(Exception):
    """``code``: password, passwords_file, keyring, write, verify, macros, locked."""

    def __init__(self, code: str, message: str = "") -> None:
        Exception.__init__(self, message or code)
        self.code = code


@dataclass
class MigrationReport:
    secrets: int = 0  # in the new store after the move
    from_file: int = 0
    from_keyring: int = 0
    keyring_missing: int = 0  # known keys the keyring had nothing for
    already_new: int = 0  # written by an earlier, interrupted run
    macros: int = 0
    keyring_keys: List[str] = field(default_factory=list)  # where copies were found (names, never values)
    removed: List[str] = field(default_factory=list)
    keyring_removed: int = 0

    def counts(self) -> dict:
        return {"secrets": self.secrets, "from_file": self.from_file, "from_keyring": self.from_keyring,
                "keyring_missing": self.keyring_missing, "macros": self.macros}


# ----------------------------------------------------------------- helpers
def write_marker(config_dir: Path) -> None:
    from .keybox import write_private_atomic

    write_private_atomic(Path(config_dir) / MARKER, b"migration to keybox.json in progress\n", backup=False)


def secure_remove(path: Path) -> bool:
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
        LOGGER.warning("migration: %s could not be removed (%s)", path.name, type(exc).__name__)
        return False


def _copy_verified(source: Path, target: Path) -> None:
    from .keybox import write_private_atomic

    raw = source.read_bytes()
    write_private_atomic(target, raw, backup=False)
    if target.read_bytes() != raw:
        raise MigrationError("verify", f"{target.name} does not match {source.name}")


def read_password_file(path: Path, password: Optional[str]) -> Secrets:
    """``passwords.json`` version 1 or 2, read only (the old reader re-wrote plain files; this never writes)."""

    from .secure_store import FileKeyring

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError, ValueError):
        raise MigrationError("passwords_file", f"{path.name} cannot be read") from None
    if not isinstance(raw, dict):
        raise MigrationError("passwords_file", f"{path.name} is not in a known format")
    if raw.get("version") == FileKeyring.FORMAT_VERSION and "data" in raw:
        if not password:
            raise MigrationError("password", f"{path.name} needs the master password")
        reader = FileKeyring.__new__(FileKeyring)  # the decrypting half only: no _load(), no writing
        reader._master_password, reader._locked, reader._data, reader._salt = password, False, {}, None
        reader._path = path
        reader._load_encrypted(raw)
        if reader._locked:
            raise MigrationError("password", f"{path.name} cannot be decrypted with this password")
        return dict(reader._data)
    if "version" in raw and "data" in raw:
        raise MigrationError("passwords_file", f"{path.name} has an unknown version")
    return FileKeyring._entries_from_payload(raw)


def credential_ids(config_dir: Path) -> List[Tuple[str, str]]:
    """``(id, username)`` of every credential in ``credentials.json`` (no secret is in that file)."""

    try:
        data = json.loads((Path(config_dir) / "credentials.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    items = data.get("credentials", []) if isinstance(data, dict) else []
    return [(str(item["id"]), str(item.get("username") or "")) for item in items
            if isinstance(item, dict) and item.get("id")]


def inventory_hosts(manager) -> list:
    if manager is None:
        return []
    try:
        manager.refresh()
    except Exception:  # an unreadable inventory: the credentials alone are still moved
        LOGGER.warning("migration: the inventory could not be read")
        return []
    return [host for children in manager.tree.values() for group in children.values() for host in group.hosts]


def known_keys(config_dir: Path, manager=None) -> List[str]:
    """Every keyring key the older versions could have used (the keyring API cannot list entries)."""

    from .host_utils import host_connection_params
    from .secure_store import SecureStore

    creds = credential_ids(config_dir)
    keys = [f"credential:{cred_id}" for cred_id, _user in creds]
    usernames = sorted({user for _id, user in creds if user})
    for host in inventory_hosts(manager):
        try:
            hostname, port, user = host_connection_params(host)
        except Exception:
            continue
        keys.append(SecureStore.key(hostname, port, user))
        keys.extend(SecureStore.key(hostname, port, other) for other in usernames)
    seen: set = set()
    return [key for key in keys if not (key in seen or seen.add(key))]


def legacy_password_file(config_dir: Path) -> Optional[Path]:
    """Where the old secrets file is: ``passwords.json`` (v1/v2), else the copy of an interrupted run."""

    for name in ("passwords.json", "passwords.json" + KEEP_SUFFIX):
        path = Path(config_dir) / name
        if json_version(path) in (1, 2, -1):
            return path
    return None


def needs_legacy_password(config_dir: Path) -> bool:
    path = legacy_password_file(config_dir)
    return (path is not None and json_version(path) == 2) or json_version(Path(config_dir) / "macros.enc") == 1


def legacy_password_ok(config_dir: Path, password: str, stored_hash: str = "") -> bool:
    """The master password of the older version (its hash, else whether it opens the old files)."""

    from .master_auth import verify_master_password

    if stored_hash:
        return verify_master_password(password, stored_hash)
    path = legacy_password_file(config_dir)
    if path is not None and json_version(path) == 2:
        try:
            read_password_file(path, password)
        except MigrationError:
            return False
    macros = Path(config_dir) / "macros.enc"
    if json_version(macros) == 1:
        from .macros import MacroStore

        try:
            MacroStore(macros).unlock(password)
        except Exception:
            return False
    return True


# ---------------------------------------------------------------- migration
class Migration:
    """Steps 3–7 for an unlocked keybox. ``after_step(name)`` is called after each step (tests inject failures)."""

    def __init__(self, config_dir: Path, keybox, *, keyring=None, manager=None,
                 legacy_password: Optional[str] = None, app_version: str = "",
                 after_step: Optional[Callable[[str], None]] = None) -> None:
        self.config_dir = Path(config_dir)
        self.keybox = keybox
        self.keyring = keyring
        self.manager = manager
        self._legacy_password = legacy_password
        self.app_version = app_version
        self.after_step = after_step or (lambda _name: None)
        self.report = MigrationReport()

    @property
    def passwords_path(self) -> Path:
        return self.config_dir / "passwords.json"

    # ----------------------------------------------------------- step 3
    def collect(self) -> Secrets:
        report = self.report
        collected: Secrets = {}
        source = legacy_password_file(self.config_dir)
        if source is not None:
            collected.update(read_password_file(source, self._legacy_password))
            report.from_file = len(collected)
        keys = known_keys(self.config_dir, self.manager)
        if self.keyring is not None:
            for key in keys:
                try:
                    value = self.keyring.get_password(SERVICE, key)
                except Exception as exc:  # locked or gone mid-way: stop, nothing changed yet
                    raise MigrationError("keyring", f"the system keyring failed ({type(exc).__name__})") from None
                if value is None:
                    report.keyring_missing += 1
                    continue
                report.keyring_keys.append(key)
                report.from_keyring += 1
                collected.setdefault((SERVICE, key), value)  # a file entry and a keyring entry: the file wins
        current = self._current_new_store()
        report.already_new = len(current)
        collected.update(current)  # written by an earlier run (or saved since): newest
        return collected

    def _current_new_store(self) -> Secrets:
        from .secure_store import EncryptedFileKeyring

        if json_version(self.passwords_path) < 3:
            return {}
        store = EncryptedFileKeyring(self.keybox, self.passwords_path)
        if store.locked:
            raise MigrationError("passwords_file", store.error)
        return store.items()

    # ----------------------------------------------------------- step 4
    def _verify(self, path: Path, expected: Secrets) -> None:
        from .secure_store import EncryptedFileKeyring

        found = EncryptedFileKeyring(self.keybox, path)
        if found.locked:
            raise MigrationError("verify", found.error)
        items = found.items()
        if set(items) != set(expected) or not all(
                hmac.compare_digest(items[key].encode("utf-8"), expected[key].encode("utf-8")) for key in expected):
            raise MigrationError("verify", "the new password file does not match")

    def write(self, secrets: Secrets) -> None:
        from .secure_store import EncryptedFileKeyring, KeyringError

        new_path = self.passwords_path.with_name(self.passwords_path.name + NEW_SUFFIX)
        new_path.unlink(missing_ok=True)  # an earlier run's: never read, rewritten from the sources
        try:
            EncryptedFileKeyring(self.keybox, new_path).replace_all(secrets)
        except (KeyringError, OSError) as exc:
            raise MigrationError("write", f"the new password file cannot be written ({exc})") from None
        self._verify(new_path, secrets)
        self.after_step("write")
        if json_version(self.passwords_path) in (1, 2, -1):
            _copy_verified(self.passwords_path, self.passwords_path.with_name("passwords.json" + KEEP_SUFFIX))
        self.after_step("keep_old")
        try:
            os.replace(new_path, self.passwords_path)
            fd = os.open(self.config_dir, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError as exc:
            raise MigrationError("write", f"passwords.json cannot be replaced ({exc.strerror or exc})") from None
        self._verify(self.passwords_path, secrets)
        self.report.secrets = len(secrets)
        self.after_step("switch")

    # ----------------------------------------------------------- step 5
    def macros(self) -> None:
        from .macros import MacroStore

        path = self.config_dir / "macros.enc"
        if json_version(path) != 1:
            self.after_step("macros")
            return
        store = MacroStore(path)
        try:
            store.unlock(self._legacy_password or "")
            self.report.macros = store.adopt_key(self.keybox.subkey("macros"), self.keybox.id)
        except Exception as exc:  # a wrong password, a damaged file, a failed write: macros.enc stays v1
            raise MigrationError("macros", f"macros.enc could not be moved ({type(exc).__name__})") from None
        self.after_step("macros")

    # ------------------------------------------------------- steps 3–6
    def run(self) -> MigrationReport:
        if self.keybox.locked:
            raise MigrationError("locked", "the keybox is locked")
        secrets = self.collect()
        self.after_step("collect")
        self.write(secrets)
        self.macros()
        info = {"from": self.app_version or "older", "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "counts": self.report.counts()}
        try:
            self.keybox.record_migration(info)
        except Exception as exc:  # informational only
            LOGGER.warning("migration: could not record the counts (%s)", type(exc).__name__)
        secure_remove(self.config_dir / MARKER)
        self.after_step("record")
        self._legacy_password = None
        return self.report

    # ----------------------------------------------------------- step 7
    def leftovers(self) -> List[Path]:
        """Old copies that can go once the new stores are verified."""

        found = [p for p in sorted(self.config_dir.glob("*" + KEEP_SUFFIX)) if p.name != MARKER]
        found += [p for p in sorted(self.config_dir.glob("*" + NEW_SUFFIX + "*")) if p.is_file()]
        for name in ("passwords.json.bak", "macros.enc.bak"):
            path = self.config_dir / name
            old = json_version(path) in (1, 2, -1) if name.startswith("passwords") else json_version(path) == 1
            if old:
                found.append(path)
        return found

    def cleanup(self, *, remove_keyring: bool) -> MigrationReport:
        """Remove the old copies; call it only after :meth:`run` succeeded (it checks the new stores again)."""

        from .macros import MacroStore

        if (self.config_dir / MARKER).exists():
            raise MigrationError("verify", "the move did not finish")
        if json_version(self.passwords_path) == 3:
            self._verify(self.passwords_path, self._current_new_store())
        elif json_version(self.passwords_path) != 0:
            raise MigrationError("verify", "passwords.json is not the new format")
        macros = self.config_dir / "macros.enc"
        if json_version(macros) == 1:
            raise MigrationError("verify", "macros.enc is not the new format")
        if macros.exists():
            MacroStore(macros).unlock_with_key(self.keybox.subkey("macros"), self.keybox.id)  # raises if unreadable
        for path in self.leftovers():
            if secure_remove(path):
                self.report.removed.append(path.name)
        if remove_keyring:
            moved = self._current_new_store()
            keys = self.report.keyring_keys or keyring_copies(self.keyring, self.config_dir, self.manager)
            # Only copies whose secret is in the verified new store.
            self.report.keyring_removed = remove_keyring_copies(self.keyring,
                                                                [k for k in keys if (SERVICE, k) in moved])
        return self.report


def keyring_copies(keyring, config_dir: Path, manager=None) -> List[str]:
    """Keys the OS keyring still holds for SSH Manager (for "Remove old copies"; names only)."""

    if keyring is None:
        return []
    found = []
    for key in known_keys(config_dir, manager):
        try:
            if keyring.get_password(SERVICE, key) is not None:
                found.append(key)
        except Exception:
            return found
    return found


def remove_keyring_copies(keyring, keys: Iterable[str]) -> int:
    removed = 0
    if keyring is None:
        return 0
    for key in keys:
        try:
            keyring.delete_password(SERVICE, key)
            removed += 1
        except Exception as exc:
            LOGGER.warning("migration: a keyring copy could not be removed (%s)", type(exc).__name__)
    return removed


__all__ = ["Migration", "MigrationError", "MigrationReport", "MARKER", "STEPS", "write_marker", "secure_remove",
           "read_password_file", "known_keys", "keyring_copies", "remove_keyring_copies", "needs_legacy_password",
           "legacy_password_ok", "legacy_password_file", "credential_ids"]
