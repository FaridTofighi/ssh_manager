"""Import an SSH Manager export (``*.sshm``): a wizard over :mod:`ophion.importers.native`.

The steps and wording follow the MobaXterm import wizard:

1. **File** – choose the file (read in a worker thread); an encrypted file asks
   for its passphrase (wrong: an inline error, retry after a short pause); the
   summary shows the version, date, counts per protocol, colours, credential
   references and the integrity check.
2. **Destination and options** – top level or under an existing group (a
   searchable list), colours and their policy, notes and tags, protocols, empty
   groups, smart groups.
3. **Select** – the file's groups with check boxes and host counts (one tree
   model, no widget per row); unticking a group leaves out its branch; a search
   box filters.
4. **Credentials** – every user found, each replaced by an existing credential,
   None (keep the user as written) or a new one; a default credential; matching
   credentials are suggested ("matched").
5. **Conflicts** – how many hosts already exist, and Skip / Import a copy /
   Update.
6. **Review** – the summary and the copyable report; **Import**.
7. **Result** – the commit runs in a worker thread (cancellable until it is
   written; a cancel or a failure changes nothing).

Nothing changes until *Import*. The caller saves the colours and smart groups
(:attr:`report`) after a successful commit and refreshes the tree once.
"""

from __future__ import annotations

import threading
import time
from enum import IntEnum
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk  # type: ignore  # noqa: E402

from ...importers.mobaxterm import ConflictPolicy, Destination, ExistingInventory  # noqa: E402
from ...importers.native import (  # noqa: E402
    ColorPolicy,
    CredentialMissingError,
    FileGroup,
    ImportStatus,
    NativeDocument,
    NativeFormatError,
    NativeImportOptions,
    NativeImportReport,
    NativeJob,
    NativePlan,
    apply_mapping,
    commit_native,
    plan_native,
    read_native,
    render_native_report,
    save_native_report,
    sniff,
    suggest_credentials,
)
from ...secure_store import SecureStoreError  # noqa: E402
from ..i18n import _, ngettext  # noqa: E402
from ..theme.tokens import Space  # noqa: E402
from ..widgets.forms import FormSection  # noqa: E402
from ..widgets.icon_button import set_accessible_name  # noqa: E402
from ..widgets.searchable_list import SearchableList  # noqa: E402
from ..widgets.secret_entry import SecretEntry  # noqa: E402
from .base import BaseDialog, DialogSize  # noqa: E402
from .confirm import confirm  # noqa: E402
from .credentials import CredentialEditDialog  # noqa: E402
from .files import choose_open_file  # noqa: E402
from .mobaxterm_import import NEW_ID, NONE_ID, UsernameRow, _human_size, _wrapped  # noqa: E402


class Step(IntEnum):
    FILE = 0
    OPTIONS = 1
    SELECT = 2
    CREDENTIALS = 3
    CONFLICTS = 4
    REVIEW = 5
    RESULT = 6


PAGES = ["file", "options", "select", "credentials", "conflicts", "review", "result"]
STEP_TITLES = {
    Step.FILE: _("File"),
    Step.OPTIONS: _("Destination and options"),
    Step.SELECT: _("Groups to import"),
    Step.CREDENTIALS: _("Credentials"),
    Step.CONFLICTS: _("Hosts that already exist"),
    Step.REVIEW: _("Review"),
    Step.RESULT: _("Result"),
}
RESPONSE_BACK = 1
RESPONSE_NEXT = 2
COL_ACTIVE, COL_LABEL, COL_COUNT, COL_KEY, COL_INCONSISTENT, COL_VISIBLE = range(6)
PROTOCOL_TAGS = {"ssh": "SSH", "rdp": "RDP", "ftp": "FTP", "ftps": "FTPS", "sftp": "SFTP"}
RETRY_DELAY_S = 1.0


class NativeImportDialog(BaseDialog):
    """The import wizard (modal). After it closes, :attr:`report` holds the outcome."""

    def __init__(self, parent: Optional[Gtk.Window], manager, credential_store, *, report_dir: Path,
                 group_colors: Optional[Dict[str, str]] = None, host_colors: Optional[Dict[str, str]] = None,
                 choose_file: Optional[Callable[[Gtk.Window], Optional[str]]] = None) -> None:
        BaseDialog.__init__(self, parent, _("Import an Ophion Export"), size=DialogSize.LARGE, height=640)
        self.get_style_context().add_class("import-wizard")
        self._manager = manager
        self._store = credential_store
        self._report_dir = report_dir
        self._group_colors = group_colors or {}
        self._host_colors = host_colors or {}
        self._choose_file = choose_file or self._default_choose_file
        self.source_path: Optional[Path] = None
        self.document: Optional[NativeDocument] = None
        self.plan: Optional[NativePlan] = None
        self.job: Optional[NativeJob] = None
        self.report: Optional[NativeImportReport] = None
        self.report_text = ""
        self.step = Step.FILE
        self._worker: Optional[threading.Thread] = None
        self._commit_lock = threading.Lock()
        self._cancel = threading.Event()
        self._close_when_done = False
        self._updating = False
        self._rows: Dict[str, UsernameRow] = {}
        self._decisions: Dict[str, Tuple[Optional[str], bool]] = {}
        self._integrity_accepted = False
        self.confirm = confirm  # tests replace the questions

        self.step_label = _wrapped("", "form-help")
        self.add_body(self.step_label)
        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.NONE)
        self.stack.set_vexpand(True)
        self.add_body(self.stack, expand=True)
        for name, build in zip(PAGES, (self._build_file_page, self._build_options_page, self._build_select_page,
                                       self._build_credentials_page, self._build_conflicts_page,
                                       self._build_review_page, self._build_result_page)):
            self.stack.add_named(build(), name)
        self.cancel_button = self.add_cancel()
        self.back_button = self.add_secondary(_("_Back"), RESPONSE_BACK)
        self.next_button = self.add_primary(_("_Next"), RESPONSE_NEXT)
        for button in (self.cancel_button, self.back_button):
            button.set_no_show_all(True)
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
            _("Ophion export"),
            _("Choose a file exported by Ophion (*.sshm). Groups, subgroups, hosts, colours and per-protocol "
              "settings are imported; it contains no passwords, and nothing is connected to."))
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.S)
        self.choose_button = Gtk.Button.new_with_mnemonic(_("_Choose File…"))
        self.choose_button.connect("clicked", lambda *_a: self.choose_file())
        set_accessible_name(self.choose_button, _("Choose an Ophion export"))
        row.pack_start(self.choose_button, False, False, 0)
        self.file_spinner = Gtk.Spinner()
        row.pack_start(self.file_spinner, False, False, 0)
        self.file_label = _wrapped(_("No file chosen."))
        row.pack_start(self.file_label, True, True, 0)
        section.add_widget(row)
        self.pass_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.S)
        self.pass_entry = SecretEntry(_("Passphrase"))
        set_accessible_name(self.pass_entry.entry, _("Passphrase of the export"))
        self.pass_entry.entry.connect("activate", lambda *_a: self.unlock())
        self.pass_box.pack_start(self.pass_entry, True, True, 0)
        self.unlock_button = Gtk.Button.new_with_mnemonic(_("_Unlock"))
        self.unlock_button.connect("clicked", lambda *_a: self.unlock())
        self.pass_box.pack_start(self.unlock_button, False, False, 0)
        self.pass_box.set_no_show_all(True)
        section.add_widget(self.pass_box)
        self.file_error = _wrapped("", "form-error")
        self.file_error.set_no_show_all(True)
        section.add_widget(self.file_error)
        self.file_summary = _wrapped("", "form-help")
        section.add_widget(self.file_summary)
        page.pack_start(section, False, False, 0)
        return page

    def _build_options_page(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=Space.M)
        where = FormSection(_("Where to import"), _("Every group of the file becomes a group with the same name; "
                                                    "groups that already exist at the same place are merged."))
        self.top_radio = Gtk.RadioButton.new_with_mnemonic_from_widget(None, _("At the _top level"))
        self.under_radio = Gtk.RadioButton.new_with_mnemonic_from_widget(self.top_radio,
                                                                         _("Under an _existing group:"))
        where.add_widget(self.top_radio)
        where.add_widget(self.under_radio)
        self.destination = SearchableList(search_label=_("Search the existing groups"), list_label=_("Existing groups"),
                                          min_height=4 * 26)
        self.destination.view.get_selection().connect("changed", lambda *_a: self._sync_buttons())
        where.add_widget(self.destination)
        self.under_radio.connect("toggled", lambda *_a: self._sync_options())
        page.pack_start(where, False, False, 0)
        what = FormSection(_("What to import"))
        self.colors_check = Gtk.CheckButton.new_with_mnemonic(_("Import _colours"))
        self.colors_check.set_active(True)
        self.colors_check.connect("toggled", lambda *_a: self._sync_options())
        what.add_widget(self.colors_check)
        self.color_policy = Gtk.ComboBoxText()
        for policy, label in ((ColorPolicy.IF_EMPTY, _("Only where a group or host has no colour yet")),
                              (ColorPolicy.OVERWRITE, _("Replace existing colours")),
                              (ColorPolicy.NONE, _("Don't change colours"))):
            self.color_policy.append(policy.value, label)
        self.color_policy.set_active_id(ColorPolicy.IF_EMPTY.value)
        what.add(_("Colours of existing _groups"), self.color_policy)
        self.notes_check = Gtk.CheckButton.new_with_mnemonic(_("Import _notes and tags"))
        self.notes_check.set_active(True)
        what.add_widget(self.notes_check)
        self.ssh_check = Gtk.CheckButton.new_with_mnemonic(_("_SSH hosts"))
        self.rdp_check = Gtk.CheckButton.new_with_mnemonic(_("_RDP hosts"))
        self.transfer_check = Gtk.CheckButton.new_with_mnemonic(_("_FTP / FTPS / SFTP hosts"))
        for check in (self.ssh_check, self.rdp_check, self.transfer_check):
            check.set_active(True)
            check.connect("toggled", lambda *_a: self._update_selection_label())
            what.add_widget(check)
        self.skip_empty_check = Gtk.CheckButton.new_with_mnemonic(_("Skip _empty groups"))
        what.add_widget(self.skip_empty_check)
        self.smart_check = Gtk.CheckButton.new_with_mnemonic(_("Import s_mart groups"))
        self.smart_check.set_no_show_all(True)
        what.add_widget(self.smart_check)
        page.pack_start(what, False, False, 0)
        return page

    def _build_select_page(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=Space.S)
        page.pack_start(_wrapped(_("Untick a group to leave it and everything in it out."), "form-help"),
                        False, False, 0)
        self.group_search = Gtk.SearchEntry()
        self.group_search.set_placeholder_text(_("Filter groups"))
        set_accessible_name(self.group_search, _("Filter groups"))
        self.group_search.connect("search-changed", lambda *_a: self._filter_groups())
        page.pack_start(self.group_search, False, False, 0)
        self.group_store = Gtk.TreeStore(bool, str, int, object, bool, bool)
        self.group_filter = self.group_store.filter_new()
        self.group_filter.set_visible_column(COL_VISIBLE)
        self.group_view = Gtk.TreeView(model=self.group_filter)
        self.group_view.set_headers_visible(False)
        set_accessible_name(self.group_view, _("Groups to import"))
        toggle = Gtk.CellRendererToggle()
        toggle.connect("toggled", self._on_group_toggled)
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
        self.group_view.append_column(column)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)
        scroller.set_shadow_type(Gtk.ShadowType.IN)
        scroller.add(self.group_view)
        page.pack_start(scroller, True, True, 0)
        self.selection_label = _wrapped("", "form-help")
        page.pack_start(self.selection_label, False, False, 0)
        return page

    def _build_credentials_page(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=Space.M)
        section = FormSection(
            _("Credentials for the users in the file"),
            _("Choose the Ophion credential that replaces each user: its username is used for those hosts, "
              "and the hosts are bound to it. With None the user stays as written. Passwords are never in the "
              "file; a new credential asks for one here."))
        self.default_combo = Gtk.ComboBoxText()
        set_accessible_name(self.default_combo, _("Default credential"))
        self.default_combo.connect("changed", self._on_default_changed)
        section.add(_("_Default credential for users that are not chosen below"), self.default_combo)
        page.pack_start(section, False, False, 0)
        self.empty_store_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=Space.S)
        self.empty_store_box.set_no_show_all(True)
        self.empty_store_box.pack_start(_wrapped(
            _("No credentials exist yet. Create one now, or import without credentials (the users are kept as "
              "written)."), "form-help"), False, False, 0)
        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.S)
        self.create_credential_button = Gtk.Button.new_with_mnemonic(_("C_reate New Credential…"))
        self.create_credential_button.connect("clicked", lambda *_a: self._create_credential(None))
        self.without_credentials_button = Gtk.Button.new_with_mnemonic(_("Import _without Credentials"))
        self.without_credentials_button.connect("clicked", lambda *_a: self.default_combo.set_active_id(NONE_ID))
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

    def _build_conflicts_page(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=Space.M)
        section = FormSection(_("When a host already exists"),
                              _("The same host: same group, protocol, host name, port and user (a different user is "
                                "a different host). Importing the same file again with “Skip” changes nothing."))
        self.conflict_summary = _wrapped("")
        self.conflict_summary.get_style_context().add_class("section-title")
        section.add_widget(self.conflict_summary)
        self.skip_radio = Gtk.RadioButton.new_with_mnemonic_from_widget(None, _("_Skip it (leave the existing host as it is)"))
        self.copy_radio = Gtk.RadioButton.new_with_mnemonic_from_widget(self.skip_radio, _("Import a _copy named “… (2)”"))
        self.update_radio = Gtk.RadioButton.new_with_mnemonic_from_widget(self.skip_radio, _("_Update the existing host"))
        for radio in (self.skip_radio, self.copy_radio, self.update_radio):
            section.add_widget(radio)
        page.pack_start(section, False, False, 0)
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
        set_accessible_name(self.review_text, _("Import report"))
        scroller = Gtk.ScrolledWindow()
        scroller.set_vexpand(True)
        scroller.set_shadow_type(Gtk.ShadowType.IN)
        scroller.add(self.review_text)
        page.pack_start(scroller, True, True, 0)
        copy_button = Gtk.Button.new_with_mnemonic(_("_Copy Report"))
        copy_button.set_halign(Gtk.Align.START)
        copy_button.connect("clicked", lambda *_a: Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(
            self.report_text, -1))
        page.pack_start(copy_button, False, False, 0)
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
        return page

    # =========================================================== navigation
    @property
    def busy(self) -> bool:
        return self._worker is not None and self._worker.is_alive()

    def _go(self, step: Step) -> None:
        self.step = step
        self.stack.set_visible_child_name(PAGES[int(step)])
        self.step_label.set_text(_("Step {n} of {total} · {title}").format(n=int(step) + 1, total=len(Step),
                                                                          title=STEP_TITLES[step]))
        self.back_button.set_visible(Step.FILE < step < Step.RESULT)
        self.cancel_button.set_visible(step != Step.RESULT or self.busy)
        label = {Step.REVIEW: _("_Import"), Step.RESULT: _("_Close")}.get(step, _("_Next"))
        self.next_button.set_label(label)
        self.next_button.set_use_underline(True)
        set_accessible_name(self.next_button, label.replace("_", ""))
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        ready = {
            Step.FILE: self.document is not None,
            Step.OPTIONS: self._options_valid(),
            Step.SELECT: self._selected_host_count() > 0 or self._selected_group_count() > 0,
            Step.CREDENTIALS: all(row.decided for row in self._rows.values()),
            Step.CONFLICTS: self.plan is not None,
            Step.REVIEW: self.job is not None and not self.job.nothing_to_do and not self.plan.destination_missing,
            Step.RESULT: self.report is not None,
        }[self.step]
        self.next_button.set_sensitive(ready and not self.busy)
        self.back_button.set_sensitive(not self.busy)
        self.choose_button.set_sensitive(not self.busy)

    def _on_navigate(self, response: int) -> bool:
        if self.busy:
            return False
        if response == RESPONSE_BACK and Step.FILE < self.step < Step.RESULT:
            self._go(Step(int(self.step) - 1))
            return False
        if response != RESPONSE_NEXT:
            return True
        if self.step == Step.FILE and self.document is not None:
            if self.document.integrity == "mismatch" and not self._integrity_accepted:
                if not self.confirm(self, _("The file was edited after export"),
                                    _("Its content differs from what Ophion wrote. Import it anyway?"),
                                    confirm_label=_("_Continue")):
                    return False
                self._integrity_accepted = True
            self._enter_options()
        elif self.step == Step.OPTIONS:
            self._enter_select()
        elif self.step == Step.SELECT:
            self._enter_credentials()
        elif self.step == Step.CREDENTIALS:
            self._enter_conflicts()
        elif self.step == Step.CONFLICTS:
            self._enter_review()
        elif self.step == Step.REVIEW:
            self.start_import()
        elif self.step == Step.RESULT:
            return True
        return False

    def _on_guard_response(self, _dialog, response: int) -> None:
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

    def _run_worker(self, work: Callable[[], object], done: Callable[[object, Optional[BaseException]], None]) -> None:
        def target() -> None:
            try:
                value, error = work(), None
            except BaseException as exc:
                value, error = None, exc
            GLib.idle_add(self._finish_worker, done, value, error)

        self._worker = threading.Thread(target=target, name="sshm-import", daemon=True)
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
        deadline = time.monotonic() + timeout
        while self._worker is not None and time.monotonic() < deadline:
            Gtk.main_iteration_do(False)
            time.sleep(0.002)
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)

    # ============================================================ step 1: file
    def _default_choose_file(self, parent: Gtk.Window) -> Optional[str]:
        return choose_open_file(parent, _("Choose an Ophion Export"), accept_label=_("_Open"),
                                filters=[(_("Ophion exports (*.sshm)"), ["*.sshm", "*.json"])])

    def choose_file(self, path: Optional[str] = None) -> None:
        path = path or self._choose_file(self)
        if path:
            self.load_file(Path(path))

    def load_file(self, path: Path, passphrase: Optional[str] = None) -> None:
        self.source_path = path
        self.document = self.plan = self.job = None
        self._integrity_accepted = False
        self.file_error.hide()
        self.file_label.set_text(path.name)
        self.file_summary.set_text(_("Reading…"))
        self.file_spinner.start()
        if passphrase is None and sniff(path) == "encrypted":
            self.file_spinner.stop()
            self.file_summary.set_text(_("This export is protected with a passphrase."))
            self.pass_box.show()  # no_show_all: shown explicitly, with its children
            self.pass_entry.show_all()
            self.unlock_button.show()
            self.pass_entry.grab_focus()
            self._sync_buttons()
            return
        self._run_worker(lambda: read_native(path, passphrase), self._on_read)

    def unlock(self) -> None:
        if self.source_path is None or self.busy or not self.unlock_button.get_sensitive():
            return
        passphrase = self.pass_entry.get_text()
        self.unlock_button.set_sensitive(False)
        self.load_file(self.source_path, passphrase)

    def _on_read(self, value, error) -> None:
        self.file_spinner.stop()
        if error is not None:
            message = error.message if isinstance(error, NativeFormatError) else _("The file could not be read.")
            self.file_error.set_text(message)
            self.file_error.show()
            self.file_summary.set_text("")
            if isinstance(error, NativeFormatError) and error.code == "wrong_passphrase":
                self.pass_entry.set_text("")
                GLib.timeout_add(int(RETRY_DELAY_S * 1000), lambda: self.unlock_button.set_sensitive(True) and False)
            else:
                self.unlock_button.set_sensitive(True)
            return
        self.pass_entry.set_text("")
        self.pass_box.hide()
        self.unlock_button.set_sensitive(True)
        self.document = value
        counts = value.protocol_counts()
        protocols = ", ".join(f"{PROTOCOL_TAGS.get(name, name)} {count}" for name, count in sorted(counts.items()))
        groups = sum(1 for _g in value.walk())
        integrity = {"ok": _("intact"), "missing": _("no integrity check"),
                     "mismatch": _("edited after export")}[value.integrity]
        self.file_label.set_text(f"{self.source_path.name} · {_human_size(value.size)}" if self.source_path
                                 else _human_size(value.size))
        lines = [_("Exported {date} by Ophion {version}.").format(date=value.exported_at or _("(unknown date)"),
                                                                       version=value.app_version or "?"),
                 ngettext("{n} group", "{n} groups", groups).format(n=groups) + " · " +
                 ngettext("{n} host", "{n} hosts", len(value.hosts())).format(n=len(value.hosts())) +
                 (f" ({protocols})" if protocols else ""),
                 _("Colours: {yes}. Credential references: {refs}. File: {integrity}.").format(
                     yes=_("yes") if value.has_colors else _("no"), refs=len(value.credentials),
                     integrity=integrity)]
        if value.encrypted:
            lines.append(_("Decrypted with the passphrase."))
        self.file_summary.set_text("\n".join(lines))
        self.smart_check.set_visible(bool(value.smart_groups))
        self._fill_group_tree(value)
        self._sync_buttons()

    # ========================================================= step 2: options
    def _enter_options(self) -> None:
        existing = ExistingInventory.from_manager(self._manager)
        paths = sorted(existing.paths.values(), key=lambda p: [part.casefold() for part in p])
        self.destination.set_items([("\x1f".join(path), " / ".join(path)) for path in paths])
        self.under_radio.set_sensitive(bool(paths))
        self._sync_options()
        self._go(Step.OPTIONS)

    def _sync_options(self) -> None:
        self.destination.set_sensitive(self.under_radio.get_active())
        self.color_policy.set_sensitive(self.colors_check.get_active())
        self._sync_buttons()

    def _options_valid(self) -> bool:
        return not self.under_radio.get_active() or self.destination.selected() is not None

    def options(self) -> NativeImportOptions:
        chosen = self.destination.selected() if self.under_radio.get_active() else None
        policy = ConflictPolicy.SKIP
        if self.copy_radio.get_active():
            policy = ConflictPolicy.RENAME
        elif self.update_radio.get_active():
            policy = ConflictPolicy.OVERWRITE
        return NativeImportOptions(
            destination=Destination.EXISTING_GROUP if chosen else Destination.NEW_TOP_LEVEL,
            target_group=tuple(chosen[0].split("\x1f")) if chosen else (),
            conflict_policy=policy,
            skip_empty_groups=self.skip_empty_check.get_active(),
            include_ssh=self.ssh_check.get_active(),
            include_rdp=self.rdp_check.get_active(),
            include_transfer=self.transfer_check.get_active(),
            import_colors=self.colors_check.get_active(),
            color_policy=ColorPolicy(self.color_policy.get_active_id() or ColorPolicy.IF_EMPTY.value),
            import_notes=self.notes_check.get_active(),
            import_smart_groups=self.smart_check.get_visible() and self.smart_check.get_active(),
        )

    # ========================================================== step 3: select
    def _fill_group_tree(self, document: NativeDocument) -> None:
        self.group_store.clear()
        self.group_view.set_model(None)  # no per-row signals while filling

        def add(parent_iter, group: FileGroup) -> None:
            it = self.group_store.append(parent_iter, [True, group.name, group.total_hosts(), group.key, False, True])
            for child in group.children:
                add(it, child)

        for root in document.groups:
            add(None, root)
        self.group_view.set_model(self.group_filter)
        self.group_view.expand_row(Gtk.TreePath.new_first(), False) if len(self.group_store) else None

    def _enter_select(self) -> None:
        self._go(Step.SELECT)
        self._update_selection_label()

    def _on_group_toggled(self, _cell, filter_path: str) -> None:
        child_path = self.group_filter.convert_path_to_child_path(Gtk.TreePath.new_from_string(filter_path))
        it = self.group_store.get_iter(child_path)
        self.set_group_active(self.group_store.get_value(it, COL_KEY), not self.group_store.get_value(it, COL_ACTIVE))

    def _find(self, key) -> Optional[Gtk.TreeIter]:
        found: List[Gtk.TreeIter] = []
        self.group_store.foreach(lambda m, _p, it: found.append(it.copy()) if m.get_value(it, COL_KEY) == key
                                 else False)
        return found[0] if found else None

    def set_group_active(self, key: Tuple[int, ...], active: bool) -> None:
        """Tick / untick a group and its branch (also used by tests)."""

        it = self._find(tuple(key))
        if it is None:
            return

        def set_branch(node) -> None:
            self.group_store.set_value(node, COL_ACTIVE, active)
            self.group_store.set_value(node, COL_INCONSISTENT, False)
            child = self.group_store.iter_children(node)
            while child is not None:
                set_branch(child)
                child = self.group_store.iter_next(child)

        set_branch(it)
        parent = self.group_store.iter_parent(it)
        while parent is not None:
            states = []
            child = self.group_store.iter_children(parent)
            while child is not None:
                states.append(self.group_store.get_value(child, COL_ACTIVE))
                child = self.group_store.iter_next(child)
            self.group_store.set_value(parent, COL_INCONSISTENT, any(states) and not all(states))
            parent = self.group_store.iter_parent(parent)
        self._update_selection_label()

    def excluded_groups(self) -> List[Tuple[int, ...]]:
        excluded: List[Tuple[int, ...]] = []

        def visit(it, parent_off: bool) -> None:
            while it is not None:
                off = not self.group_store.get_value(it, COL_ACTIVE)
                if off and not parent_off:
                    excluded.append(self.group_store.get_value(it, COL_KEY))
                visit(self.group_store.iter_children(it), parent_off or off)
                it = self.group_store.iter_next(it)

        visit(self.group_store.get_iter_first(), False)
        return excluded

    def _filter_groups(self) -> None:
        needle = self.group_search.get_text().strip().casefold()

        def visit(it) -> bool:
            any_visible = False
            while it is not None:
                child_visible = visit(self.group_store.iter_children(it))
                own = not needle or needle in self.group_store.get_value(it, COL_LABEL).casefold()
                self.group_store.set_value(it, COL_VISIBLE, own or child_visible)
                any_visible = any_visible or own or child_visible
                it = self.group_store.iter_next(it)
            return any_visible

        visit(self.group_store.get_iter_first())
        if needle:
            self.group_view.expand_all()

    def _selected_group_count(self) -> int:
        if self.document is None:
            return 0
        excluded = set(self.excluded_groups())
        return sum(1 for g in self.document.walk() if not any(g.key[:d] in excluded for d in range(1, len(g.key) + 1)))

    def _selected_host_count(self) -> int:
        if self.document is None:
            return 0
        excluded = set(self.excluded_groups())
        options = self.options()
        return sum(1 for g in self.document.walk() if not any(g.key[:d] in excluded for d in range(1, len(g.key) + 1))
                   for h in g.hosts if options.includes(h.protocol))

    def _update_selection_label(self) -> None:
        count = self._selected_host_count()
        self.selection_label.set_text(ngettext("{n} host selected.", "{n} hosts selected.", count).format(n=count))
        self._sync_buttons()

    # ===================================================== step 4: credentials
    def _enter_credentials(self) -> None:
        document, options, excluded = self.document, self.options(), self.excluded_groups()
        existing = ExistingInventory.from_manager(self._manager)
        self._run_worker(lambda: plan_native(document, options, excluded, existing), self._on_planned)

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

    def _build_rows(self) -> None:
        for key, row in self._rows.items():
            self._decisions[key] = (row.last_id, row.manual)
        self._updating = True
        default_id = self.default_combo.get_active_id()
        self._fill_combo(self.default_combo, with_new=False, none_label=_("None (keep the users as written)"))
        if default_id:
            self.default_combo.set_active_id(default_id)
        self._updating = False
        for child in self.rows_grid.get_children():
            self.rows_grid.remove(child)
        self._rows.clear()
        for column, title in enumerate((_("User in the file"), _("Hosts"), _("Replace with credential"), "")):
            heading = Gtk.Label(label=title)
            heading.set_xalign(0.0)
            heading.get_style_context().add_class("caption")
            self.rows_grid.attach(heading, column, 0, 1, 1)
        suggestions = suggest_credentials(self.plan, self._credentials()) if self.plan else {}
        for index, entry in enumerate(self.plan.users if self.plan else [], start=1):
            name = entry.key or _("(no user)")
            tags = " ".join(sorted(PROTOCOL_TAGS.get(p, p.upper()) for p in entry.protocols))
            label = Gtk.Label(label=(f"{name}  [{tags}]" if tags else name) + (" ⚠" if entry.case_variants else ""))
            label.set_xalign(0.0)
            tips = []
            if entry.raw_forms:
                tips.append(_("In the file as: {forms}").format(forms=", ".join(sorted(entry.raw_forms))))
            if entry.case_variants:
                tips.append(_("Differs only by case from: {names}").format(names=", ".join(entry.case_variants)))
            label.set_tooltip_text("\n".join(tips) or None)
            count = Gtk.Label(label=str(entry.count))
            count.set_xalign(1.0)
            combo = Gtk.ComboBoxText()
            set_accessible_name(combo, _("Credential for {name}").format(name=name))
            self._fill_combo(combo, with_new=True, none_label=_("None (keep the user as written)"))
            status = Gtk.Label()
            status.set_xalign(0.0)
            status.get_style_context().add_class("caption")
            row = UsernameRow(entry.key, combo, status)
            row.matched = False
            self._rows[entry.key] = row
            previous, manual = self._decisions.get(entry.key, (None, False))
            self._updating = True
            if previous and manual:
                combo.set_active_id(previous)
                row.manual = combo.get_active_id() == previous
            elif entry.key in suggestions:
                combo.set_active_id(suggestions[entry.key])
                row.manual = row.matched = True  # a suggestion counts as chosen; the default does not override it
                status.set_text(_("matched"))
            row.last_id = combo.get_active_id()
            self._updating = False
            combo.connect("changed", self._on_row_changed, row)
            for column, widget in enumerate((label, count, combo, status)):
                self.rows_grid.attach(widget, column, index, 1, 1)
        self.rows_grid.show_all()
        self.empty_store_box.set_visible(not self._credentials())
        self._apply_default()
        hosts = len(self.plan.hosts) if self.plan else 0
        hints = [ngettext("{users} user found in {hosts} host.", "{users} users found in {hosts} hosts.",
                          hosts).format(users=len(self._rows), hosts=hosts)]
        if self.plan and self.plan.host_specific:
            hints.append(ngettext("{n} host will ask for its password on first connect.",
                                  "{n} hosts will ask for their password on first connect.",
                                  self.plan.host_specific).format(n=self.plan.host_specific))
        self.credentials_hint.set_text(" ".join(hints))
        self.credentials_error.hide()
        self._sync_buttons()

    def _on_default_changed(self, _combo) -> None:
        if not self._updating:
            self._apply_default()

    def _apply_default(self) -> None:
        default = self.default_combo.get_active_id()
        self._updating = True
        for row in self._rows.values():
            if row.manual:
                if not getattr(row, "matched", False):
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
        row.matched = False
        row.status.set_text("")
        self._sync_buttons()

    def set_row_credential(self, key: str, cred_id: Optional[str]) -> None:
        self._rows[key].combo.set_active_id(cred_id or NONE_ID)

    def _create_credential(self, row: Optional[UsernameRow]) -> None:
        username = row.key if row is not None else ""
        editor = CredentialEditDialog(self)
        editor.name_entry.set_text(username)
        editor.user_entry.set_text(username)
        created = None
        try:
            while editor.run() == Gtk.ResponseType.OK:
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
        self._build_rows()
        if created is not None:
            if row is not None:
                self._rows[row.key].combo.set_active_id(created.id)
            elif not self.default_combo.get_active_id():
                self.default_combo.set_active_id(created.id)
        self._sync_buttons()

    def mapping(self) -> Dict[str, Optional[str]]:
        return {key: row.credential_id for key, row in self._rows.items()}

    # ======================================================== step 5: conflicts
    def _job(self) -> Optional[NativeJob]:
        credentials = {cred.id: cred for cred in self._credentials()}
        try:
            return apply_mapping(self.plan, self.mapping(), credentials, existing_group_colors=self._group_colors,
                                 existing_host_colors=self._host_colors)
        except CredentialMissingError:
            self.credentials_error.set_text(_("A chosen credential no longer exists; choose again."))
            self.credentials_error.show()
            self._build_rows()
            self._go(Step.CREDENTIALS)
            return None

    def _enter_conflicts(self) -> None:
        if self._job() is None:
            return
        probe = self._replan(ConflictPolicy.SKIP)
        existing = probe.skipped_existing if probe is not None else 0
        self.conflict_summary.set_text(
            ngettext("{n} host of the file already exists.", "{n} hosts of the file already exist.", existing).format(
                n=existing) if existing else _("None of the hosts exists yet."))
        for radio in (self.skip_radio, self.copy_radio, self.update_radio):
            radio.set_sensitive(existing > 0)
        self._go(Step.CONFLICTS)

    def _replan(self, policy: ConflictPolicy) -> Optional[NativeJob]:
        from dataclasses import replace

        self.plan.options = replace(self.plan.options, conflict_policy=policy)
        return self._job()

    # ========================================================== step 6: review
    def _enter_review(self) -> None:
        self.job = self._replan(self.options().conflict_policy)
        if self.job is None:
            return
        job, plan = self.job, self.plan
        self.report_text = render_native_report(plan, job, source_name=self.source_path.name if self.source_path
                                                else "")
        self.review_text.get_buffer().set_text(self.report_text)
        self.review_summary.set_text(_("{groups} groups ({new} new, {merged} merged), {hosts} hosts to import.").format(
            groups=len(plan.groups), new=len(plan.new_groups), merged=len(plan.merged_groups), hosts=job.host_count))
        notes = []
        if job.skipped_existing:
            notes.append(_("Already there: {n} skipped.").format(n=job.skipped_existing))
        if job.renamed:
            notes.append(ngettext("{n} host is imported as a numbered copy.",
                                  "{n} hosts are imported as numbered copies.", job.renamed).format(n=job.renamed))
        if job.overwritten:
            notes.append(ngettext("{n} existing host will be updated.", "{n} existing hosts will be updated.",
                                  job.overwritten).format(n=job.overwritten))
        colors = len(job.group_colors) + len(job.host_colors)
        if colors:
            notes.append(ngettext("{n} colour will be set.", "{n} colours will be set.", colors).format(n=colors))
        bindings = sum(1 for _e, cid in job.bindings if cid)
        if bindings:
            notes.append(ngettext("{n} host will be bound to a credential.", "{n} hosts will be bound to a credential.",
                                  bindings).format(n=bindings))
        warnings = sum(1 for i in plan.issues + job.issues if i.severity.value == "warning")
        if warnings:
            notes.append(ngettext("{n} warning (see the report).", "{n} warnings (see the report).",
                                  warnings).format(n=warnings))
        if plan.destination_missing:
            notes.insert(0, _("The destination group no longer exists; go back and choose it again."))
        if job.nothing_to_do:
            notes.insert(0, _("Nothing new to import."))
        self.review_notes.set_text(" ".join(notes))
        self._go(Step.REVIEW)

    # ========================================================== step 7: import
    def start_import(self, *, confirm_overwrite: Optional[Callable[[int], bool]] = None) -> bool:
        if self.job is None or self.busy or not self._commit_lock.acquire(blocking=False):
            return False
        job = self.job
        if job.overwritten:
            ask = confirm_overwrite or (lambda n: self.confirm(
                self, _("Update existing hosts?"),
                ngettext("{n} existing host will be updated from the file (a backup is made first).",
                         "{n} existing hosts will be updated from the file (a backup is made first).", n).format(n=n),
                confirm_label=_("_Update"), destructive=True))
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
        self._run_worker(lambda: commit_native(job, manager, store, progress=progress,
                                               should_cancel=self._cancel.is_set), self._on_committed)
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
            value = NativeImportReport(ImportStatus.FAILED, _("Import failed, no changes were made."))
        self.report = value
        titles = {ImportStatus.SUCCESS: _("Import complete"), ImportStatus.NOTHING: _("Nothing to import"),
                  ImportStatus.FAILED: _("Import failed"), ImportStatus.CANCELLED: _("Import cancelled")}
        self.result_title.set_text(titles[value.status])
        self.progress.set_fraction(1.0 if value.status is ImportStatus.SUCCESS else 0.0)
        self.progress.set_text("")
        text = render_native_report(self.plan, self.job, value,
                                    source_name=self.source_path.name if self.source_path else "")
        detail = value.message
        try:
            value.log_path = save_native_report(text, self._report_dir)
            detail += "\n" + _("Report saved to {path}").format(path=value.log_path)
        except OSError:
            detail += "\n" + _("The report could not be saved.")
        self.report_text = text
        self.result_detail.set_text(detail)
        self.cancel_button.set_visible(False)
        self._go(Step.RESULT)
        if self._close_when_done:
            self.response(Gtk.ResponseType.CANCEL)

    @property
    def rows(self) -> Dict[str, UsernameRow]:
        return dict(self._rows)

    def excluded_set(self) -> Set[Tuple[int, ...]]:
        return set(self.excluded_groups())


__all__ = ["NativeImportDialog", "Step"]
