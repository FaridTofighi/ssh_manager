"""Dialogs for reusable credentials (username/password pairs)."""

from __future__ import annotations

from typing import Optional, Tuple

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # type: ignore  # noqa: E402

from ...credentials import Credential  # noqa: E402
from ..i18n import _  # noqa: E402
from ..validation import validate_required  # noqa: E402
from ..widgets.forms import FormSection, entry  # noqa: E402
from ..widgets.secret_entry import SecretEntry  # noqa: E402
from .base import BaseDialog, DialogSize, is_accept  # noqa: E402


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
