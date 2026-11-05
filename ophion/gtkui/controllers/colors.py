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


class ColorController:
    """User-chosen tree colors for groups and hosts (stored in UI state)."""

    def __init__(self, win: "SSHManagerWindow") -> None:
        self.win = win
        self.group_colors: Dict[str, str] = dict(win._ui.group_colors)
        self.host_colors: Dict[str, str] = dict(win._ui.host_colors)
        self.default_group_color: str = getattr(win._ui, "default_group_color", "") or ""
        self.default_host_color: str = getattr(win._ui, "default_host_color", "") or ""

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
