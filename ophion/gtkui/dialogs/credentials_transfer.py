"""Export and import shared credentials (``*.sshcred``) from the Credentials manager.

Export: a passphrase typed twice (with a strength hint), then a file name; the
file is always encrypted. Import: the file, its passphrase (a wrong one asks
again), then the credentials with a check box each and what to do when a name
is already used. Nothing is written before *Import*.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # type: ignore  # noqa: E402

from ...credentials_export import (  # noqa: E402
    EXTENSION,
    CredentialExportError,
    ExportedCredential,
    export_credentials,
    import_credentials,
    read_credentials_export,
)
from ...secure_store import SecureStoreError  # noqa: E402
from ..i18n import _, ngettext  # noqa: E402
from ..widgets.forms import FormSection  # noqa: E402
from ..widgets.icon_button import set_accessible_name  # noqa: E402
from ..widgets.secret_entry import SecretEntry  # noqa: E402
from .base import BaseDialog, DialogSize, is_accept  # noqa: E402
from .files import choose_open_file, choose_save_file  # noqa: E402
from .native_export import MIN_PASSPHRASE, passphrase_strength  # noqa: E402


class PassphraseDialog(BaseDialog):
    """A new export passphrase (typed twice) or, with ``confirm=False``, the passphrase of a file."""

    def __init__(self, parent: Optional[Gtk.Window], title: str, *, confirm: bool, message: str = "") -> None:
        BaseDialog.__init__(self, parent, title, size=DialogSize.SMALL, resizable=False)
        self._confirm = confirm
        section = FormSection("", message)
        self.first = SecretEntry(_("Passphrase"))
        set_accessible_name(self.first.entry, _("Passphrase"))
        section.add_widget(self.first)
        self.second = SecretEntry(_("Repeat the passphrase"))
        set_accessible_name(self.second.entry, _("Repeat the passphrase"))
        if confirm:
            section.add_widget(self.second)
        self.strength = Gtk.Label()
        self.strength.set_xalign(0.0)
        self.strength.get_style_context().add_class("form-help")
        if confirm:
            section.add_widget(self.strength)
        self.error = Gtk.Label()
        self.error.set_xalign(0.0)
        self.error.set_line_wrap(True)
        self.error.get_style_context().add_class("form-error")
        section.add_widget(self.error)
        self.add_body(section)
        self.first.entry.connect("changed", lambda *_a: self.strength.set_text(
            passphrase_strength(self.first.get_text())[1]))
        self.first.entry.set_activates_default(True)
        self.second.entry.set_activates_default(True)
        self.add_cancel()
        self.add_primary(_("_Continue"))
        self.set_initial_focus(self.first.entry)
        self.set_response_validator(lambda r: not is_accept(r) or self._valid())

    def _valid(self) -> bool:
        first = self.first.get_text()
        if self._confirm and len(first) < MIN_PASSPHRASE:
            self.error.set_text(_("The passphrase is too short."))
            return False
        if self._confirm and first != self.second.get_text():
            self.error.set_text(_("The two passphrases differ."))
            return False
        if not first:
            self.error.set_text(_("Enter the passphrase."))
            return False
        return True

    def ask(self) -> Optional[str]:
        self.show_all()
        try:
            return self.first.get_text() if is_accept(int(self.run())) else None
        finally:
            for entry in (self.first, self.second):
                entry.set_text("")
            self.destroy()


class CredentialImportDialog(BaseDialog):
    """The credentials of a file, each with a check box, and the name-collision policy."""

    def __init__(self, parent: Optional[Gtk.Window], items: Sequence[ExportedCredential]) -> None:
        BaseDialog.__init__(self, parent, _("Import Credentials"), size=DialogSize.MEDIUM, height=420)
        self.items = list(items)
        self.model = Gtk.ListStore(bool, str, str, int)
        for index, item in enumerate(self.items):
            self.model.append([True, item.name, item.username, index])
        view = Gtk.TreeView(model=self.model)
        set_accessible_name(view, _("Credentials in the file"))
        toggle = Gtk.CellRendererToggle()
        toggle.connect("toggled", lambda _c, path: self.model.set_value(
            self.model.get_iter(path), 0, not self.model[path][0]))
        view.append_column(Gtk.TreeViewColumn("", toggle, active=0))
        for title, column in ((_("Name"), 1), (_("Username"), 2)):
            view.append_column(Gtk.TreeViewColumn(title, Gtk.CellRendererText(), text=column))
        scroller = Gtk.ScrolledWindow()
        scroller.set_vexpand(True)
        scroller.set_shadow_type(Gtk.ShadowType.IN)
        scroller.add(view)
        self.add_body(scroller, expand=True)
        section = FormSection()
        self.policy = Gtk.ComboBoxText()
        for value, label in (("rename", _("Import it with a number (“… (2)”)")), ("skip", _("Skip it")),
                             ("reuse", _("Keep the existing one when username and password are the same"))):
            self.policy.append(value, label)
        self.policy.set_active_id("rename")
        section.add(_("When the _name is already used"), self.policy)
        self.add_body(section)
        self.add_cancel()
        self.add_primary(_("_Import"))

    def selected(self) -> List[ExportedCredential]:
        return [self.items[row[3]] for row in self.model if row[0]]


def run_export(parent: Optional[Gtk.Window], credential_store, *,
               ask_passphrase: Optional[Callable[[], Optional[str]]] = None,
               choose_path: Optional[Callable[[], Optional[str]]] = None) -> Optional[int]:
    """Export the shared credentials; returns how many were written (``None``: cancelled or failed)."""

    if not credential_store.list():
        _message(parent, _("There are no credentials to export."))
        return None
    passphrase = (ask_passphrase or (lambda: PassphraseDialog(
        parent, _("Protect the Export"), confirm=True,
        message=_("The file is encrypted with this passphrase; you need it to import the credentials. "
                  "It is not your master password and is not stored."))
        .ask()))()
    if passphrase is None:
        return None
    path = (choose_path or (lambda: choose_save_file(parent, _("Export Credentials"),
                                                     current_name="credentials" + EXTENSION)))()
    if not path:
        return None
    target = Path(path)
    if not target.suffix:
        target = target.with_suffix(EXTENSION)
    try:
        count = export_credentials(credential_store, passphrase, target)
    except (OSError, SecureStoreError) as exc:
        reason = exc.strerror if isinstance(exc, OSError) and exc.strerror else str(exc)
        _message(parent, _("The export could not be written: {reason}").format(reason=reason), error=True)
        return None
    _message(parent, ngettext("Exported {n} credential to {file}.", "Exported {n} credentials to {file}.",
                              count).format(n=count, file=target.name))
    return count


def run_import(parent: Optional[Gtk.Window], credential_store, *,
               choose_path: Optional[Callable[[], Optional[str]]] = None,
               ask_passphrase: Optional[Callable[[str], Optional[str]]] = None,
               choose_items: Optional[Callable[[Sequence[ExportedCredential]], Optional[Tuple[list, str]]]] = None):
    """Import from a ``*.sshcred`` file; returns the :class:`ImportResult` (``None``: cancelled or failed)."""

    path = (choose_path or (lambda: choose_open_file(parent, _("Import Credentials"), filters=[
        (_("Ophion credentials (*.sshcred)"), ["*" + EXTENSION])])))()
    if not path:
        return None
    error = ""
    while True:
        passphrase = (ask_passphrase or (lambda message: PassphraseDialog(
            parent, _("Passphrase of the Export"), confirm=False, message=message).ask()))(
            error or _("Enter the passphrase the file was exported with."))
        if passphrase is None:
            return None
        try:
            items = read_credentials_export(Path(path), passphrase)
            break
        except CredentialExportError as exc:
            if exc.code != "wrong_passphrase":
                _message(parent, exc.message, error=True)
                return None
            error = exc.message
    if not items:
        _message(parent, _("The file contains no credentials."))
        return None

    def default_choose(found):
        dialog = CredentialImportDialog(parent, found)
        dialog.show_all()
        try:
            return (dialog.selected(), dialog.policy.get_active_id()) if is_accept(int(dialog.run())) else None
        finally:
            dialog.destroy()

    chosen = (choose_items or default_choose)(items)
    if not chosen or not chosen[0]:
        return None
    try:
        result = import_credentials(credential_store, chosen[0], chosen[1])
    except SecureStoreError as exc:
        _message(parent, _("The credentials could not be saved: {reason}").format(reason=exc), error=True)
        return None
    _message(parent, ngettext("Imported {n} credential.", "Imported {n} credentials.", len(result.created)).format(
        n=len(result.created)))
    return result


def _message(parent: Optional[Gtk.Window], text: str, *, error: bool = False) -> None:
    from ..theme.tokens import Level
    from .confirm import show_message

    show_message(parent, text, "", level=Level.ERROR if error else Level.INFO)


__all__ = ["run_export", "run_import", "PassphraseDialog", "CredentialImportDialog"]
