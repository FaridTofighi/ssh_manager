"""The RDP session toolbar: a thin bar above the remote screen (auto-hides, can be pinned, keyboard-usable).

It cannot float *over* the screen: FreeRDP's window is a native X window and
always stacks above anything GTK draws. So it is a collapsible bar; when
hidden a small handle stays visible, and the bar returns on that handle, on
the pointer entering it, or with F10 while the remote screen does not have
the keyboard. Engine limits come in through :meth:`set_capabilities` (the
button stays visible, disabled, with the reason as tooltip).
"""

from __future__ import annotations

import functools
from typing import Any, Callable, Dict, Optional

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # type: ignore  # noqa: E402

from ..constants import Icons  # noqa: E402
from ..i18n import _  # noqa: E402
from ..menu_spec import MenuItemSpec, MenuSection  # noqa: E402
from ..theme.tokens import Size, Space  # noqa: E402
from ..widgets.icon_button import IconButton, IconToggleButton, set_accessible_name  # noqa: E402
from ..widgets.icons import make_icon  # noqa: E402
from ..widgets.menus import build_menu, popup_menu  # noqa: E402

AUTO_HIDE_MS = 3000
COMMON_SIZES = ((1280, 720), (1366, 768), (1600, 900), (1920, 1080), (2560, 1440))


class RdpToolbar(Gtk.Box):
    """Callbacks are plain attributes (``on_fullscreen`` …), set by the tab."""

    def __init__(self) -> None:
        Gtk.Box.__init__(self, orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.get_style_context().add_class("rdp-toolbar")
        self.on_send_keys: Optional[Callable[..., Any]] = None
        self.on_fullscreen: Optional[Callable[..., Any]] = None
        self.on_scale: Optional[Callable[..., Any]] = None
        self.on_resolution: Optional[Callable[[Optional[tuple]], None]] = None
        self.on_keyboard: Optional[Callable[..., Any]] = None
        self.on_clipboard: Optional[Callable[..., Any]] = None
        self.on_screenshot: Optional[Callable[..., Any]] = None
        self.on_reconnect: Optional[Callable[..., Any]] = None
        self.on_disconnect: Optional[Callable[..., Any]] = None
        self.on_open_drives: Optional[Callable[..., Any]] = None
        self.info_lines: Callable[[], list] = lambda: []
        self.drive_names: Callable[[], list] = lambda: []
        self._hide_source = 0
        self._pinned = False
        self._pointer_inside = False
        self._suppress = False

        self.revealer = Gtk.Revealer()
        self.revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.XS)
        bar.get_style_context().add_class("toolbar")
        for setter in (bar.set_margin_start, bar.set_margin_end):
            setter(Space.S)
        set_accessible_name(bar, _("Remote desktop toolbar"))
        self.buttons: Dict[str, Gtk.Widget] = {}

        def button(key: str, icon: str, tip: str, callback: Callable[[], None]) -> IconButton:
            widget = IconButton(icon, tip, callback)
            self.buttons[key] = widget
            return widget

        self.status = Gtk.Label()
        self.status.set_xalign(0.0)
        self.status.get_style_context().add_class("dim-label")
        bar.pack_start(self.status, True, True, 0)
        items = [
            button("send_keys", Icons.INTERRUPT, _("Send Ctrl+Alt+Del"), lambda: self._call(self.on_send_keys)),
            button("scale", "zoom-fit-best-symbolic", _("Scale mode"), lambda: self._menu("scale")),
            button("resolution", Icons.CPU, _("Resolution"), lambda: self._menu("resolution")),
            button("keyboard", Icons.KEY, _("Keyboard"), lambda: self._call(self.on_keyboard)),
            button("clipboard", Icons.PLAIN_TEXT, _("Clipboard sharing"), lambda: self._menu("clipboard")),
            button("screenshot", "camera-photo-symbolic", _("Save a screenshot…"),
                   lambda: self._call(self.on_screenshot)),
            button("drives", Icons.OPEN_FOLDER, _("Shared folders"), lambda: self._menu("drives")),
            button("info", Icons.INFO, _("Connection info"), self._show_info),
            button("reconnect", Icons.RECONNECT, _("Reconnect"), lambda: self._call(self.on_reconnect)),
            button("disconnect", Icons.CLOSE, _("Disconnect"), lambda: self._call(self.on_disconnect)),
        ]
        self.fullscreen = IconToggleButton(Icons.MAXIMIZE, _("Full screen (Ctrl+Alt+Enter)"), self._on_fullscreen)
        self.buttons["fullscreen"] = self.fullscreen
        items.insert(1, self.fullscreen)
        self.pin = IconToggleButton("view-pin-symbolic", _("Keep the toolbar open"), self._on_pin)
        items.append(self.pin)
        for item in items:
            bar.pack_start(item, False, False, 0)
        hover = Gtk.EventBox()  # a Box has no window: enter / leave need one
        hover.add(bar)
        hover.connect("enter-notify-event", self._on_enter)
        hover.connect("leave-notify-event", self._on_leave)
        self.revealer.add(hover)
        self.pack_start(self.revealer, False, False, 0)
        self._bar = bar

        self.handle = Gtk.Button()
        self.handle.set_relief(Gtk.ReliefStyle.NONE)
        self.handle.set_halign(Gtk.Align.CENTER)
        self.handle.get_style_context().add_class("rdp-toolbar-handle")
        handle_icon = make_icon(Icons.CHEVRON_DOWN, Size.ICON_SMALL)
        handle_icon.show()  # the handle is no-show-all: its icon must be shown explicitly
        self.handle.add(handle_icon)
        self.handle.set_tooltip_text(_("Show the toolbar (F10)"))
        set_accessible_name(self.handle, _("Show the remote desktop toolbar"))
        self.handle.connect("clicked", lambda *_: self.reveal())
        self.handle.set_no_show_all(True)
        self.pack_start(self.handle, False, False, 0)

        self.connect("destroy", lambda *_: self._cancel_hide())
        self.revealer.set_reveal_child(True)

    # ---------------------------------------------------------- showing
    def reveal(self) -> None:
        self.revealer.set_reveal_child(True)
        self.handle.hide()
        self._schedule_hide()
        first = self.buttons.get("fullscreen")
        if first is not None:
            first.grab_focus()

    def conceal(self) -> None:
        if self._pinned:
            return
        self.revealer.set_reveal_child(False)
        self.handle.show()

    @property
    def visible_bar(self) -> bool:
        return self.revealer.get_reveal_child()

    def _schedule_hide(self) -> bool:
        self._cancel_hide()
        if not self._pinned:
            self._hide_source = GLib.timeout_add(AUTO_HIDE_MS, self._hide_due)
        return False

    def _cancel_hide(self) -> bool:
        if self._hide_source:
            GLib.source_remove(self._hide_source)
            self._hide_source = 0
        return False

    def _hide_due(self) -> bool:
        self._hide_source = 0
        if not self._pointer_inside and self._bar.get_focus_child() is None:
            self.conceal()
        return GLib.SOURCE_REMOVE

    def _on_enter(self, *_args) -> bool:
        self._pointer_inside = True
        self._cancel_hide()
        return False

    def _on_leave(self, _widget, event) -> bool:
        if event.detail != 2:  # 2 = GDK_NOTIFY_INFERIOR: the pointer only moved onto a button
            self._pointer_inside = False
            self._schedule_hide()
        return False

    def _on_pin(self, active: bool) -> None:
        self._pinned = active
        if active:
            self._cancel_hide()
            self.reveal()
        else:
            self._schedule_hide()

    def _on_fullscreen(self, active: bool) -> None:
        self.fullscreen.set_tooltip_text(_("Leave full screen (Ctrl+Alt+Enter)") if active
                                         else _("Full screen (Ctrl+Alt+Enter)"))
        if not self._suppress:
            self._call(self.on_fullscreen, active)

    def set_fullscreen(self, active: bool) -> None:
        """Show the state without calling ``on_fullscreen`` (the window changed it)."""

        self._suppress = True
        try:
            self.fullscreen.set_active(active)
        finally:
            self._suppress = False

    # ---------------------------------------------------------- state
    def set_capabilities(self, reasons: Dict[str, str]) -> None:
        """``{button key: reason}``; a key with a reason is disabled and explains why in its tooltip."""

        for key, widget in self.buttons.items():
            reason = reasons.get(key, "")
            widget.set_sensitive(not reason)
            if reason:
                widget.set_tooltip_text(reason)

    def set_status(self, text: str) -> None:
        self.status.set_text(text)

    def set_keyboard_state(self, grabbed: bool, hint: str) -> None:
        widget = self.buttons["keyboard"]
        widget.set_tooltip_text((_("Keyboard: sent to the remote computer") if grabbed
                                 else _("Keyboard: not grabbed")) + "\n" + hint)
        context = widget.get_style_context()
        (context.add_class if grabbed else context.remove_class)("active")

    # ----------------------------------------------------------- menus
    def _menu(self, which: str) -> None:
        if which == "scale":
            spec = [MenuSection("scale", [
                MenuItemSpec(_("Fit to the window"), lambda: self._call(self.on_scale, "fit")),
                MenuItemSpec(_("Actual size (1:1)"), lambda: self._call(self.on_scale, "actual")),
                MenuItemSpec(_("Smart sizing (scale the picture)"), lambda: self._call(self.on_scale, "smart")),
            ])]
        elif which == "resolution":
            sizes = [MenuItemSpec(f"{w} × {h}", functools.partial(self._call, self.on_resolution, (w, h)))
                     for w, h in COMMON_SIZES]
            spec = [MenuSection("fit", [MenuItemSpec(_("Fit to the window"),
                                                     lambda: self._call(self.on_resolution, None))]),
                    MenuSection("sizes", sizes)]
        elif which == "drives":
            spec = [MenuSection("drives", [MenuItemSpec(_("Open “{name}”").format(name=name),
                                                        functools.partial(self._call, self.on_open_drives, name))
                                           for name in self.drive_names()])]
        else:
            spec = [MenuSection("clipboard", [
                MenuItemSpec(_("Both directions"), lambda: self._call(self.on_clipboard, "both")),
                MenuItemSpec(_("Only to the remote computer"), lambda: self._call(self.on_clipboard, "to_remote")),
                MenuItemSpec(_("Only from the remote computer"), lambda: self._call(self.on_clipboard, "from_remote")),
                MenuItemSpec(_("Off"), lambda: self._call(self.on_clipboard, "off")),
            ])]
        menu = build_menu(spec)
        anchor = self.buttons[which]
        menu.attach_to_widget(anchor, None)
        popup_menu(menu, None, widget=anchor)

    def _show_info(self) -> None:
        popover = Gtk.Popover.new(self.buttons["info"])
        grid = Gtk.Grid(column_spacing=Space.M, row_spacing=Space.XS)
        for margin in (grid.set_margin_top, grid.set_margin_bottom, grid.set_margin_start, grid.set_margin_end):
            margin(Space.M)
        for row, (label, value) in enumerate(self.info_lines()):
            key = Gtk.Label(label=label)
            key.set_xalign(1.0)
            key.get_style_context().add_class("dim-label")
            text = Gtk.Label(label=value or "—")  # plain text: some values come from the server
            text.set_xalign(0.0)
            text.set_selectable(True)
            text.set_max_width_chars(48)
            text.set_line_wrap(True)
            grid.attach(key, 0, row, 1, 1)
            grid.attach(text, 1, row, 1, 1)
        popover.add(grid)
        grid.show_all()
        popover.popup()
        self.info_popover = popover

    @staticmethod
    def _call(callback, *args) -> None:
        if callback is not None:
            callback(*args)


__all__ = ["RdpToolbar", "AUTO_HIDE_MS"]
