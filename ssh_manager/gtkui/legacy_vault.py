"""Moving the removed Credential Vault into Credentials: the dialogs (see ``ssh_manager/legacy_vault.py``).

At start-up, when ``vault.json`` exists (one check), the user is told that the
vault was removed and asked to move its entries now or later. The master
password is tried silently first (many users used the same one), else the
vault password is asked. After a verified conversion the user decides whether
the old file is deleted. *Later* changes nothing and asks again at the next
start; the Credentials manager offers **Import old vault…** meanwhile. Like
the module it calls, this file exists only for the migration.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Callable, List, Optional

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # type: ignore  # noqa: E402

from ..legacy_vault import (  # noqa: E402
    ConversionReport,
    LegacyEntry,
    LegacyVaultError,
    convert_vault,
    legacy_vault_path,
    read_legacy_vault,
    secure_delete_vault_files,
)
from .dialogs.confirm import confirm, show_message  # noqa: E402
from .dialogs.secrets import UnlockDialog  # noqa: E402
from .i18n import _, ngettext  # noqa: E402
from .theme.tokens import Level  # noqa: E402

if TYPE_CHECKING:  # pragma: no cover
    from ..gtk_app import SSHManagerWindow

LOGGER = logging.getLogger(__name__)


class LegacyMigration:
    """Offers the move once per start and runs it (tests replace the ``ask_*`` callables)."""

    def __init__(self, win: "SSHManagerWindow") -> None:
        self.win = win
        self.offered = False
        self.ask_move: Callable[[], bool] = self._ask_move
        self.ask_password: Callable[[Callable[[str], bool]], Optional[str]] = self._ask_password
        self.ask_delete: Callable[[ConversionReport], bool] = self._ask_delete
        self.message: Callable[[str, str, bool], None] = self._message

    @staticmethod
    def pending() -> bool:
        return legacy_vault_path().exists()

    def offer_at_startup(self) -> bool:
        """At most once per session; ``False`` so it can be an idle callback."""

        if self.offered or not self.pending():
            return False
        self.offered = True
        if self.ask_move():
            self.run()
        return False

    def run(self, parent: Optional[Gtk.Window] = None) -> Optional[ConversionReport]:
        path = legacy_vault_path()
        if not path.exists():
            self.message(_("There is nothing to move."), "", False)
            return None
        entries: List[LegacyEntry] = []
        try:
            master = getattr(self.win, "_master_password", None)
            silent = read_legacy_vault(path, master) if master else None
            if silent is not None:
                entries = silent
            else:
                found: List[List[LegacyEntry]] = []

                def verify(text: str) -> bool:
                    result = read_legacy_vault(path, text)
                    if result is None:
                        return False
                    found.append(result)
                    return True

                if self.ask_password(verify) is None or not found:
                    return None  # "Later": nothing changes
                entries = found[0]
        except LegacyVaultError as exc:
            self.message(_("The old vault cannot be read"), str(exc), True)
            return None
        try:
            report = convert_vault(entries, self.win._credential_store, self.win.manager,
                                   resolve=self._resolve)
        except LegacyVaultError as exc:
            self.message(_("The entries could not be moved"), str(exc), True)
            return None
        except Exception as exc:  # a store or inventory problem: the old file stays
            LOGGER.warning("old vault: moving failed (%s)", type(exc).__name__)
            self.message(_("The entries could not be moved"), _("Nothing was deleted; try again later."), True)
            return None
        self.win.tree.rebuild()
        if self.ask_delete(report):
            if not secure_delete_vault_files(path):
                self.message(_("The old vault file could not be deleted"), str(path), True)
        LOGGER.info("old vault: %d entries moved, %d hosts updated", report.entries, report.hosts_rebound)
        self.win._notify(ngettext("Moved {n} entry to Credentials.", "Moved {n} entries to Credentials.",
                                  report.entries).format(n=report.entries), Level.SUCCESS)
        return report

    def _resolve(self, host):
        resolution = self.win.creds.resolve_password(host)
        return resolution.password, resolution.source

    # ---------------------------------------------------------------- dialogs
    def _ask_move(self) -> bool:
        dialog = Gtk.MessageDialog(transient_for=self.win, modal=True, message_type=Gtk.MessageType.INFO,
                                   text=_("The Credential Vault has been removed"))
        dialog.format_secondary_text(_(
            "Your vault entries will be moved to Credentials, and the hosts that used them keep working with the "
            "same passwords. Nothing is deleted before the move is verified."))
        dialog.add_button(_("_Later"), Gtk.ResponseType.CANCEL)
        dialog.add_button(_("_Move Now"), Gtk.ResponseType.OK)
        dialog.set_default_response(Gtk.ResponseType.OK)
        try:
            return dialog.run() == Gtk.ResponseType.OK
        finally:
            dialog.destroy()

    def _ask_password(self, verify: Callable[[str], bool]) -> Optional[str]:
        return UnlockDialog(self.win, _("Move the Old Vault"), _("Enter the vault password"),
                            _("The password of the removed Credential Vault (often the same as the master password)."),
                            verify, accept_label=_("_Move"),
                            error_text=_("Incorrect vault password. Please try again.")).ask()

    def _ask_delete(self, report: ConversionReport) -> bool:
        text = ngettext("Moved {n} entry to Credentials ({hosts} hosts updated). Delete the old vault file?",
                        "Moved {n} entries to Credentials ({hosts} hosts updated). Delete the old vault file?",
                        report.entries).format(n=report.entries, hosts=report.hosts_rebound)
        detail = report.summary() + "\n\n" + _("The file is overwritten before it is deleted (best effort).")
        return confirm(self.win, text, detail, confirm_label=_("_Delete"), default_confirm=True)

    def _message(self, title: str, detail: str, error: bool) -> None:
        show_message(self.win, title, detail, level=Level.ERROR if error else Level.INFO)

    # --------------------------------------------------- credentials manager
    def add_import_button(self, dialog) -> Optional[Gtk.Button]:
        """**Import old vault…** in the Credentials manager, while ``vault.json`` exists."""

        if not self.pending():
            return None
        button = Gtk.Button.new_with_mnemonic(_("Import _old vault…"))
        button.set_tooltip_text(_("Move the entries of the removed Credential Vault into Credentials"))

        def clicked(_button) -> None:
            if self.run(dialog) is not None:
                dialog.refresh()
                button.set_visible(self.pending())

        button.connect("clicked", clicked)
        dialog.toolbar.pack_start(button, False, False, 0)
        button.show()
        return button

    # ------------------------------------------------- Preferences → Security
    def move_button(self, parent: Optional[Gtk.Window], on_done: Callable[[], None]) -> Optional[Gtk.Button]:
        """**Move the Old Vault…**, while ``vault.json`` exists (``on_done`` refreshes the page)."""

        if not self.pending():
            return None
        button = Gtk.Button.new_with_mnemonic(_("Move the Old _Vault…"))
        button.set_tooltip_text(_("Move the entries of the removed Credential Vault into Credentials"))
        button.connect("clicked", lambda _b: (self.run(parent), on_done()))
        return button


__all__ = ["LegacyMigration"]
