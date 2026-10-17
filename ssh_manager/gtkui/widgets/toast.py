"""Toast overlay and message-history popover (views for ``ToastController``)."""

from __future__ import annotations

import time
from typing import Dict

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk, Pango  # type: ignore  # noqa: E402

from .icons import make_icon  # noqa: E402
from ..controllers.notifications import Message, MessageHistory, ToastController  # noqa: E402
from ..i18n import _  # noqa: E402
from ..theme.loader import get_theme_manager  # noqa: E402
from ..theme.tokens import Level, Motion, Space  # noqa: E402
from .icon_button import IconButton, set_accessible_name  # noqa: E402

LEVEL_ICONS: Dict[Level, str] = {
    Level.INFO: "sshm-info",
    Level.SUCCESS: "sshm-check",
    Level.WARNING: "sshm-warning",
    Level.ERROR: "sshm-error",
}

LEVEL_NAMES: Dict[Level, str] = {
    Level.INFO: _("Information"),
    Level.SUCCESS: _("Success"),
    Level.WARNING: _("Warning"),
    Level.ERROR: _("Error"),
}


def level_icon(level: Level, size: int = 16) -> Gtk.Widget:
    image = make_icon(LEVEL_ICONS[level], size)
    image.get_style_context().add_class(f"level-{level.value}")
    set_accessible_name(image, LEVEL_NAMES[level])
    return image


class Toast(Gtk.Revealer):
    """A single toast: level icon, text, optional action and dismiss button."""

    def __init__(self, message: Message, controller: ToastController) -> None:
        Gtk.Revealer.__init__(self)
        self.message = message
        self.set_transition_type(Gtk.RevealerTransitionType.SLIDE_UP)
        self.set_transition_duration(get_theme_manager().motion_ms(Motion.NORMAL))

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.S)
        ctx = box.get_style_context()
        ctx.add_class("toast")
        ctx.add_class(f"toast-{message.level.value}")
        box.pack_start(level_icon(message.level), False, False, 0)

        label = Gtk.Label(label=message.text)
        label.set_xalign(0.0)
        label.set_line_wrap(True)
        label.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        label.set_max_width_chars(60)
        label.set_selectable(True)
        label.set_can_focus(False)
        box.pack_start(label, True, True, 0)

        if message.action is not None and message.action_label:
            action = Gtk.Button.new_with_mnemonic(message.action_label)
            action.get_style_context().add_class("flat")
            action.connect("clicked", lambda _b: controller.activate(message.id))
            box.pack_start(action, False, False, 0)

        close = IconButton("window-close-symbolic", _("Dismiss"), lambda: controller.dismiss(message.id))
        box.pack_start(close, False, False, 0)
        self.add(box)
        # Screen readers announce toasts like status messages.
        accessible = self.get_accessible()
        if accessible is not None:
            accessible.set_name(f"{LEVEL_NAMES[message.level]}: {message.text}")


class ToastOverlay(Gtk.Overlay):
    """Overlay that stacks toasts at the bottom-end of its main child."""

    def __init__(self, controller: ToastController) -> None:
        Gtk.Overlay.__init__(self)
        self.controller = controller
        self._toasts: Dict[int, Toast] = {}
        self._stack = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._stack.set_halign(Gtk.Align.END)
        self._stack.set_valign(Gtk.Align.END)
        self._stack.set_margin_end(Space.S)
        self._stack.set_margin_bottom(Space.S)
        self.add_overlay(self._stack)
        self.set_overlay_pass_through(self._stack, False)
        controller.on_show(self._on_show)
        controller.on_dismiss(self._on_dismiss)
        self.connect("destroy", lambda *_: controller.shutdown())

    def _on_show(self, message: Message) -> None:
        toast = Toast(message, self.controller)
        self._toasts[message.id] = toast
        self._stack.pack_end(toast, False, False, 0)
        toast.show_all()
        toast.set_reveal_child(True)

    def _on_dismiss(self, message: Message) -> None:
        toast = self._toasts.pop(message.id, None)
        if toast is None:
            return
        toast.set_reveal_child(False)
        delay = toast.get_transition_duration()
        if delay:
            GLib.timeout_add(delay, self._remove_toast, toast)
        else:
            self._remove_toast(toast)

    def _remove_toast(self, toast: Toast) -> bool:
        if toast.get_parent() is self._stack:
            self._stack.remove(toast)
        toast.destroy()
        return GLib.SOURCE_REMOVE


def _format_time(timestamp: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(timestamp))


class MessageHistoryButton(Gtk.MenuButton):
    """Header-bar button with an unread badge and a history popover."""

    ATTENTION_LEVELS = {Level.WARNING, Level.ERROR}

    def __init__(self, history: MessageHistory) -> None:
        Gtk.MenuButton.__init__(self)
        self._history = history
        self.set_relief(Gtk.ReliefStyle.NONE)
        self.get_style_context().add_class("flat")
        self.set_tooltip_text(_("Message history"))
        set_accessible_name(self, _("Message history"))

        overlay = Gtk.Overlay()
        overlay.add(make_icon("sshm-activity", 18))
        self._badge = Gtk.Label()
        self._badge.get_style_context().add_class("badge")
        self._badge.set_halign(Gtk.Align.END)
        self._badge.set_valign(Gtk.Align.START)
        self._badge.set_no_show_all(True)
        overlay.add_overlay(self._badge)
        self.add(overlay)

        self._popover = Gtk.Popover()
        self._popover.set_size_request(380, -1)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=Space.S)
        body.set_border_width(Space.M)
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.S)
        title = Gtk.Label(label=_("Messages"))
        title.get_style_context().add_class("section-title")
        title.set_xalign(0.0)
        header.pack_start(title, True, True, 0)
        clear = Gtk.Button.new_with_mnemonic(_("_Clear"))
        clear.get_style_context().add_class("flat")
        clear.connect("clicked", lambda _b: self._history.clear())
        header.pack_end(clear, False, False, 0)
        body.pack_start(header, False, False, 0)

        self._list = Gtk.ListBox()
        self._list.set_selection_mode(Gtk.SelectionMode.NONE)
        self._list.set_placeholder(self._placeholder())
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_max_content_height(360)
        scroller.set_propagate_natural_height(True)
        scroller.add(self._list)
        body.pack_start(scroller, True, True, 0)
        body.show_all()
        self._popover.add(body)
        self.set_popover(self._popover)
        self._popover.connect("show", lambda *_: self._on_popover_shown())

        history.add_listener(self._refresh)
        self._refresh()

    def _placeholder(self) -> Gtk.Widget:
        label = Gtk.Label(label=_("No messages yet."))
        label.get_style_context().add_class("dim-label")
        label.set_margin_top(Space.M)
        label.set_margin_bottom(Space.M)
        label.show()
        return label

    def _on_popover_shown(self) -> None:
        self._history.mark_all_read()

    def _refresh(self) -> None:
        for row in list(self._list.get_children()):
            self._list.remove(row)
        for message in self._history.items()[:100]:
            self._list.add(self._row(message))
        self._list.show_all()
        unread = self._history.unread_count(levels=self.ATTENTION_LEVELS)
        self._badge.set_text(str(unread) if unread < 100 else "99+")
        self._badge.set_visible(unread > 0)
        tip = _("Message history") if not unread else _("Message history ({count} unread)").format(count=unread)
        self.set_tooltip_text(tip)

    def _row(self, message: Message) -> Gtk.Widget:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.S)
        row.set_border_width(Space.XS)
        row.pack_start(level_icon(message.level), False, False, 0)
        text = Gtk.Label(label=message.text)
        text.set_xalign(0.0)
        text.set_line_wrap(True)
        text.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        text.set_max_width_chars(44)
        text.set_selectable(True)
        row.pack_start(text, True, True, 0)
        stamp = Gtk.Label(label=_format_time(message.timestamp))
        stamp.get_style_context().add_class("caption")
        stamp.get_style_context().add_class("dim-label")
        stamp.set_valign(Gtk.Align.START)
        row.pack_end(stamp, False, False, 0)
        return row
