"""Opening the inventory at start-up, with a decision for every failure (no GTK).

``open_inventory_store(path, ask)`` loops until it has a store or the user
quits; ``ask(kind, message)`` returns a :class:`Choice` (the GTK layer shows a
dialog, tests pass a function):

=================  =====================================================
kind               choices
=================  =====================================================
``locked``         RETRY, READ_ONLY, QUIT (another instance is writing)
``corrupt``        RESTORE (latest backup), START_EMPTY (the damaged file
                   is kept aside), QUIT
``too_new``        READ_ONLY (an empty read-only inventory), QUIT — the
                   newer file is never touched
``unavailable``    READ_ONLY (the existing file read-only, else empty), QUIT
=================  =====================================================
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Callable, Optional

from .store import InventoryCorrupt, InventoryError, InventoryLocked, InventoryStore, InventoryTooNew, InventoryUnavailable


class Choice(str, Enum):
    RETRY = "retry"
    RESTORE = "restore"
    START_EMPTY = "start_empty"
    READ_ONLY = "read_only"
    QUIT = "quit"


Ask = Callable[[str, str], Choice]


def empty_read_only_store(reason: str) -> InventoryStore:
    """An empty, read-only inventory in memory (the app runs; every change explains why it cannot)."""

    store = InventoryStore(Path(":memory:"))
    store.read_only = True
    store.read_only_reason = reason
    return store


def _read_only(path: Path, reason: str) -> InventoryStore:
    try:
        store = InventoryStore(path, read_only=True)
        store.read_only_reason = reason
        return store
    except InventoryError:
        return empty_read_only_store(reason)


def open_inventory_store(path: Path, ask: Ask) -> Optional[InventoryStore]:
    """The inventory at ``path`` (or a read-only stand-in); ``None`` when the user chose to quit."""

    path = Path(path)
    while True:
        try:
            return InventoryStore(path)
        except InventoryLocked as exc:
            choice = ask("locked", str(exc))
            if choice is Choice.RETRY:
                continue
            if choice is Choice.READ_ONLY:
                return _read_only(path, str(exc))
            return None
        except InventoryCorrupt as exc:
            choice = ask("corrupt", str(exc))
            if choice is Choice.RESTORE:
                if InventoryStore.restore_latest_backup(path) is None:
                    if ask("corrupt", "No readable backup was found. " + str(exc)) is Choice.QUIT:
                        return None
                    InventoryStore.set_aside(path)
                continue
            if choice is Choice.START_EMPTY:
                InventoryStore.set_aside(path)
                continue
            return None
        except InventoryTooNew as exc:
            if ask("too_new", str(exc)) is Choice.READ_ONLY:
                return empty_read_only_store(str(exc))
            return None
        except InventoryUnavailable as exc:
            if ask("unavailable", str(exc)) is Choice.READ_ONLY:
                return _read_only(path, str(exc)) if path.exists() else empty_read_only_store(str(exc))
            return None
