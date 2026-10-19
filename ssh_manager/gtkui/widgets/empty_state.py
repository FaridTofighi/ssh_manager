"""Empty-state placeholder: icon, title, description and optional actions."""

from __future__ import annotations

from typing import Callable, Optional

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # type: ignore  # noqa: E402

from .icons import make_icon  # noqa: E402
from ..theme.tokens import Size, Space  # noqa: E402


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
        link_label: str = "",
        on_link: Optional[Callable[[], None]] = None,
        compact: bool = False,
    ) -> None:
        Gtk.Box.__init__(self, orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.get_style_context().add_class("empty-state")
        self.set_halign(Gtk.Align.CENTER)
        self.set_valign(Gtk.Align.CENTER)
        self._compact = compact

        self._icon = make_icon(icon_name, Size.EMPTY_CARD_ICON - (4 if compact else 0))
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
        self._buttons = buttons = Gtk.Box(orientation=orientation, spacing=0)
        buttons.set_halign(Gtk.Align.CENTER)
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
        self.link_button: Optional[Gtk.Button] = None
        if link_label:  # a third, flat action under the buttons
            self.link_button = Gtk.Button.new_with_mnemonic(link_label)
            self.link_button.set_relief(Gtk.ReliefStyle.NONE)
            self.link_button.get_style_context().add_class("flat")
            if on_link is not None:
                self.link_button.connect("clicked", lambda _b: on_link())
            buttons.pack_start(self.link_button, False, False, 0)
        if action_label or secondary_label or link_label:
            self.pack_start(buttons, False, False, 0)
        self.apply_density()

    def apply_density(self) -> None:
        """Spacing and icon size from the active density preset."""

        self.set_spacing(Space.S)
        for side in ("start", "end", "top", "bottom"):
            getattr(self, f"set_margin_{side}")(Space.XL)
        icon = Size.EMPTY_CARD_ICON - (4 if self._compact else 0)
        setter = getattr(self._icon, "set_pixel_size", None)
        if callable(setter):
            setter(icon)
        self._buttons.set_spacing(Space.S)
        self._buttons.set_margin_top(Space.S)

    def set_text(self, title: str, description: str = "") -> None:
        self._title.set_text(title)
        self._description.set_text(description)
        self._description.set_no_show_all(not description)
        self._description.set_visible(bool(description))
