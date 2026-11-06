"""Favorites of the active inventory: starring, the lookup the sidebar section draws from, and sync.

The keys and lists are :mod:`gtkui.favorites`; this controller knows the window: which inventory is
active (the scope), how a tree item maps to its key, and how a key maps back to the item. That lookup
(:meth:`FavoritesController.index`) is built from the parsed inventory once per rebuild and only while
the inventory has favorites, so starring never rebuilds the tree and an inventory without favorites
costs nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Dict, List, Optional

from ...config import HostEntry
from ..constants import RowKind, TreeColumn
from ..favorites import FAVORITES_LIMIT, Favorites, group_key, group_path, host_key, scope_for
from ..i18n import ngettext
from ..theme.tokens import Level
from .tree_controller import SHARED_PREFIX

if TYPE_CHECKING:  # pragma: no cover
    from ...gtk_app import SSHManagerWindow


@dataclass(frozen=True)
class Target:
    """A tree item a favorite key points at."""

    kind: RowKind
    parent: str
    child: str = ""
    alias: str = ""
    host: Optional[HostEntry] = None


class FavoritesController:
    """Starred hosts and groups of the active inventory."""

    def __init__(self, win: "SSHManagerWindow") -> None:
        self.win = win
        self.lists = Favorites(win._ui.favorites)
        self.listeners: List[Callable[[], None]] = []  # the list or the inventory changed
        self._index: Optional[Dict[str, Target]] = None

    # ------------------------------------------------------------ keys
    @property
    def scope(self) -> str:
        return scope_for(self.win.manager.base_dir)

    def keys(self) -> List[str]:
        return self.lists.keys(self.scope)

    def key_for(self, kind: RowKind, parent: str, child: str = "", host: Optional[HostEntry] = None) -> Optional[str]:
        """The favorite key of a tree item; None for items that cannot be favorites (a shared inventory's)."""

        if parent.startswith(SHARED_PREFIX):
            return None
        if kind is RowKind.HOST:
            return host_key(self.win.creds.credential_key(host)) if host is not None else None
        child = "" if kind is RowKind.PARENT else child
        group_id = getattr(self.win.manager, "group_id", None)  # the app store: stable ids
        stable = group_id(parent, child) if group_id is not None else None
        return group_key(stable or group_path(parent, child))

    def is_favorite(self, key: Optional[str]) -> bool:
        return bool(key) and self.lists.contains(self.scope, key)

    def is_favorite_host(self, host: HostEntry) -> bool:
        return self.is_favorite(host_key(self.win.creds.credential_key(host)))

    def selected_keys(self) -> List[str]:
        """Keys of the selected hosts, else of the selected group; [] when one of them cannot be a favorite."""

        selection = self.win.tree_view.get_selection()
        model, paths = selection.get_selected_rows() if selection is not None else (None, [])
        if not model or not paths:
            return []
        rows = [model.get_iter(path) for path in paths]
        hosts = [it for it in rows if model.get_value(it, TreeColumn.KIND) == RowKind.HOST.value]
        keys = [self._key_at(model, it) for it in (hosts or rows[:1])]
        return [] if None in keys else keys  # type: ignore[return-value]

    def _key_at(self, model, tree_iter) -> Optional[str]:
        kind = RowKind.coerce(model.get_value(tree_iter, TreeColumn.KIND))
        if kind is None:
            return None
        return self.key_for(kind, model.get_value(tree_iter, TreeColumn.PARENT) or "",
                            model.get_value(tree_iter, TreeColumn.CHILD) or "",
                            self.win.tree.coerce_host(model.get_value(tree_iter, TreeColumn.HOST)))

    def selection_state(self) -> Optional[bool]:
        """Whether the selection is starred; None when nothing (or nothing that can be) is selected."""

        keys = self.selected_keys()
        if not keys:
            return None
        return all(self.is_favorite(key) for key in keys)

    # ---------------------------------------------------------- changes
    def toggle_selected(self) -> None:
        keys = self.selected_keys()
        if keys:
            self.toggle(keys)

    def toggle(self, keys: List[str]) -> bool:
        """Star ``keys`` (unstar them when all are starred); returns True when they are favorites now."""

        scope = self.scope
        before = len(self.lists.keys(scope))
        starred = self.lists.toggle(scope, keys)
        if starred:
            missing = [key for key in keys if not self.lists.contains(scope, key)]
            if missing:
                self.win._notify(ngettext(
                    "Favorites are limited to {limit} items: {n} item was not added.",
                    "Favorites are limited to {limit} items: {n} items were not added.", len(missing),
                ).format(limit=FAVORITES_LIMIT, n=len(missing)), Level.INFO)
        if len(self.lists.keys(scope)) != before:
            self._changed()
        return starred

    def _changed(self) -> None:
        self.win._save_ui_state()
        for listener in list(self.listeners):
            listener()

    # ------------------------------------------------------------ sync
    @property
    def _paths_are_keys(self) -> bool:
        return getattr(self.win.manager, "group_id", None) is None  # folder inventories have no ids

    def rename_host(self, old_path: str, new_path: str) -> None:
        """A folder-inventory host got a new path (renamed or moved); app-store ids need nothing."""

        if self._paths_are_keys and self.lists.rename(self.scope, host_key(old_path), host_key(new_path)):
            self.win._save_ui_state()

    def remap_prefix(self, old_prefix: str, new_prefix: str) -> None:
        """A folder-inventory group was renamed or moved: its favorites and those below it follow."""

        if not self._paths_are_keys:
            return
        before = self.keys()
        self.lists.remap_prefix(self.scope, old_prefix, new_prefix)
        if self.keys() != before:
            self.win._save_ui_state()

    def inventory_changed(self) -> None:
        """After every tree rebuild: a fresh lookup, favorites of deleted items dropped, listeners told."""

        self._index = None
        manager = self.win.manager
        if self.keys() and manager.tree and not getattr(manager, "read_only", False):
            # A store opened read-only after an error, or an empty one, must not wipe the list.
            if self.lists.prune(self.scope, set(self.index())):
                self.win._save_ui_state()
        for listener in list(self.listeners):
            listener()

    # ---------------------------------------------------------- lookup
    def index(self) -> Dict[str, Target]:
        """Every item of the active inventory by its favorite key (built once per rebuild)."""

        if self._index is None:
            self._index = self._build_index()
        return self._index

    def _build_index(self) -> Dict[str, Target]:
        index: Dict[str, Target] = {}
        manager = self.win.manager
        group_id = getattr(manager, "group_id", None)
        credential_key = self.win.creds.credential_key
        for parent, children in manager.tree.items():
            top = group_id(parent) if group_id is not None else None
            index[group_key(top or group_path(parent, ""))] = Target(RowKind.PARENT, parent)
            for child, group in children.items():
                if child:
                    parts = child.split("/")
                    for depth in range(1, len(parts)):  # folder inventories: the folders above *.conf files
                        folder = "/".join(parts[:depth])
                        if folder not in children:
                            index.setdefault(group_key(group_path(parent, folder)),
                                             Target(RowKind.CHILD_FOLDER, parent, folder))
                    index[group_key(group.id or group_path(parent, child))] = Target(RowKind.CHILD, parent, child)
                for host in group.hosts:
                    index[host_key(credential_key(host))] = Target(RowKind.HOST, parent, child, host.alias, host)
        return index

    def targets(self) -> List[tuple]:
        """``[(key, target), …]`` of the favorites that exist, in starring order (unknown keys skipped)."""

        keys = self.keys()
        if not keys:
            return []
        index = self.index()
        return [(key, index[key]) for key in keys if key in index]

