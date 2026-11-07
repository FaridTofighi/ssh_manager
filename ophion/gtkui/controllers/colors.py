"""User-chosen tree colors for groups and hosts (stored in UI state).

The colour an item's icon is drawn in (:meth:`ColorController.item_color_for`, the one resolver the tree,
the details header and the group pickers share):

1. the host's own colour (hosts only);
2. the nearest coloured group above it: the item's own group path, then each shorter path, then the
   top-level group (``a/b/c`` → ``a/b`` → ``a`` → the top-level group);
3. the default for its kind (Preferences → Appearance: groups or hosts), drawn at a lightness that keeps
   3:1 against the tree surface and the selected row of the current theme (the stored value never
   changes);
4. ``""``: the theme's own icon colour (``icon_default``).

Per-item colours are drawn exactly as chosen. Only per-item colours are exported with the inventory; the
defaults are a personal display preference.

Per-item colours are keyed by path (groups ``parent`` / ``parent/child``, hosts ``parent::child::alias``),
so they are moved with their item (:func:`remap_color_keys`, after a rename or move) and removed with it
(:func:`forget_color_keys`, after a delete); otherwise a renamed item lost its colour and a later item of
the old name inherited it.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Dict, Optional, Tuple

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gtk  # type: ignore  # noqa: E402


from ..constants import RowKind  # noqa: E402
from ..i18n import _  # noqa: E402

if TYPE_CHECKING:  # pragma: no cover
    from ...gtk_app import SSHManagerWindow


LOGGER = logging.getLogger(__name__)


def _group_under(key: str, parent: str, child: str) -> Optional[str]:
    """The part of group key ``key`` below ``parent``/``child`` (``""`` = the group itself), else None."""

    base = parent if not child else f"{parent}/{child}"
    if key == base:
        return ""
    return key[len(base):] if key.startswith(base + "/") else None


def _host_under(key: str, parent: str, child: str) -> Optional[Tuple[str, str]]:
    """``(child path below child, alias)`` when host key ``key`` is inside ``parent``/``child``, else None."""

    parts = key.split("::", 2)
    if len(parts) != 3 or parts[0] != parent:
        return None
    if not child:
        return parts[1], parts[2]
    if parts[1] == child or parts[1].startswith(child + "/"):
        return parts[1][len(child):], parts[2]
    return None


def remap_color_keys(group_colors: Dict[str, str], host_colors: Dict[str, str], old_parent: str, old_child: str,
                     new_parent: str, new_child: str) -> None:
    """A group (``old_child`` empty: a top-level one) was renamed or moved: it and everything below it keep
    their colours at the new place; keys left at the new place by an earlier rename are dropped."""

    def group_key(rest: str) -> str:
        return (new_parent if not new_child else f"{new_parent}/{new_child}") + rest

    def host_key(rest: str, alias: str) -> str:
        return f"{new_parent}::{(new_child + rest) if new_child else rest}::{alias}"

    moved_groups = {group_key(rest): color for key, color in group_colors.items()
                    if (rest := _group_under(key, old_parent, old_child)) is not None}
    moved_hosts = {host_key(*found): color for key, color in host_colors.items()
                   if (found := _host_under(key, old_parent, old_child)) is not None}
    forget_color_keys(group_colors, host_colors, old_parent, old_child)
    forget_color_keys(group_colors, host_colors, new_parent, new_child)
    group_colors.update(moved_groups)
    host_colors.update(moved_hosts)


def forget_color_keys(group_colors: Dict[str, str], host_colors: Dict[str, str], parent: str, child: str) -> None:
    """A group was deleted: its colour and those of its subgroups and hosts go too."""

    for key in [key for key in group_colors if _group_under(key, parent, child) is not None]:
        del group_colors[key]
    for key in [key for key in host_colors if _host_under(key, parent, child) is not None]:
        del host_colors[key]


class ColorController:
    """User-chosen tree colors for groups and hosts (stored in UI state)."""

    def __init__(self, win: "SSHManagerWindow") -> None:
        self.win = win
        self.scope = ""
        self.group_colors: Dict[str, str] = {}
        self.host_colors: Dict[str, str] = {}
        ui = win._ui
        if getattr(ui, "unscoped_colors", None) is None:  # a file from before per-inventory colours: one set
            ui.unscoped_colors = {"groups": dict(ui.group_colors), "hosts": dict(ui.host_colors)}
        self.activate()
        self.default_group_color: str = getattr(win._ui, "default_group_color", "") or ""
        self.default_host_color: str = getattr(win._ui, "default_host_color", "") or ""

    # ------------------------------------------------------- per inventory
    def activate(self) -> None:
        """Use the active inventory's colours (at start, and after a workspace switch before the rebuild)."""

        from ..favorites import scope_for

        ui = self.win._ui
        manager = getattr(self.win, "manager", None)
        if manager is None or getattr(ui, "inventory_colors", None) is None:  # no inventory to scope by
            self.group_colors, self.host_colors = dict(ui.group_colors), dict(ui.host_colors)
            return
        self.scope = scope_for(manager.base_dir)
        maps = ui.inventory_colors.get(self.scope)
        if maps is None:
            maps = self._first_colors()
            if manager.tree or not getattr(manager, "read_only", False):  # not a store that failed to open
                ui.inventory_colors[self.scope] = maps
        self.group_colors, self.host_colors = maps["groups"], maps["hosts"]  # edited in place, saved with the state

    def _first_colors(self) -> Dict[str, Dict[str, str]]:
        """An inventory seen for the first time: the old shared colours of its own items (what it showed)."""

        from .tree_controller import SHARED_PREFIX

        unscoped = self.win._ui.unscoped_colors or {}
        groups, hosts = unscoped.get("groups") or {}, unscoped.get("hosts") or {}
        tree = getattr(self.win.manager, "tree", None)
        if not isinstance(tree, dict):  # no parsed inventory to match against: all of them, as before
            return {"groups": dict(groups), "hosts": dict(hosts)}
        if not groups and not hosts:
            return {"groups": {}, "hosts": {}}
        group_keys, host_keys = set(), set()
        for parent, children in tree.items():
            group_keys.add(parent)
            for child, group in children.items():
                parts = [part for part in child.split("/") if part]
                group_keys.update(f"{parent}/{'/'.join(parts[:depth])}" for depth in range(1, len(parts) + 1))
                host_keys.update(f"{parent}::{child}::{host.alias}" for host in group.hosts)

        def own(key: str, known: set) -> bool:
            return key in known or key.startswith(SHARED_PREFIX)  # a shared inventory shows next to every one

        return {"groups": {k: v for k, v in groups.items() if own(k, group_keys)},
                "hosts": {k: v for k, v in hosts.items() if own(k, host_keys)}}

    def group_color_for(self, parent: str, child: str) -> str:
        key = parent if not child else f"{parent}/{child}"
        return self.group_colors.get(key, "")

    def resolved_group_color_for(self, parent: str, child: str) -> str:
        """The nearest group colour: ``child``'s path, each shorter path, then the top-level group."""

        parts = [part for part in (child or "").split("/") if part]
        for depth in range(len(parts), 0, -1):
            color = self.group_color_for(parent, "/".join(parts[:depth]))
            if color:
                return color
        return self.group_color_for(parent, "")

    def default_color(self, host: bool) -> str:
        """The default for hosts or groups as drawn on the current theme; ``""`` = the theme's own."""

        stored = self.default_host_color if host else self.default_group_color
        if not stored:
            return ""
        return self.drawn_default(stored)[0]

    @staticmethod
    def drawn_default(stored: str) -> Tuple[str, bool]:
        """``(colour drawn, adjusted)`` for a default: ≥ 3:1 on the tree surface and the selected row."""

        from ..theme.accent import readable_color
        from ..theme.loader import current_palette

        palette = current_palette()
        return readable_color(stored, (palette.bg_surface, palette.bg_selected))

    def set_default_colors(self, *, group: Optional[str] = None, host: Optional[str] = None) -> None:
        """New defaults (``""`` = automatic); saved, and the open tree recoloured in place."""

        from ..theme.accent import normalize_color

        if group is not None:
            self.default_group_color = normalize_color(group) or ""
            self.win._ui.default_group_color = self.default_group_color
        if host is not None:
            self.default_host_color = normalize_color(host) or ""
            self.win._ui.default_host_color = self.default_host_color
        self.win._save_ui_state()
        self.win.tree.recolor()

    def set_group_color(self, parent: str, child: str, color: Optional[str]) -> None:
        key = parent if not child else f"{parent}/{child}"
        if color:
            self.group_colors[key] = color
        else:
            self.group_colors.pop(key, None)
        self.win._save_ui_state()

    def host_color_for(self, parent: str, child: str, alias: str) -> str:
        return self.host_colors.get(self.win.tree.make_host_key(parent, child, alias), "")

    def set_host_color(self, parent: str, child: str, alias: str, color: Optional[str]) -> None:
        key = self.win.tree.make_host_key(parent, child, alias)
        if color:
            self.host_colors[key] = color
        else:
            self.host_colors.pop(key, None)
        self.win._save_ui_state()

    def move_host(self, old: Tuple[str, str, str], new: Tuple[str, str, str]) -> None:
        """A host got a new ``(parent, child, alias)``: its own colour (or none) goes with it."""

        old_key, new_key = self.win.tree.make_host_key(*old), self.win.tree.make_host_key(*new)
        if old_key == new_key or (old_key not in self.host_colors and new_key not in self.host_colors):
            return
        color = self.host_colors.pop(old_key, None)
        self.host_colors.pop(new_key, None)  # left there by an earlier rename
        if color:
            self.host_colors[new_key] = color
        self.win._save_ui_state()

    def forget_host(self, parent: str, child: str, alias: str) -> None:
        if self.host_colors.pop(self.win.tree.make_host_key(parent, child, alias), None) is not None:
            self.win._save_ui_state()

    def move_group(self, old_parent: str, old_child: str, new_parent: str, new_child: str) -> None:
        """A group was renamed or moved (``old_child`` empty: a top-level one); see :func:`remap_color_keys`."""

        before = (dict(self.group_colors), dict(self.host_colors))
        remap_color_keys(self.group_colors, self.host_colors, old_parent, old_child, new_parent, new_child)
        if (self.group_colors, self.host_colors) != before:
            self.win._save_ui_state()

    def forget_group(self, parent: str, child: str = "") -> None:
        before = (dict(self.group_colors), dict(self.host_colors))
        forget_color_keys(self.group_colors, self.host_colors, parent, child)
        if (self.group_colors, self.host_colors) != before:
            self.win._save_ui_state()

    def item_color_for(self, parent: str, child: str, alias: str = "") -> str:
        """The colour to draw a group (``alias == ""``) or host icon in (see the module docstring)."""

        if alias:
            return (self.host_color_for(parent, child, alias) or self.resolved_group_color_for(parent, child)
                    or self.default_color(host=True))
        return self.resolved_group_color_for(parent, child) or self.default_color(host=False)

    def rgba_to_hex(self, rgba: Gdk.RGBA) -> str:
        r = int(rgba.red * 255 + 0.5)
        g = int(rgba.green * 255 + 0.5)
        b = int(rgba.blue * 255 + 0.5)
        return f"#{r:02x}{g:02x}{b:02x}"

    def change_item_color(self) -> None:
        kind, data = self.win.tree.current_item()
        if kind not in {RowKind.PARENT.value, RowKind.CHILD.value, RowKind.CHILD_FOLDER.value, RowKind.HOST.value}:
            return
        parent = data.get("parent") or ""
        child = data.get("child") or ""
        alias = data.get("alias") or ""
        title = _("Choose Host Color") if kind == RowKind.HOST.value else _("Choose Group Color")
        dialog = Gtk.ColorChooserDialog(title=title, transient_for=self.win)
        # The colour drawn now (own, inherited or default), for hosts and groups alike.
        current = self.item_color_for(parent, "" if kind == RowKind.PARENT.value else child,
                                      alias if kind == RowKind.HOST.value else "")
        if current:
            rgba = Gdk.RGBA()
            if rgba.parse(current):
                dialog.set_rgba(rgba)
        response = dialog.run()
        if response == Gtk.ResponseType.OK:
            rgba = dialog.get_rgba()
            color = self.rgba_to_hex(rgba)
            if kind == RowKind.HOST.value:
                self.set_host_color(parent, child, alias, color)
            else:
                self.set_group_color(parent, child, color)
            self.win.tree.rebuild(refresh=False)
        dialog.destroy()

    def change_host_color(self) -> None:
        """Pick a color for the selected host (not its group)."""

        self.change_item_color()

    def clear_item_color(self) -> None:
        kind, data = self.win.tree.current_item()
        if kind not in {RowKind.PARENT.value, RowKind.CHILD.value, RowKind.CHILD_FOLDER.value, RowKind.HOST.value}:
            return
        parent = data.get("parent") or ""
        child = data.get("child") or ""
        alias = data.get("alias") or ""
        if kind == RowKind.HOST.value:
            self.set_host_color(parent, child, alias, None)
        else:
            self.set_group_color(parent, child, None)
        self.win.tree.rebuild(refresh=False)
