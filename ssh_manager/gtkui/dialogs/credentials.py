"""Dialogs for reusable credentials (username/password pairs)."""

from __future__ import annotations

from typing import Optional, Tuple

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # type: ignore  # noqa: E402

from ...credentials import Credential, CredentialStore  # noqa: E402
from ...secure_store import SecureStoreError  # noqa: E402
from ..constants import Icons  # noqa: E402
from ..i18n import _  # noqa: E402
from ..theme.tokens import Level  # noqa: E402
from ..validation import validate_required  # noqa: E402
from ..widgets.forms import FormSection, entry  # noqa: E402
from ..widgets.secret_entry import SecretEntry  # noqa: E402
from .base import BaseDialog, DialogSize, is_accept  # noqa: E402
from .confirm import confirm, show_message  # noqa: E402
from .list_manager import ListManagerDialog  # noqa: E402


class CredentialEditDialog(BaseDialog):
    """Add or edit a stored credential."""

    def __init__(self, parent: Gtk.Window, credential: Optional[Credential] = None) -> None:
        title = _("Edit Credential") if credential else _("New Credential")
        BaseDialog.__init__(self, parent, title, size=DialogSize.SMALL, resizable=False)
        self._editing = credential is not None

        section = FormSection()
        self.name_entry = entry(credential.name if credential else "", _("e.g. Production"))
        self._name_row = section.add(
            _("_Name"), self.name_entry, validator=lambda v: validate_required(v, _("Name"))
        )
        self.user_entry = entry(credential.username if credential else "", _("Username"))
        self._user_row = section.add(
            _("_Username"), self.user_entry, validator=lambda v: validate_required(v, _("Username"))
        )
        self.pass_secret = SecretEntry(_("Password"), generate=True)
        self.pass_entry = self.pass_secret.entry
        self._pass_row = section.add(
            _("_Password"),
            self.pass_secret,
            help_text=_("Leave empty to keep the current password.") if credential else "",
            validator=None if credential else (lambda v: validate_required(v, _("Password"))),
        )
        self.add_body(section)

        self.add_cancel()
        self.add_primary(_("_Save"))
        self.set_response_validator(lambda r: not is_accept(r) or section.validate())
        self.set_initial_focus(self.name_entry)
        self.show_all()

    def values(self) -> Optional[Tuple[str, str, Optional[str]]]:
        """``(name, username, password_or_None)`` or ``None`` when invalid."""

        name = self.name_entry.get_text().strip()
        user = self.user_entry.get_text().strip()
        password = self.pass_entry.get_text()
        valid = self._name_row.validate() & self._user_row.validate()
        if not self._editing and not password:
            self._pass_row.set_error(_("Password is required."))
            valid = False
        if not valid or not name or not user:
            return None
        return name, user, password or None


class CredentialManagerDialog(ListManagerDialog):
    """List, add, edit and delete reusable credentials."""

    def __init__(self, parent: Gtk.Window, store: CredentialStore) -> None:
        ListManagerDialog.__init__(
            self,
            parent,
            _("Credentials"),
            [_("Name"), _("Username")],
            empty_icon=Icons.CREDENTIALS,
            empty_title=_("No saved credentials"),
            empty_description=_("Save a username and password once and reuse it for many hosts."),
            add_label=_("_Add Credential…"),
        )
        self._store = store
        self.set_hint(_("Passwords are kept in the system keyring or the encrypted password file."))
        self.refresh()

    def load_rows(self):
        return [(cred.id, cred.name, cred.username) for cred in self._store.list()]

    def _show_store_error(self, message: str, exc: SecureStoreError) -> None:
        show_message(self, message, str(exc), level=Level.ERROR)

    def _run_editor(self, credential: Optional[Credential]) -> Optional[Tuple[str, str, Optional[str]]]:
        dialog = CredentialEditDialog(self, credential)
        try:
            if not is_accept(dialog.run()):
                return None
            return dialog.values()
        finally:
            dialog.destroy()

    def on_add(self) -> None:
        values = self._run_editor(None)
        if not values or values[2] is None:
            return
        name, user, password = values
        try:
            created = self._store.add(name, user, password)
        except SecureStoreError as exc:
            self._show_store_error(_("Could not save the credential."), exc)
            return
        self.refresh()
        self.select_id(created.id)

    def on_edit(self, item_id: str) -> None:
        cred = self._store.get(item_id)
        if not cred:
            return
        values = self._run_editor(cred)
        if not values:
            return
        name, user, password = values
        try:
            self._store.update(cred.id, name, user, password=password)
        except SecureStoreError as exc:
            self._show_store_error(_("Could not update the credential."), exc)
            return
        self.refresh()

    def on_delete(self, item_id: str) -> None:
        cred = self._store.get(item_id)
        if not cred:
            self.refresh()
            return
        if not confirm(
            self,
            _("Delete credential “{name}”?").format(name=cred.name),
            _("The stored username and password are removed and any hosts using it are unbound."),
            confirm_label=_("_Delete Credential"),
            destructive=True,
        ):
            return
        try:
            self._store.delete(item_id)
        except SecureStoreError as exc:
            self._show_store_error(_("Could not delete the credential."), exc)
            return
        self.refresh()
