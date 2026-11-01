"""Pane frame: one terminal pane shown next to others (split pane, tile mode).

A :class:`PaneFrame` encloses a terminal in a full border with a header and an
optional footer, so panes shown together can be told apart at a glance::

    ╭───────────────────────────────────────────────────────────╮
    │ ● web-01 · pane 1   deploy@web-01:22   BROADCAST [✓ Broadcast] ⤢ × │  header
    │ terminal                                                  │
    │ ● Connected · 59×35                                       │  footer
    ╰───────────────────────────────────────────────────────────╯

States (CSS classes on the frame's styled box, ``frame.style()``; all token colors):

* ``focused``   — 2 px accent outline (1 px border + 1 px inset ring, so the
  content does not move), accent-tinted header;
* ``broadcast`` — 2 px violet outline and a ``BROADCAST`` badge (with an
  accent inner ring when it is also focused);
* ``failed`` / ``disconnected`` — 1 px danger / warning border; the footer and
  the header tooltip spell the status out.

Unfocused panes get a light veil (an overlay child that lets clicks through;
never widget opacity on the terminal).
"""

from __future__ import annotations

from typing import Callable, Optional

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gtk, Pango  # type: ignore  # noqa: E402

from ..constants import Icons  # noqa: E402
from ..i18n import _  # noqa: E402
from ..theme.tokens import ConnectionStatus, Size, Space  # noqa: E402
from ..view_models import status_text  # noqa: E402
from .icon_button import IconButton, set_accessible_name  # noqa: E402
from .icons import make_icon  # noqa: E402
from .status_dot import StatusDot  # noqa: E402


class BroadcastChip(Gtk.ToggleButton):
    """Compact ``✓ Broadcast`` toggle (replaces the stock check box)."""

    def __init__(self, active: bool, on_toggled: Callable[[bool], None]) -> None:
        Gtk.ToggleButton.__init__(self)
        self.get_style_context().add_class("broadcast-chip")
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.XS)
        self._check = make_icon(Icons.CHECK, Size.ICON_SMALL)
        box.pack_start(self._check, False, False, 0)
        box.pack_start(Gtk.Label.new_with_mnemonic(_("_Broadcast")), False, False, 0)
        self.add(box)
        self.set_tooltip_text(_("Include this session when broadcasting input"))
        set_accessible_name(self, _("Include in broadcast"))
        self.set_active(active)
        self._sync()
        self.connect("toggled", lambda button: (self._sync(), on_toggled(button.get_active())))

    def _sync(self) -> None:
        self._check.set_opacity(1.0 if self.get_active() else 0.0)  # keeps the width; icon only


class PaneHeader(Gtk.EventBox):
    """Status dot, label, muted address; broadcast badge/chip, zoom and close on the right."""

    def __init__(
        self,
        title: str,
        *,
        on_focus: Callable[[], None],
        on_zoom: Callable[[], None],
        on_close: Optional[Callable[[], None]],
        broadcast_chip: Optional[BroadcastChip] = None,
    ) -> None:
        Gtk.EventBox.__init__(self)
        self.set_visible_window(False)
        self._on_focus = on_focus
        self._on_zoom = on_zoom
        self.box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.S)
        self.box.get_style_context().add_class("pane-header")
        self.dot = StatusDot(size=Size.DOT_TAB)
        self.box.pack_start(self.dot, False, False, 0)
        self.title = Gtk.Label(label=title)
        self.title.get_style_context().add_class("pane-title")
        self.title.set_ellipsize(Pango.EllipsizeMode.END)
        self.title.set_xalign(0.0)
        self.box.pack_start(self.title, False, False, 0)
        self.address = Gtk.Label()
        self.address.get_style_context().add_class("pane-address")
        self.address.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.address.set_xalign(0.0)
        self.box.pack_start(self.address, True, True, 0)
        self.badge = Gtk.Label(label=_("BROADCAST"))
        self.badge.get_style_context().add_class("pane-badge")
        self.badge.set_no_show_all(True)
        self.box.pack_start(self.badge, False, False, 0)
        self.broadcast_chip = broadcast_chip
        if broadcast_chip is not None:
            self.box.pack_start(broadcast_chip, False, False, 0)
        self.zoom_button = IconButton(Icons.MAXIMIZE, _("Maximize pane (double-click the header)"), on_zoom)
        self.box.pack_start(self.zoom_button, False, False, 0)
        self.close_button: Optional[IconButton] = None
        if on_close is not None:
            self.close_button = IconButton(Icons.CLOSE, _("Close pane"), on_close)
            self.box.pack_start(self.close_button, False, False, 0)
        self.add(self.box)
        self.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.ENTER_NOTIFY_MASK | Gdk.EventMask.LEAVE_NOTIFY_MASK)
        self.connect("button-press-event", self._on_button_press)
        self.connect("enter-notify-event", lambda *_: self._set_hover(True))
        self.connect("leave-notify-event", lambda *_: self._set_hover(False))

    def set_zoomed(self, zoomed: bool) -> None:
        self.zoom_button.set_icon(Icons.RESTORE if zoomed else Icons.MAXIMIZE)
        tip = _("Restore panes") if zoomed else _("Maximize pane (double-click the header)")
        self.zoom_button.set_tooltip_text(tip)
        set_accessible_name(self.zoom_button, tip)

    def _set_hover(self, hover: bool) -> bool:
        ctx = self.box.get_style_context()
        (ctx.add_class if hover else ctx.remove_class)("hover")
        return False

    def _on_button_press(self, _widget, event: Gdk.EventButton) -> bool:
        # A single press already activated the pane (PaneFrame's capture gesture).
        if event.button == 1 and event.type == Gdk.EventType._2BUTTON_PRESS:
            self._on_zoom()
            return True
        return False


class PaneFooter(Gtk.Box):
    """``● Connected · 59×35`` — the pane's own status line, inside its border."""

    def __init__(self) -> None:
        Gtk.Box.__init__(self, orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.S)
        self.get_style_context().add_class("pane-footer")
        self.dot = StatusDot(size=Size.DOT_TAB)
        self.pack_start(self.dot, False, False, 0)
        self.label = Gtk.Label()
        self.label.set_xalign(0.0)
        self.label.set_ellipsize(Pango.EllipsizeMode.END)
        self.pack_start(self.label, True, True, 0)

    def set_status(self, status: object, columns: int = 0, rows: int = 0) -> None:
        self.dot.set_status(status)
        text = status_text(ConnectionStatus.coerce(status))
        self.label.set_text(f"{text} · {columns}×{rows}" if columns and rows else text)


class PaneFrame(Gtk.EventBox):
    """Bordered pane: header, body (terminal) and optional footer.

    The frame is an input-only event box around the styled box (``.box``, CSS
    class ``pane-frame`` and the state classes), so a press anywhere in the
    pane — terminal, scrollbar, header, footer, padding, border — passes
    through it. A capture-phase gesture calls ``on_focus`` (activate the pane)
    on every button press and never claims the sequence: the same press still
    reaches the terminal (selection, links, mouse reporting, context menu) or
    the header button. Scrolling does not activate.
    """

    def __init__(
        self,
        title: str,
        *,
        on_focus: Callable[[], None] = lambda: None,
        on_zoom: Callable[[], None] = lambda: None,
        on_close: Optional[Callable[[], None]] = None,
        broadcast_chip: Optional[BroadcastChip] = None,
    ) -> None:
        Gtk.EventBox.__init__(self)
        self.set_visible_window(False)
        self.set_above_child(False)
        self.set_hexpand(True)
        self.set_vexpand(True)
        self._on_activate = on_focus
        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.box.get_style_context().add_class("pane-frame")
        self.add(self.box)
        self.header = PaneHeader(title, on_focus=on_focus, on_zoom=on_zoom, on_close=on_close,
                                 broadcast_chip=broadcast_chip)
        self.box.pack_start(self.header, False, False, 0)
        self.overlay = Gtk.Overlay()
        self.overlay.get_style_context().add_class("pane-body")
        self.dim = Gtk.Box()
        self.dim.get_style_context().add_class("pane-dim")
        self.dim.set_no_show_all(True)
        self.overlay.add_overlay(self.dim)
        self.overlay.set_overlay_pass_through(self.dim, True)
        self.box.pack_start(self.overlay, True, True, 0)
        self.body: Optional[Gtk.Widget] = None
        self.footer: Optional[Gtk.Widget] = None
        self._focused = False
        self.set_size_request(Size.PANE_MIN, Size.PANE_MIN)
        self.press_gesture = Gtk.GestureMultiPress.new(self)
        self.press_gesture.set_button(0)  # left, middle and right
        self.press_gesture.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        self.press_gesture.connect("pressed", self._on_pressed)

    # ----------------------------------------------------------- input
    def _on_pressed(self, gesture: Gtk.GestureMultiPress, _n_press: int, _x: float, _y: float) -> None:
        sequence = gesture.get_current_sequence()
        event = gesture.get_last_event(sequence)
        # Never claim: the press continues to the terminal / header button.
        gesture.set_state(Gtk.EventSequenceState.NONE)
        if not self._inside_nested_frame(event):
            self._on_activate()  # idempotent: double/triple clicks change nothing

    def _inside_nested_frame(self, event: Optional[Gdk.Event]) -> bool:
        """A press inside a split pane of a tile: that pane's own frame activates it."""

        widget = Gtk.get_event_widget(event) if event is not None else None
        while widget is not None and widget is not self:
            if isinstance(widget, PaneFrame):
                return True
            widget = widget.get_parent()
        return False

    # ------------------------------------------------------------- api
    def style(self) -> Gtk.StyleContext:
        """The style context that carries ``pane-frame`` and the state classes."""

        return self.box.get_style_context()

    def set_body(self, widget: Gtk.Widget) -> None:
        if self.body is not None and self.body.get_parent() is self.overlay:
            self.overlay.remove(self.body)
        self.body = widget
        parent = widget.get_parent()
        if parent is not None and parent is not self.overlay:
            parent.remove(widget)
        if widget.get_parent() is None:
            self.overlay.add(widget)

    def release_body(self) -> Optional[Gtk.Widget]:
        widget = self.body
        if widget is not None and widget.get_parent() is self.overlay:
            self.overlay.remove(widget)
        self.body = None
        return widget

    def set_footer(self, widget: Gtk.Widget) -> None:
        if self.footer is not None and self.footer.get_parent() is self.box:
            self.box.remove(self.footer)
        self.footer = widget
        if widget.get_parent() is not None:
            widget.get_parent().remove(widget)
        self.box.pack_end(widget, False, False, 0)

    def set_title(self, title: str, address: str = "") -> None:
        self.header.title.set_text(title)
        self.header.address.set_text(address)
        self.header.address.set_visible(bool(address))
        set_accessible_name(self, title)

    def set_status(self, status: object) -> None:
        state = ConnectionStatus.coerce(status)
        self.header.dot.set_status(state)
        self.header.set_tooltip_text(status_text(state))
        ctx = self.style()
        for css, on in (("failed", state is ConnectionStatus.FAILED), ("disconnected", state is ConnectionStatus.UNKNOWN)):
            (ctx.add_class if on else ctx.remove_class)(css)

    def set_focused(self, focused: bool, *, dim_others: bool = True) -> None:
        self._focused = focused
        for ctx in (self.style(), self.header.box.get_style_context()):
            (ctx.add_class if focused else ctx.remove_class)("focused")
        self.dim.set_visible(dim_others and not focused)

    @property
    def focused(self) -> bool:
        return self._focused

    def set_broadcast(self, active: bool) -> None:
        ctx = self.style()
        (ctx.add_class if active else ctx.remove_class)("broadcast")
        self.header.badge.set_visible(active)

    def set_zoomed(self, zoomed: bool) -> None:
        self.header.set_zoomed(zoomed)
