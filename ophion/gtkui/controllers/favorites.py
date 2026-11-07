"""Favorites of the active inventory: starring, the lookup the sidebar section draws from, and sync.

The keys and lists are :mod:`gtkui.favorites`; this controller knows the window: which inventory is
active (the scope), how a tree item maps to its key, and how a key maps back to the item. That lookup
(:meth:`FavoritesController.index`) is built from the parsed inventory once per rebuild and only while
the inventory has favorites, so starring never rebuilds the tree and an inventory without favorites
costs nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Dict, List, Optional, Tuple

from ...config import HostEntry
from ..constants import RowKind, TreeColumn
from ..favorites import FAVORITES_LIMIT, Favorites, group_key, group_path, host_key, scope_for
from ..i18n import _, ngettext
from ..theme.tokens import ConnectionStatus, Level
from .tree_controller import SHARED_PREFIX, TreeRow, host_tooltip

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
        self.listeners: List[Callable[[], None]] = [self.refresh_section]  # the list or the inventory changed
        self._index: Optional[Dict[str, Target]] = None
        self.section = None  # views.favorites.FavoritesSection (attach())
        self._shown: List[Tuple[str, Target]] = []  # the section's rows, in order
        self.section_refreshes = 0  # tests and benchmarks

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
        self.win.tree.update_details()  # the header star
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
        host = RowKind.HOST
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
                # host_key(creds.credential_key(entry)), spelled out: this loop sees every host of the inventory
                path = f"host:{parent}::{child}::"
                for entry in group.hosts:
                    index[f"host:{entry.id}" if entry.id else path + entry.alias] = Target(
                        host, parent, child, entry.alias, entry)
        return index

    def targets(self) -> List[tuple]:
        """``[(key, target), …]`` of the favorites that exist, in starring order (unknown keys skipped)."""

        keys = self.keys()
        if not keys:
            return []
        index = self.index()
        return [(key, index[key]) for key in keys if key in index]

    # --------------------------------------------------------- section
    def attach(self, section) -> None:
        self.section = section
        section.on_expanded = self._on_expanded
        section.on_selected = lambda index: self.reveal(index)
        section.on_activated = self.activate
        section.on_context_menu = self.show_context_menu
        section.on_toggle_favorite = lambda index: self.toggle([self._shown[index][0]])
        section.set_expanded(self.win._ui.favorites_expanded)

    def _on_expanded(self, expanded: bool) -> None:
        if self.win._ui.favorites_expanded != expanded:
            self.win._ui.favorites_expanded = expanded
            self.win._save_ui_state()

    def refresh_section(self) -> None:
        """Rows again (O(favorites)); none while a search or filter is active: the tree shows the results."""

        if self.section is None:
            return
        self.section_refreshes += 1
        filtering = bool(self.win._sidebar.search_text or self.win.tree.filter_query.strip())
        self._shown = [] if filtering else self.targets()
        self.section.set_rows([self._row(key, target) for key, target in self._shown])
        self.sync_selection()

    def _row(self, key: str, target: Target) -> TreeRow:
        tree, colors = self.win.tree, self.win.colors
        if target.kind is RowKind.HOST and target.host is not None:
            host = target.host
            tree_iter = tree.host_iters.get(self.win.creds.host_key(host))
            if tree_iter is not None:  # the tree's row has it all already (status, colour, tooltip)
                model = self.win.tree_store
                status = model.get_value(tree_iter, TreeColumn.STATUS) or ConnectionStatus.UNKNOWN.value
                return TreeRow(RowKind.HOST, target.alias, target.parent, target.child, target.alias, host,
                               color=model.get_value(tree_iter, TreeColumn.COLOR) or "", status=status,
                               tooltip=self._with_location(model.get_value(tree_iter, TreeColumn.TOOLTIP) or "", target),
                               read_only=bool(model.get_value(tree_iter, TreeColumn.READ_ONLY)))
            status = ConnectionStatus.coerce(tree.host_status.get(self.win.creds.host_key(host), "unknown")).value
            return TreeRow(RowKind.HOST, target.alias, target.parent, target.child, target.alias, host,
                           color=colors.item_color_for(target.parent, target.child, target.alias), status=status,
                           tooltip=self._host_tooltip(target, status),
                           read_only=not self.win.creds.permissions_for_host(host).get("can_edit", True))
        child = "" if target.kind is RowKind.PARENT else target.child
        hosts = tree.hosts_in_parent_group(target.parent) if not child else tree.hosts_in_child_group(target.parent,
                                                                                                         child)
        group = self.win.manager.tree.get(target.parent, {}).get(child)
        count = len(hosts)
        return TreeRow(target.kind, child.rsplit("/", 1)[-1] if child else target.parent, target.parent, child,
                       color=colors.item_color_for(target.parent, child), count=count,
                       tooltip="\n".join(filter(None, (self._location(target.parent, child.rpartition("/")[0]),
                                                      ngettext("{n} host", "{n} hosts", count).format(n=count)))),
                       file_backed=group.file_backed if group is not None else True)

    @staticmethod
    def _location(parent: str, child: str) -> str:
        """Where an item lives: ``prod › db``; ``""`` for a top-level group."""

        return " › ".join([parent] + [part for part in child.split("/") if part]) if parent else ""

    def _host_tooltip(self, target: Target, status: str) -> str:
        host = target.host
        user = self.win.creds.display_username_for_host(host)
        return self._with_location(host_tooltip(host, user, status, self.win.creds.password_summary(host)), target)

    def _with_location(self, tooltip: str, target: Target) -> str:
        return tooltip + "\n" + _("In {location}").format(location=self._location(target.parent, target.child))

    def on_status(self, path_key: str, state: str) -> None:
        """The tree's status event for a host (the same source; no polling of its own)."""

        for index, (_key, target) in enumerate(self._shown):
            if target.host is not None and self.win.creds.host_key(target.host) == path_key:
                tree_iter = self.win.tree.host_iters.get(path_key)  # the tree set its tooltip just before
                tooltip = (self._with_location(self.win.tree_store.get_value(tree_iter, TreeColumn.TOOLTIP) or "", target)
                           if tree_iter is not None else self._host_tooltip(target, state))
                self.section.update_row(index, ConnectionStatus.coerce(state).value, tooltip)

    def sync_selection(self) -> None:
        """Mirror the tree's selection into the list (the tree stays the one selection)."""

        if self.section is None:
            return
        keys = self.selected_keys()
        index = None
        if len(keys) == 1:
            index = next((i for i, (key, _t) in enumerate(self._shown) if key == keys[0]), None)
        self.section.select_index(index)

    def tree_path(self, target: Target):
        if target.kind is RowKind.HOST and target.host is not None:
            tree_iter = self.win.tree.host_iters.get(self.win.creds.host_key(target.host))
            return self.win.tree_store.get_path(tree_iter) if tree_iter is not None else None
        child = "" if target.kind is RowKind.PARENT else target.child
        return self.win.tree_view._find_path(target.kind.value, target.parent, child)

    def reveal(self, index: int):
        """Select the favorite in the tree: its groups open and it scrolls into view."""

        if not 0 <= index < len(self._shown):
            return None
        path = self.tree_path(self._shown[index][1])
        if path is not None:
            selection = self.win.tree_view.get_selection()
            selection.unselect_all()
            self.win.tree.select_path(path)
        return path

    def activate(self, index: int) -> None:
        """Enter / double-click: a host connects; a group opens in the tree."""

        path = self.reveal(index)
        target = self._shown[index][1] if 0 <= index < len(self._shown) else None
        if target is None or path is None:
            return
        if target.kind is RowKind.HOST:
            self.win.sessions.open_hosts([target.host])
        else:
            self.win.tree_view.expand_row(path, False)
            self.win.tree_view.scroll_to_cell(path, None, False, 0.0, 0.0)

    def show_context_menu(self, event=None) -> None:
        """The tree row's own menu, next to the favorite."""

        from gi.repository import Gdk

        from ..widgets.menus import build_context_menu, popup_menu

        index = self.section.list.selected_index()
        if index is None:
            return
        self.reveal(index)
        menu = build_context_menu(self.win.tree.context_menu_spec(), attach_to=self.section.list)
        if event is not None:
            popup_menu(menu, event)
            return
        rect, window = self.section.selected_row_rect(), self.section.list.get_bin_window()
        if rect is not None and window is not None:
            menu.popup_at_rect(window, rect, Gdk.Gravity.SOUTH_WEST, Gdk.Gravity.NORTH_WEST, None)
        else:
            popup_menu(menu, None, widget=self.section.list)

