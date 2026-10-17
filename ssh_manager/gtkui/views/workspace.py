"""Workspace widgets: session toolbar, broadcast strip, tab labels and tiles.

These are pure views. ``TerminalNotebook`` (``gtk_terminal.py``) composes them
and binds the toolbar to whichever session is active.
"""

from __future__ import annotations

from typing import Callable, Optional

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gtk, Pango  # type: ignore  # noqa: E402

from ..constants import Icons  # noqa: E402
from ..i18n import _, ngettext  # noqa: E402
from ..menu_spec import MenuItemSpec, MenuSection  # noqa: E402
from ..theme.loader import get_theme_manager  # noqa: E402
from ..theme.tokens import ConnectionStatus, Motion, Space  # noqa: E402
from ..widgets.icon_button import (  # noqa: E402
    ActionBar,
    IconButton,
    IconMenuButton,
    IconToggleButton,
    set_accessible_name,
)
from ..widgets.menus import build_menu  # noqa: E402
from ..widgets.status_dot import StatusDot  # noqa: E402


class SessionToolbar(ActionBar):
    """One toolbar for the whole workspace; actions apply to the active session."""

    def __init__(self) -> None:
        ActionBar.__init__(self, css_class="session-toolbar")
        self.split = IconToggleButton(Icons.SPLIT, _("Split pane (Ctrl+Shift+E)"))
        self.save = IconButton(Icons.SAVE, _("Save transcript… (Ctrl+Shift+S)"))
        self.plain_copy = IconToggleButton(Icons.PLAIN_TEXT, _("Copy as plain text (strip ANSI colors)"))
        self.add_start(self.split, self.save, self.plain_copy)
        self.add_separator()
        self.tile = IconToggleButton(Icons.TILE, _("Show all sessions (Ctrl+Shift+A)"))
        self.broadcast = IconToggleButton(
            Icons.BROADCAST,
            _("Broadcast input to all included sessions (Ctrl+Shift+B)"),
            css_classes=("danger-toggle",),
        )
        self.add_start(self.tile, self.broadcast)
        self.add_separator()
        self.stats = IconToggleButton(Icons.STATS, _("Show remote CPU, memory and disk"))
        self.add_start(self.stats)

        self.more = IconMenuButton(Icons.MORE, _("More session actions"))
        self.reconnect = IconButton(Icons.RECONNECT, _("Reconnect (Ctrl+Shift+R)"))
        self.add_end(self.more, self.reconnect)

    def set_overflow(self, sections) -> None:
        self.more.set_popup(build_menu(sections))


class BroadcastStrip(Gtk.Revealer):
    """Persistent, high-contrast warning shown while broadcast input is on."""

    def __init__(self, on_stop: Callable[[], None]) -> None:
        Gtk.Revealer.__init__(self)
        self.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self.set_transition_duration(get_theme_manager().motion_ms(Motion.FAST))
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.S)
        box.get_style_context().add_class("broadcast-strip")
        icon = Gtk.Image.new_from_icon_name("dialog-warning-symbolic", Gtk.IconSize.BUTTON)
        box.pack_start(icon, False, False, 0)
        self._label = Gtk.Label()
        self._label.set_xalign(0.0)
        self._label.set_line_wrap(True)
        box.pack_start(self._label, True, True, 0)
        stop = Gtk.Button.new_with_mnemonic(_("_Stop Broadcasting"))
        stop.get_style_context().add_class("destructive-action")
        stop.set_tooltip_text(_("Turn broadcast off (Ctrl+Shift+B, or Esc outside a terminal)"))
        stop.connect("clicked", lambda _b: on_stop())
        box.pack_end(stop, False, False, 0)
        self.add(box)

    def set_target_count(self, count: int) -> None:
        text = ngettext(
            "Broadcast is ON — typing is mirrored to {n} session.",
            "Broadcast is ON — typing is mirrored to {n} sessions.",
            count,
        ).format(n=count)
        self._label.set_text(text)
        accessible = self.get_accessible()
        if accessible is not None:
            accessible.set_name(text)


class TabLabel(Gtk.EventBox):
    """Notebook tab label: status dot, title, activity marker, close button."""

    def __init__(
        self,
        title: str,
        *,
        on_close: Callable[[], None],
        on_context_menu: Callable[[Gdk.EventButton], None],
    ) -> None:
        Gtk.EventBox.__init__(self)
        self.set_visible_window(False)
        self._current = False
        self._hover = False
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.XS)
        self.dot = StatusDot(size=8)
        box.pack_start(self.dot, False, False, 0)
        self.label = Gtk.Label(label=title)
        self.label.set_ellipsize(Pango.EllipsizeMode.END)
        self.label.set_width_chars(min(max(len(title), 6), 22))
        self.label.set_max_width_chars(22)
        box.pack_start(self.label, True, True, 0)
        self.activity = Gtk.Label(label="•")
        self.activity.get_style_context().add_class("tab-activity")
        self.activity.set_tooltip_text(_("New output"))
        self.activity.set_no_show_all(True)
        box.pack_start(self.activity, False, False, 0)
        self.close_button = IconButton(Icons.CLOSE, _("Close session (Ctrl+W)"), on_close)
        box.pack_start(self.close_button, False, False, 0)
        self.add(box)
        self.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.ENTER_NOTIFY_MASK | Gdk.EventMask.LEAVE_NOTIFY_MASK)
        self.connect("button-press-event", self._on_button_press, on_close, on_context_menu)
        self.connect("enter-notify-event", lambda *_: self._set_hover(True))
        self.connect("leave-notify-event", lambda *_: self._set_hover(False))
        self._update_close_visibility()
        self.show_all()

    def set_title(self, title: str, status: object) -> None:
        self.label.set_text(title)
        self.label.set_width_chars(min(max(len(title), 6), 22))
        self.dot.set_status(status)
        state = ConnectionStatus.coerce(status)
        self.set_tooltip_text(f"{title} — {self.dot.get_tooltip_text()}")
        set_accessible_name(self, f"{title}, {state.value}")

    def set_activity(self, active: bool) -> None:
        self.activity.set_visible(active)

    def set_current(self, current: bool) -> None:
        self._current = current
        if current:
            self.set_activity(False)
        self._update_close_visibility()

    def _set_hover(self, hover: bool) -> bool:
        self._hover = hover
        self._update_close_visibility()
        return False

    def _update_close_visibility(self) -> None:
        # Keep the button in the layout (no width jumps); dim it when idle.
        self.close_button.set_opacity(1.0 if (self._hover or self._current) else 0.35)

    @staticmethod
    def _on_button_press(_widget, event: Gdk.EventButton, on_close, on_context_menu) -> bool:
        if event.type == Gdk.EventType.BUTTON_PRESS and event.button == 2:
            on_close()
            return True
        if event.type == Gdk.EventType.BUTTON_PRESS and event.button == 3:
            on_context_menu(event)
            return True
        return False


class TileFrame(Gtk.Box):
    """Wrapper for one session in tile mode, with a header matching tab labels."""

    def __init__(
        self,
        title: str,
        status: object,
        *,
        included: bool,
        on_include_toggled: Callable[[bool], None],
        on_close: Callable[[], None],
        on_focus: Callable[[], None],
    ) -> None:
        Gtk.Box.__init__(self, orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.get_style_context().add_class("tile-frame")
        self.set_hexpand(True)
        self.set_vexpand(True)
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.S)
        header.get_style_context().add_class("tile-header")
        self.dot = StatusDot(status, size=8)
        header.pack_start(self.dot, False, False, 0)
        self.label = Gtk.Label(label=title)
        self.label.set_xalign(0.0)
        self.label.set_ellipsize(Pango.EllipsizeMode.END)
        header.pack_start(self.label, True, True, 0)
        self.include = Gtk.CheckButton.new_with_mnemonic(_("_Broadcast"))
        self.include.set_tooltip_text(_("Include this session when broadcasting input"))
        self.include.set_active(included)
        self.include.connect("toggled", lambda btn: on_include_toggled(btn.get_active()))
        header.pack_start(self.include, False, False, 0)
        header.pack_start(IconButton(Icons.CLOSE, _("Close session"), on_close), False, False, 0)
        events = Gtk.EventBox()
        events.add(header)
        events.connect("button-press-event", lambda *_: on_focus() or False)
        self.pack_start(events, False, False, 0)
        self.body: Optional[Gtk.Widget] = None

    def set_body(self, widget: Gtk.Widget) -> None:
        self.body = widget
        self.pack_start(widget, True, True, 0)

    def set_title(self, title: str, status: object) -> None:
        self.label.set_text(title)
        self.dot.set_status(status)


def tab_menu_spec(
    *,
    reconnect: Callable[[], None],
    duplicate: Callable[[], None],
    split: Callable[[], None],
    is_split: bool,
    rename: Callable[[], None],
    save: Callable[[], None],
    close: Callable[[], None],
    close_others: Callable[[], None],
    close_right: Callable[[], None],
    has_others: bool,
    has_right: bool,
):
    """Context menu for a session tab."""

    return [
        MenuSection("session", [
            MenuItemSpec(_("_Reconnect"), reconnect, Icons.RECONNECT, "Shift+Ctrl+R"),
            MenuItemSpec(_("_Duplicate Session"), duplicate, Icons.DUPLICATE, "Shift+Ctrl+D"),
            MenuItemSpec(_("Close Split Pane") if is_split else _("_Split Pane"), split, Icons.SPLIT, "Shift+Ctrl+E"),
        ]),
        MenuSection("edit", [
            MenuItemSpec(_("Re_name…"), rename, Icons.EDIT, "F2"),
            MenuItemSpec(_("_Save Transcript…"), save, Icons.SAVE, "Shift+Ctrl+S"),
        ]),
        MenuSection("close", [
            MenuItemSpec(_("_Close"), close, Icons.CLOSE, "Ctrl+W"),
            MenuItemSpec(_("Close _Others"), close_others, sensitive=has_others),
            MenuItemSpec(_("Close Tabs to the _Right"), close_right, sensitive=has_right),
        ]),
    ]
