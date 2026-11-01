"""Workspace widgets: session toolbar, broadcast strip, tab labels and tiles.

These are pure views. ``TerminalNotebook`` (``gtk_terminal.py``) composes them
and binds the toolbar to whichever session is active.
"""

from __future__ import annotations

from typing import Callable, Optional

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk, Pango  # type: ignore  # noqa: E402

from ..constants import Icons  # noqa: E402
from ..i18n import _, ngettext  # noqa: E402
from ..menu_spec import MenuItemSpec, MenuSection  # noqa: E402
from ..theme.loader import get_theme_manager  # noqa: E402
from ..theme.tokens import ConnectionStatus, Motion, Size, Space  # noqa: E402
from ..view_models import status_text  # noqa: E402
from ..controllers.shortcuts import set_accel_tooltip  # noqa: E402
from ..widgets.icon_button import (  # noqa: E402
    ActionBar,
    IconButton,
    IconMenuButton,
    IconToggleButton,
    set_accessible_name,
)
from ..widgets.icons import make_icon  # noqa: E402
from ..widgets.menus import build_menu  # noqa: E402
from ..widgets.pane_frame import BroadcastChip, PaneFrame  # noqa: E402
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
            css_classes=("broadcast-toggle",),
        )
        self.add_start(self.tile, self.broadcast)
        self.add_separator()
        self.stats = IconToggleButton(Icons.STATS, _("Show remote CPU, memory and disk"))
        self.add_start(self.stats)
        self.add_separator()
        # Terminal macros: Record ↔ Stop (●); the label shows the time and steps while recording.
        self.record = IconButton(Icons.RECORD, _("Record macro"))
        self.record_status = Gtk.Label()
        self.record_status.get_style_context().add_class("macro-recording-status")
        self.record_status.set_no_show_all(True)
        self.add_start(self.record, self.record_status)

        self.more = IconMenuButton(Icons.MORE, _("More session actions"))
        self.reconnect = IconButton(Icons.RECONNECT, _("Reconnect (Ctrl+Shift+R)"))
        self.add_end(self.more, self.reconnect)

    def set_overflow(self, sections) -> None:
        self.more.set_popup(build_menu(sections))

    def set_recording(self, recording: bool, status: str = "") -> None:
        """The Record button becomes Stop (●) with ``status`` (elapsed time, steps) next to it."""

        self.record.set_icon(Icons.STOP if recording else Icons.RECORD)
        tooltip = _("Stop recording the macro") if recording else _("Record macro")
        self.record.set_tooltip_text(tooltip)
        set_accessible_name(self.record, tooltip)
        context = self.record.get_style_context()
        (context.add_class if recording else context.remove_class)("macro-recording")
        self.record_status.set_text(status)
        self.record_status.set_visible(recording)


class BroadcastStrip(Gtk.Revealer):
    """Persistent, high-contrast warning shown while broadcast input is on."""

    def __init__(self, on_stop: Callable[[], None]) -> None:
        Gtk.Revealer.__init__(self)
        self.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self.set_transition_duration(get_theme_manager().motion_ms(Motion.FAST))
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.S)
        box.get_style_context().add_class("broadcast-strip")
        icon = make_icon(Icons.BROADCAST, Size.ICON)  # the mode's own icon, not a warning
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


TAB_DRAG_TARGET = "application/x-sshm-session-tab"


class SessionTab(Gtk.EventBox):
    """One session in the strip: status dot, label, activity marker, close button.

    Focusable (Enter/Space activates, Delete/Ctrl+W closes, the context-menu key
    opens the menu), middle-click closes, right-click opens the tab menu and it
    can be dragged onto another tab to reorder. The tooltip is built on demand
    (``tooltip_text()``) so it always shows the current status and size.
    """

    def __init__(
        self,
        title: str,
        *,
        on_activate: Callable[[], None] = lambda: None,
        on_close: Callable[[], None],
        on_context_menu: Callable[[Optional[Gdk.EventButton]], None],
        tooltip_text: Callable[[], str] = lambda: "",
    ) -> None:
        Gtk.EventBox.__init__(self)
        self.set_visible_window(False)
        self.set_can_focus(True)
        self.get_style_context().add_class("session-tab")
        self._on_activate = on_activate
        self._on_close = on_close
        self._on_context_menu = on_context_menu
        self._tooltip_text = tooltip_text
        self._current = False
        self._hover = False
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.S)
        box.get_style_context().add_class("session-tab-box")
        self._box = box
        self.dot = StatusDot(size=Size.DOT_TAB)
        self.dot.set_has_tooltip(False)  # the tab tooltip spells the status out
        box.pack_start(self.dot, False, False, 0)
        self.label = Gtk.Label(label=title)
        self.label.set_ellipsize(Pango.EllipsizeMode.END)
        self.label.set_xalign(0.0)
        box.pack_start(self.label, True, True, 0)
        self.activity = Gtk.Label(label="•")
        self.activity.get_style_context().add_class("tab-activity")
        self.activity.set_tooltip_text(_("New output"))
        self.activity.set_no_show_all(True)
        box.pack_start(self.activity, False, False, 0)
        self.recording = Gtk.Label(label="●")
        self.recording.get_style_context().add_class("macro-recording-dot")
        self.recording.set_tooltip_text(_("Recording a macro"))
        self.recording.set_no_show_all(True)
        box.pack_start(self.recording, False, False, 0)
        self.close_button = IconButton(Icons.CLOSE, _("Close session (Ctrl+W)"), on_close)
        self.close_button.set_can_focus(False)  # the tab itself takes focus; Delete closes
        box.pack_start(self.close_button, False, False, 0)
        self.add(box)
        self._fit_label_width()
        self.set_has_tooltip(True)
        self.connect("query-tooltip", self._on_query_tooltip)
        self.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.ENTER_NOTIFY_MASK
            | Gdk.EventMask.LEAVE_NOTIFY_MASK | Gdk.EventMask.KEY_PRESS_MASK
        )
        self.connect("button-press-event", self._on_button_press)
        self.connect("key-press-event", self._on_key_press)
        self.connect("enter-notify-event", lambda *_: self._set_hover(True))
        self.connect("leave-notify-event", lambda *_: self._set_hover(False))
        self.connect("popup-menu", lambda *_: self._on_context_menu(None) or True)
        self._update_close_visibility()
        self.show_all()

    def _fit_label_width(self) -> None:
        """Ellipsize between ``Size.TAB_LABEL_MIN`` and ``Size.TAB_LABEL_MAX`` px."""

        metrics = self.label.get_pango_context().get_metrics(None, None)
        char = max(1.0, metrics.get_approximate_char_width() / Pango.SCALE)
        self.label.set_max_width_chars(max(4, int(Size.TAB_LABEL_MAX / char)))
        self.label.set_size_request(Size.TAB_LABEL_MIN, -1)

    # ------------------------------------------------------------- api
    @property
    def title(self) -> str:
        return self.label.get_text()

    def set_title(self, title: str, status: object) -> None:
        self.label.set_text(title)
        self.dot.set_status(status)
        state = ConnectionStatus.coerce(status)
        set_accessible_name(self, f"{title}, {status_text(state)}")

    def set_activity(self, active: bool) -> None:
        self.activity.set_visible(active)

    def set_recording(self, recording: bool) -> None:
        self.recording.set_visible(recording)

    def set_current(self, current: bool) -> None:
        self._current = current
        if current:
            self.set_activity(False)
        # The inner box carries the styles; it has no input window of its own,
        # so state is expressed with classes (current / hover).
        ctx = self._box.get_style_context()
        (ctx.add_class if current else ctx.remove_class)("current")
        self._update_close_visibility()

    @property
    def current(self) -> bool:
        return self._current

    def tooltip(self) -> str:
        return self._tooltip_text() or self.title

    # -------------------------------------------------------- internal
    def _on_query_tooltip(self, _widget, _x, _y, _keyboard, tooltip) -> bool:
        tooltip.set_text(self.tooltip())
        return True

    def _set_hover(self, hover: bool) -> bool:
        self._hover = hover
        ctx = self._box.get_style_context()
        (ctx.add_class if hover else ctx.remove_class)("hover")
        self._update_close_visibility()
        return False

    def _update_close_visibility(self) -> None:
        # Keep the button in the layout (no width jumps); show it on hover and on the active tab.
        self.close_button.set_opacity(1.0 if (self._hover or self._current) else 0.0)

    def _on_button_press(self, _widget, event: Gdk.EventButton) -> bool:
        if event.type != Gdk.EventType.BUTTON_PRESS:
            return False
        if event.button == 1:
            self._on_activate()
            return False  # let a drag start
        if event.button == 2:
            self._on_close()
            return True
        if event.button == 3:
            self._on_context_menu(event)
            return True
        return False

    def _on_key_press(self, _widget, event: Gdk.EventKey) -> bool:
        if event.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_space):
            self._on_activate()
            return True
        if event.keyval == Gdk.KEY_Delete:
            self._on_close()
            return True
        if event.keyval == Gdk.KEY_Menu:
            self._on_context_menu(None)
            return True
        return False


TabLabel = SessionTab  # former name


class SessionStrip(Gtk.Box):
    """The session row: tabs (scrollable), ``+`` right after the last tab, actions at the end.

    ``set_tabs()`` takes the tab widgets in order; dragging one tab onto another
    calls ``on_reorder(widget, index)``. Overflow chevrons appear when the tabs
    do not fit; the actions box (all-sessions menu and the session toolbar) is
    filled by the owner.
    """

    def __init__(self, on_new: Callable[[], None], on_quick_connect: Optional[Callable[[], None]] = None) -> None:
        Gtk.Box.__init__(self, orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self.get_style_context().add_class("session-strip")
        self.on_reorder: Callable[[SessionTab, int], None] = lambda _tab, _index: None
        self._dragged: Optional[SessionTab] = None

        self.scroll_left = IconButton(Icons.CHEVRON_LEFT, _("Scroll tabs left"), lambda: self._scroll(-1))
        self.scroll_left.set_no_show_all(True)
        self.pack_start(self.scroll_left, False, False, 0)
        self._scroller = Gtk.ScrolledWindow()
        self._scroller.set_policy(Gtk.PolicyType.EXTERNAL, Gtk.PolicyType.NEVER)
        self._scroller.get_style_context().add_class("session-strip-scroller")
        self._row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self.tabs_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self.tabs_box.get_style_context().add_class("session-tabs-row")
        set_accessible_name(self.tabs_box, _("Sessions"))
        self._row.pack_start(self.tabs_box, False, False, 0)
        self.new_button = IconButton(Icons.NEW, _("New local terminal"), on_new)
        set_accel_tooltip(self.new_button, _("New local terminal"), "new-local-tab")
        self._row.pack_start(self.new_button, False, False, 0)
        # The other ways to open a tab: a chevron next to "+" and a right-click on "+".
        self.new_menu = Gtk.Menu()
        for label, callback in ((_("New _Local Terminal"), on_new), (_("_Quick Connect…"), on_quick_connect)):
            if callback is not None:
                item = Gtk.MenuItem.new_with_mnemonic(label)
                item.connect("activate", lambda _i, cb=callback: cb())
                self.new_menu.append(item)
        self.new_menu.show_all()
        self.new_menu_button = Gtk.MenuButton()
        self.new_menu_button.set_relief(Gtk.ReliefStyle.NONE)
        self.new_menu_button.set_popup(self.new_menu)
        self.new_menu_button.get_style_context().add_class("session-new-menu")
        set_accessible_name(self.new_menu_button, _("More ways to open a session"))
        self.new_menu_button.set_tooltip_text(_("New local terminal or Quick Connect"))
        self._row.pack_start(self.new_menu_button, False, False, 0)
        self.new_button.connect("button-press-event", self._on_new_button_press)
        self._scroller.add(self._row)
        self.pack_start(self._scroller, True, True, 0)
        self.scroll_right = IconButton(Icons.CHEVRON_RIGHT, _("Scroll tabs right"), lambda: self._scroll(1))
        self.scroll_right.set_no_show_all(True)
        self.pack_start(self.scroll_right, False, False, 0)
        self.actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self.actions.get_style_context().add_class("session-strip-actions")
        self.pack_end(self.actions, False, False, 0)
        # Keep our own reference: the scrolled window drops it while it is destroyed.
        self._hadj = self._scroller.get_hadjustment()
        # The adjustment changes while the strip is being laid out: showing the chevrons right then made GTK
        # finish that layout with stale sizes ("Negative content width" for the actions box at the end, from
        # the tab that first overflows). They follow once the layout is done instead.
        self._chevrons_idle = 0
        for signal in ("changed", "value-changed"):
            self._hadj.connect(signal, lambda *_: self._queue_chevrons())
        self.connect("destroy", lambda *_: self._cancel_chevrons())

    def _on_new_button_press(self, _button, event) -> bool:
        if event.type == Gdk.EventType.BUTTON_PRESS and event.button == 3:
            self.new_menu.popup_at_pointer(event)
            return True
        return False

    # ------------------------------------------------------------- api
    def set_tabs(self, tabs: list) -> None:
        """Show ``tabs`` (SessionTab widgets) in this order."""

        for child in list(self.tabs_box.get_children()):
            if child not in tabs:
                self.tabs_box.remove(child)
        for index, tab in enumerate(tabs):
            if tab.get_parent() is not self.tabs_box:
                if tab.get_parent() is not None:
                    tab.get_parent().remove(tab)
                self.tabs_box.pack_start(tab, False, False, 0)
                self._enable_drag(tab)
            self.tabs_box.reorder_child(tab, index)
        self._sync_chevrons()

    def tabs(self) -> list:
        return list(self.tabs_box.get_children())

    def scroll_to(self, tab: SessionTab) -> None:
        """Bring ``tab`` into view (after it got allocated)."""

        GLib.idle_add(self._scroll_to_idle, tab)

    # -------------------------------------------------------- internal
    def _scroll_to_idle(self, tab: SessionTab) -> bool:
        if tab.get_parent() is self.tabs_box:
            alloc = tab.get_allocation()
            adjustment = self._hadj
            left, right = adjustment.get_value(), adjustment.get_value() + adjustment.get_page_size()
            if alloc.x < left:
                adjustment.set_value(alloc.x)
            elif alloc.x + alloc.width > right:
                adjustment.set_value(alloc.x + alloc.width - adjustment.get_page_size())
        return GLib.SOURCE_REMOVE

    def _scroll(self, direction: int) -> None:
        adjustment = self._hadj
        step = max(1.0, adjustment.get_page_size() * 0.8)
        adjustment.set_value(adjustment.get_value() + direction * step)

    def _queue_chevrons(self) -> None:
        if not self._chevrons_idle:
            self._chevrons_idle = GLib.idle_add(self._chevrons_after_layout)

    def _chevrons_after_layout(self) -> bool:
        self._chevrons_idle = 0
        self._sync_chevrons()
        return False

    def _cancel_chevrons(self) -> None:
        if self._chevrons_idle:
            GLib.source_remove(self._chevrons_idle)
            self._chevrons_idle = 0

    def _sync_chevrons(self) -> None:
        adjustment = self._hadj
        overflow = adjustment.get_upper() - adjustment.get_page_size() > 1
        value = adjustment.get_value()
        self.scroll_left.set_visible(overflow)
        self.scroll_right.set_visible(overflow)
        self.scroll_left.set_sensitive(overflow and value > 0)
        self.scroll_right.set_sensitive(overflow and value < adjustment.get_upper() - adjustment.get_page_size() - 1)

    def _enable_drag(self, tab: SessionTab) -> None:
        if getattr(tab, "_drag_ready", False):
            return
        tab._drag_ready = True  # type: ignore[attr-defined]
        target = Gtk.TargetEntry.new(TAB_DRAG_TARGET, Gtk.TargetFlags.SAME_APP, 0)
        tab.drag_source_set(Gdk.ModifierType.BUTTON1_MASK, [target], Gdk.DragAction.MOVE)
        tab.drag_dest_set(Gtk.DestDefaults.ALL, [target], Gdk.DragAction.MOVE)
        tab.connect("drag-begin", lambda widget, _ctx: setattr(self, "_dragged", widget))
        tab.connect("drag-data-get", lambda _w, _ctx, data, _info, _time: data.set_text("tab", -1))
        tab.connect("drag-data-received", self._on_drop)
        tab.connect("drag-end", lambda *_: setattr(self, "_dragged", None))

    def _on_drop(self, target: SessionTab, _ctx, _x, _y, _data, _info, _time) -> None:
        dragged = self._dragged
        if dragged is None or dragged is target:
            return
        index = self.tabs().index(target)
        self.on_reorder(dragged, index)


class TileFrame(PaneFrame):
    """One session in tile mode: a PaneFrame with the Broadcast chip, zoom and close."""

    def __init__(
        self,
        title: str,
        status: object,
        *,
        included: bool,
        on_include_toggled: Callable[[bool], None],
        on_close: Callable[[], None],
        on_focus: Callable[[], None],
        on_zoom: Callable[[], None] = lambda: None,
        address: str = "",
    ) -> None:
        self.include = BroadcastChip(included, on_include_toggled)
        PaneFrame.__init__(self, title, on_focus=on_focus, on_zoom=on_zoom, on_close=on_close,
                           broadcast_chip=self.include)
        self.style().add_class("tile-frame")
        self.dot = self.header.dot
        self.label = self.header.title
        self.set_title(title, address)
        self.set_status(status)

    def set_title(self, title: str, status: object = None, address: Optional[str] = None) -> None:  # type: ignore[override]
        PaneFrame.set_title(self, title, self.header.address.get_text() if address is None else address)
        if status is not None:
            self.set_status(status)


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
    terminal: bool = True,
):
    """Context menu for a session tab (split and transcript only for terminals)."""

    return [
        MenuSection("session", [
            MenuItemSpec(_("_Reconnect"), reconnect, Icons.RECONNECT, "Shift+Ctrl+R"),
            MenuItemSpec(_("_Duplicate Session"), duplicate, Icons.DUPLICATE, "Shift+Ctrl+D"),
            MenuItemSpec(_("Close Split Pane") if is_split else _("_Split Pane"), split, Icons.SPLIT, "Shift+Ctrl+E",
                         visible=terminal),
        ]),
        MenuSection("edit", [
            MenuItemSpec(_("Re_name…"), rename, Icons.EDIT, "F2"),
            MenuItemSpec(_("_Save Transcript…"), save, Icons.SAVE, "Shift+Ctrl+S", visible=terminal),
        ]),
        MenuSection("close", [
            MenuItemSpec(_("_Close"), close, Icons.CLOSE, "Ctrl+W"),
            MenuItemSpec(_("Close _Others"), close_others, sensitive=has_others),
            MenuItemSpec(_("Close Tabs to the _Right"), close_right, sensitive=has_right),
        ]),
    ]
