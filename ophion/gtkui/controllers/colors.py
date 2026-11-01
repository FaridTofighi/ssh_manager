"""User-chosen tree colors for groups and hosts (stored in UI state)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Dict, Optional

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

    def group_color_for(self, parent: str, child: str) -> str:
        key = parent if not child else f"{parent}/{child}"
        return self.group_colors.get(key, "")

    def resolved_group_color_for(self, parent: str, child: str) -> str:
        if child:
            color = self.group_color_for(parent, child)
            if color:
                return color
        return self.group_color_for(parent, "")

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
        if alias:
            return self.host_color_for(parent, child, alias) or self.resolved_group_color_for(parent, child)
        return self.resolved_group_color_for(parent, child)

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
        current = self.item_color_for(parent, child, alias) if kind == RowKind.HOST.value else self.group_color_for(parent, child)
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
