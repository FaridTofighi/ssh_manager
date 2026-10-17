"""Secret-entry dialogs: unlock with a password, or choose a new one."""

from __future__ import annotations

from typing import Callable, Optional

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Pango  # type: ignore  # noqa: E402

from ..widgets.icons import make_icon  # noqa: E402
from ..i18n import _  # noqa: E402
from ..theme.tokens import Space  # noqa: E402
from ..widgets.forms import FormSection  # noqa: E402
from ..widgets.secret_entry import SecretEntry  # noqa: E402
from .base import BaseDialog, DialogSize, is_accept  # noqa: E402


def _intro(icon_name: str, title: str, text: str) -> Gtk.Widget:
    row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.M)
    icon = make_icon(icon_name, 32)
    icon.set_valign(Gtk.Align.START)
    row.pack_start(icon, False, False, 0)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=Space.XS)
    heading = Gtk.Label(label=title)
    heading.set_xalign(0.0)
    heading.get_style_context().add_class("section-title")
    box.pack_start(heading, False, False, 0)
    if text:
        body = Gtk.Label(label=text)
        body.set_xalign(0.0)
        body.set_line_wrap(True)
        body.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        body.set_max_width_chars(48)
        body.get_style_context().add_class("section-description")
        box.pack_start(body, False, False, 0)
    row.pack_start(box, True, True, 0)
    return row


class UnlockDialog(BaseDialog):
    """Ask for an existing secret; ``verify(password)`` decides whether it is accepted."""

    def __init__(
        self,
        parent: Optional[Gtk.Window],
        title: str,
        heading: str,
        description: str,
        verify: Callable[[str], bool],
        *,
        accept_label: str = "",
        icon_name: str = "sshm-lock",
        error_text: str = "",
    ) -> None:
        BaseDialog.__init__(self, parent, title, size=DialogSize.SMALL, resizable=False)
        self._verify = verify
        self._error_text = error_text or _("Incorrect password. Please try again.")
        self.add_body(_intro(icon_name, heading, description))
        self.secret = SecretEntry(_("Password"))
        self.add_body(self.secret)
        self.error = Gtk.Label()
        self.error.set_xalign(0.0)
        self.error.get_style_context().add_class("form-error")
        self.error.set_no_show_all(True)
        self.add_body(self.error)
        self.add_cancel()
        self.add_primary(accept_label or _("_Unlock"))
        self.set_initial_focus(self.secret.entry)
        self.set_response_validator(self._validate)
        self.accepted_password: Optional[str] = None

    def _validate(self, response: int) -> bool:
        if not is_accept(response):
            return True
        text = self.secret.get_text().strip()
        if text and self._verify(text):
            self.accepted_password = text
            return True
        self.secret.set_text("")
        self.secret.set_invalid(True)
        self.error.set_text(self._error_text if text else _("Enter the password."))
        self.error.show()
        self.secret.grab_focus()
        return False

    def ask(self) -> Optional[str]:
        response = self.run_and_destroy()
        return self.accepted_password if is_accept(response) else None


class NewSecretDialog(BaseDialog):
    """Choose a new secret with confirmation, strength meter and match feedback."""

    def __init__(
        self,
        parent: Optional[Gtk.Window],
        title: str,
        heading: str,
        description: str = "",
        *,
        accept_label: str = "",
        icon_name: str = "sshm-key",
        current_label: str = "",
        verify_current: Optional[Callable[[str], bool]] = None,
        min_length: int = 1,
    ) -> None:
        BaseDialog.__init__(self, parent, title, size=DialogSize.SMALL, resizable=False)
        self._verify_current = verify_current
        self._min_length = min_length
        self.add_body(_intro(icon_name, heading, description))
        section = FormSection()
        self.current: Optional[SecretEntry] = None
        if verify_current is not None:
            self.current = SecretEntry(_("Current password"))
            self._current_row = section.add(current_label or _("C_urrent"), self.current)
        self.new = SecretEntry(_("New password"), show_strength=True, generate=False)
        self._new_row = section.add(_("_New"), self.new)
        self.confirm = SecretEntry(_("Repeat the new password"))
        self._confirm_row = section.add(_("Con_firm"), self.confirm)
        self.add_body(section)
        self.confirm.entry.connect("changed", lambda *_: self._update_match())
        self.new.entry.connect("changed", lambda *_: self._update_match())
        self.add_cancel()
        self.add_primary(accept_label or _("_Save"))
        self.set_initial_focus((self.current or self.new).entry)
        self.set_response_validator(self._validate)
        self.current_password: Optional[str] = None
        self.new_password: Optional[str] = None

    def _update_match(self) -> None:
        confirm = self.confirm.get_text()
        if not confirm:
            self._confirm_row.set_error(None)
            return
        if confirm != self.new.get_text():
            self._confirm_row.set_error(_("Passwords do not match."))
        else:
            self._confirm_row.set_error(None)

    def _validate(self, response: int) -> bool:
        if not is_accept(response):
            return True
        ok = True
        if self.current is not None and self._verify_current is not None:
            current = self.current.get_text().strip()
            if not current or not self._verify_current(current):
                self._current_row.set_error(_("The current password is incorrect."))
                ok = False
            else:
                self._current_row.set_error(None)
                self.current_password = current
        new = self.new.get_text().strip()
        if len(new) < self._min_length or not new:
            self._new_row.set_error(_("Password cannot be empty."))
            ok = False
        else:
            self._new_row.set_error(None)
        if self.confirm.get_text().strip() != new:
            self._confirm_row.set_error(_("Passwords do not match."))
            ok = False
        if ok:
            self.new_password = new
        return ok

    def ask(self) -> Optional[str]:
        response = self.run_and_destroy()
        return self.new_password if is_accept(response) else None
