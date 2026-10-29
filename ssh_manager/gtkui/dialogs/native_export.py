"""Export as an SSH Manager file (``*.sshm``): what to export, what to include, an optional passphrase.

One page. The scope is the whole inventory, the selected group (with its
subgroups) or the selected hosts. The passphrase is typed twice, checked for
strength and never stored; without one a notice says that the file lists
servers, users and settings but no passwords.
"""

from __future__ import annotations

from typing import Optional, Tuple

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # type: ignore  # noqa: E402

from ...exporters.native import ExportOptions  # noqa: E402
from ..i18n import _, ngettext  # noqa: E402
from ..widgets.forms import FormSection  # noqa: E402
from ..widgets.icon_button import set_accessible_name  # noqa: E402
from ..widgets.secret_entry import SecretEntry  # noqa: E402
from .base import BaseDialog, DialogSize, is_accept  # noqa: E402

MIN_PASSPHRASE = 8


def passphrase_strength(text: str) -> Tuple[int, str]:
    """``(0–3, hint)``: length and the kinds of characters (a hint, not a guarantee)."""

    if not text:
        return 0, ""
    kinds = sum(bool(test) for test in (any(c.islower() for c in text), any(c.isupper() for c in text),
                                        any(c.isdigit() for c in text), any(not c.isalnum() for c in text)))
    if len(text) < MIN_PASSPHRASE:
        return 0, _("Too short: use at least {n} characters.").format(n=MIN_PASSPHRASE)
    if len(text) >= 16 or (len(text) >= 12 and kinds >= 3):
        return 3, _("Strong.")
    if len(text) >= 12 or kinds >= 3:
        return 2, _("Fair: a longer passphrase is better.")
    return 1, _("Weak: use more words, or mix letters, digits and symbols.")


class NativeExportDialog(BaseDialog):
    """``values()`` → ``(scope, ExportOptions, passphrase or None)``."""

    def __init__(self, parent: Optional[Gtk.Window], *, group_label: str = "", host_count: int = 0,
                 scope: str = "all", has_smart_groups: bool = False) -> None:
        BaseDialog.__init__(self, parent, _("Export as SSH Manager File"), size=DialogSize.MEDIUM)
        what = FormSection(_("What to export"))
        self.all_radio = Gtk.RadioButton.new_with_mnemonic_from_widget(None, _("The _whole inventory"))
        self.group_radio = Gtk.RadioButton.new_with_mnemonic_from_widget(
            self.all_radio, _("The _group “{name}” and its subgroups").format(name=group_label) if group_label
            else _("The selected _group (select one in the sidebar)"))
        self.hosts_radio = Gtk.RadioButton.new_with_mnemonic_from_widget(
            self.all_radio, ngettext("The selected _host", "The {n} selected _hosts", host_count).format(n=host_count)
            if host_count else _("The selected _hosts (select them in the sidebar)"))
        self.group_radio.set_sensitive(bool(group_label))
        self.hosts_radio.set_sensitive(host_count > 0)
        for radio in (self.all_radio, self.group_radio, self.hosts_radio):
            what.add_widget(radio)
        {"groups": self.group_radio, "hosts": self.hosts_radio}.get(scope, self.all_radio).set_active(True)
        self.add_body(what)

        include = FormSection(_("Include"))
        self.colors_check = Gtk.CheckButton.new_with_mnemonic(_("_Colours of groups and hosts"))
        self.notes_check = Gtk.CheckButton.new_with_mnemonic(_("_Notes and tags"))
        self.credentials_check = Gtk.CheckButton.new_with_mnemonic(
            _("C_redential references (names and usernames only)"))
        self.smart_check = Gtk.CheckButton.new_with_mnemonic(_("_Smart groups"))
        for check, active in ((self.colors_check, True), (self.notes_check, True), (self.credentials_check, True),
                              (self.smart_check, False)):
            check.set_active(active)
            include.add_widget(check)
        self.smart_check.set_sensitive(has_smart_groups)
        self.add_body(include)

        protect = FormSection(_("Protection"))
        self.protect_check = Gtk.CheckButton.new_with_mnemonic(_("_Protect with a passphrase (recommended)"))
        self.protect_check.connect("toggled", lambda *_a: self._sync())
        protect.add_widget(self.protect_check)
        self.pass1 = SecretEntry(_("Passphrase"))
        self.pass2 = SecretEntry(_("Repeat the passphrase"))
        set_accessible_name(self.pass1.entry, _("Passphrase"))
        set_accessible_name(self.pass2.entry, _("Repeat the passphrase"))
        for entry in (self.pass1, self.pass2):
            entry.entry.connect("changed", lambda *_a: self._sync())
            protect.add_widget(entry)
        self.strength = Gtk.Label()
        self.strength.set_xalign(0.0)
        self.strength.get_style_context().add_class("form-help")
        protect.add_widget(self.strength)
        self.error = Gtk.Label()
        self.error.set_xalign(0.0)
        self.error.get_style_context().add_class("form-error")
        protect.add_widget(self.error)
        self.add_body(protect)

        self.notice = Gtk.Label(label=_("This file lists your servers, users and settings. It contains no passwords."))
        self.notice.set_xalign(0.0)
        self.notice.set_line_wrap(True)
        self.add_body(self.notice)
        help_text = Gtk.Label(label=_(
            "Never exported: passwords, private keys, certificate pins, macros, the master "
            "password, application settings, history and window state."))
        help_text.set_xalign(0.0)
        help_text.set_line_wrap(True)
        help_text.get_style_context().add_class("form-help")
        self.add_body(help_text)

        self.add_cancel()
        self.add_primary(_("_Export…"))
        self.set_initial_focus(self.all_radio)
        self.set_response_validator(lambda response: not is_accept(response) or self._valid())
        self.show_all()
        self._sync()

    def _sync(self) -> None:
        protect = self.protect_check.get_active()
        for entry in (self.pass1, self.pass2):
            entry.set_sensitive(protect)
        self.notice.set_visible(not protect)
        self.strength.set_text(passphrase_strength(self.pass1.get_text())[1] if protect else "")
        self.error.set_text("")

    def _valid(self) -> bool:
        if not self.protect_check.get_active():
            return True
        first, second = self.pass1.get_text(), self.pass2.get_text()
        if len(first) < MIN_PASSPHRASE:
            self.error.set_text(_("The passphrase is too short."))
            return False
        if first != second:
            self.error.set_text(_("The two passphrases differ."))
            return False
        return True

    def values(self) -> Tuple[str, ExportOptions, Optional[str]]:
        scope = "groups" if self.group_radio.get_active() else "hosts" if self.hosts_radio.get_active() else "all"
        options = ExportOptions(colors=self.colors_check.get_active(), notes=self.notes_check.get_active(),
                                credential_refs=self.credentials_check.get_active(),
                                smart_groups=self.smart_check.get_active())
        passphrase = self.pass1.get_text() if self.protect_check.get_active() else None
        return scope, options, passphrase

    def forget(self) -> None:
        for entry in (self.pass1, self.pass2):
            entry.set_text("")


__all__ = ["NativeExportDialog", "passphrase_strength"]
