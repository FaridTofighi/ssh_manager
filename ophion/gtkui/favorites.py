"""Favorites: the hosts and groups the user starred, per inventory (GTK-free).

A favorite is a key: ``host:<id>`` / ``group:<id>`` where the inventory has stable ids (the app
store), else the item's path (``host:parent::child::alias``, ``group:parent::child::``; a top-level
group is ``group:parent::``). Ids survive renames and moves; path keys are carried along by
:meth:`Favorites.rename` / :meth:`Favorites.remap_prefix` in the same places that move credential
bindings. Keys of items that no longer exist are skipped when shown and dropped by
:meth:`Favorites.prune`.

The lists live in the UI state (``UiState.favorites``), one per inventory, keyed by
:func:`scope_for` of the inventory path, so a favorite of one workspace never shows in another.
They are a personal display preference: never part of an export, credentials or the keybox.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Iterable, List, Set, Tuple

FAVORITES_LIMIT = 100  # a soft cap: the section must not become a second tree


def host_key(stable: str) -> str:
    """``stable`` is the host's id, else its path key ``parent::child::alias``."""

    return f"host:{stable}"


def group_key(stable: str) -> str:
    """``stable`` is the group's id, else :func:`group_path`."""

    return f"group:{stable}"


def group_path(parent: str, child: str) -> str:
    """A group's path key; it is the prefix of every path key below the group."""

    return f"{parent}::{child}::" if child else f"{parent}::"


def scope_for(base_dir: Path) -> str:
    """The inventory a favorites list belongs to (as ``recent_inventories`` names it)."""

    try:
        return str(Path(base_dir).expanduser().resolve())
    except (OSError, TypeError):
        return str(base_dir)


def clean_keys(keys: Iterable[object]) -> List[str]:
    """Distinct non-empty strings in their order, at most :data:`FAVORITES_LIMIT`."""

    seen: Dict[str, None] = {}
    for key in keys:
        if isinstance(key, str) and key and key not in seen:
            seen[key] = None
            if len(seen) == FAVORITES_LIMIT:
                break
    return list(seen)


class Favorites:
    """Edits ``lists`` (``{scope: [key, …]}``, the UI state's own dict) in place."""

    def __init__(self, lists: Dict[str, List[str]]) -> None:
        self.lists = lists

    def keys(self, scope: str) -> List[str]:
        return list(self.lists.get(scope, ()))

    def contains(self, scope: str, key: str) -> bool:
        return key in self.lists.get(scope, ())

    def _store(self, scope: str, keys: List[str]) -> None:
        if keys:
            self.lists[scope] = keys
        else:
            self.lists.pop(scope, None)

    def add(self, scope: str, keys: Iterable[str]) -> Tuple[List[str], int]:
        """Append the new ``keys``; returns ``(added, refused by the cap)``."""

        current = self.keys(scope)
        added: List[str] = []
        refused = 0
        for key in keys:
            if key in current:
                continue
            if len(current) >= FAVORITES_LIMIT:
                refused += 1
                continue
            current.append(key)
            added.append(key)
        self._store(scope, current)
        return added, refused

    def remove(self, scope: str, keys: Iterable[str]) -> None:
        gone = set(keys)
        self._store(scope, [key for key in self.keys(scope) if key not in gone])

    def toggle(self, scope: str, keys: Iterable[str]) -> bool:
        """All of ``keys`` favorites: remove them (False); else add the missing ones (True)."""

        keys = list(keys)
        if keys and all(self.contains(scope, key) for key in keys):
            self.remove(scope, keys)
            return False
        self.add(scope, keys)
        return True

    def rename(self, scope: str, old: str, new: str) -> bool:
        current = self.keys(scope)
        if old not in current:
            return False
        if new in current:
            current.remove(old)
        else:
            current[current.index(old)] = new
        self._store(scope, current)
        return True

    def remap_prefix(self, scope: str, old_prefix: str, new_prefix: str) -> None:
        """Path keys under ``old_prefix`` (a :func:`group_path`) move to ``new_prefix``."""

        moved: List[str] = []
        for key in self.keys(scope):
            kind, _sep, path = key.partition(":")
            if path.startswith(old_prefix):
                key = f"{kind}:{new_prefix}{path[len(old_prefix):]}"
            if key not in moved:
                moved.append(key)
        self._store(scope, moved)

    def prune(self, scope: str, valid: Set[str]) -> bool:
        """Drop keys not in ``valid``; True when something was dropped."""

        current = self.keys(scope)
        kept = [key for key in current if key in valid]
        if len(kept) == len(current):
            return False
        self._store(scope, kept)
        return True
