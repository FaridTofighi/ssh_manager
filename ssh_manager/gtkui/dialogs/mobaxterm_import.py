"""Import from MobaXterm: a five-step wizard over :mod:`ssh_manager.importers.mobaxterm`.

1. **File** – pick a ``*.mxtsessions`` export; parsed in a worker thread.
2. **Destination & options** – new parent groups or one existing parent, the
   conflict policy, empty folders, duplicate skipping, and a checkable folder
   tree (unticking a folder excludes its branch).
3. **Credentials** – every username found in the selected SSH sessions, each
   mapped to an existing credential (or None / a new one); a default credential
   pre-fills the rows that were not set by hand.
4. **Review** – what will be imported and skipped, the mapping, a copyable report.
5. **Result** – the commit runs in a worker thread (cancellable; a cancel or any
   failure rolls everything back), then the outcome and the saved report.

The dialog changes nothing until *Import*; the caller refreshes the tree when
:attr:`report` says the import succeeded.
"""

from __future__ import annotations

import threading
from enum import IntEnum
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk  # type: ignore  # noqa: E402

from ...importers.mobaxterm import (  # noqa: E402
    RDP_TYPE,
    SSH_TYPE,
    TRANSFER_TYPES,
    TYPE_NAMES,
    ConflictPolicy,
    CredentialMissingError,
    Destination,
    ExistingInventory,
    FolderNode,
    ImportJob,
    ImportOptions,
    ImportPlan,
    ImportReport,
    ImportStatus,
    IssueCode,
    MxtFileError,
    MxtParseResult,
    apply_credential_mapping,
    build_folder_tree,
    commit,
    count_types,
    parse_mxtsessions,
    plan_import,
    render_report,
    save_report,
)
from ...secure_store import SecureStoreError  # noqa: E402
from ..i18n import _, ngettext  # noqa: E402
from ..theme.tokens import Space  # noqa: E402
from ..widgets.forms import FormSection  # noqa: E402
from ..widgets.icon_button import set_accessible_name  # noqa: E402
from .base import BaseDialog, DialogSize  # noqa: E402
from .confirm import confirm  # noqa: E402
from .credentials import CredentialEditDialog  # noqa: E402
from .files import choose_open_file  # noqa: E402


class Step(IntEnum):
    FILE = 0
    OPTIONS = 1
    CREDENTIALS = 2
    REVIEW = 3
    RESULT = 4


RESPONSE_BACK = 1
RESPONSE_NEXT = 2
NONE_ID = "__none__"
NEW_ID = "__new__"
STEP_TITLES = {
    Step.FILE: _("File"),
    Step.OPTIONS: _("Destination and options"),
    Step.CREDENTIALS: _("Credentials"),
    Step.REVIEW: _("Review"),
    Step.RESULT: _("Result"),
}
COL_ACTIVE, COL_LABEL, COL_COUNT, COL_PATH, COL_INCONSISTENT = range(5)


def _human_size(size: int) -> str:
    if size < 1024:
        return ngettext("{n} byte", "{n} bytes", size).format(n=size)
    if size < 1024 * 1024:
        return _("{n:.1f} KB").format(n=size / 1024.0)
    return _("{n:.1f} MB").format(n=size / (1024.0 * 1024.0))


def _wrapped(text: str = "", css: str = "") -> Gtk.Label:
    label = Gtk.Label(label=text)
    label.set_xalign(0.0)
    label.set_line_wrap(True)
    label.set_selectable(False)
    if css:
        label.get_style_context().add_class(css)
    return label


class UsernameRow:
    """One "username in file → credential" row of the Credentials step."""

    def __init__(self, key: str, combo: Gtk.ComboBoxText, status: Gtk.Label) -> None:
        self.key = key
        self.combo = combo
        self.status = status
        self.manual = False
        self.last_id: Optional[str] = None  # last real choice (never "Create new…")

    @property
    def decided(self) -> bool:
        return self.combo.get_active_id() not in (None, NEW_ID)

    @property
    def credential_id(self) -> Optional[str]:
        active = self.combo.get_active_id()
        return None if active in (None, NONE_ID, NEW_ID) else active


class MobaXtermImportDialog(BaseDialog):
    """The import wizard (modal). After it closes, :attr:`report` holds the outcome."""

    def __init__(
        self,
        parent: Optional[Gtk.Window],
        manager,
        credential_store,
        *,
        report_dir: Path,
        choose_file: Optional[Callable[[Gtk.Window], Optional[str]]] = None,
    ) -> None:
        BaseDialog.__init__(self, parent, _("Import from MobaXterm"), size=DialogSize.LARGE, height=620)
        self.get_style_context().add_class("import-wizard")
        self._manager = manager
        self._store = credential_store
        self._report_dir = report_dir
        self._choose_file = choose_file or self._default_choose_file
        self.source_path: Optional[Path] = None
        self.result: Optional[MxtParseResult] = None
        self.plan: Optional[ImportPlan] = None
        self.job: Optional[ImportJob] = None
        self.report: Optional[ImportReport] = None
        self.report_text = ""
        self.step = Step.FILE
        self._worker: Optional[threading.Thread] = None
        self._commit_lock = threading.Lock()
        self._cancel = threading.Event()
        self._close_when_done = False
        self._updating = False
        self._rows: Dict[str, UsernameRow] = {}
        self._decisions: Dict[str, Tuple[Optional[str], bool]] = {}  # key → (combo id, manual)
        self._default_id: Optional[str] = None

        self.step_label = _wrapped("", "form-help")
        self.add_body(self.step_label)
        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.NONE)
        self.stack.set_vexpand(True)
        self.add_body(self.stack, expand=True)
        self.stack.add_named(self._build_file_page(), "file")
        self.stack.add_named(self._build_options_page(), "options")
        self.stack.add_named(self._build_credentials_page(), "credentials")
        self.stack.add_named(self._build_review_page(), "review")
        self.stack.add_named(self._build_result_page(), "result")

        self.cancel_button = self.add_cancel()
        self.back_button = self.add_secondary(_("_Back"), RESPONSE_BACK)
        self.next_button = self.add_primary(_("_Next"), RESPONSE_NEXT)
        for button in (self.cancel_button, self.back_button):
            button.set_no_show_all(True)  # _go() decides; show_all() must not bring them back
        set_accessible_name(self.back_button, _("Back"))
        set_accessible_name(self.next_button, _("Next step"))
        self.set_response_validator(self._on_navigate)
        self.connect("response", self._on_guard_response)
        self.connect("delete-event", self._on_delete_event)
        self.set_initial_focus(self.choose_button)
        self.show_all()
        self._go(Step.FILE)

    # ================================================================ pages
    def _build_file_page(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=Space.M)
        section = FormSection(
            _("MobaXterm sessions file"),
            _("Export your sessions from MobaXterm (a *.mxtsessions file) and choose it here. SSH, FTP, SFTP and RDP "
              "sessions and their folders are imported; passwords are never part of an export, and nothing is connected to."),
        )
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.S)
        self.choose_button = Gtk.Button.new_with_mnemonic(_("_Choose File…"))
        self.choose_button.connect("clicked", lambda *_a: self.choose_file())
        set_accessible_name(self.choose_button, _("Choose a MobaXterm sessions file"))
        row.pack_start(self.choose_button, False, False, 0)
        self.file_spinner = Gtk.Spinner()
        set_accessible_name(self.file_spinner, _("Reading the file"))
        row.pack_start(self.file_spinner, False, False, 0)
        self.file_label = _wrapped(_("No file chosen."))
        row.pack_start(self.file_label, True, True, 0)
        section.add_widget(row)
        self.file_error = _wrapped("", "form-error")
        self.file_error.set_no_show_all(True)
        section.add_widget(self.file_error)
        self.file_summary = _wrapped("", "form-help")
        section.add_widget(self.file_summary)
        page.pack_start(section, False, False, 0)
        return page

    def _build_options_page(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=Space.M)
        section = FormSection(_("Where to import"),
                              _("Every MobaXterm folder becomes a group with the same name, at any depth."))
        self.new_parents_radio = Gtk.RadioButton.new_with_mnemonic_from_widget(
            None, _("Create _top-level groups (one per top-level MobaXterm folder)"))
        self.existing_parent_radio = Gtk.RadioButton.new_with_mnemonic_from_widget(
            self.new_parents_radio, _("Import under an _existing group:"))
        section.add_widget(self.new_parents_radio)
        parent_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.S)
        parent_row.pack_start(self.existing_parent_radio, False, False, 0)
        self.parent_combo = Gtk.ComboBoxText()  # any group, shown as its path
        set_accessible_name(self.parent_combo, _("Existing group"))
        parent_row.pack_start(self.parent_combo, False, False, 0)
        section.add_widget(parent_row)
        self.existing_parent_radio.connect("toggled", lambda *_a: self._sync_options())
        self.parent_combo.connect("changed", lambda *_a: self._sync_options())

        self.policy_combo = Gtk.ComboBoxText()
        for policy, label in ((ConflictPolicy.SKIP, _("Skip it (leave the existing host as it is)")),
                              (ConflictPolicy.RENAME, _("Import a copy named “… (2)”")),
                              (ConflictPolicy.OVERWRITE, _("Update the existing host"))):
            self.policy_combo.append(policy.value, label)
        self.policy_combo.set_active_id(ConflictPolicy.SKIP.value)
        set_accessible_name(self.policy_combo, _("When a host already exists"))
        section.add(_("When a host already _exists"), self.policy_combo,
                    help_text=_("The same host: same group, protocol, host name, port and user. Importing the "
                                "same file again with “Skip” changes nothing."))
        self.skip_empty_check = Gtk.CheckButton.new_with_mnemonic(_("Skip _empty folders"))
        section.add_widget(self.skip_empty_check)
        self.transfer_check = Gtk.CheckButton.new_with_mnemonic(_("Import _FTP/SFTP sessions"))
        self.transfer_check.set_active(True)
        self.transfer_check.set_tooltip_text(_("FTP sessions are imported as plain FTP; check whether the server "
                                               "uses FTPS."))
        self.transfer_check.connect("toggled", lambda *_a: self._on_transfer_toggled())
        section.add_widget(self.transfer_check)
        self.rdp_check = Gtk.CheckButton.new_with_mnemonic(_("Import _RDP sessions"))
        self.rdp_check.set_active(True)
        self.rdp_check.set_tooltip_text(_("Host, port and user are imported; display and device options keep "
                                          "their defaults."))
        self.rdp_check.connect("toggled", lambda *_a: self._on_transfer_toggled())
        section.add_widget(self.rdp_check)
        page.pack_start(section, False, False, 0)

        tree_section = FormSection(_("Folders to import"), _("Untick a folder to leave it and everything in it out."))
        page.pack_start(tree_section, False, False, 0)
        self.folder_store = Gtk.TreeStore(bool, str, int, object, bool)
        self.folder_view = Gtk.TreeView(model=self.folder_store)
        self.folder_view.set_headers_visible(False)
        set_accessible_name(self.folder_view, _("Folders to import"))
        toggle = Gtk.CellRendererToggle()
        toggle.connect("toggled", self._on_folder_toggled)
        column = Gtk.TreeViewColumn()
        column.pack_start(toggle, False)
        column.add_attribute(toggle, "active", COL_ACTIVE)
        column.add_attribute(toggle, "inconsistent", COL_INCONSISTENT)
        text = Gtk.CellRendererText()
        column.pack_start(text, True)
        column.add_attribute(text, "text", COL_LABEL)
        count = Gtk.CellRendererText()
        count.set_property("xalign", 1.0)
        column.pack_end(count, False)
        column.set_cell_data_func(count, lambda _c, cell, model, it, _d=None: cell.set_property(
            "text", str(model.get_value(it, COL_COUNT))))
        self.folder_view.append_column(column)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)
        scroller.set_shadow_type(Gtk.ShadowType.IN)
        scroller.add(self.folder_view)
        page.pack_start(scroller, True, True, 0)
        self.selection_label = _wrapped("", "form-help")
        page.pack_start(self.selection_label, False, False, 0)
        return page

    def _build_credentials_page(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=Space.M)
        section = FormSection(
            _("Credentials for the usernames in the file"),
            _("A username in a MobaXterm export is often only the label of a credential saved in MobaXterm. "
              "Choose the SSH Manager credential that replaces each one: its username is used for those hosts, "
              "and the hosts are bound to it."),
        )
        self.default_combo = Gtk.ComboBoxText()
        set_accessible_name(self.default_combo, _("Default credential"))
        self.default_combo.connect("changed", self._on_default_changed)
        section.add(_("_Default credential for usernames that are not mapped below"), self.default_combo)
        page.pack_start(section, False, False, 0)

        self.empty_store_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=Space.S)
        self.empty_store_box.set_no_show_all(True)
        self.empty_store_box.pack_start(_wrapped(
            _("No credentials exist yet. Create one now, or import without credentials (the usernames that are "
              "real login names are kept)."), "form-help"), False, False, 0)
        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.S)
        self.create_credential_button = Gtk.Button.new_with_mnemonic(_("C_reate New Credential…"))
        self.create_credential_button.connect("clicked", lambda *_a: self._create_credential(None))
        self.without_credentials_button = Gtk.Button.new_with_mnemonic(_("Import _without Credentials"))
        self.without_credentials_button.connect("clicked", lambda *_a: self._set_default(NONE_ID))
        buttons.pack_start(self.create_credential_button, False, False, 0)
        buttons.pack_start(self.without_credentials_button, False, False, 0)
        self.empty_store_box.pack_start(buttons, False, False, 0)
        for child in self.empty_store_box.get_children():
            child.show_all()
        page.pack_start(self.empty_store_box, False, False, 0)

        self.rows_grid = Gtk.Grid(column_spacing=Space.M, row_spacing=Space.XS)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)
        scroller.add(self.rows_grid)
        page.pack_start(scroller, True, True, 0)
        self.credentials_error = _wrapped("", "form-error")
        self.credentials_error.set_no_show_all(True)
        page.pack_start(self.credentials_error, False, False, 0)
        self.credentials_hint = _wrapped("", "form-help")
        page.pack_start(self.credentials_hint, False, False, 0)
        return page

    def _build_review_page(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=Space.M)
        self.review_summary = _wrapped("")
        self.review_summary.get_style_context().add_class("section-title")
        page.pack_start(self.review_summary, False, False, 0)
        self.review_notes = _wrapped("", "form-help")
        page.pack_start(self.review_notes, False, False, 0)
        self.review_text = Gtk.TextView()
        self.review_text.set_editable(False)
        self.review_text.set_cursor_visible(False)
        self.review_text.set_monospace(True)
        self.review_text.get_style_context().add_class("mono")
        set_accessible_name(self.review_text, _("Import report"))
        scroller = Gtk.ScrolledWindow()
        scroller.set_vexpand(True)
        scroller.set_shadow_type(Gtk.ShadowType.IN)
        scroller.add(self.review_text)
        page.pack_start(scroller, True, True, 0)
        self.copy_button = Gtk.Button.new_with_mnemonic(_("_Copy Report"))
        self.copy_button.set_halign(Gtk.Align.START)
        self.copy_button.connect("clicked", lambda *_a: self._copy_report())
        page.pack_start(self.copy_button, False, False, 0)
        return page

    def _build_result_page(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=Space.M)
        self.progress = Gtk.ProgressBar()
        self.progress.set_show_text(True)
        set_accessible_name(self.progress, _("Import progress"))
        page.pack_start(self.progress, False, False, 0)
        self.result_title = _wrapped("")
        self.result_title.get_style_context().add_class("section-title")
        page.pack_start(self.result_title, False, False, 0)
        self.result_detail = _wrapped("", "form-help")
        page.pack_start(self.result_detail, False, False, 0)
        self.open_report_button = Gtk.Button.new_with_mnemonic(_("_Open Report"))
        self.open_report_button.set_halign(Gtk.Align.START)
        self.open_report_button.set_no_show_all(True)
        self.open_report_button.connect("clicked", lambda *_a: self._open_report())
        page.pack_start(self.open_report_button, False, False, 0)
        return page

    # =========================================================== navigation
    @property
    def busy(self) -> bool:
        return self._worker is not None and self._worker.is_alive()

    def _go(self, step: Step) -> None:
        self.step = step
        self.stack.set_visible_child_name(["file", "options", "credentials", "review", "result"][int(step)])
        self.step_label.set_text(_("Step {n} of {total} · {title}").format(
            n=int(step) + 1, total=len(Step), title=STEP_TITLES[step]))
        self.back_button.set_visible(Step.FILE < step < Step.RESULT)
        self.cancel_button.set_visible(step != Step.RESULT or self.busy)
        label = {Step.REVIEW: _("_Import"), Step.RESULT: _("_Close")}.get(step, _("_Next"))
        self.next_button.set_label(label)
        self.next_button.set_use_underline(True)
        set_accessible_name(self.next_button, label.replace("_", ""))
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        busy = self.busy
        ready = {
            Step.FILE: self.result is not None,
            Step.OPTIONS: self._selected_host_count() > 0 and self._options_valid(),
            Step.CREDENTIALS: all(row.decided for row in self._rows.values()),
            Step.REVIEW: self.job is not None and not self.job.nothing_to_do and not self.job.plan.destination_missing,
            Step.RESULT: self.report is not None,
        }[self.step]
        self.next_button.set_sensitive(ready and not busy)
        self.back_button.set_sensitive(not busy)
        self.choose_button.set_sensitive(not busy)

    def _on_navigate(self, response: int) -> bool:
        """BaseDialog validator: Back/Next move between steps; only Close ends the dialog."""

        if self.busy:
            return False
        if response == RESPONSE_BACK and Step.FILE < self.step < Step.RESULT:
            self._go(Step(int(self.step) - 1))
            return False
        if response != RESPONSE_NEXT:
            return True
        if self.step == Step.FILE and self.result is not None:
            self._enter_options()
        elif self.step == Step.OPTIONS:
            self._enter_credentials()
        elif self.step == Step.CREDENTIALS:
            self._enter_review()
        elif self.step == Step.REVIEW:
            self.start_import()
        elif self.step == Step.RESULT:
            return True
        return False

    def _on_guard_response(self, _dialog, response: int) -> None:
        """Cancel / Esc / close while importing: cancel the worker, roll back, then close."""

        if response in (Gtk.ResponseType.CANCEL, Gtk.ResponseType.DELETE_EVENT) and self.busy:
            self._cancel.set()
            self._close_when_done = True
            self.result_detail.set_text(_("Cancelling — undoing what was written…"))
            self.stop_emission_by_name("response")

    def _on_delete_event(self, *_args) -> bool:
        if self.busy:
            self._cancel.set()
            self._close_when_done = True
            return True
        return False

    # ============================================================== workers
    def _run_worker(self, work: Callable[[], object], done: Callable[[object, Optional[BaseException]], None]) -> None:
        def target() -> None:
            try:
                value, error = work(), None
            except BaseException as exc:  # reported on the main thread
                value, error = None, exc
            GLib.idle_add(self._finish_worker, done, value, error)

        self._worker = threading.Thread(target=target, name="mobaxterm-import", daemon=True)
        self._worker.start()
        self._sync_buttons()

    def _finish_worker(self, done, value, error) -> bool:
        if self._worker is not None:
            self._worker.join()
        self._worker = None
        done(value, error)
        self._sync_buttons()
        return GLib.SOURCE_REMOVE

    def wait(self, timeout: float = 30.0) -> None:
        """Run the main loop until the worker is done (tests, and closing during work)."""

        import time

        deadline = time.monotonic() + timeout
        while (self._worker is not None) and time.monotonic() < deadline:
            Gtk.main_iteration_do(False)
            time.sleep(0.002)
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)

    # ============================================================ step 1: file
    def _default_choose_file(self, parent: Gtk.Window) -> Optional[str]:
        return choose_open_file(parent, _("Choose a MobaXterm Sessions File"), accept_label=_("_Open"),
                                filters=[(_("MobaXterm sessions (*.mxtsessions)"), ["*.mxtsessions"])])

    def choose_file(self, path: Optional[str] = None) -> None:
        path = path or self._choose_file(self)
        if not path:
            return
        self.load_file(Path(path))

    def load_file(self, path: Path) -> None:
        self.source_path = path
        self.result = None
        self.plan = None
        self.job = None
        self.file_error.hide()
        self.file_label.set_text(path.name)
        self.file_summary.set_text(_("Reading…"))
        self.file_spinner.start()
        self._run_worker(lambda: parse_mxtsessions(path), self._on_parsed)

    def _on_parsed(self, value, error) -> None:
        self.file_spinner.stop()
        if error is not None:
            message = error.message if isinstance(error, MxtFileError) else _("The file could not be read.")
            self.file_error.set_text(message)
            self.file_error.show()
            self.file_summary.set_text("")
            return
        self.result = value
        counts = count_types(value)
        ssh = counts.get(SSH_TYPE, 0)
        transfer = sum(counts.get(code, 0) for code in TRANSFER_TYPES)
        rdp = counts.get(RDP_TYPE, 0)
        other = len(value.sessions) - ssh - transfer - rdp
        size = _human_size(value.size)
        self.file_label.set_text(f"{self.source_path.name} · {size}" if self.source_path else size)
        self.file_summary.set_text(_("{folders} folders, {ssh} SSH sessions, {transfer} FTP/SFTP sessions, "
                                     "{rdp} RDP sessions, {other} other sessions (not imported). "
                                     "Encoding: {encoding}.").format(
            folders=len(value.folders), ssh=ssh, transfer=transfer, rdp=rdp, other=other, encoding=value.encoding))
        self.transfer_check.set_sensitive(transfer > 0)
        self.rdp_check.set_sensitive(rdp > 0)
        self._fill_folder_tree(value)

    # ========================================================= step 2: options
    def _enter_options(self) -> None:
        self.parent_combo.remove_all()
        existing = ExistingInventory.from_manager(self._manager)
        for path in sorted(existing.paths.values(), key=lambda p: [part.casefold() for part in p]):
            self.parent_combo.append("\x1f".join(path), " / ".join(path))
        has_parents = bool(existing.paths)
        self.existing_parent_radio.set_sensitive(has_parents)
        if self.parent_combo.get_active() < 0 and has_parents:
            self.parent_combo.set_active(0)
        self._go(Step.OPTIONS)
        self._update_selection_label()

    def _fill_folder_tree(self, result: MxtParseResult) -> None:
        self.folder_store.clear()

        def add(parent_iter, node: FolderNode) -> None:
            it = self.folder_store.append(parent_iter, [True, node.label, node.total_hosts, node.path, False])
            for child in node.children:
                add(it, child)

        for root in build_folder_tree(result, self.transfer_check.get_active(), self.rdp_check.get_active()):
            add(None, root)
        self.folder_view.expand_to_path(Gtk.TreePath.new_first())

    def _on_folder_toggled(self, _cell, path: str) -> None:
        it = self.folder_store.get_iter(path)
        self.set_folder_active(self.folder_store.get_value(it, COL_PATH), not self.folder_store.get_value(it, COL_ACTIVE))

    def set_folder_active(self, folder: Tuple[str, ...], active: bool) -> None:
        """Tick/untick a folder and its whole branch (also used by tests)."""

        found = []
        self.folder_store.foreach(lambda m, p, it: found.append(it.copy()) if m.get_value(it, COL_PATH) == folder else False)
        if not found:
            return

        def set_branch(it) -> None:
            self.folder_store.set_value(it, COL_ACTIVE, active)
            self.folder_store.set_value(it, COL_INCONSISTENT, False)
            child = self.folder_store.iter_children(it)
            while child is not None:
                set_branch(child)
                child = self.folder_store.iter_next(child)

        set_branch(found[0])
        parent = self.folder_store.iter_parent(found[0])
        while parent is not None:  # parents show "partly selected"
            states = []
            child = self.folder_store.iter_children(parent)
            while child is not None:
                states.append(self.folder_store.get_value(child, COL_ACTIVE))
                child = self.folder_store.iter_next(child)
            self.folder_store.set_value(parent, COL_INCONSISTENT, any(states) and not all(states))
            parent = self.folder_store.iter_parent(parent)
        self._update_selection_label()

    def excluded_folders(self) -> List[Tuple[str, ...]]:
        """Top-most unticked folders."""

        excluded: List[Tuple[str, ...]] = []

        def visit(it, parent_excluded: bool) -> None:
            while it is not None:
                path = self.folder_store.get_value(it, COL_PATH)
                off = not self.folder_store.get_value(it, COL_ACTIVE)
                if off and not parent_excluded:
                    excluded.append(path)
                visit(self.folder_store.iter_children(it), parent_excluded or off)
                it = self.folder_store.iter_next(it)

        visit(self.folder_store.get_iter_first(), False)
        return excluded

    def _selected_host_count(self, types=(SSH_TYPE,)) -> int:
        if self.result is None:
            return 0
        excluded = set(self.excluded_folders())
        return sum(1 for s in self.result.sessions if s.type_code in types and not any(
            s.folder[:depth] in excluded for depth in range(1, len(s.folder) + 1)))

    def _update_selection_label(self) -> None:
        count = self._selected_host_count()
        text = ngettext("{n} SSH session selected.", "{n} SSH sessions selected.", count).format(n=count)
        transfer = self._selected_host_count(tuple(TRANSFER_TYPES)) if self.transfer_check.get_active() else 0
        if transfer:
            text += " " + ngettext("{n} FTP/SFTP session selected.", "{n} FTP/SFTP sessions selected.",
                                   transfer).format(n=transfer)
        rdp = self._selected_host_count((RDP_TYPE,)) if self.rdp_check.get_active() else 0
        if rdp:
            text += " " + ngettext("{n} RDP session selected.", "{n} RDP sessions selected.", rdp).format(n=rdp)
        self.selection_label.set_text(text)
        self._sync_buttons()

    def _on_transfer_toggled(self) -> None:
        """Recount the folder tree (ticks stay as they are)."""

        if self.result is None:
            return
        totals = {}

        def collect(node: FolderNode) -> None:
            totals[node.path] = node.total_hosts
            for child in node.children:
                collect(child)

        for root in build_folder_tree(self.result, self.transfer_check.get_active(), self.rdp_check.get_active()):
            collect(root)
        self.folder_store.foreach(lambda m, _p, it: m.set_value(it, COL_COUNT, totals.get(m.get_value(it, COL_PATH), 0))
                                  and False)
        self._update_selection_label()

    def _options_valid(self) -> bool:
        return not self.existing_parent_radio.get_active() or bool(self.parent_combo.get_active_id())

    def _sync_options(self) -> None:
        self.parent_combo.set_sensitive(self.existing_parent_radio.get_active())
        self._sync_buttons()

    def options(self) -> ImportOptions:
        target = self.parent_combo.get_active_id() if self.existing_parent_radio.get_active() else None
        return ImportOptions(
            destination=Destination.EXISTING_GROUP if target else Destination.NEW_TOP_LEVEL,
            target_group=tuple(target.split("\x1f")) if target else (),
            conflict_policy=ConflictPolicy(self.policy_combo.get_active_id() or ConflictPolicy.SKIP.value),
            skip_empty_folders=self.skip_empty_check.get_active(),
            include_transfer=self.transfer_check.get_active(),
            include_rdp=self.rdp_check.get_active(),
        )

    # ===================================================== step 3: credentials
    def _enter_credentials(self) -> None:
        result, options, excluded = self.result, self.options(), self.excluded_folders()
        existing = ExistingInventory.from_manager(self._manager)
        self._run_worker(lambda: plan_import(result, options, excluded, existing), self._on_planned)

    def _on_planned(self, value, error) -> None:
        if error is not None:
            self.selection_label.set_text(_("The import could not be prepared: {error}").format(error=error))
            return
        self.plan = value
        self._build_rows()
        self._go(Step.CREDENTIALS)

    def _credentials(self) -> list:
        try:
            return list(self._store.list())
        except SecureStoreError:
            return []

    def _fill_combo(self, combo: Gtk.ComboBoxText, *, with_new: bool, none_label: str) -> None:
        combo.remove_all()
        combo.append(NONE_ID, none_label)
        for cred in self._credentials():
            combo.append(cred.id, f"{cred.name} — {cred.username}")
        if with_new:
            combo.append(NEW_ID, _("Create new credential…"))

    def _save_decisions(self) -> None:
        for key, row in self._rows.items():
            self._decisions[key] = (row.last_id, row.manual)

    def _build_rows(self) -> None:
        self._save_decisions()
        self._updating = True
        default_id = self.default_combo.get_active_id()
        self._fill_combo(self.default_combo, with_new=False, none_label=_("None (leave unbound)"))
        if default_id:
            self.default_combo.set_active_id(default_id)
        self._updating = False
        for child in self.rows_grid.get_children():
            self.rows_grid.remove(child)
        self._rows.clear()
        for column, title in enumerate((_("Username in file"), _("Hosts"), _("Replace with credential"), "")):
            heading = Gtk.Label(label=title)
            heading.set_xalign(0.0)
            heading.get_style_context().add_class("caption")
            self.rows_grid.attach(heading, column, 0, 1, 1)
        for index, entry in enumerate(self.plan.usernames if self.plan else [], start=1):
            name = entry.key or _("(no username)")
            tags = " ".join(sorted({"rdp": "RDP", "ftp": "FTP", "sftp": "SFTP"}[p]
                                   for p in entry.protocols if p in ("rdp", "ftp", "sftp")))
            label = Gtk.Label(label=f"{name}  [{tags}]" if tags else name)
            label.set_xalign(0.0)
            label.set_selectable(True)
            tips = []
            if entry.raw_forms:
                tips.append(_("Written in the file as: {forms}").format(forms=", ".join(sorted(entry.raw_forms))))
            if entry.references:
                tips.append(_("Bracketed forms are MobaXterm credential labels, not login names."))
            if entry.case_variants:
                tips.append(_("Differs only by case from: {names}").format(names=", ".join(entry.case_variants)))
                name_box_hint = " ⚠"
            else:
                name_box_hint = ""
            if name_box_hint:
                label.set_text(name + name_box_hint)
            label.set_tooltip_text("\n".join(tips) or None)
            count = Gtk.Label(label=str(entry.count))
            count.set_xalign(1.0)
            combo = Gtk.ComboBoxText()
            set_accessible_name(combo, _("Credential for {name}").format(name=name))
            self._fill_combo(combo, with_new=True, none_label=_("None (no credential)"))
            status = Gtk.Label()
            status.set_xalign(0.0)
            status.get_style_context().add_class("caption")
            row = UsernameRow(entry.key, combo, status)
            self._rows[entry.key] = row
            previous, manual = self._decisions.get(entry.key, (None, False))
            if previous and manual and combo.get_model() is not None:
                self._updating = True
                combo.set_active_id(previous)
                self._updating = False
                row.manual = combo.get_active_id() == previous
                row.last_id = combo.get_active_id()
            combo.connect("changed", self._on_row_changed, row)
            for column, widget in enumerate((label, count, combo, status)):
                self.rows_grid.attach(widget, column, index, 1, 1)
        self.rows_grid.show_all()
        empty = not self._credentials()
        self.empty_store_box.set_visible(empty)
        self._apply_default()
        hosts = len(self.plan.hosts) if self.plan else 0
        self.credentials_hint.set_text(ngettext(
            "{users} username found in {hosts} host.", "{users} usernames found in {hosts} hosts.",
            hosts).format(users=len(self._rows), hosts=hosts))
        self.credentials_error.hide()
        self._sync_buttons()

    def _set_default(self, cred_id: str) -> None:
        self.default_combo.set_active_id(cred_id)

    def _on_default_changed(self, _combo) -> None:
        if not self._updating:
            self._apply_default()

    def _apply_default(self) -> None:
        default = self.default_combo.get_active_id()
        self._updating = True
        for row in self._rows.values():
            if row.manual:
                row.status.set_text("")
                continue
            if default:
                row.combo.set_active_id(default)
                row.status.set_text(_("default"))
            else:
                row.combo.set_active(-1)
                row.status.set_text(_("choose…"))
            row.last_id = row.combo.get_active_id()
        self._updating = False
        self._sync_buttons()

    def _on_row_changed(self, combo: Gtk.ComboBoxText, row: UsernameRow) -> None:
        if self._updating:
            return
        if combo.get_active_id() == NEW_ID:
            self._create_credential(row)
            return
        row.last_id = combo.get_active_id()
        row.manual = True
        row.status.set_text("")
        self._sync_buttons()

    def set_row_credential(self, key: str, cred_id: Optional[str]) -> None:
        """Choose a credential for one username row (also used by tests)."""

        self._rows[key].combo.set_active_id(cred_id or NONE_ID)

    def _create_credential(self, row: Optional[UsernameRow]) -> None:
        username = row.key if row is not None else ""
        editor = CredentialEditDialog(self)
        editor.name_entry.set_text(username)
        editor.user_entry.set_text(username)
        created = None
        try:
            while True:
                if editor.run() != Gtk.ResponseType.OK:
                    break
                values = editor.values()
                if values is None:
                    continue
                name, user, password = values
                try:
                    created = self._store.add(name, user, password or "")
                except SecureStoreError:
                    self.credentials_error.set_text(_("The credential could not be saved."))
                    self.credentials_error.show()
                break
        finally:
            editor.destroy()
        self._build_rows()  # restores every row's last real choice (a cancelled "Create new…" reverts)
        if created is not None:
            if row is not None:
                self._rows[row.key].combo.set_active_id(created.id)
            elif not self.default_combo.get_active_id():
                self.default_combo.set_active_id(created.id)
        self._sync_buttons()

    def mapping(self) -> Dict[str, Optional[str]]:
        return {key: row.credential_id for key, row in self._rows.items()}

    # ========================================================== step 4: review
    def _enter_review(self) -> None:
        credentials = {cred.id: cred for cred in self._credentials()}
        try:
            self.job = apply_credential_mapping(self.plan, self.mapping(), credentials)
        except CredentialMissingError:
            self.credentials_error.set_text(_("A chosen credential no longer exists; choose again."))
            self.credentials_error.show()
            self._build_rows()
            return
        job, plan = self.job, self.plan
        self.report_text = render_report(plan, job, source_name=self.source_path.name if self.source_path else "")
        self.review_text.get_buffer().set_text(self.report_text)
        self.review_summary.set_text(_("{groups} groups ({new} new), {hosts} hosts to import.").format(
            groups=len(plan.groups), new=len(plan.new_groups), hosts=job.host_count))
        notes = []
        skipped = sum(plan.skipped_types.values())
        if skipped:
            notes.append(_("Not imported: {items}.").format(items=", ".join(
                f"{TYPE_NAMES.get(code, str(code))} {count}" for code, count in sorted(plan.skipped_types.items()))))
        problems = sum(1 for i in plan.issues if i.code in (IssueCode.EMPTY_HOSTNAME, IssueCode.INVALID_HOSTNAME,
                                                             IssueCode.UNPARSEABLE_LINE, IssueCode.ORPHAN_SESSION))
        if problems:
            notes.append(ngettext("{n} session could not be read.", "{n} sessions could not be read.",
                                  problems).format(n=problems))
        if job.skipped_existing:
            notes.append(_("Already there: {n} skipped.").format(n=job.skipped_existing))
        if job.renamed:
            notes.append(ngettext("{n} host is imported as a numbered copy.", "{n} hosts are imported as numbered copies.",
                                  job.renamed).format(n=job.renamed))
        if job.overwritten:
            notes.append(ngettext("{n} existing host will be updated.", "{n} existing hosts will be updated.",
                                  job.overwritten).format(n=job.overwritten))
        if plan.renamed:
            notes.append(ngettext("{n} name had to change (see the report).", "{n} names had to change (see the report).",
                                  len(plan.renamed)).format(n=len(plan.renamed)))
        if plan.destination_missing:
            notes.insert(0, _("The destination group no longer exists; go back and choose it again."))
        notes.append(_("Hosts mapped to None keep a real username from the file; a bracketed MobaXterm credential "
                       "label is dropped."))
        if job.nothing_to_do:
            notes.insert(0, _("Nothing new to import."))
        self.review_notes.set_text(" ".join(notes))
        self._go(Step.REVIEW)

    def _copy_report(self) -> None:
        clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        clipboard.set_text(self.report_text, -1)

    # ====================================================== step 5: import
    def start_import(self, *, confirm_overwrite: Optional[Callable[[int], bool]] = None) -> bool:
        """Commit the job in a worker thread (returns False when not started)."""

        if self.job is None or self.busy or not self._commit_lock.acquire(blocking=False):
            return False
        job = self.job
        if job.overwritten:
            ask = confirm_overwrite or (lambda n: confirm(
                self, _("Overwrite existing hosts?"),
                ngettext("{n} existing host will be replaced by the imported one.",
                         "{n} existing hosts will be replaced by the imported ones.", n).format(n=n),
                confirm_label=_("_Overwrite"), destructive=True))
            if not ask(job.overwritten):
                self._commit_lock.release()
                return False
        self._cancel.clear()
        self.progress.set_fraction(0.0)
        self.progress.set_text(_("Writing…"))
        self.result_title.set_text(_("Importing…"))
        self.result_detail.set_text("")
        self._go(Step.RESULT)

        def progress(done: int, total: int) -> None:
            GLib.idle_add(self._on_progress, done, total)

        manager, store = self._manager, self._store
        self._run_worker(lambda: commit(job, manager, store, progress=progress, should_cancel=self._cancel.is_set),
                         self._on_committed)
        self.cancel_button.set_visible(True)
        return True

    def _on_progress(self, done: int, total: int) -> bool:
        if total:
            self.progress.set_fraction(done / float(total))
            self.progress.set_text(_("{done} of {total} groups").format(done=done, total=total))
        return GLib.SOURCE_REMOVE

    def _on_committed(self, value, error) -> None:
        self._commit_lock.release()
        if error is not None:
            value = ImportReport(ImportStatus.FAILED, _("Import failed, no changes were made."))
        self.report = value
        titles = {
            ImportStatus.SUCCESS: _("Import complete"),
            ImportStatus.NOTHING: _("Nothing to import"),
            ImportStatus.FAILED: _("Import failed"),
            ImportStatus.CANCELLED: _("Import cancelled"),
        }
        self.result_title.set_text(titles[value.status])
        self.progress.set_fraction(1.0 if value.status is ImportStatus.SUCCESS else 0.0)
        self.progress.set_text("")
        text = render_report(self.plan, self.job, value, source_name=self.source_path.name if self.source_path else "")
        detail = value.message
        try:
            value.log_path = save_report(text, self._report_dir)
            detail += "\n" + _("Report saved to {path}").format(path=value.log_path)
            self.open_report_button.show()
        except OSError:
            detail += "\n" + _("The report could not be saved.")
        self.result_detail.set_text(detail)
        self.cancel_button.set_visible(False)
        self._go(Step.RESULT)
        if self._close_when_done:
            self.response(Gtk.ResponseType.CANCEL)

    def _open_report(self) -> None:
        if self.report is None or self.report.log_path is None:
            return
        try:
            Gtk.show_uri_on_window(self, self.report.log_path.as_uri(), Gtk.get_current_event_time())
        except GLib.Error:
            pass

    # ------------------------------------------------------------- helpers
    @property
    def rows(self) -> Dict[str, UsernameRow]:
        return dict(self._rows)

    def excluded_set(self) -> Set[Tuple[str, ...]]:
        return set(self.excluded_folders())
