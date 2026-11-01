"""The app's folders, and the one-time move of "SSH Manager"'s folders to Ophion's (no GTK).

:func:`app_dir` is where every file of the app lives (``config``, ``data``,
``state``, honouring ``XDG_*``): ``~/.config/ophion`` … — or, for a run where
the move failed, the old ``~/.config/ssh-manager`` …, so the app never starts
empty while the user's data is elsewhere.

:func:`migrate` runs once at start-up, **before** anything opens the
inventory, settings, keybox or macros:

* nothing to do when the new folder exists (never merged, never overwritten)
  or the old one does not;
* refused while the old app may be running (its application id is owned on
  the session bus, or its inventory database is open);
* otherwise ``os.rename(old, new)`` and a symlink ``old → new`` (an older
  build still finds its data); when renaming is impossible (another file
  system, permissions), a **verified copy** (modes and times kept, every file
  compared by size and SHA-256) published with one rename — the old folder is
  then left untouched, never deleted;
* ``<new>/.migrated-from-ssh-manager`` records when, from where and how.

Any error leaves the folders as they were and makes this run use the old ones.
Nothing secret is logged (names of exceptions and paths only).
"""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Tuple

from .identity import APP_SLUG, LEGACY_APP_ID, LEGACY_SLUG

LOGGER = logging.getLogger(__name__)
KINDS = {"config": ("XDG_CONFIG_HOME", (".config",)), "data": ("XDG_DATA_HOME", (".local", "share")),
         "state": ("XDG_STATE_HOME", (".local", "state"))}
MARKER = ".migrated-from-ssh-manager"

NOTHING, DONE, COPIED, BLOCKED, FAILED = "nothing", "done", "copied", "blocked", "failed"

_use_legacy = False  # this run uses the old folders (the move failed)


class MigrationError(Exception):
    pass


# ---------------------------------------------------------------- folders
def base_dir(kind: str) -> Path:
    variable, default = KINDS[kind]
    value = os.environ.get(variable)
    return Path(value).expanduser() if value else Path.home().joinpath(*default)


def new_dir(kind: str) -> Path:
    return base_dir(kind) / APP_SLUG


def legacy_dir(kind: str) -> Path:
    return base_dir(kind) / LEGACY_SLUG


def app_dir(kind: str) -> Path:
    """The folder the app uses for ``kind`` in this run."""

    return legacy_dir(kind) if _use_legacy else new_dir(kind)


def using_legacy_dirs() -> bool:
    return _use_legacy


def rebase(path: str) -> str:
    """A path saved under an old folder (a setting), moved to this run's folder; anything else unchanged."""

    for kind in KINDS:
        old = legacy_dir(kind)
        try:
            relative = Path(path).expanduser().relative_to(old)
        except ValueError:
            continue
        return str(app_dir(kind) / relative)
    return path


# ------------------------------------------------------------- old app
def database_in_use(path: Path) -> bool:
    """Another process has this SQLite database open (an exclusive lock is refused); never writes to it."""

    import sqlite3

    if not path.is_file():
        return False
    try:
        conn = sqlite3.connect(str(path), timeout=0, isolation_level=None)
    except sqlite3.Error:
        return False
    try:
        conn.execute("PRAGMA locking_mode=EXCLUSIVE")
        conn.execute("BEGIN EXCLUSIVE")
        conn.execute("COMMIT")
        return False
    except sqlite3.Error as exc:
        return "locked" in str(exc).lower() or "busy" in str(exc).lower()
    finally:
        conn.close()


def bus_name_owned(name: str = LEGACY_APP_ID) -> bool:
    """The old app registers its application id on the session bus while it runs."""

    try:
        from gi.repository import Gio, GLib  # type: ignore

        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        reply = bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                              "NameHasOwner", GLib.Variant("(s)", (name,)), GLib.VariantType("(b)"),
                              Gio.DBusCallFlags.NONE, 1000, None)
        return bool(reply.unpack()[0])
    except Exception as exc:  # no session bus: rely on the database check
        LOGGER.debug("legacy app check: no session bus (%s)", type(exc).__name__)
        return False


def legacy_app_running() -> bool:
    return bus_name_owned() or database_in_use(legacy_dir("data") / "inventory.db")


# ------------------------------------------------------------- the move
@dataclass
class Moved:
    kind: str
    old: Path
    new: Path
    method: str  # "rename" or "copy"


@dataclass
class MigrationResult:
    status: str
    moved: List[Moved] = field(default_factory=list)
    seconds: float = 0.0

    @property
    def copied(self) -> List[Moved]:
        return [m for m in self.moved if m.method == "copy"]


def _exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def plan() -> List[Tuple[str, Path, Path]]:
    """``(kind, old, new)`` for every old folder that still has to move."""

    found = []
    for kind in KINDS:
        old, new = legacy_dir(kind), new_dir(kind)
        if old.is_dir() and not old.is_symlink() and not _exists(new):
            found.append((kind, old, new))
    return found


def migrate(*, app_running: Callable[[], bool] = legacy_app_running,
            clock: Callable[[], float] = time.time) -> MigrationResult:
    global _use_legacy

    _use_legacy = False
    started = time.monotonic()
    todo = plan()
    if not todo:
        return MigrationResult(NOTHING)
    if app_running():
        return MigrationResult(BLOCKED)
    moved: List[Moved] = []
    try:
        for kind, old, new in todo:
            method = _move(old, new)
            moved.append(Moved(kind, old, new, method))
            _write_marker(new, old, method, clock())
            LOGGER.info("moved %s to %s (%s)", old, new, method)
    except Exception as exc:
        LOGGER.warning("the folders of SSH Manager were not moved (%s); using them where they are",
                       type(exc).__name__)
        _use_legacy = True
        return MigrationResult(FAILED, moved, time.monotonic() - started)
    status = COPIED if any(m.method == "copy" for m in moved) else DONE
    return MigrationResult(status, moved, time.monotonic() - started)


def _move(old: Path, new: Path) -> str:
    new.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.rename(old, new)
    except OSError as exc:
        LOGGER.info("rename %s failed (%s); copying instead", old, errno.errorcode.get(exc.errno, exc.errno))
        verified_copy(old, new)
        return "copy"
    try:
        os.symlink(new, old)  # an older build started later still finds its data
    except OSError as exc:
        LOGGER.info("no symlink %s (%s)", old, type(exc).__name__)
    return "rename"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_tree(root: Path) -> None:
    for folder, _dirs, files in os.walk(root):
        for name in files:
            path = Path(folder) / name
            if path.is_symlink():
                continue
            fd = os.open(path, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        fd = os.open(folder, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def verify_copy(old: Path, copy: Path) -> None:
    """Every file of ``old`` is in ``copy`` with the same size, SHA-256 and mode; raises otherwise."""

    for folder, _dirs, files in os.walk(old):
        for name in files:
            source = Path(folder) / name
            target = copy / source.relative_to(old)
            if source.is_symlink():
                if not target.is_symlink() or os.readlink(source) != os.readlink(target):
                    raise MigrationError(f"{target} is not the same link")
                continue
            a, b = source.stat(), target.stat()
            if a.st_size != b.st_size or (a.st_mode & 0o7777) != (b.st_mode & 0o7777):
                raise MigrationError(f"{target} differs in size or mode")
            if _sha256(source) != _sha256(target):
                raise MigrationError(f"{target} differs in content")


def verified_copy(old: Path, new: Path) -> None:
    """Copy ``old`` to ``new`` (modes, times), check it, then publish it with one rename; ``old`` is untouched."""

    partial = new.with_name(f"{new.name}.partial-{os.getpid()}")
    shutil.rmtree(partial, ignore_errors=True)
    try:
        shutil.copytree(old, partial, symlinks=True, copy_function=shutil.copy2)
        _fsync_tree(partial)
        verify_copy(old, partial)
        os.rename(partial, new)
    except BaseException:
        shutil.rmtree(partial, ignore_errors=True)
        raise


def _write_marker(new: Path, old: Path, method: str, now: float) -> None:
    from .persistence import write_bytes_private

    record = {"from": str(old), "method": method, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))}
    write_bytes_private(new / MARKER, (json.dumps(record, indent=1) + "\n").encode("utf-8"))


def use_legacy_dirs(value: bool) -> None:
    """For tests and the failure path: make :func:`app_dir` return the old folders (or not)."""

    global _use_legacy
    _use_legacy = bool(value)


__all__ = ["app_dir", "new_dir", "legacy_dir", "base_dir", "rebase", "migrate", "MigrationResult", "Moved",
           "legacy_app_running", "database_in_use", "bus_name_owned", "verified_copy", "verify_copy", "plan",
           "using_legacy_dirs", "use_legacy_dirs", "NOTHING", "DONE", "COPIED", "BLOCKED", "FAILED", "MARKER"]
