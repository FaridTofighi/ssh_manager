"""Render :mod:`menu_spec` descriptions as ``Gtk.Menu`` widgets."""

from __future__ import annotations

from typing import Optional, Sequence

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gtk  # type: ignore  # noqa: E402

from .icons import make_icon  # noqa: E402
from ..menu_spec import MenuItemSpec, MenuSection, menu_layout  # noqa: E402
from ..theme.tokens import Size, Space  # noqa: E402


def _menu_item(spec: MenuItemSpec) -> Gtk.MenuItem:
    item = Gtk.MenuItem()
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.S)
    icon = make_icon(spec.icon, Size.ICON, css_class="menu-icon") if spec.icon else Gtk.Box()
    icon.set_size_request(Size.ICON, Size.ICON)
    box.pack_start(icon, False, False, 0)
    label = Gtk.Label.new_with_mnemonic(spec.label)
    label.set_xalign(0.0)
    label.set_use_underline(True)
    box.pack_start(label, True, True, 0)
    if spec.accel:
        accel = Gtk.Label(label=spec.accel)
        accel.get_style_context().add_class("accel-hint")
        accel.set_margin_start(Space.L)
        box.pack_end(accel, False, False, 0)
    item.add(box)
    item.set_sensitive(spec.sensitive)
    if spec.destructive:
        item.get_style_context().add_class("destructive")
    if spec.tooltip:
        item.set_tooltip_text(spec.tooltip)
    if spec.submenu:
        item.set_submenu(build_menu(spec.submenu))
    elif spec.callback is not None:
        callback = spec.callback
        item.connect("activate", lambda _item: callback())
    return item


def build_menu(spec: Sequence[MenuSection]) -> Gtk.Menu:
    """Build a ``Gtk.Menu`` from ``spec`` (sections separated by separators)."""

    menu = Gtk.Menu()
    for kind, item_spec in menu_layout(spec):
        if kind == "separator" or item_spec is None:
            menu.append(Gtk.SeparatorMenuItem())
        else:
            menu.append(_menu_item(item_spec))
    menu.show_all()
    return menu


def build_context_menu(
    spec: Sequence[MenuSection],
    *,
    attach_to: Optional[Gtk.Widget] = None,
) -> Gtk.Menu:
    """Build a context menu, optionally attached to ``attach_to``."""

    menu = build_menu(spec)
    if attach_to is not None:
        menu.attach_to_widget(attach_to, None)
    return menu


def popup_menu(menu: Gtk.Menu, event: Optional[Gdk.Event] = None, *, widget: Optional[Gtk.Widget] = None) -> None:
    """Show ``menu`` at the pointer (mouse) or at ``widget`` (keyboard)."""

    if event is not None and hasattr(menu, "popup_at_pointer") and event.type in (
        Gdk.EventType.BUTTON_PRESS,
        Gdk.EventType.BUTTON_RELEASE,
    ):
        menu.popup_at_pointer(event)
        return
    if widget is not None and hasattr(menu, "popup_at_widget"):
        menu.popup_at_widget(widget, Gdk.Gravity.CENTER, Gdk.Gravity.NORTH_WEST, event)
        return
    menu.popup_at_pointer(event)
