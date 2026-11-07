"""Session tree: rows, selection, filters, smart groups and context menus."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Dict, Iterable, List, Optional, Set, Tuple

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gtk  # type: ignore  # noqa: E402

from ...config import HostEntry  # noqa: E402
from ... import protocols  # noqa: E402
from ...smart_groups import SmartGroup, load_smart_groups, save_smart_groups  # noqa: E402
from ..constants import RowKind, TreeColumn  # noqa: E402
from ..i18n import _  # noqa: E402
from . import context_menus, details_presenter  # noqa: E402
from ..views.sidebar import TreePage  # noqa: E402
from ..widgets.menus import build_context_menu, popup_menu  # noqa: E402
from .shortcuts import accel_label  # noqa: E402
from .tree_controller import (  # noqa: E402
    FilterTerms, TreeCallbacks, TreeRow, build_tree_rows, filter_host, host_tooltip, parse_filter, search_text_for_host,
    sidebar_page_for,
)

if TYPE_CHECKING:  # pragma: no cover
    from ...gtk_app import SSHManagerWindow


LOGGER = logging.getLogger(__name__)


class TreeController:
    """Session tree: rows, selection, filters, smart groups and context menus."""

    def __init__(self, win: "SSHManagerWindow") -> None:
        self.win = win
        self.host_lookup: Dict[str, HostEntry] = {}
        self.host_status: Dict[str, str] = {}
        self.host_iters: Dict[str, Gtk.TreeIter] = {}
        self.read_only_keys: set = set()
        self.filter_query: str = win._ui.filter_query
        self.filter_error: str = ""
        self.smart_groups: List[SmartGroup] = load_smart_groups()

    def inventories(self):
        inventories = [(self.win.manager.tree, False)]
        if self.win.inventory.shared_manager:
            inventories.append((self.win.inventory.shared_manager.tree, True))
        return inventories

    def rebuild(self, *, refresh: bool = False) -> None:
        if refresh:
            self.win.manager.refresh()
        query = self.win._sidebar.search_text
        terms, error = parse_filter(self.filter_query)
        self.filter_error = error
        self.win._sidebar.set_filter_error(error)
        inventories = self.inventories()
        rows = build_tree_rows(
            inventories,
            query,
            terms,
            TreeCallbacks(
                display_user=self.win.creds.display_username_for_host,
                status=lambda key: self.host_status.get(key, "unknown"),
                color=self.win.colors.item_color_for,
                read_only=lambda host: not self.win.creds.permissions_for_host(host).get("can_edit", True),
                password=self.win.creds.password_summary,
                favorite=self.win.favorites.is_favorite_host,
            ),
        )
        selected = self.current_item()
        filtered = bool(query.strip() or terms)
        self.host_lookup.clear()
        self.read_only_keys = set()
        for top in rows:
            for row in top.walk():
                if row.kind is RowKind.HOST and row.host is not None:
                    self.host_lookup[row.key] = row.host
                    self.host_status.setdefault(row.key, "unknown")
                    if row.shared:
                        self.read_only_keys.add(row.key)
        # Unfiltered: exactly the groups the user opened this session (none at startup).
        # Filtered: the ancestors of the matches, temporarily (clearing restores the user's view).
        temporary = search_reveal_keys(rows, query, terms, self.win.favorites.is_favorite_host) if filtered else None
        self.host_iters = self.win.tree_view.populate(rows, temporary=temporary)
        self.win.creds.prune_bindings()  # app store: bindings of deleted hosts go away
        favorites = getattr(self.win, "favorites", None)
        if favorites is not None:
            favorites.inventory_changed()  # a fresh lookup; favorites of deleted items go away
        self.restore_selection(*selected)
        self.win._sidebar.set_page(TreePage(sidebar_page_for(inventories, rows, bool(query or terms))))
        if hasattr(self.win._sidebar, "set_host_count"):
            self.win._sidebar.set_host_count(
                sum(len(group.hosts) for groups in self.win.manager.tree.values() for group in groups.values())
            )
        self.update_details()
        self.update_action_state()

    def recolor(self) -> None:
        """Icon colours again (defaults or theme changed), in place: selection, expansion and scroll stay."""

        model = self.win.tree_store
        color_for = self.win.colors.item_color_for

        def walk(tree_iter) -> None:
            while tree_iter is not None:
                kind = model.get_value(tree_iter, TreeColumn.KIND)
                parent = model.get_value(tree_iter, TreeColumn.PARENT) or ""
                child = model.get_value(tree_iter, TreeColumn.CHILD) or ""
                alias = (model.get_value(tree_iter, TreeColumn.ALIAS) or "") if kind == RowKind.HOST.value else ""
                color = color_for(parent, "" if kind == RowKind.PARENT.value else child, alias)
                if (model.get_value(tree_iter, TreeColumn.COLOR) or "") != color:
                    model.set_value(tree_iter, TreeColumn.COLOR, color)
                walk(model.iter_children(tree_iter))
                tree_iter = model.iter_next(tree_iter)

        walk(model.get_iter_first())
        self.update_details()

    def restore_selection(self, kind: str, data: Dict[str, str]) -> None:
        """Re-select the previous row after a rebuild, only if it is visible: never expands anything."""

        row_kind = RowKind.coerce(kind)
        if row_kind is RowKind.HOST:
            self.select_host(data.get("parent", ""), data.get("child", ""), data.get("alias", ""), reveal=False)
        elif row_kind is RowKind.CHILD:
            self.select_child_group(data.get("parent", ""), data.get("child", ""), reveal=False)
        elif row_kind is RowKind.PARENT:
            self.select_parent_group(data.get("parent", ""), reveal=False)

    # ------------------------------------------------------ expand state
    def expand_all_groups(self) -> None:
        self.win.tree_view.expand_all_rows()

    def collapse_all_groups(self) -> None:
        self.win.tree_view.collapse_all_rows()

    def reveal_imported(self, top_groups: Iterable[str]) -> None:
        """After an import: open only the first level of the imported top-level groups and select the first."""

        names = [name for name in top_groups if name]
        view = self.win.tree_view
        for name in names:
            view.set_row_expanded(RowKind.PARENT.value, name)
        if names:
            self.select_parent_group(names[0])

    def remap_group_keys(self, old_parent: str, new_parent: str, old_child: str = "", new_child: str = "") -> None:
        """A group was renamed or moved: it stays open if it was (call before the rebuild)."""

        self.win.tree_view.remap_keys(old_parent, new_parent, old_child, new_child)

    def search_text_for_host(self, host: HostEntry) -> str:
        return search_text_for_host(host, self.win.creds.display_username_for_host(host))

    def refresh_smart_groups(self) -> None:
        self.win._sidebar.set_smart_groups([group.name for group in self.smart_groups])

    def on_filter_text_changed(self, text: str) -> None:
        self.filter_query = text.strip()
        self.win._sidebar.set_filter_active(self.filter_query)
        self.rebuild(refresh=False)

    def on_smart_group_activated(self, index: int) -> None:
        if index < 0 or index >= len(self.smart_groups):
            return
        group = self.smart_groups[index]
        self.win._sidebar.set_filter_text(group.query)
        self.on_filter_text_changed(group.query)

    def save_current_filter_as_group(self) -> None:
        if not self.filter_query:
            self.win._sidebar.set_filter_error(_("Enter a filter to save it as a smart group."))
            return
        name = self.win._prompt_text(
            _("Save Smart Group"),
            _("Name for the filter “{query}”:").format(query=self.filter_query),
            accept_label=_("_Save"),
        )
        if not name:
            return
        self.smart_groups.append(SmartGroup(name=name, query=self.filter_query))
        if not save_smart_groups(self.smart_groups):
            self.smart_groups.pop()
            self.win._notify_error(_("Could not save the smart group."))
            return
        self.refresh_smart_groups()
        self.win._notify_info(_("Saved smart group “{name}”.").format(name=name))

    def delete_smart_group(self, index: int) -> None:
        if index < 0 or index >= len(self.smart_groups):
            return
        group = self.smart_groups[index]
        if not self.win._confirm(
            _("Delete smart group “{name}”?").format(name=group.name),
            detail=_("Only the saved filter is deleted; hosts are not affected."),
            confirm_label=_("_Delete Smart Group"),
        ):
            return
        removed = self.smart_groups.pop(index)
        if not save_smart_groups(self.smart_groups):
            self.smart_groups.insert(index, removed)
            self.win._notify_error(_("Could not delete the smart group."))
            return
        self.refresh_smart_groups()

    def get_selected_iter(self) -> Optional[Gtk.TreeIter]:
        selection = self.win.tree_view.get_selection()
        if selection is None:  # tree already destroyed
            return None
        model_rows, paths = selection.get_selected_rows()
        if model_rows and paths:
            first_path = paths[0]
            return model_rows.get_iter(first_path)
        return None

    def coerce_host(self, obj) -> Optional[HostEntry]:
        if isinstance(obj, HostEntry):
            return obj
        if isinstance(obj, (list, tuple)):
            for item in obj:
                if isinstance(item, HostEntry):
                    return item
        return None

    def current_item(self) -> Tuple[str, Dict[str, str]]:
        tree_iter = self.get_selected_iter()
        if not tree_iter:
            return "", {}
        model = self.win.tree_store
        kind = model.get_value(tree_iter, TreeColumn.KIND)
        data = {
            "parent": model.get_value(tree_iter, TreeColumn.PARENT),
            "child": model.get_value(tree_iter, TreeColumn.CHILD),
            "alias": model.get_value(tree_iter, TreeColumn.ALIAS),
        }
        return kind, data

    def current_item_is_read_only(self) -> bool:
        kind, data = self.current_item()
        if kind != RowKind.HOST.value:
            return False
        key = self.make_host_key(data.get("parent", ""), data.get("child", ""), data.get("alias", ""))
        if key in self.read_only_keys:
            return True
        host = self.host_lookup.get(key)
        if host:
            perms = self.win.creds.permissions_for_host(host)
            return not perms.get("can_edit", True)
        return False

    def selected_hosts(self) -> List[HostEntry]:
        selection = self.win.tree_view.get_selection()
        if selection is None:
            return []
        model, paths = selection.get_selected_rows()
        hosts: List[HostEntry] = []
        if not model:
            return hosts
        for path in paths:
            tree_iter = model.get_iter(path)
            host = model.get_value(tree_iter, TreeColumn.HOST)
            coerced = self.coerce_host(host)
            if coerced:
                hosts.append(coerced)
        return hosts

    def selected_host(self) -> Optional[HostEntry]:
        tree_iter = self.get_selected_iter()
        if not tree_iter:
            return None
        host_obj = self.win.tree_store.get_value(tree_iter, TreeColumn.HOST)
        return self.coerce_host(host_obj)

    def set_host_status_for_key(self, key: str, state: str) -> None:
        state = state if state in {"connected", "connecting", "failed", "unknown"} else "unknown"
        previous = self.host_status.get(key, "unknown")
        self.host_status[key] = state
        host_page = getattr(self.win, "host_page", None)
        if host_page is not None:
            host_page.on_host_status(key, previous, state)
        tree_iter = self.host_iters.get(key)
        if tree_iter:
            try:
                self.win.tree_store.set_value(tree_iter, TreeColumn.STATUS, state)
                host = self.host_lookup.get(key)
                if host is not None:  # the tooltip spells the status out (color is not the only cue)
                    user = self.win.creds.display_username_for_host(host)
                    self.win.tree_store.set_value(tree_iter, TreeColumn.TOOLTIP,
                                                  host_tooltip(host, user, state, self.win.creds.password_summary(host)))
            except Exception:
                pass
        favorites = getattr(self.win, "favorites", None)
        if favorites is not None:
            favorites.on_status(key, state)
        if getattr(self.win, "closing", False):
            return
        if host_page is not None and self.selected_host() is not None:
            self.update_details()

    def set_host_status(self, host: HostEntry, state: str) -> None:
        self.set_host_status_for_key(self.win.creds.host_key(host), state)

    def on_key_press(self, _widget: Gtk.TreeView, event: Gdk.EventKey) -> bool:
        modifiers = event.state & Gtk.accelerator_get_default_mod_mask()
        if event.keyval == Gdk.KEY_Escape and not modifiers:
            if self.win.terminal_tabs.broadcast_active:
                self.win.terminal_tabs.set_broadcast_mode(False)
                return True
            if self.win._sidebar.search_text:
                self.win._sidebar.search_entry.set_text("")
                return True
            self.win.tree_view.get_selection().unselect_all()
            return True
        if event.keyval in (Gdk.KEY_Delete, Gdk.KEY_KP_Delete) and not modifiers:
            self.win.hosts.delete_current_item()
            return True
        if is_favorite_key(event):
            self.win.favorites.toggle_selected()
            return True
        return False

    def on_selection_changed(self, *_args) -> None:
        self.win.favorites.sync_selection()
        self.update_details()
        self.update_action_state()
        sidebar = getattr(self.win, "_sidebar", None)
        if sidebar is not None and hasattr(sidebar, "set_bulk_count"):
            sidebar.set_bulk_count(len(self.selected_hosts()))

    def on_row_activated(self, view: Gtk.TreeView, path, _column) -> None:
        # Double-click connects the activated host, even if it was not part of the selection.
        selection = view.get_selection()
        if selection:
            selection.unselect_all()
            selection.select_path(path)
        tree_iter = self.win.tree_store.get_iter(path)
        if not tree_iter:
            return
        host_obj = self.win.tree_store.get_value(tree_iter, TreeColumn.HOST)
        host = self.coerce_host(host_obj)
        if host:
            self.win.sessions.open_hosts([host])
        elif view.row_expanded(path):
            view.collapse_row(path)
        else:
            view.expand_row(path, False)

    def on_button_press(self, widget: Gtk.TreeView, event: Gdk.EventButton) -> bool:
        if event.button == 1:
            if not widget.get_path_at_pos(int(event.x), int(event.y)):
                widget.get_selection().unselect_all()
            return False
        if event.button != 3 or event.type != Gdk.EventType.BUTTON_PRESS:
            return False
        pos = widget.get_path_at_pos(int(event.x), int(event.y))
        if pos:
            path, column, _x, _y = pos
            widget.grab_focus()
            selection = widget.get_selection()
            # Preserve multi-selection when right-clicking on already-selected rows.
            if not selection.path_is_selected(path):
                widget.set_cursor(path, column, False)
        else:
            widget.get_selection().unselect_all()
        self.show_context_menu(event)
        return True

    def menu_accels(self) -> Dict[str, str]:
        return {name: accel_label(name) for name in ("host-connect", "rename", "host-delete", "new-host", "quick-connect",
                                                     "expand-all-groups", "collapse-all-groups", "toggle-favorite")}

    def context_menu_spec(self):
        kind_value, data = self.current_item()
        kind = RowKind.coerce(kind_value)
        accels = self.menu_accels()
        if kind is RowKind.HOST:
            host = self.selected_host()
            perms = self.win.creds.permissions_for_host(host) if host else {}
            can_edit = bool(host) and not self.win.creds.host_is_read_only(host)
            has_session = bool(host and self.win.terminal_tabs.find_tab(host))
            return context_menus.host_menu(
                self.win.actions,
                can_edit=can_edit and perms.get("can_edit", True),
                has_session=has_session,
                is_ssh=bool(host) and protocols.is_ssh(host),
                is_rdp=bool(host) and protocols.protocol_of(host) == "rdp",
                can_wake=bool(host) and bool(((host.metadata or {}).get("rdp") or {}).get("wol_mac")),
                favorite=self.win.favorites.selection_state(),
                accels=accels,
            )
        if kind is not None:
            parent = data.get("parent", "")
            child = data.get("child", "")
            if kind is RowKind.PARENT:
                has_hosts = bool(self.hosts_in_parent_group(parent))
            else:
                has_hosts = bool(self.hosts_in_child_group(parent, child))
            return context_menus.group_menu(
                self.win.actions, kind, read_only=parent.startswith("[Shared] "), has_hosts=has_hosts,
                favorite=self.win.favorites.selection_state(), accels=accels,
            )
        return context_menus.blank_menu(self.win.actions, accels=accels)

    def show_context_menu(self, event: Optional[Gdk.Event] = None) -> None:
        menu = build_context_menu(self.context_menu_spec(), attach_to=self.win.tree_view)
        if event is not None:
            popup_menu(menu, event)
            return
        rect = self.win.tree_view.selected_row_rect()
        window = self.win.tree_view.get_bin_window()
        if rect is not None and window is not None:
            menu.popup_at_rect(window, rect, Gdk.Gravity.SOUTH_WEST, Gdk.Gravity.NORTH_WEST, None)
        else:
            popup_menu(menu, None, widget=self.win.tree_view)

    def hosts_in_child_group(self, parent: str, child: str) -> List[HostEntry]:
        groups = self.win.manager.tree.get(parent, {})
        group = groups.get(child)
        if group:
            return list(group.hosts)
        prefix = f"{child}/" if child else ""
        hosts: List[HostEntry] = []
        if prefix:
            for name, grp in groups.items():
                if name.startswith(prefix):
                    hosts.extend(grp.hosts)
        return hosts

    def hosts_in_parent_group(self, parent: str) -> List[HostEntry]:
        groups = self.win.manager.tree.get(parent, {})
        hosts: List[HostEntry] = []
        for group in groups.values():
            hosts.extend(group.hosts)
        return list(hosts)

    def find_tree_path(self, predicate) -> Optional[Gtk.TreePath]:
        def traverse(model, tree_iter):
            while tree_iter:
                if predicate(model, tree_iter):
                    return model.get_path(tree_iter)
                if model.iter_has_child(tree_iter):
                    child = traverse(model, model.iter_children(tree_iter))
                    if child is not None:
                        return child
                tree_iter = model.iter_next(tree_iter)
            return None

        return traverse(self.win.tree_store, self.win.tree_store.get_iter_first())

    def select_path(self, path: Optional[Gtk.TreePath], *, reveal: bool = True) -> None:
        """Select ``path``. ``reveal`` (a user action) opens its ancestors; otherwise a hidden row is skipped."""

        if path is None:
            return
        view = self.win.tree_view
        if reveal:
            view.reveal_path(path)
        elif not view.is_row_visible(path):
            return  # e.g. the previous selection inside a group that is now closed
        view.set_cursor(path)
        view.scroll_to_cell(path)

    def select_host(self, parent: str, child: str, alias: str, *, reveal: bool = True) -> None:
        path = self.find_tree_path(
            lambda model, tree_iter: (
                model.get_value(tree_iter, TreeColumn.KIND) == RowKind.HOST.value
                and model.get_value(tree_iter, TreeColumn.PARENT) == parent
                and model.get_value(tree_iter, TreeColumn.CHILD) == child
                and model.get_value(tree_iter, TreeColumn.ALIAS) == alias
            )
        )
        self.select_path(path, reveal=reveal)

    def select_child_group(self, parent: str, child: str, *, reveal: bool = True) -> None:
        path = self.find_tree_path(
            lambda model, tree_iter: (
                model.get_value(tree_iter, TreeColumn.KIND) == RowKind.CHILD.value
                and model.get_value(tree_iter, TreeColumn.PARENT) == parent
                and model.get_value(tree_iter, TreeColumn.CHILD) == child
            )
        )
        self.select_path(path, reveal=reveal)

    def select_parent_group(self, parent: str, *, reveal: bool = True) -> None:
        path = self.find_tree_path(
            lambda model, tree_iter: (
                model.get_value(tree_iter, TreeColumn.KIND) == RowKind.PARENT.value
                and model.get_value(tree_iter, TreeColumn.PARENT) == parent
            )
        )
        self.select_path(path, reveal=reveal)

    def infer_target_group(self) -> Tuple[Optional[str], Optional[str]]:
        kind, data = self.current_item()
        if kind == RowKind.HOST.value:
            return data["parent"], data["child"]
        if kind == RowKind.CHILD.value:
            return data["parent"], data["child"]
        if kind == RowKind.PARENT.value:
            return data["parent"], None
        return None, None

    def make_host_key(self, parent: str, child: str, alias: str) -> str:
        return f"{parent}::{child}::{alias}"

    def generate_duplicate_alias(self, host: HostEntry) -> str:
        base = f"{host.alias}-copy"
        candidate = base
        counter = 2
        existing = {
            h.alias
            for groups in self.win.manager.tree.values()
            for group in groups.values()
            for h in group.hosts
        }
        while candidate in existing:
            candidate = f"{base}-{counter}"
            counter += 1
        return candidate

    def find_host_by_alias(self, alias: str) -> Optional[HostEntry]:
        for groups in self.win.manager.tree.values():
            for group in groups.values():
                for host in group.hosts:
                    if host.alias == alias:
                        return host
        return None

    def update_action_state(self) -> None:
        hosts = self.selected_hosts()
        kind, data = self.current_item()
        has_hosts = bool(hosts)
        editable = True
        if hosts:
            editable = not self.win.creds.host_is_read_only(hosts[0])
        if not has_hosts:
            if kind == RowKind.CHILD.value:
                parent = data.get("parent", "")
                child = data.get("child", "")
                has_hosts = bool(self.hosts_in_child_group(parent, child))
            elif kind == RowKind.PARENT.value:
                parent = data.get("parent", "")
                has_hosts = bool(self.hosts_in_parent_group(parent))
        for name in ("host-edit", "host-duplicate", "host-move", "host-delete"):
            action = self.win.lookup_action(name)
            if action:
                action.set_enabled(has_hosts and editable)

    def update_details(self) -> None:
        host_page = getattr(self.win, "host_page", None)
        if host_page is not None:
            host_page.refresh()

    def detail_content(self):
        hosts = self.selected_hosts()
        if len(hosts) > 1:
            return details_presenter.multi_detail(hosts, self.win.actions)
        kind_value, data = self.current_item()
        kind = RowKind.coerce(kind_value)
        if kind is RowKind.HOST:
            host = self.selected_host()
            if not host:
                return details_presenter.empty_detail()
            perms = self.win.creds.permissions_for_host(host)
            key = self.make_host_key(data.get("parent", ""), data.get("child", ""), data.get("alias", ""))
            profile = self.win.creds.profile_for_host(host)
            return details_presenter.host_detail(
                host,
                self.win.actions,
                display_user=self.win.creds.display_username_for_host(host),
                status=self.host_status.get(key, "unknown"),
                profile_name=profile.name if profile else "",
                permissions_text=self.win.creds.render_permissions(host),
                read_only=self.win.creds.host_is_read_only(host),
                can_view=perms.get("can_view", True),
                can_connect=perms.get("can_connect", True),
            )
        if kind is not None:
            parent = data.get("parent", "")
            child = data.get("child", "")
            if kind is RowKind.PARENT:
                children = self.win.manager.tree.get(parent, {})
                host_count = sum(len(group.hosts) for group in children.values())
                path = str(self.win.manager.base_dir / parent)
                return details_presenter.group_detail(
                    kind, parent, child, self.win.actions, host_count=host_count, child_count=len(children), path=path,
                    read_only=parent.startswith("[Shared] "),
                )
            host_count = len(self.hosts_in_child_group(parent, child))
            group = self.win.manager.tree.get(parent, {}).get(child)
            path = str(group.path) if group else str(self.win.manager.base_dir / parent / child)
            return details_presenter.group_detail(
                kind, parent, child, self.win.actions, host_count=host_count, path=path,
                read_only=parent.startswith("[Shared] "),
            )
        return details_presenter.empty_detail()


def is_favorite_key(event: Gdk.EventKey) -> bool:
    """Ctrl+D: handled by the tree and the Favorites list only (in a terminal it is end-of-input)."""

    modifiers = event.state & Gtk.accelerator_get_default_mod_mask()
    return modifiers == Gdk.ModifierType.CONTROL_MASK and Gdk.keyval_to_lower(event.keyval) == Gdk.KEY_d


def search_reveal_keys(rows: Iterable[TreeRow], query: str, terms: Optional[FilterTerms],
                       favorite=None) -> Set[Tuple[str, str, str]]:
    """Group keys to open (temporarily) so every match of a search/filter is visible.

    A host matches when the query is in its search text (or, with only filter
    terms, when it passes them); a group matches when the query is in its own
    search text. Their ancestors are opened, not the matching groups themselves.
    """

    needle = (query or "").strip().lower()
    keys: Set[Tuple[str, str, str]] = set()

    def visit(row: TreeRow, ancestors: List[Tuple[str, str, str]]) -> None:
        if row.kind is RowKind.HOST:
            matched = (needle in row.search) if needle else bool(
                terms and row.host and filter_host(row.host, terms, None if row.shared else favorite))
        else:
            matched = bool(needle) and needle in row.search
        if matched:
            keys.update(ancestors)
        if row.children:
            own = (row.kind.value, row.parent, row.child)
            for child in row.children:
                visit(child, ancestors + [own])

    for top in rows:
        visit(top, [])
    return keys
