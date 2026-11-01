"""Preferences → Security (replaces "Master Password"); the actions are in ``controllers/security.py``.

Rows: change the master password; how SSH Manager unlocks (ask / remember on this computer); the
recovery key; how many passwords are saved; the leftovers of older versions (remove the old keyring
copies, finish an interrupted migration); **Don't save passwords**.
"""

from __future__ import annotations

from typing import Callable, Optional

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # type: ignore  # noqa: E402

from ..i18n import _, ngettext  # noqa: E402
from ..theme.tokens import Space  # noqa: E402
from ..widgets.forms import FormSection  # noqa: E402
from ..widgets.icon_button import set_accessible_name  # noqa: E402


def _help(text: str) -> Gtk.Label:
    label = Gtk.Label(label=text)
    label.set_xalign(0.0)
    label.set_line_wrap(True)
    label.get_style_context().add_class("form-help")
    return label


class SecurityPage(Gtk.Box):
    """Rebuilt from ``actions.state()`` after every action."""

    def __init__(self, actions, parent: Optional[Gtk.Window] = None) -> None:
        Gtk.Box.__init__(self, orientation=Gtk.Orientation.VERTICAL, spacing=Space.M)
        self.actions = actions
        self.parent = parent
        self.state = None
        self.refresh()

    def refresh(self) -> None:
        for child in self.get_children():
            self.remove(child)
            child.destroy()
        self.state = state = self.actions.state()
        self.change_button = self.ask_radio = self.remember_radio = self.recovery_button = None
        self.finish_button = self.leftovers_button = None
        self._updating = True
        if state.migration_pending:
            self._migration_section(state)
        if state.has_keybox:
            self._password_section(state)
            self._unlock_section(state)
            self._recovery_section(state)
        self._saved_section(state)
        self._leftovers_section(state)
        self._updating = False
        self.show_all()

    # --------------------------------------------------------------- parts
    def _button(self, label: str, callback: Callable[[], object]) -> Gtk.Button:
        button = Gtk.Button.new_with_mnemonic(label)
        button.set_halign(Gtk.Align.START)
        button.connect("clicked", lambda _b: (callback(), self.refresh()))
        return button

    def _migration_section(self, state) -> None:
        section = FormSection(_("Migration"))
        section.add_widget(_help(_("Saved passwords could not be moved. Nothing was lost. Finish the migration to "
                                   "use them again.")))
        self.finish_button = self._button(_("_Finish Migration…"), self.actions.finish_migration)
        section.add_widget(self.finish_button)
        self.pack_start(section, False, False, 0)

    def _password_section(self, state) -> None:
        section = FormSection(_("Master Password"))
        section.add_widget(_help(_("It encrypts your saved passwords and macros on this computer. Changing it "
                                   "only updates the key file.")))
        self.change_button = self._button(_("_Change Master Password…"), self.actions.change_password)
        section.add_widget(self.change_button)
        self.pack_start(section, False, False, 0)

    def _unlock_section(self, state) -> None:
        section = FormSection(_("When Ophion Starts"))
        self.ask_radio = Gtk.RadioButton.new_with_mnemonic(None, _("_Ask for the master password"))
        self.remember_radio = Gtk.RadioButton.new_with_mnemonic_from_widget(
            self.ask_radio, _("_Remember on this computer (no prompt)"))
        (self.remember_radio if state.remembered else self.ask_radio).set_active(True)
        section.add_widget(self.ask_radio)
        section.add_widget(self.remember_radio)
        if state.keyring_available:
            section.add_widget(_help(_("Anyone who can sign in to this computer account can open Ophion; "
                                       "your saved passwords stay encrypted on disk.")))
        else:
            self.remember_radio.set_sensitive(False)
            section.add_widget(_help(_("Not available: no system keyring (for example GNOME Keyring or KWallet) "
                                       "is running.")))
        self.remember_radio.connect("toggled", self._on_remember_toggled)
        self.pack_start(section, False, False, 0)

    def _on_remember_toggled(self, radio: Gtk.RadioButton) -> None:
        if self._updating:
            return
        self.actions.set_remember(radio.get_active())
        self.refresh()  # shows what is in effect (a cancelled confirmation goes back)

    def _recovery_section(self, state) -> None:
        section = FormSection(_("Recovery Key"))
        section.add_widget(_help(_("A recovery key is set up. It opens your saved passwords if you forget the "
                                   "master password.") if state.has_recovery else
                                 _("No recovery key: if you forget the master password, the saved passwords "
                                   "cannot be opened.")))
        self.recovery_button = self._button(_("Create a _New Recovery Key…") if state.has_recovery else
                                            _("Create a _Recovery Key…"), self.actions.recovery_key)
        section.add_widget(self.recovery_button)
        self.pack_start(section, False, False, 0)

    def _saved_section(self, state) -> None:
        section = FormSection(_("Saved Passwords"))
        if state.passwords_off:
            text = _("Saving passwords is turned off: Ophion asks for them every time.")
        elif state.store_error:
            text = _("The password file cannot be opened: {reason}").format(reason=state.store_error)
        elif state.has_keybox:
            text = ngettext("{n} stored · encrypted", "{n} stored · encrypted", state.saved).format(n=state.saved)
        else:
            text = _("Unavailable until the migration is finished.")
        self.saved_label = _help(text)
        section.add_widget(self.saved_label)
        if not state.migration_pending:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.M)
            label = Gtk.Label.new_with_mnemonic(_("_Don't save passwords"))
            label.set_xalign(0.0)
            self.off_switch = Gtk.Switch()
            self.off_switch.set_active(state.passwords_off)
            label.set_mnemonic_widget(self.off_switch)
            set_accessible_name(self.off_switch, _("Don't save passwords"))
            self.off_switch.connect("notify::active", self._on_off_toggled)
            row.pack_start(label, True, True, 0)
            row.pack_start(self.off_switch, False, False, 0)
            section.add_widget(row)
        else:
            self.off_switch = None
        self.pack_start(section, False, False, 0)

    def _on_off_toggled(self, switch: Gtk.Switch, _param) -> None:
        if self._updating:
            return
        self.actions.set_passwords_off(switch.get_active())
        self.refresh()

    def _leftovers_section(self, state) -> None:
        if not state.keyring_leftovers:
            return
        section = FormSection(_("From Older Versions"))
        section.add_widget(_help(_("Copies of saved passwords are still in the system keyring.")))
        self.leftovers_button = self._button(_("Remove Old Copies from the System _Keyring"),
                                             self.actions.remove_keyring_copies)
        section.add_widget(self.leftovers_button)
        self.pack_start(section, False, False, 0)


__all__ = ["SecurityPage"]
