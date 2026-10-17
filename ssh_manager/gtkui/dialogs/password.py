"""Reusable password prompt for SSH authentication (Paramiko backend)."""

from __future__ import annotations

from typing import Optional, Protocol, Tuple

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # type: ignore  # noqa: E402

from ..i18n import _  # noqa: E402
from ..widgets.forms import FormSection, entry  # noqa: E402
from ..widgets.secret_entry import SecretEntry  # noqa: E402
from .base import BaseDialog, DialogSize, is_accept  # noqa: E402


class HostDetails(Protocol):
    host: str
    port: int
    username: str
    remember_password_default: bool
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

        self._remember_check = Gtk.CheckButton.new_with_mnemonic(_("_Remember password for this host"))
        self.add_body(self._remember_check)

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

    def show_for(self, host_cfg: HostDetails, *, error_message: Optional[str] = None) -> Tuple[Optional[str], bool]:
        """Ask for the password of ``host_cfg``; returns ``(password_or_None, remember)``."""

        parent = getattr(host_cfg, "parent_window", None)
        if isinstance(parent, Gtk.Window):
            self.set_transient_for(parent)
            self.set_position(Gtk.WindowPosition.CENTER_ON_PARENT)
        else:
            self.set_position(Gtk.WindowPosition.CENTER_ALWAYS)
        target = f"{host_cfg.username}@{host_cfg.host}:{host_cfg.port}"
        self.set_title(_("Authenticate to {target}").format(target=target))
        self._heading.set_text(_("Enter the password for {target}").format(target=target))
        self._username_entry.set_text(host_cfg.username)
        self._password_entry.set_text("")
        remember_default = bool(getattr(host_cfg, "remember_password_default", True))
        self._remember_check.set_active(remember_default)
        self._error_label.set_text(error_message or "")
        self._error_label.set_visible(bool(error_message))
        self._secret.set_invalid(bool(error_message))
        self._connect_button.set_sensitive(False)

        response = self.run_keep()
        password = self._password_entry.get_text() if is_accept(response) else None
        remember = self._remember_check.get_active() if password else False
        # Clear sensitive data promptly.
        if password is not None:
            GLib.idle_add(self._password_entry.set_text, "")
        else:
            self._password_entry.set_text("")
        return (password or None), remember

    def _on_password_changed(self, password_entry: Gtk.Entry) -> None:
        self._connect_button.set_sensitive(bool(password_entry.get_text()))
