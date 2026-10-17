"""Encrypted credential vault: lock state, entries, import and export."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Optional

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # type: ignore  # noqa: E402

from ...vault import CredentialVault, VaultEntry, VaultError  # noqa: E402
from ..constants import Icons  # noqa: E402
from ..i18n import _  # noqa: E402
from ..menu_spec import MenuItemSpec, MenuSection  # noqa: E402
from ..theme.tokens import Level, Space  # noqa: E402
from ..validation import validate_required  # noqa: E402
from ..widgets.forms import FormSection, entry  # noqa: E402
from ..widgets.icon_button import IconMenuButton  # noqa: E402
from ..widgets.menus import build_menu  # noqa: E402
from ..widgets.secret_entry import SecretEntry  # noqa: E402
from .base import BaseDialog, DialogSize, is_accept  # noqa: E402
from .confirm import confirm, show_message  # noqa: E402
from .files import choose_open_file, choose_save_file  # noqa: E402
from .list_manager import ListManagerDialog  # noqa: E402
from .prompt import prompt_text  # noqa: E402
from .secrets import NewSecretDialog  # noqa: E402


class VaultEntryDialog(BaseDialog):
    """One form for a vault entry (replaces three sequential prompts)."""

    def __init__(self, parent: Gtk.Window, existing: Optional[VaultEntry] = None) -> None:
        BaseDialog.__init__(
            self, parent, _("Edit Vault Entry") if existing else _("New Vault Entry"), size=DialogSize.SMALL, resizable=False
        )
        section = FormSection()
        self.label_entry = entry(existing.label if existing else "", _("e.g. Production root"))
        section.add(_("_Label"), self.label_entry, validator=lambda v: validate_required(v, _("Label")))
        self.user_entry = entry(existing.username if existing else "", _("Username"))
        section.add(_("_Username"), self.user_entry, validator=lambda v: validate_required(v, _("Username")))
        self.password = SecretEntry(_("Password"), generate=True)
        section.add(
            _("_Password"),
            self.password,
            help_text=_("Leave empty to keep the current password.") if existing else "",
        )
        self.add_body(section)
        self.add_cancel()
        self.add_primary(_("_Save"))
        self.set_initial_focus(self.label_entry)
        self.set_response_validator(lambda r: not is_accept(r) or section.validate())
        self._existing = existing

    def ask(self) -> Optional[VaultEntry]:
        self.show_all()
        try:
            if not is_accept(self.run()):
                return None
            existing = self._existing
            password = self.password.get_text()
            return VaultEntry(
                existing.id if existing else uuid.uuid4().hex,
                self.label_entry.get_text().strip(),
                self.user_entry.get_text().strip(),
                password if password or not existing else existing.password,
            )
        finally:
            self.destroy()


class VaultDialog(ListManagerDialog):
    """Manage the encrypted vault."""

    def __init__(self, parent: Gtk.Window, vault: CredentialVault) -> None:
        ListManagerDialog.__init__(
            self,
            parent,
            _("Credential Vault"),
            [_("Label"), _("Username")],
            empty_icon=Icons.VAULT,
            empty_title=_("The vault is empty"),
            empty_description=_("Add usernames and passwords; they are encrypted with the vault password."),
            add_label=_("_Add Entry…"),
            height=460,
        )
        self._vault = vault
        self._status_label = Gtk.Label()
        if not vault.available:
            self.toolbar.hide()
            self.list_stack.hide()
            self.set_hint(_("Encryption support is unavailable because the 'cryptography' package is missing."))
            return
        self._build_lock_bar()
        self._build_menu()
        self.refresh()

    # ------------------------------------------------------------- building
    def _build_lock_bar(self) -> None:
        self._lock_bar = Gtk.InfoBar()
        self._lock_bar.set_message_type(Gtk.MessageType.INFO)
        area = self._lock_bar.get_content_area()
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.S)
        self._status_label.set_xalign(0.0)
        self._status_label.set_line_wrap(True)
        box.pack_start(self._status_label, True, True, 0)
        self._unlock_secret = SecretEntry(_("Vault password"))
        self._unlock_secret.connect_activate(self._unlock)
        box.pack_start(self._unlock_secret, False, False, 0)
        self._unlock_button = Gtk.Button.new_with_mnemonic(_("_Unlock"))
        self._unlock_button.get_style_context().add_class("suggested-action")
        self._unlock_button.connect("clicked", lambda *_: self._unlock())
        box.pack_start(self._unlock_button, False, False, 0)
        self._create_button = Gtk.Button.new_with_mnemonic(_("_Create Vault…"))
        self._create_button.get_style_context().add_class("suggested-action")
        self._create_button.connect("clicked", lambda *_: self._create_master())
        box.pack_start(self._create_button, False, False, 0)
        for widget in (self._unlock_secret, self._unlock_button, self._create_button):
            widget.set_valign(Gtk.Align.CENTER)
            # Visibility follows the lock state; dialog.show_all() must not override it.
            widget.show_all()
            widget.set_no_show_all(True)
        area.pack_start(box, True, True, 0)
        content = self.get_content_area()
        content.pack_start(self._lock_bar, False, False, 0)
        content.reorder_child(self._lock_bar, 0)

    def _build_menu(self) -> None:
        sections = [
            MenuSection("transfer", [
                MenuItemSpec(_("_Export Vault…"), self._export, Icons.SAVE),
                MenuItemSpec(_("_Import Vault…"), self._import, Icons.OPEN_FOLDER),
            ]),
            MenuSection("password", [
                MenuItemSpec(_("_Change Vault Password…"), self._change_master, Icons.CREDENTIALS),
            ]),
            MenuSection("danger", [
                MenuItemSpec(_("_Reset Vault…"), self._reset_vault, Icons.DELETE, destructive=True),
            ]),
        ]
        self._more = IconMenuButton(Icons.MORE, _("Vault actions"), menu=build_menu(sections))
        self.toolbar.pack_end(self._more, False, False, 0)

    # ------------------------------------------------------------ list hooks
    def can_modify(self) -> bool:
        return bool(getattr(self, "_vault", None) and self._vault.unlocked)

    def load_rows(self):
        if not self._vault.unlocked:
            return []
        return [(e.id, e.label, e.username) for e in self._vault.entries()]

    def refresh(self) -> None:
        super().refresh()
        if not getattr(self, "_vault", None) or not self._vault.available:
            return
        unlocked = self._vault.unlocked
        initialized = self._vault.initialized
        self._set_status(
            _("Unlocked.") if unlocked
            else (_("Locked. Enter the vault password to view entries.") if initialized
                  else _("No vault yet. Create one to store encrypted credentials."))
        )
        self._lock_bar.set_message_type(Gtk.MessageType.INFO if unlocked else Gtk.MessageType.WARNING)
        self._unlock_secret.set_visible(initialized and not unlocked)
        self._unlock_button.set_visible(initialized and not unlocked)
        self._create_button.set_visible(not initialized)
        if unlocked:
            title, text = _("The vault is empty"), _("Add usernames and passwords; they are encrypted with the vault password.")
        elif initialized:
            title, text = _("The vault is locked"), _("Unlock it above to see and edit entries.")
        else:
            title, text = _("No vault yet"), _("Create a vault above to store encrypted credentials.")
        self.empty_state.set_text(title, text)

    def _set_status(self, text: str) -> None:
        self._status_label.set_text(text)

    # --------------------------------------------------------------- actions
    def _unlock(self) -> None:
        password = self._unlock_secret.get_text()
        if not password:
            self._set_status(_("Enter the vault password to unlock."))
            return
        try:
            ok = self._vault.unlock(password)
        except VaultError as exc:
            self._set_status(str(exc))
            return
        self._unlock_secret.set_text("")
        self._unlock_secret.set_invalid(not ok)
        if ok:
            self.refresh()
        else:
            self._set_status(_("Incorrect vault password."))

    def _create_master(self) -> None:
        password = NewSecretDialog(
            self,
            _("Create Vault"),
            _("Choose a vault password"),
            _("Entries are encrypted with this password. It cannot be recovered if you forget it."),
            accept_label=_("_Create"),
        ).ask()
        if not password:
            return
        try:
            self._vault.create_master(password)
        except VaultError as exc:
            show_message(self, _("Could not create the vault"), str(exc), level=Level.ERROR)
            return
        self.refresh()

    def _reset_vault(self) -> None:
        if not confirm(
            self,
            _("Reset the vault?"),
            _("All entries are deleted and a new vault password is set. This cannot be undone."),
            confirm_label=_("_Reset Vault"),
            destructive=True,
        ):
            return
        self._create_master()

    def _change_master(self) -> None:
        if not self._vault.initialized:
            self._create_master()
            return
        dialog = NewSecretDialog(
            self,
            _("Change Vault Password"),
            _("Change the vault password"),
            accept_label=_("_Change"),
            verify_current=lambda value: self._safe_unlock(value),
        )
        new_password = dialog.ask()
        if not new_password or not dialog.current_password:
            return
        try:
            if self._vault.change_master(dialog.current_password, new_password):
                self._set_status(_("Vault password changed."))
                self.refresh()
        except VaultError as exc:
            show_message(self, _("Could not change the vault password"), str(exc), level=Level.ERROR)

    def _safe_unlock(self, password: str) -> bool:
        try:
            return self._vault.unlock(password)
        except VaultError:
            return False

    def on_add(self) -> None:
        if not self._vault.unlocked:
            self._set_status(_("Unlock the vault first."))
            return
        new_entry = VaultEntryDialog(self).ask()
        if new_entry is None:
            return
        try:
            self._vault.add_entry(new_entry)
        except VaultError as exc:
            self._set_status(str(exc))
            return
        self.refresh()
        self.select_id(new_entry.id)

    def on_edit(self, item_id: str) -> None:
        existing = self._vault.get_entry(item_id)
        if not existing:
            return
        updated = VaultEntryDialog(self, existing).ask()
        if updated is None:
            return
        try:
            self._vault.update_entry(updated)
        except VaultError as exc:
            self._set_status(str(exc))
            return
        self.refresh()

    def on_delete(self, item_id: str) -> None:
        existing = self._vault.get_entry(item_id)
        if not existing or not confirm(
            self,
            _("Delete vault entry “{label}”?").format(label=existing.label),
            _("Hosts that use this entry will ask for a password again."),
            confirm_label=_("_Delete Entry"),
            destructive=True,
        ):
            return
        try:
            self._vault.delete_entry(item_id)
        except VaultError as exc:
            self._set_status(str(exc))
            return
        self.refresh()

    def _export(self) -> None:
        if not self._vault.unlocked:
            self._set_status(_("Unlock the vault first."))
            return
        target = choose_save_file(self, _("Export Vault"), current_name="vault-export.json")
        if not target:
            return
        password = NewSecretDialog(
            self, _("Export Vault"), _("Protect the export"), _("The exported file is encrypted with this password."),
            accept_label=_("_Export"),
        ).ask()
        if not password:
            return
        try:
            self._vault.export_encrypted(Path(target), password)
            self._set_status(_("Exported the vault to {path}.").format(path=target))
        except VaultError as exc:
            show_message(self, _("Export failed"), str(exc), level=Level.ERROR)

    def _import(self) -> None:
        if not self._vault.unlocked:
            self._set_status(_("Unlock the vault first."))
            return
        source = choose_open_file(self, _("Import Vault"))
        if not source:
            return
        password = prompt_text(self, _("Import Vault"), _("Password of the exported file:"), secret=True,
                               accept_label=_("_Import"))
        if not password:
            return
        try:
            ok = self._vault.import_encrypted(Path(source), password)
        except VaultError as exc:
            show_message(self, _("Import failed"), str(exc), level=Level.ERROR)
            return
        if ok:
            self._set_status(_("Imported entries from {path}.").format(path=source))
            self.refresh()
        else:
            show_message(self, _("Import failed"), _("Wrong password or not a vault export."), level=Level.ERROR)
