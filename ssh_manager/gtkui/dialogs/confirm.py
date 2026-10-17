"""Confirmation and message dialogs.

``ConfirmDialog`` replaces the old ``_confirm()`` helper that always labelled
the accept button "_Delete": callers now pass the real action label and whether
the action is destructive. Destructive confirmations default to *Cancel* so a
stray Enter never deletes anything.
"""

from __future__ import annotations

from typing import Optional

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Pango  # type: ignore  # noqa: E402

from ..i18n import _  # noqa: E402
from ..theme.tokens import Level, Space  # noqa: E402
from ..widgets.toast import LEVEL_ICONS  # noqa: E402
from .base import BaseDialog, DialogSize, is_accept  # noqa: E402


def _message_block(icon_name: str, title: str, message: str, *, icon_class: str = "") -> Gtk.Box:
    row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.M)
    icon = Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.DIALOG)
    icon.set_valign(Gtk.Align.START)
    if icon_class:
        icon.get_style_context().add_class(icon_class)
    row.pack_start(icon, False, False, 0)
    text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=Space.XS)
    heading = Gtk.Label(label=title)
    heading.set_xalign(0.0)
    heading.set_line_wrap(True)
    heading.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
    heading.set_selectable(True)
    heading.set_can_focus(False)
    heading.get_style_context().add_class("section-title")
    text_box.pack_start(heading, False, False, 0)
    if message:
        body = Gtk.Label(label=message)
        body.set_xalign(0.0)
        body.set_line_wrap(True)
        body.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        body.set_selectable(True)
        body.set_can_focus(False)
        body.set_max_width_chars(56)
        text_box.pack_start(body, False, False, 0)
    row.pack_start(text_box, True, True, 0)
    return row


class ConfirmDialog(BaseDialog):
    """Ask the user to confirm an action."""

    def __init__(
        self,
        parent: Optional[Gtk.Window],
        title: str,
        message: str = "",
        *,
        confirm_label: str = "",
        cancel_label: str = "",
        destructive: bool = False,
        default_confirm: Optional[bool] = None,
    ) -> None:
        BaseDialog.__init__(self, parent, title, size=DialogSize.SMALL, resizable=False)
        icon = "dialog-warning-symbolic" if destructive else "dialog-question-symbolic"
        self.add_body(_message_block(icon, title, message, icon_class="level-warning" if destructive else ""))
        self.cancel_button = self.add_cancel(cancel_label)
        self.confirm_button = self.add_primary(
            confirm_label or _("_OK"),
            Gtk.ResponseType.OK,
            destructive=destructive,
            default=False,
        )
        # Destructive actions default to Cancel; others to the action itself.
        confirm_by_default = (not destructive) if default_confirm is None else default_confirm
        if confirm_by_default:
            self.confirm_button.set_can_default(True)
            self.set_default_response(Gtk.ResponseType.OK)
            self.set_initial_focus(self.confirm_button)
        else:
            self.cancel_button.set_can_default(True)
            self.set_default_response(Gtk.ResponseType.CANCEL)
            self.set_initial_focus(self.cancel_button)

    def ask(self) -> bool:
        return is_accept(self.run_and_destroy())


def confirm(
    parent: Optional[Gtk.Window],
    title: str,
    message: str = "",
    *,
    confirm_label: str = "",
    destructive: bool = False,
    default_confirm: Optional[bool] = None,
) -> bool:
    """Show a :class:`ConfirmDialog` and return True when confirmed."""

    return ConfirmDialog(
        parent,
        title,
        message,
        confirm_label=confirm_label,
        destructive=destructive,
        default_confirm=default_confirm,
    ).ask()


class MessageDialog(BaseDialog):
    """Informational dialog with a single Close/OK button."""

    def __init__(
        self,
        parent: Optional[Gtk.Window],
        title: str,
        message: str = "",
        *,
        level: Level = Level.INFO,
        button_label: str = "",
    ) -> None:
        BaseDialog.__init__(self, parent, title, size=DialogSize.SMALL, resizable=False)
        self.add_body(_message_block(LEVEL_ICONS[level], title, message, icon_class=f"level-{level.value}"))
        button = self.add_primary(button_label or _("_Close"), Gtk.ResponseType.CLOSE)
        button.get_style_context().remove_class("suggested-action")
        self.set_initial_focus(button)


def show_message(
    parent: Optional[Gtk.Window],
    title: str,
    message: str = "",
    *,
    level: Level = Level.INFO,
) -> None:
    MessageDialog(parent, title, message, level=level).run_and_destroy()
