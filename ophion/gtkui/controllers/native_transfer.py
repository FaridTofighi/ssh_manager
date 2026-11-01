"""SSH Manager's own export / import (``*.sshm``) in the window.

Export: the dialog (scope, options, passphrase), a file name, then the
document is built from the store on the main thread (the store is not
thread-safe; ~0.1 s for 10 000 hosts) and encrypted / written in a worker
thread. A toast reports the result with *Details* (the export report).
Import: the wizard (``dialogs/native_import.py``); colours and smart groups are
saved after the database commit.
"""

from __future__ import annotations

import logging
import subprocess
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Callable, List, Optional

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # type: ignore  # noqa: E402

from ...exporters.native import ExportReport, build_export, render_export_report  # noqa: E402
from ...native_format import EXTENSION, default_file_name, dump_document, encrypt_document  # noqa: E402
from ...persistence import write_bytes_private  # noqa: E402
from ..constants import RowKind  # noqa: E402
from ..dialogs.files import choose_save_file  # noqa: E402
from ..dialogs.native_export import NativeExportDialog  # noqa: E402
from ..i18n import _, ngettext  # noqa: E402
from ..theme.tokens import Level  # noqa: E402

if TYPE_CHECKING:  # pragma: no cover
    from ...gtk_app import SSHManagerWindow

LOGGER = logging.getLogger(__name__)
_APP_VERSION: Optional[str] = None


def app_version() -> str:
    """``git describe`` of the source tree when there is one (cached), else ``""``."""

    global _APP_VERSION
    if _APP_VERSION is None:
        root = Path(__file__).resolve().parents[3]
        try:
            result = subprocess.run(["git", "-C", str(root), "describe", "--always", "--dirty"],
                                    capture_output=True, text=True, timeout=2, check=False)
            _APP_VERSION = result.stdout.strip() if result.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError):
            _APP_VERSION = ""
    return _APP_VERSION


def show_text(parent: Optional[Gtk.Window], title: str, text: str) -> None:
    """A read-only, copyable report."""

    from ..dialogs.base import BaseDialog, DialogSize

    dialog = BaseDialog(parent, title, size=DialogSize.LARGE, height=480)
    view = Gtk.TextView()
    view.set_editable(False)
    view.set_monospace(True)
    view.get_buffer().set_text(text)
    scroller = Gtk.ScrolledWindow()
    scroller.set_vexpand(True)
    scroller.add(view)
    dialog.add_body(scroller, expand=True)
    dialog.add_button(_("_Close"), Gtk.ResponseType.CLOSE)
    dialog.run_and_destroy()


class NativeTransferController:
    """``win.export-native`` / ``win.import-native`` and the tree's "Export Group…" / "Export Selected Hosts…"."""

    def __init__(self, win: "SSHManagerWindow") -> None:
        self.win = win
        self.worker: Optional[threading.Thread] = None
        self.last_report: Optional[ExportReport] = None
        self.choose_path: Callable[[str], Optional[str]] = self._choose_path
        self.ask_export: Callable[..., Optional[tuple]] = self._ask_export
        self.wizard = None

    # ================================================================ export
    def _store(self):
        store = getattr(self.win.manager, "store", None)
        if store is None:
            self.win._notify_info(_("Ophion exports are made from the Ophion inventory; this window shows a "
                                    "folder inventory. Use Export → To OpenSSH Config instead."))
        return store

    def _selected_group_id(self) -> tuple:
        kind, data = self.win.tree.current_item()
        if kind not in (RowKind.PARENT.value, RowKind.CHILD.value, RowKind.CHILD_FOLDER.value):
            return None, ""
        parent, child = data.get("parent", ""), data.get("child", "")
        store = getattr(self.win.manager, "store", None)
        path = [parent] + [part for part in child.split("/") if part]
        group = store.find_group(path) if store is not None else None
        return (group.id, " / ".join(path)) if group is not None else (None, "")

    def _selected_host_ids(self) -> List[str]:
        try:
            return [host.id for host in self.win.tree.selected_hosts() if getattr(host, "id", "")]
        except Exception:
            return []

    def export(self, scope: str = "all") -> None:
        """Ask, then write. ``scope`` preselects whole inventory / the selected group / the selected hosts."""

        store = self._store()
        if store is None or self.busy:
            return
        group_id, group_label = self._selected_group_id()
        host_ids = self._selected_host_ids()
        try:
            from ...smart_groups import load_smart_groups

            smart = load_smart_groups()
        except Exception:
            smart = []
        answer = self.ask_export(group_label=group_label, host_count=len(host_ids), scope=scope,
                                 has_smart_groups=bool(smart))
        if answer is None:
            return
        scope, options, passphrase = answer
        path = self.choose_path(default_file_name())
        if not path:
            return
        target = Path(path)
        if not target.suffix:
            target = target.with_suffix(EXTENSION)
        try:
            document, report = build_export(
                store, scope=scope, group_id=group_id, host_ids=host_ids, options=options,
                group_colors=self.win.colors.group_colors, host_colors=self.win.colors.host_colors,
                credential_for=self._credential_for,
                smart_groups=[{"name": g.name, "query": g.query} for g in smart], app_version=app_version())
        except LookupError as exc:
            self.win._notify_error(str(exc))
            return
        plain = dump_document(document)
        self.last_report = report

        def work() -> None:
            data = encrypt_document(plain, passphrase) if passphrase else plain
            write_bytes_private(target, data)

        self._run(work, lambda error: self._exported(target, report, error))

    def _ask_export(self, **kw) -> Optional[tuple]:
        dialog = NativeExportDialog(self.win, **kw)
        try:
            if not dialog.run() == Gtk.ResponseType.OK:
                return None
            return dialog.values()
        finally:
            dialog.forget()
            dialog.destroy()

    def _choose_path(self, name: str) -> Optional[str]:
        return choose_save_file(self.win, _("Export as Ophion File"), current_name=name)

    def _credential_for(self, host_id: str):
        try:
            return self.win._credential_store.credential_for_host(host_id)
        except Exception:  # a locked keyring: no reference
            return None

    @property
    def busy(self) -> bool:
        return self.worker is not None and self.worker.is_alive()

    def _run(self, work: Callable[[], None], done: Callable[[Optional[BaseException]], None]) -> None:
        def target() -> None:
            try:
                work()
                error = None
            except BaseException as exc:  # reported on the main thread
                error = exc
            GLib.idle_add(lambda: (done(error), False)[1])

        self.worker = threading.Thread(target=target, name="sshm-export", daemon=True)
        self.worker.start()

    def wait(self, timeout: float = 30.0) -> None:
        """Run the main loop until the worker is done (tests)."""

        import time

        deadline = time.monotonic() + timeout
        while self.busy and time.monotonic() < deadline:
            Gtk.main_iteration_do(False)
            time.sleep(0.002)
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)

    def _exported(self, target: Path, report: ExportReport, error: Optional[BaseException]) -> None:
        self.worker = None
        if error is not None:
            reason = error.strerror if isinstance(error, OSError) and error.strerror else str(error)
            LOGGER.warning("Ophion export failed (%s)", type(error).__name__)
            self.win._notify_error(_("The export could not be written: {reason}").format(reason=reason))
            return
        LOGGER.info("Ophion export: %d group(s), %d host(s)", report.groups, report.hosts)
        text = ngettext("Exported {n} host to {file}.", "Exported {n} hosts to {file}.", report.hosts).format(
            n=report.hosts, file=target.name)
        self.win._notify(text, Level.SUCCESS, action_label=_("_Details"),
                         action=lambda: show_text(self.win, _("Export Report"), render_export_report(report)))


    # ================================================================ import
    def import_file(self, path: Optional[str] = None) -> None:
        """The import wizard; afterwards the colours and smart groups are saved, the tree refreshed once."""

        from ...importers.native import ImportStatus
        from ..dialogs.native_import import NativeImportDialog
        from .mobaxterm_import import import_report_dir

        if self.wizard is not None:
            self.wizard.present()
            return
        if getattr(self.win.manager, "store", None) is None:
            self.win._notify_info(_("Ophion exports are imported into the Ophion inventory; this window "
                                    "shows a folder inventory."))
            return
        if self.win.manager.read_only:
            self.win.ensure_editable_inventory()  # explains why
            return
        self.wizard = NativeImportDialog(self.win, self.win.manager, self.win._credential_store,
                                         report_dir=import_report_dir(), group_colors=self.win.colors.group_colors,
                                         host_colors=self.win.colors.host_colors)
        try:
            if path:
                self.wizard.load_file(Path(path))
            self.wizard.run()
            if self.wizard.busy:  # closed while writing: wait for the rollback
                self.wizard.wait()
            report = self.wizard.report
        finally:
            self.wizard.destroy()
            self.wizard = None
        if report is None:
            return
        LOGGER.info("Ophion import: %s, %d host(s), %d group(s)", report.status.value, report.hosts,
                    report.groups)
        if report.status is ImportStatus.SUCCESS:
            warnings = self.apply_after_commit(report)
            self.win.tree.rebuild()
            self.win.tree.reveal_imported(report.parents)
            self.win._notify(ngettext("Imported {n} host from the Ophion export.",
                                      "Imported {n} hosts from the Ophion export.", report.hosts).format(
                n=report.hosts), Level.SUCCESS)
            for warning in warnings + list(report.warnings):
                self.win._notify_warning(warning)
        elif report.status is ImportStatus.FAILED:
            self.win._notify(_("The import failed; no changes were made."), Level.ERROR)

    def apply_after_commit(self, report) -> List[str]:
        """Colours (one UI-state save) and smart groups, after the database commit. Returns warnings."""

        warnings: List[str] = []
        if report.group_colors or report.host_colors:
            self.win.colors.group_colors.update(report.group_colors)
            self.win.colors.host_colors.update(report.host_colors)
            try:
                self.win._save_ui_state()
            except Exception as exc:  # the inventory import itself stays committed
                LOGGER.warning("Ophion import: the colours could not be saved (%s)", type(exc).__name__)
                warnings.append(_("The hosts were imported, but their colours could not be saved."))
        if report.smart_groups:
            from ...importers.native import merge_smart_groups
            from ...smart_groups import load_smart_groups, save_smart_groups

            merged, added = merge_smart_groups(load_smart_groups(), report.smart_groups)
            if added and not save_smart_groups(merged):
                warnings.append(_("The smart groups could not be saved."))
        return warnings


__all__ = ["NativeTransferController", "app_version", "show_text"]
