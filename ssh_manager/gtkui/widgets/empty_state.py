"""Empty-state placeholder: icon, title, description and optional actions."""

from __future__ import annotations

from typing import Callable, Optional

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # type: ignore  # noqa: E402

from ..theme.tokens import Space  # noqa: E402


class EmptyState(Gtk.Box):
    """Centered placeholder used by the tree, the workspace and list dialogs."""

    def __init__(
        self,
        icon_name: str,
        title: str,
        description: str = "",
        *,
        action_label: str = "",
        on_action: Optional[Callable[[], None]] = None,
        secondary_label: str = "",
        on_secondary: Optional[Callable[[], None]] = None,
        compact: bool = False,
    ) -> None:
        Gtk.Box.__init__(self, orientation=Gtk.Orientation.VERTICAL, spacing=Space.S)
        self.get_style_context().add_class("empty-state")
        self.set_halign(Gtk.Align.CENTER)
        self.set_valign(Gtk.Align.CENTER)
        self.set_margin_start(Space.XL)
        self.set_margin_end(Space.XL)
        self.set_margin_top(Space.XL)
        self.set_margin_bottom(Space.XL)

        size = Gtk.IconSize.DND if compact else Gtk.IconSize.DIALOG
        self._icon = Gtk.Image.new_from_icon_name(icon_name, size)
        self._icon.get_style_context().add_class("empty-icon")
        self.pack_start(self._icon, False, False, 0)

        self._title = Gtk.Label(label=title)
        self._title.get_style_context().add_class("empty-title")
        self._title.set_line_wrap(True)
        self._title.set_justify(Gtk.Justification.CENTER)
        self.pack_start(self._title, False, False, 0)

        self._description = Gtk.Label(label=description)
        self._description.get_style_context().add_class("empty-description")
        self._description.set_line_wrap(True)
        self._description.set_justify(Gtk.Justification.CENTER)
        self._description.set_max_width_chars(48)
        self._description.set_no_show_all(not description)
        self.pack_start(self._description, False, False, 0)

        # Compact states live in narrow panes: stack their buttons vertically.
        orientation = Gtk.Orientation.VERTICAL if compact else Gtk.Orientation.HORIZONTAL
        buttons = Gtk.Box(orientation=orientation, spacing=Space.S)
        buttons.set_halign(Gtk.Align.CENTER)
        buttons.set_margin_top(Space.S)
        self.primary_button: Optional[Gtk.Button] = None
        self.secondary_button: Optional[Gtk.Button] = None
        if action_label:
            self.primary_button = Gtk.Button.new_with_mnemonic(action_label)
            self.primary_button.get_style_context().add_class("suggested-action")
            if on_action is not None:
                self.primary_button.connect("clicked", lambda _b: on_action())
            buttons.pack_start(self.primary_button, False, False, 0)
        if secondary_label:
            self.secondary_button = Gtk.Button.new_with_mnemonic(secondary_label)
            if on_secondary is not None:
                self.secondary_button.connect("clicked", lambda _b: on_secondary())
            buttons.pack_start(self.secondary_button, False, False, 0)
        if action_label or secondary_label:
            self.pack_start(buttons, False, False, 0)

    def set_text(self, title: str, description: str = "") -> None:
        self._title.set_text(title)
        self._description.set_text(description)
        self._description.set_no_show_all(not description)
        self._description.set_visible(bool(description))
