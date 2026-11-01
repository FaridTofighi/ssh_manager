"""Password dialogs for SSH authentication.

* :class:`PasswordRequestDialog` — "Password for ‘alias’" (first prompt or
  retry after ``Permission denied``, "Attempt N of 3"), non-blocking.
* :class:`SavePasswordDialog` — "Save this password for ‘alias’?" after a
  verified login, non-blocking; a failed save keeps it open with **Retry Save**.
* :class:`PasswordDialog` — the Paramiko backend's blocking prompt (called
  from its worker thread through the main loop).

The texts come from :mod:`ophion.auth_flow`; nothing here stores a password.
"""

from __future__ import annotations

from typing import Callable, Optional, Protocol, Tuple

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # type: ignore  # noqa: E402

from ...auth_flow import Decision, PasswordRequest, SaveOffer  # noqa: E402
from ..i18n import _  # noqa: E402
from ..widgets.icon_button import set_accessible_name  # noqa: E402
from ..widgets.forms import FormSection, entry  # noqa: E402
from ..widgets.secret_entry import SecretEntry  # noqa: E402
from .base import BaseDialog, DialogSize, is_accept  # noqa: E402


class HostDetails(Protocol):
    host: str
    port: int
    username: str
    parent_window: Optional[Gtk.Window]


class PasswordDialog(BaseDialog):
    """Modal password dialog reused across authentication attempts."""

    def __init__(self) -> None:
        BaseDialog.__init__(self, None, _("Authenticate"), size=DialogSize.SMALL, resizable=False)
        self.set_destroy_with_parent(False)
        self._heading = Gtk.Label()
        self._heading.set_xalign(0.0)
        self._heading.set_line_wrap(True)
        self._heading.get_style_context().add_class("section-title")
        self.add_body(self._heading)

        section = FormSection()
        self._username_entry = entry()
        self._username_entry.set_editable(False)
        self._username_entry.set_can_focus(False)
        section.add(_("Username"), self._username_entry)
        self._secret = SecretEntry(_("Password"))
        self._password_entry = self._secret.entry
        self._password_entry.connect("changed", self._on_password_changed)
        section.add(_("_Password"), self._secret)
        self.add_body(section)

        self._error_label = Gtk.Label()
        self._error_label.set_xalign(0.0)
        self._error_label.set_line_wrap(True)
        self._error_label.get_style_context().add_class("form-error")
        self._error_label.set_no_show_all(True)
        self.add_body(self._error_label)

        self.add_cancel()
        self._connect_button = self.add_primary(_("C_onnect"))
        self._connect_button.set_sensitive(False)
        self.set_initial_focus(self._password_entry)

    def show_for(self, host_cfg: HostDetails, *, error_message: Optional[str] = None,
                 request: Optional[PasswordRequest] = None) -> Tuple[Optional[str], bool]:
        """Ask for the password of ``host_cfg``; returns ``(password_or_None, False)``.

        Nothing is remembered here: after a verified login the caller asks
        "Save this password for this host?" (the second value is always False).
        """

        parent = getattr(host_cfg, "parent_window", None)
        if isinstance(parent, Gtk.Window):
            self.set_transient_for(parent)
            self.set_position(Gtk.WindowPosition.CENTER_ON_PARENT)
        else:
            self.set_position(Gtk.WindowPosition.CENTER_ALWAYS)
        target = f"{host_cfg.username}@{host_cfg.host}:{host_cfg.port}"
        self.set_title(_("Authenticate to {target}").format(target=target))
        self._heading.set_text(_(request.message) if request else _("Enter the password for {target}").format(target=target))
        if request is not None:
            self.set_title(_(request.title))
            error_message = error_message or _(request.attempt_text)
        self._username_entry.set_text(host_cfg.username)
        self._password_entry.set_text("")
        self._error_label.set_text(error_message or "")
        self._error_label.set_visible(bool(error_message))
        self._secret.set_invalid(bool(error_message))
        self._connect_button.set_sensitive(False)

        response = self.run_keep()
        password = self._password_entry.get_text() if is_accept(response) else None
        # Clear sensitive data promptly.
        if password is not None:
            GLib.idle_add(self._password_entry.set_text, "")
        else:
            self._password_entry.set_text("")
        return (password or None), False

    def _on_password_changed(self, password_entry: Gtk.Entry) -> None:
        self._connect_button.set_sensitive(bool(password_entry.get_text()))


class PasswordRequestDialog(BaseDialog):
    """Non-blocking "Password for ‘alias’" dialog: **Retry** (or **Connect**) / **Cancel**.

    ``on_done(password)`` runs once: the typed password, or ``None`` for
    Cancel, Escape or an empty entry. :meth:`dismiss` closes it without a
    callback (tab closed, reconnect started).
    """

    def __init__(self, parent: Optional[Gtk.Window], request: PasswordRequest,
                 on_done: Callable[[Optional[str]], None]) -> None:
        BaseDialog.__init__(self, parent, _(request.title), size=DialogSize.SMALL, resizable=False)
        self._on_done: Optional[Callable[[Optional[str]], None]] = on_done
        self.message_label = Gtk.Label(label=_(request.message))
        self.message_label.set_xalign(0.0)
        self.message_label.set_line_wrap(True)
        self.message_label.set_width_chars(40)  # a wrapped label's minimum width keeps the dialog compact
        self.message_label.set_max_width_chars(48)
        self.add_body(self.message_label)

        self.secret = SecretEntry(_("Password"))
        set_accessible_name(self.secret.entry, _("Password for {alias}").format(alias=request.alias))
        self.add_body(self.secret)

        self.attempt_label = Gtk.Label(label=_(request.attempt_text))
        self.attempt_label.set_xalign(0.0)
        self.attempt_label.get_style_context().add_class("form-help")
        self.add_body(self.attempt_label)

        self.add_cancel()
        self.retry_button: Gtk.Button = self.add_primary(_("_Retry") if request.rejected else _("C_onnect"))
        self.retry_button.set_sensitive(False)  # nothing typed = nothing to try
        self.secret.entry.connect("changed", lambda e: self.retry_button.set_sensitive(bool(e.get_text())))
        self.set_initial_focus(self.secret.entry)
        self.connect("response", self._on_done_response)
        self.show_all()

    def _on_done_response(self, _dialog: Gtk.Dialog, response: int) -> None:
        password = self.secret.entry.get_text() if is_accept(response) else ""
        self.secret.entry.set_text("")
        callback, self._on_done = self._on_done, None
        self.destroy()
        if callback is not None:
            callback(password or None)  # empty = cancel

    def dismiss(self) -> None:
        self._on_done = None
        self.secret.entry.set_text("")
        self.destroy()


class SavePasswordDialog(BaseDialog):
    """Non-blocking "Save this password for ‘alias’?": **Save for this host** (default) / **Don't save**.

    Read-only hosts get **Use for this session** instead of Save. Escape or
    closing the window means **Don't save**. ``on_decision(decision)`` returns
    an error message to keep the dialog open (the primary button becomes
    **Retry Save**), or ``None`` to close it.
    """

    def __init__(self, parent: Optional[Gtk.Window], offer: SaveOffer,
                 on_decision: Callable[[Decision], Optional[str]]) -> None:
        BaseDialog.__init__(self, parent, _(offer.title), size=DialogSize.SMALL, resizable=False)
        self._on_decision: Optional[Callable[[Decision], Optional[str]]] = on_decision
        self._can_save = offer.context.can_save
        heading = Gtk.Label(label=_(offer.title))
        heading.set_xalign(0.0)
        heading.set_line_wrap(True)
        heading.set_width_chars(36)
        heading.get_style_context().add_class("section-title")
        self.add_body(heading)
        self.message_label = Gtk.Label(label=_(offer.message))
        self.message_label.set_xalign(0.0)
        self.message_label.set_line_wrap(True)
        self.message_label.set_width_chars(40)
        self.message_label.set_max_width_chars(52)
        self.add_body(self.message_label)
        self.error_label = Gtk.Label()
        self.error_label.set_xalign(0.0)
        self.error_label.set_line_wrap(True)
        self.error_label.get_style_context().add_class("form-error")
        self.error_label.set_no_show_all(True)
        self.add_body(self.error_label)

        self.dont_save_button = self.add_cancel(_("_Don't Save"))
        self.save_button: Gtk.Button = self.add_primary(
            _("_Save for This Host") if self._can_save else _("_Use for This Session"))
        set_accessible_name(self, _(offer.title))
        self.set_initial_focus(self.save_button)
        self.connect("response", self._on_save_response)
        self.show_all()

    def _on_save_response(self, _dialog: Gtk.Dialog, response: int) -> None:
        callback = self._on_decision
        if callback is None:
            return
        if not is_accept(response):
            decision = Decision.DONT_SAVE  # Don't save, Escape, window closed
        else:
            decision = Decision.SAVE if self._can_save else Decision.SESSION_ONLY
        error = callback(decision)
        if error and decision is Decision.SAVE:
            self.error_label.set_text(error)
            self.error_label.show()
            self.save_button.set_label(_("_Retry Save"))
            self.save_button.set_use_underline(True)
            return
        self._on_decision = None
        self.destroy()

    def dismiss(self) -> None:
        self._on_decision = None
        self.destroy()
