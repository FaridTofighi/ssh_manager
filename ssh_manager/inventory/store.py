"""``InventoryStore``: groups (any depth) and hosts in SQLite (stdlib only, no GTK).

* One file, ``~/.local/share/ssh-manager/inventory.db`` (``$XDG_DATA_HOME``),
  mode 0600 in a 0700 folder; ``PRAGMA foreign_keys=ON``, WAL, schema version
  in ``PRAGMA user_version``.
* Writers use ``BEGIN IMMEDIATE`` (:meth:`InventoryStore.transaction`); a
  locked database (another instance) raises :class:`InventoryLocked`.
* Groups form an arbitrary-depth tree; a group can hold hosts *and*
  subgroups; empty groups are real rows. Names are any readable Unicode text
  (1–128 chars, no ``/``, no control or bidi-override characters), unique per
  parent case-insensitively; host aliases likewise per group. Uniqueness uses
  a stored ``casefold()`` key (``name_key`` / ``alias_key``) so it holds for
  Unicode too (SQLite's ``NOCASE`` only folds ASCII).
* Each host has a ``protocol`` (``ssh`` by default; ``ftp``, ``ftps``,
  ``sftp`` …, see :mod:`ssh_manager.protocols`), added by schema v2.
* Rolling backups (``backups/inventory-<stamp>-<reason>.db``, newest 5 kept,
  plus ``inventory.db.bak``) on schema migration and before destructive bulk
  operations.

Errors a user can fix come back as :class:`ValidationError` (a message for
the dialog); storage problems as :class:`InventoryError` subclasses.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import unicodedata
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

SCHEMA_VERSION = 2
MAX_NAME = 128
BACKUP_KEEP = 5
BUSY_TIMEOUT_S = 2.0
_BIDI_OVERRIDES = {"‪", "‫", "‬", "‭", "‮", "⁦", "⁧", "⁨", "⁩"}

_SCHEMA_V1 = """
CREATE TABLE groups (
    id TEXT PRIMARY KEY,
    parent_id TEXT NULL REFERENCES groups(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    name_key TEXT NOT NULL,
    position INTEGER NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE UNIQUE INDEX groups_root_name ON groups(name_key) WHERE parent_id IS NULL;
CREATE UNIQUE INDEX groups_child_name ON groups(parent_id, name_key) WHERE parent_id IS NOT NULL;
CREATE INDEX groups_parent ON groups(parent_id);
CREATE TABLE hosts (
    id TEXT PRIMARY KEY,
    group_id TEXT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    alias TEXT NOT NULL,
    alias_key TEXT NOT NULL,
    hostname TEXT NOT NULL DEFAULT '',
    port INTEGER NULL,
    user TEXT NOT NULL DEFAULT '',
    options_json TEXT NOT NULL DEFAULT '{}',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    position INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE (group_id, alias_key)
);
CREATE INDEX hosts_group ON hosts(group_id);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

# v2: the connection protocol of a host (existing hosts are SSH).
_SCHEMA_V2 = """
ALTER TABLE hosts ADD COLUMN protocol TEXT NOT NULL DEFAULT 'ssh';
CREATE INDEX hosts_protocol ON hosts(protocol);
"""
DEFAULT_PROTOCOL = "ssh"
_PROTOCOL_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789_-")


# --------------------------------------------------------------------- errors
class InventoryError(Exception):
    """A storage problem (shown to the user as is)."""


class InventoryLocked(InventoryError):
    """Another SSH Manager instance holds the write lock right now."""


class InventoryCorrupt(InventoryError):
    """The database file is damaged (offer: restore a backup / start empty / quit)."""


class InventoryTooNew(InventoryError):
    """The database was written by a newer SSH Manager (never downgrade it)."""


class InventoryUnavailable(InventoryError):
    """The database cannot be opened for writing (missing, read-only, full disk…)."""


class ValidationError(InventoryError):
    """User-fixable input problem (duplicate name, empty name, moving into a descendant…)."""


# ----------------------------------------------------------------- validation
def clean_name(text: str) -> str:
    return unicodedata.normalize("NFC", str(text or "")).strip()


def validate_name(name: str, what: str = "Name") -> Optional[str]:
    """Problem with a group name / host alias, or ``None`` when it is fine."""

    text = clean_name(name)
    if not text:
        return f"{what} cannot be empty."
    if len(text) > MAX_NAME:
        return f"{what} is longer than {MAX_NAME} characters."
    if "/" in text:
        return f"{what} must not contain '/'."
    for ch in text:
        category = unicodedata.category(ch)
        if category in ("Cc", "Zl", "Zp") or ch in _BIDI_OVERRIDES:
            return f"{what} must not contain control characters or line breaks."
    return None


def _valid_target(value: str) -> bool:
    return bool(value) and not value.startswith("-") and not any(
        ch.isspace() or unicodedata.category(ch) == "Cc" for ch in value)


def validate_alias(alias: str, hostname: str = "") -> Optional[str]:
    """Alias rules; without a host name the alias is the ssh target, so it must be one."""

    error = validate_name(alias, "Host alias")
    if error:
        return error
    if not (hostname or "").strip() and not _valid_target(clean_name(alias)):
        return "Without a host name, the alias is used as the target: it must be a host name or IP address."
    return None


def validate_target_fields(hostname: str, port: Optional[int], user: str) -> Optional[str]:
    if hostname and not _valid_target(hostname):
        return "The host name must not contain spaces or start with '-'."
    if port is not None and not (1 <= int(port) <= 65535):
        return "The port must be between 1 and 65535."
    if user and not _valid_target(user):
        return "The user name must not contain spaces or start with '-'."
    return None


def _fold(text: str) -> str:
    return clean_name(text).casefold()


def default_inventory_path() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "ssh-manager" / "inventory.db"


# ---------------------------------------------------------------------- rows
@dataclass(frozen=True)
class GroupRow:
    id: str
    parent_id: Optional[str]
    name: str
    position: int = 0
    note: str = ""


@dataclass(frozen=True)
class HostRow:
    id: str
    group_id: str
    alias: str
    hostname: str = ""
    port: Optional[int] = None
    user: str = ""
    options: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)
    position: int = 0
    protocol: str = DEFAULT_PROTOCOL


@dataclass
class NewHost:
    """Input for :meth:`InventoryStore.add_host` / ``add_hosts_bulk``."""

    group_id: str
    alias: str
    hostname: str = ""
    port: Optional[int] = None
    user: str = ""
    options: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)
    id: Optional[str] = None
    protocol: str = DEFAULT_PROTOCOL


def clean_protocol(value: Optional[str]) -> str:
    """A protocol id (``ssh``, ``ftp`` …): lower-case letters, digits, ``_`` and ``-``, 1–16 characters.

    Unknown ids are kept (a newer version may know them); malformed ones are a
    :class:`ValidationError`.
    """

    text = (value or DEFAULT_PROTOCOL).strip().lower()
    if not (1 <= len(text) <= 16) or not set(text) <= _PROTOCOL_CHARS:
        raise ValidationError(f"'{value}' is not a valid protocol.")
    return text


def _new_id() -> str:
    return uuid.uuid4().hex


def _group(row: sqlite3.Row) -> GroupRow:
    return GroupRow(row["id"], row["parent_id"], row["name"], row["position"], row["note"])


def _host(row: sqlite3.Row) -> HostRow:
    return HostRow(row["id"], row["group_id"], row["alias"], row["hostname"], row["port"], row["user"],
                   json.loads(row["options_json"] or "{}"), json.loads(row["metadata_json"] or "{}"), row["position"],
                   row["protocol"] or DEFAULT_PROTOCOL)


class InventoryStore:
    """The inventory database. Not thread-safe: use it from one thread at a time."""

    def __init__(self, path: Optional[Path] = None, *, read_only: bool = False) -> None:
        self.path = Path(path) if path is not None else default_inventory_path()
        self.read_only = read_only
        self.read_only_reason = "The inventory is open read-only." if read_only else ""
        self.created = False  # True when this open created the database (first run)
        self._conn: Optional[sqlite3.Connection] = None
        self._depth = 0
        self.open()

    # ------------------------------------------------------------- lifecycle
    def open(self) -> None:
        memory = str(self.path) == ":memory:"
        if not memory and not self.read_only:
            try:
                self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            except OSError as exc:
                raise InventoryUnavailable(f"Cannot create {self.path.parent}: {exc.strerror or exc}") from exc
        existed = memory or self.path.exists()
        try:
            if self.read_only and not memory:
                conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=BUSY_TIMEOUT_S,
                                       isolation_level=None, check_same_thread=False)
            else:
                conn = sqlite3.connect(str(self.path), timeout=BUSY_TIMEOUT_S, isolation_level=None,
                                       check_same_thread=False)
        except sqlite3.Error as exc:
            raise InventoryUnavailable(f"Cannot open the inventory {self.path}: {exc}") from exc
        conn.row_factory = sqlite3.Row
        self._conn = conn
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            check = conn.execute("PRAGMA quick_check(1)").fetchone()[0]
            if check != "ok":
                raise InventoryCorrupt(f"The inventory {self.path} is damaged ({check}).")
            # Read the version before anything that writes (WAL changes the file header):
            # a database from a newer SSH Manager must stay byte-identical.
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version <= SCHEMA_VERSION and not self.read_only and not memory:
                conn.execute("PRAGMA journal_mode=WAL")
        except InventoryCorrupt:
            self.close()
            raise
        except sqlite3.DatabaseError as exc:
            self.close()
            if "locked" in str(exc).lower():
                raise InventoryLocked("The inventory is in use by another SSH Manager window. Try again.") from exc
            raise InventoryCorrupt(f"The inventory {self.path} is damaged or not a database ({exc}).") from exc
        if version > SCHEMA_VERSION:
            self.close()
            raise InventoryTooNew(
                f"The inventory {self.path} was written by a newer SSH Manager (schema {version}, this version "
                f"understands {SCHEMA_VERSION}). Update SSH Manager to open it.")
        if version < SCHEMA_VERSION:
            if self.read_only:
                self.close()
                raise InventoryUnavailable(f"The inventory {self.path} needs an upgrade but is read-only.")
            self._migrate(version, backup=existed and version > 0)
            self.created = not existed
        if not memory and not self.read_only:
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise InventoryUnavailable("The inventory is closed.")
        return self._conn

    def _migrate(self, version: int, *, backup: bool) -> None:
        if backup:
            self.backup(f"schema-v{version}")
        with self.transaction():
            # One statement at a time: executescript() would COMMIT the open transaction.
            steps = [(1, _SCHEMA_V1), (2, _SCHEMA_V2)]
            for target, script in steps:
                if version < target:
                    for statement in (part.strip() for part in script.split(";")):
                        if statement:
                            self.conn.execute(statement)
            self.conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def data_version(self) -> int:
        """Changes when *another* connection commits (external-change detection)."""

        return int(self.conn.execute("PRAGMA data_version").fetchone()[0])

    @property
    def schema_version(self) -> int:
        return int(self.conn.execute("PRAGMA user_version").fetchone()[0])

    # ----------------------------------------------------------- transactions
    @contextmanager
    def transaction(self) -> Iterator["InventoryStore"]:
        """``BEGIN IMMEDIATE`` … ``COMMIT`` (nested calls join the outer one); rollback on any error."""

        if self.read_only:
            raise InventoryUnavailable("The inventory is open read-only.")
        if self._depth:
            self._depth += 1
            try:
                yield self
            finally:
                self._depth -= 1
            return
        try:
            self.conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            raise self._storage_error(exc) from exc
        self._depth = 1
        try:
            yield self
        except BaseException:
            self._depth = 0
            try:
                self.conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        self._depth = 0
        try:
            self.conn.execute("COMMIT")
        except sqlite3.Error as exc:
            try:
                self.conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise self._storage_error(exc) from exc

    @staticmethod
    def _storage_error(exc: sqlite3.Error) -> InventoryError:
        text = str(exc).lower()
        if "locked" in text or "busy" in text:
            return InventoryLocked("The inventory is in use by another SSH Manager window. Try again.")
        if "full" in text:
            return InventoryUnavailable("The disk is full; the inventory could not be saved.")
        if "readonly" in text or "read-only" in text:
            return InventoryUnavailable("The inventory is read-only; changes cannot be saved.")
        if isinstance(exc, sqlite3.DatabaseError) and "malformed" in text:
            return InventoryCorrupt(f"The inventory is damaged ({exc}).")
        return InventoryError(f"The inventory could not be saved ({exc}).")

    def _exec(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        try:
            return self.conn.execute(sql, params)
        except sqlite3.IntegrityError as exc:
            raise ValidationError(self._integrity_message(exc)) from exc
        except sqlite3.Error as exc:
            raise self._storage_error(exc) from exc

    @staticmethod
    def _integrity_message(exc: sqlite3.IntegrityError) -> str:
        text = str(exc)
        if "hosts.group_id, hosts.alias_key" in text:
            return "A host with this alias already exists in this group."
        if "groups" in text:
            return "A group with this name already exists here."
        if "FOREIGN KEY" in text:
            return "The target group no longer exists."
        return f"The change conflicts with existing data ({text})."

    # ----------------------------------------------------------------- meta
    def get_meta(self, key: str, default: Optional[str] = None) -> Optional[str]:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self.transaction():
            self._exec("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       (key, value))

    # --------------------------------------------------------------- groups
    def list_groups(self) -> List[GroupRow]:
        rows = self.conn.execute("SELECT * FROM groups ORDER BY position, name COLLATE NOCASE").fetchall()
        return [_group(r) for r in rows]

    def _group_exists(self, group_id: str) -> bool:
        return self.conn.execute("SELECT 1 FROM groups WHERE id=?", (group_id,)).fetchone() is not None

    def get_group(self, group_id: str) -> Optional[GroupRow]:
        row = self.conn.execute("SELECT * FROM groups WHERE id=?", (group_id,)).fetchone()
        return _group(row) if row else None

    def children(self, parent_id: Optional[str]) -> List[GroupRow]:
        if parent_id is None:
            rows = self.conn.execute("SELECT * FROM groups WHERE parent_id IS NULL").fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM groups WHERE parent_id=?", (parent_id,)).fetchall()
        return sorted((_group(r) for r in rows), key=lambda g: (g.position, g.name.casefold()))

    def find_child(self, parent_id: Optional[str], name: str) -> Optional[GroupRow]:
        if parent_id is None:
            row = self.conn.execute("SELECT * FROM groups WHERE parent_id IS NULL AND name_key=?",
                                    (_fold(name),)).fetchone()
        else:
            row = self.conn.execute("SELECT * FROM groups WHERE parent_id=? AND name_key=?",
                                    (parent_id, _fold(name))).fetchone()
        return _group(row) if row else None

    def find_group(self, path: Sequence[str]) -> Optional[GroupRow]:
        group: Optional[GroupRow] = None
        for name in path:
            group = self.find_child(group.id if group else None, name)
            if group is None:
                return None
        return group

    def group_path(self, group_id: str) -> Tuple[str, ...]:
        names: List[str] = []
        current = self.get_group(group_id)
        seen = set()
        while current is not None and current.id not in seen:
            seen.add(current.id)
            names.append(current.name)
            current = self.get_group(current.parent_id) if current.parent_id else None
        return tuple(reversed(names))

    def descendants(self, group_id: str) -> List[str]:
        rows = self.conn.execute(
            "WITH RECURSIVE sub(id) AS (SELECT id FROM groups WHERE parent_id=? "
            "UNION ALL SELECT g.id FROM groups g JOIN sub ON g.parent_id=sub.id) SELECT id FROM sub",
            (group_id,)).fetchall()
        return [r["id"] for r in rows]

    def _check_name_free(self, parent_id: Optional[str], name: str, *, except_id: Optional[str] = None) -> None:
        clash = self.find_child(parent_id, name)
        if clash is not None and clash.id != except_id:
            raise ValidationError(f"A group named '{clash.name}' already exists here.")

    def create_group(self, parent_id: Optional[str], name: str, *, note: str = "",
                     group_id: Optional[str] = None) -> GroupRow:
        error = validate_name(name, "Group name")
        if error:
            raise ValidationError(error)
        name = clean_name(name)
        with self.transaction():
            if parent_id is not None and self.get_group(parent_id) is None:
                raise ValidationError("The parent group no longer exists.")
            self._check_name_free(parent_id, name)
            now = time.time()
            gid = group_id or _new_id()
            self._exec("INSERT INTO groups(id, parent_id, name, name_key, position, note, created_at, updated_at) "
                       "VALUES(?,?,?,?,?,?,?,?)", (gid, parent_id, name, _fold(name), 0, note, now, now))
        return GroupRow(gid, parent_id, name, 0, note)

    def create_group_path(self, path: Sequence[str] | str, parent_id: Optional[str] = None) -> GroupRow:
        """Get or create every group of ``path`` (``"a/b/c"`` or a sequence) under ``parent_id``."""

        names = [part for part in (path.split("/") if isinstance(path, str) else path) if clean_name(part)]
        if not names:
            raise ValidationError("Group name cannot be empty.")
        group: Optional[GroupRow] = self.get_group(parent_id) if parent_id else None
        with self.transaction():
            for name in names:
                found = self.find_child(group.id if group else None, name)
                group = found or self.create_group(group.id if group else None, name)
        assert group is not None
        return group

    def rename_group(self, group_id: str, name: str) -> GroupRow:
        error = validate_name(name, "Group name")
        if error:
            raise ValidationError(error)
        name = clean_name(name)
        with self.transaction():
            group = self.get_group(group_id)
            if group is None:
                raise ValidationError("The group no longer exists.")
            self._check_name_free(group.parent_id, name, except_id=group_id)
            self._exec("UPDATE groups SET name=?, name_key=?, updated_at=? WHERE id=?",
                       (name, _fold(name), time.time(), group_id))
        return GroupRow(group_id, group.parent_id, name, group.position, group.note)

    def move_group(self, group_id: str, new_parent_id: Optional[str], *, new_name: Optional[str] = None) -> GroupRow:
        with self.transaction():
            group = self.get_group(group_id)
            if group is None:
                raise ValidationError("The group no longer exists.")
            if new_parent_id is not None:
                if new_parent_id == group_id or new_parent_id in self.descendants(group_id):
                    raise ValidationError("A group cannot be moved into itself or one of its subgroups.")
                if self.get_group(new_parent_id) is None:
                    raise ValidationError("The target group no longer exists.")
            name = clean_name(new_name) if new_name is not None else group.name
            error = validate_name(name, "Group name")
            if error:
                raise ValidationError(error)
            self._check_name_free(new_parent_id, name, except_id=group_id)
            self._exec("UPDATE groups SET parent_id=?, name=?, name_key=?, updated_at=? WHERE id=?",
                       (new_parent_id, name, _fold(name), time.time(), group_id))
        return GroupRow(group_id, new_parent_id, name, group.position, group.note)

    def count_hosts(self, group_id: str, *, recursive: bool = True) -> int:
        ids = [group_id] + (self.descendants(group_id) if recursive else [])
        marks = ",".join("?" * len(ids))
        return int(self.conn.execute(f"SELECT COUNT(*) FROM hosts WHERE group_id IN ({marks})", ids).fetchone()[0])

    def delete_group(self, group_id: str) -> List[str]:
        """Delete a group, its subgroups and their hosts; returns the deleted host ids."""

        with self.transaction():
            ids = [group_id] + self.descendants(group_id)
            marks = ",".join("?" * len(ids))
            hosts = [r["id"] for r in self.conn.execute(f"SELECT id FROM hosts WHERE group_id IN ({marks})", ids)]
            self._exec("DELETE FROM groups WHERE id=?", (group_id,))
        return hosts

    def duplicate_group(self, group_id: str, new_parent_id: Optional[str], new_name: str) -> Tuple[GroupRow, Dict[str, str]]:
        """Deep copy (subgroups and hosts get new ids); returns the copy and ``{old host id: new host id}``."""

        host_map: Dict[str, str] = {}
        with self.transaction():
            source = self.get_group(group_id)
            if source is None:
                raise ValidationError("The group no longer exists.")
            if new_parent_id is not None and (new_parent_id == group_id or new_parent_id in self.descendants(group_id)):
                raise ValidationError("A group cannot be copied into itself.")
            copy = self.create_group(new_parent_id, new_name, note=source.note)

            def copy_tree(src: str, dst: str) -> None:
                for host in self.list_hosts(src):
                    new = self.add_host(NewHost(dst, host.alias, host.hostname, host.port, host.user,
                                                dict(host.options), dict(host.metadata), protocol=host.protocol))
                    host_map[host.id] = new.id
                for child in self.children(src):
                    new_child = self.create_group(dst, child.name, note=child.note)
                    copy_tree(child.id, new_child.id)

            copy_tree(group_id, copy.id)
        return copy, host_map

    # ---------------------------------------------------------------- hosts
    def list_hosts(self, group_id: Optional[str] = None) -> List[HostRow]:
        if group_id is None:
            rows = self.conn.execute("SELECT * FROM hosts").fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM hosts WHERE group_id=?", (group_id,)).fetchall()
        return sorted((_host(r) for r in rows), key=lambda h: (h.position, h.alias.casefold()))

    def get_host(self, host_id: str) -> Optional[HostRow]:
        row = self.conn.execute("SELECT * FROM hosts WHERE id=?", (host_id,)).fetchone()
        return _host(row) if row else None

    def find_host(self, group_id: str, alias: str) -> Optional[HostRow]:
        row = self.conn.execute("SELECT * FROM hosts WHERE group_id=? AND alias_key=?",
                                (group_id, _fold(alias))).fetchone()
        return _host(row) if row else None

    def _check_alias_free(self, group_id: str, alias: str, *, except_id: Optional[str] = None) -> None:
        clash = self.find_host(group_id, alias)
        if clash is not None and clash.id != except_id:
            raise ValidationError(f"A host named '{clash.alias}' already exists in this group.")

    @staticmethod
    def _validate_host(alias: str, hostname: str, port: Optional[int], user: str) -> None:
        error = validate_alias(alias, hostname) or validate_target_fields(hostname, port, user)
        if error:
            raise ValidationError(error)

    @staticmethod
    def _json(value: Dict[str, Any], what: str) -> str:
        for key, item in value.items():
            if isinstance(item, str) and ("\n" in item or "\r" in item):
                raise ValidationError(f"{what} '{key}' must not contain line breaks.")
        return json.dumps(value, ensure_ascii=False)

    def add_host(self, new: NewHost) -> HostRow:
        alias, hostname, user = clean_name(new.alias), (new.hostname or "").strip(), (new.user or "").strip()
        port = int(new.port) if new.port is not None and new.port != "" else None
        self._validate_host(alias, hostname, port, user)
        options_json, metadata_json = self._json(new.options, "Option"), self._json(new.metadata, "Metadata")
        protocol = clean_protocol(new.protocol)
        hid = new.id or _new_id()
        with self.transaction():
            if not self._group_exists(new.group_id):
                raise ValidationError("The group no longer exists.")
            self._check_alias_free(new.group_id, alias)
            now = time.time()
            self._exec("INSERT INTO hosts(id, group_id, alias, alias_key, hostname, port, user, options_json, "
                       "metadata_json, position, created_at, updated_at, protocol) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (hid, new.group_id, alias, _fold(alias), hostname, port, user, options_json, metadata_json,
                        0, now, now, protocol))
        return HostRow(hid, new.group_id, alias, hostname, port, user, dict(new.options), dict(new.metadata),
                       protocol=protocol)

    def add_hosts_bulk(self, hosts: Iterable[NewHost]) -> List[HostRow]:
        """Insert many hosts in one transaction (all or nothing)."""

        with self.transaction():
            return [self.add_host(host) for host in hosts]

    def update_host(self, host_id: str, *, alias: Optional[str] = None, hostname: Optional[str] = None,
                    port: Any = ..., user: Optional[str] = None, options: Optional[Dict[str, Any]] = None,
                    metadata: Optional[Dict[str, Any]] = None, protocol: Optional[str] = None) -> HostRow:
        with self.transaction():
            host = self.get_host(host_id)
            if host is None:
                raise ValidationError("The host no longer exists.")
            new_alias = clean_name(alias) if alias is not None else host.alias
            new_hostname = hostname.strip() if hostname is not None else host.hostname
            new_port = host.port if port is ... else (int(port) if port not in (None, "") else None)
            new_user = user.strip() if user is not None else host.user
            self._validate_host(new_alias, new_hostname, new_port, new_user)
            self._check_alias_free(host.group_id, new_alias, except_id=host_id)
            new_options = host.options if options is None else options
            new_metadata = host.metadata if metadata is None else metadata
            new_protocol = host.protocol if protocol is None else clean_protocol(protocol)
            self._exec("UPDATE hosts SET alias=?, alias_key=?, hostname=?, port=?, user=?, options_json=?, "
                       "metadata_json=?, updated_at=?, protocol=? WHERE id=?",
                       (new_alias, _fold(new_alias), new_hostname, new_port, new_user, self._json(new_options, "Option"),
                        self._json(new_metadata, "Metadata"), time.time(), new_protocol, host_id))
        return HostRow(host_id, host.group_id, new_alias, new_hostname, new_port, new_user, dict(new_options),
                       dict(new_metadata), host.position, new_protocol)

    def move_hosts(self, host_ids: Sequence[str], group_id: str) -> None:
        with self.transaction():
            if self.get_group(group_id) is None:
                raise ValidationError("The target group no longer exists.")
            taken = {h.alias.casefold() for h in self.list_hosts(group_id) if h.id not in host_ids}
            for host_id in host_ids:
                host = self.get_host(host_id)
                if host is None:
                    raise ValidationError("A host no longer exists.")
                if host.alias.casefold() in taken:
                    raise ValidationError(f"A host named '{host.alias}' already exists in the target group.")
                taken.add(host.alias.casefold())
                self._exec("UPDATE hosts SET group_id=?, updated_at=? WHERE id=?", (group_id, time.time(), host_id))

    def delete_hosts(self, host_ids: Sequence[str]) -> None:
        with self.transaction():
            for host_id in host_ids:
                self._exec("DELETE FROM hosts WHERE id=?", (host_id,))

    def host_ids(self) -> set:
        return {r["id"] for r in self.conn.execute("SELECT id FROM hosts")}

    # -------------------------------------------------------------- backups
    @property
    def backup_dir(self) -> Path:
        return self.path.parent / "backups"

    def backup(self, reason: str = "manual") -> Optional[Path]:
        """Snapshot to ``backups/inventory-<stamp>-<reason>.db`` (keeps the newest 5) and ``inventory.db.bak``."""

        if str(self.path) == ":memory:":
            return None
        self.backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in reason)[:32] or "backup"
        target = self.backup_dir / f"inventory-{stamp}-{safe}.db"
        counter = 2
        while target.exists():
            target = self.backup_dir / f"inventory-{stamp}-{safe}-{counter}.db"
            counter += 1
        for destination in (target, self.path.with_name(self.path.name + ".bak")):
            out = sqlite3.connect(str(destination))
            try:
                self.conn.backup(out)
            finally:
                out.close()
            os.chmod(destination, 0o600)
        snapshots = sorted(self.backup_dir.glob("inventory-*.db"), key=lambda p: p.stat().st_mtime)
        for old in snapshots[:-BACKUP_KEEP]:
            try:
                old.unlink()
            except OSError:
                pass
        return target

    def list_backups(self) -> List[Path]:
        if not self.backup_dir.exists():
            return []
        return sorted(self.backup_dir.glob("inventory-*.db"), key=lambda p: p.stat().st_mtime, reverse=True)

    @classmethod
    def restore_latest_backup(cls, path: Path) -> Optional[Path]:
        """Replace a (damaged) database with its newest readable backup; returns the backup used."""

        path = Path(path)
        candidates = sorted((path.parent / "backups").glob("inventory-*.db"), key=lambda p: p.stat().st_mtime,
                            reverse=True)
        bak = path.with_name(path.name + ".bak")
        if bak.exists():
            candidates.append(bak)
        for candidate in candidates:
            try:
                probe = sqlite3.connect(f"file:{candidate}?mode=ro", uri=True)
                ok = probe.execute("PRAGMA quick_check(1)").fetchone()[0] == "ok"
                probe.close()
            except sqlite3.Error:
                ok = False
            if not ok:
                continue
            cls.set_aside(path)
            data = candidate.read_bytes()
            path.write_bytes(data)
            os.chmod(path, 0o600)
            return candidate
        return None

    @staticmethod
    def set_aside(path: Path) -> Optional[Path]:
        """Move a damaged database (and its WAL files) out of the way, keeping it for inspection."""

        path = Path(path)
        if not path.exists():
            return None
        aside = path.with_name(f"{path.name}.damaged-{time.strftime('%Y%m%d-%H%M%S')}")
        os.replace(path, aside)
        for suffix in ("-wal", "-shm"):
            extra = path.with_name(path.name + suffix)
            if extra.exists():
                try:
                    extra.unlink()
                except OSError:
                    pass
        return aside
