"""Opens the MobaXterm import wizard and shows the result in the window."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ...importers.mobaxterm import ImportStatus
from ...settings import _default_data_dir
from ..dialogs.mobaxterm_import import MobaXtermImportDialog
from ..i18n import _
from ..theme.tokens import Level

if TYPE_CHECKING:  # pragma: no cover
    from ...gtk_app import SSHManagerWindow

LOGGER = logging.getLogger(__name__)


def import_report_dir():
    """Where import reports go (``<data dir>/imports``)."""

    return _default_data_dir() / "imports"


class MobaXtermImportController:
    """``win.import-mobaxterm``: run the wizard, then refresh the tree once and select the result."""

    def __init__(self, win: "SSHManagerWindow") -> None:
        self.win = win
        self.dialog = None

    def open(self) -> None:
        if self.dialog is not None:  # one wizard at a time
            self.dialog.present()
            return
        if getattr(self.win.manager, "store", None) is None:
            self.win._notify_info(_("MobaXterm imports go into the SSH Manager inventory; this window shows a "
                                    "folder inventory."))
            return
        if self.win.manager.read_only:
            self.win.ensure_editable_inventory()  # explains why
            return
        self.dialog = MobaXtermImportDialog(self.win, self.win.manager, self.win._credential_store,
                                            report_dir=import_report_dir())
        try:
            self.dialog.run()
            if self.dialog.busy:  # closed while writing: wait for the rollback to finish
                self.dialog.wait()
            report = self.dialog.report
        finally:
            self.dialog.destroy()
            self.dialog = None
        if report is None:
            return
        # Counts only: never names or values from the file in the log.
        LOGGER.info("MobaXterm import: %s, %d host(s), %d group(s)", report.status.value, report.hosts, report.groups)
        if report.status is ImportStatus.SUCCESS:
            self.win.tree.rebuild()
            if report.parents:
                self.win.tree.select_parent_group(report.parents[0])
            self.win._notify(_("Imported {hosts} host(s) from MobaXterm.").format(hosts=report.hosts), Level.SUCCESS)
        elif report.status is ImportStatus.FAILED:
            self.win._notify(_("MobaXterm import failed; no changes were made."), Level.ERROR)
