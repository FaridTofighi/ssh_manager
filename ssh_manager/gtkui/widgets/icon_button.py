"""Icon-only buttons with a tooltip and an accessible name, and toolbar helpers."""

from __future__ import annotations

from typing import Callable, Iterable, Optional

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # type: ignore  # noqa: E402

from .icons import make_icon  # noqa: E402
from ..theme.tokens import Space  # noqa: E402


def set_accessible_name(widget: Gtk.Widget, name: str, description: str = "") -> None:
    """Give ``widget`` a screen-reader name (and optional description)."""

    accessible = widget.get_accessible()
    if accessible is None:
        return
    accessible.set_name(name)
    if description:
        accessible.set_description(description)


def _decorate(button: Gtk.Button, icon_name: str, tooltip: str, accessible_name: Optional[str], css_classes: Iterable[str]) -> None:
    button.set_image(make_icon(icon_name, 16))
    button.set_always_show_image(True)
    button.set_tooltip_text(tooltip)
    button.set_relief(Gtk.ReliefStyle.NONE)
    button.set_valign(Gtk.Align.CENTER)
    ctx = button.get_style_context()
    ctx.add_class("flat")
    ctx.add_class("toolbar-icon")
    for css in css_classes:
        ctx.add_class(css)
    set_accessible_name(button, accessible_name or tooltip)


class IconButton(Gtk.Button):
    """Flat icon-only button. ``tooltip`` doubles as the accessible name."""

    def __init__(
        self,
        icon_name: str,
        tooltip: str,
        on_clicked: Optional[Callable[[], None]] = None,
        *,
        accessible_name: Optional[str] = None,
        css_classes: Iterable[str] = (),
        action_name: Optional[str] = None,
    ) -> None:
        Gtk.Button.__init__(self)
        _decorate(self, icon_name, tooltip, accessible_name, css_classes)
        if action_name:
            self.set_action_name(action_name)
        if on_clicked is not None:
            self.connect("clicked", lambda _btn: on_clicked())

    def set_icon(self, icon_name: str) -> None:
        self.set_image(make_icon(icon_name, 16))


class IconToggleButton(Gtk.ToggleButton):
    """Flat icon-only toggle button."""

    def __init__(
        self,
        icon_name: str,
        tooltip: str,
        on_toggled: Optional[Callable[[bool], None]] = None,
        *,
        accessible_name: Optional[str] = None,
        css_classes: Iterable[str] = (),
    ) -> None:
        Gtk.ToggleButton.__init__(self)
        _decorate(self, icon_name, tooltip, accessible_name, css_classes)
        if on_toggled is not None:
            self.connect("toggled", lambda btn: on_toggled(btn.get_active()))

    def set_active_silently(self, active: bool, handler: Callable) -> None:
        """Change state without re-entering the ``toggled`` handler."""

        self.handler_block_by_func(handler)
        try:
            self.set_active(active)
        finally:
            self.handler_unblock_by_func(handler)


class IconMenuButton(Gtk.MenuButton):
    """Icon-only ``Gtk.MenuButton`` (for overflow and app menus)."""

    def __init__(
        self,
        icon_name: str,
        tooltip: str,
        *,
        popover: Optional[Gtk.Popover] = None,
        menu: Optional[Gtk.Menu] = None,
        accessible_name: Optional[str] = None,
        css_classes: Iterable[str] = (),
    ) -> None:
        Gtk.MenuButton.__init__(self)
        self.add(make_icon(icon_name, 16))
        self.set_tooltip_text(tooltip)
        self.set_relief(Gtk.ReliefStyle.NONE)
        self.set_valign(Gtk.Align.CENTER)
        ctx = self.get_style_context()
        ctx.add_class("flat")
        ctx.add_class("toolbar-icon")
        for css in css_classes:
            ctx.add_class(css)
        if popover is not None:
            self.set_popover(popover)
        elif menu is not None:
            self.set_popup(menu)
        set_accessible_name(self, accessible_name or tooltip)


class ActionBar(Gtk.Box):
    """Horizontal toolbar: start-aligned groups and end-aligned groups."""

    def __init__(self, *, css_class: str = "") -> None:
        Gtk.Box.__init__(self, orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.XS)
        if css_class:
            self.get_style_context().add_class(css_class)

    def add_start(self, *widgets: Gtk.Widget) -> None:
        for widget in widgets:
            self.pack_start(widget, False, False, 0)

    def add_end(self, *widgets: Gtk.Widget) -> None:
        for widget in widgets:
            self.pack_end(widget, False, False, 0)

    def add_separator(self, *, end: bool = False) -> None:
        sep = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        sep.set_margin_top(Space.XS)
        sep.set_margin_bottom(Space.XS)
        (self.pack_end if end else self.pack_start)(sep, False, False, Space.XS)


def linked_box(*widgets: Gtk.Widget) -> Gtk.Box:
    """Return a ``.linked`` box (GTK built-in style for button groups)."""

    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
    box.get_style_context().add_class("linked")
    for widget in widgets:
        box.pack_start(widget, False, False, 0)
    return box
