"""Secret-entry dialogs: unlock with a password, or choose a new one."""

from __future__ import annotations

from typing import Callable, Optional

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Pango  # type: ignore  # noqa: E402

from ..widgets.icons import make_icon  # noqa: E402
from ..i18n import _  # noqa: E402
from ..theme.tokens import Size, Space  # noqa: E402
from ..widgets.forms import FormSection  # noqa: E402
from ..widgets.secret_entry import SecretEntry  # noqa: E402
from .base import BaseDialog, DialogSize, is_accept  # noqa: E402


def _intro(icon_name: str, title: str, text: str) -> Gtk.Widget:
    row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.M)
    icon = make_icon(icon_name, Size.EMPTY_CARD_ICON)
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
        in_thread: bool = False,
        forgot_label: str = "",
    ) -> None:
        BaseDialog.__init__(self, parent, title, size=DialogSize.SMALL, resizable=False)
        self._verify = verify
        self._in_thread = in_thread  # verify() is slow (key derivation): run it in a worker thread
        self._busy = False
        self._verified = False
        self.failures = 0
        self.forgot = False
        self._error_text = error_text or _("Incorrect password. Please try again.")
        self.add_body(_intro(icon_name, heading, description))
        self.secret = SecretEntry(_("Password"))
        self.add_body(self.secret)
        self.error = Gtk.Label()
        self.error.set_xalign(0.0)
        self.error.get_style_context().add_class("form-error")
        self.error.set_no_show_all(True)
        self.add_body(self.error)
        self.spinner = Gtk.Spinner()
        self.spinner.set_no_show_all(True)
        self.add_body(self.spinner)
        if forgot_label:
            link = Gtk.Button.new_with_mnemonic(forgot_label)
            link.set_relief(Gtk.ReliefStyle.NONE)
            link.get_style_context().add_class("flat")
            link.set_halign(Gtk.Align.START)
            link.connect("clicked", lambda *_a: self._on_forgot())
            self.add_body(link)
        self.add_cancel()
        self.add_primary(accept_label or _("_Unlock"))
        self.set_initial_focus(self.secret.entry)
        self.set_response_validator(self._validate)
        self.accepted_password: Optional[str] = None

    @property
    def busy(self) -> bool:
        return self._busy

    def _on_forgot(self) -> None:
        self.forgot = True
        self.response(Gtk.ResponseType.CANCEL)

    def _validate(self, response: int) -> bool:
        if not is_accept(response):
            return True
        if self._verified:
            return True
        if self._busy or not self.primary_button.get_sensitive():
            return False
        text = self.secret.get_text().strip()
        if not text:
            self._failed("", count=False)
            return False
        if not self._in_thread:
            if self._verify(text):
                self.accepted_password = text
                return True
            self._failed(text)
            return False
        import threading

        from gi.repository import GLib  # type: ignore

        self._busy = True
        self.primary_button.set_sensitive(False)
        self.secret.set_sensitive(False)
        self.spinner.show()
        self.spinner.start()

        def work() -> None:
            try:
                ok = bool(self._verify(text))
            except Exception:  # a damaged key file: the same message, never the details
                ok = False
            GLib.idle_add(self._checked, ok, text)

        threading.Thread(target=work, name="unlock", daemon=True).start()
        return False

    def _checked(self, ok: bool, text: str) -> bool:
        self._busy = False
        self.spinner.stop()
        self.spinner.hide()
        self.secret.set_sensitive(True)
        if ok:
            self.accepted_password = text
            self._verified = True
            self.response(Gtk.ResponseType.OK)
        else:
            self.primary_button.set_sensitive(True)
            self._failed(text)
        return False

    def _failed(self, text: str, *, count: bool = True) -> None:
        from ...unlock import unlock_delay

        self.secret.set_text("")
        self.secret.set_invalid(True)
        self.error.set_text(self._error_text if text else _("Enter the password."))
        self.error.show()
        self.secret.grab_focus()
        if not count:
            return
        self.failures += 1
        delay = unlock_delay(self.failures)
        if delay:
            from gi.repository import GLib  # type: ignore

            self.primary_button.set_sensitive(False)
            self.error.set_text(_("{error} Wait {n} s before the next try.").format(error=self._error_text,
                                                                                     n=int(delay)))
            GLib.timeout_add(int(delay * 1000), lambda: self.primary_button.set_sensitive(True) and False)

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
