"""One side of the file browser (local or remote): toolbar, path bar, filter, file list, footer.

Listing runs in the connection's worker and streams rows in batches; a
spinner appears after 300 ms; a newer navigation cancels an older one. The
list is sorted by the clicked header (folders first, natural order), filtered
live by name, and capped at :data:`MAX_ROWS` rendered rows ("refine the
filter" beyond that). Names from the server are untrusted: they are shown as
plain text (tooltips escaped), wrapped in Unicode isolates so right-to-left
names never reorder the row, and never interpreted.

Keyboard: Enter opens, Backspace / Alt+Up goes up, Alt+Left / Alt+Right
history, F2 rename, Delete, F5 refresh, F7 new folder, Ctrl+L path, Ctrl+F
filter, Ctrl+A select all, Ctrl+C / Ctrl+V copy / paste between panes.
"""

from __future__ import annotations

import os
import posixpath
import uuid
import weakref
from typing import Any, Callable, Dict, List, Optional, Tuple

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gio, GLib, GObject, Gtk, Pango  # type: ignore  # noqa: E402

from ...transfer.backend import (  # noqa: E402
    F_CHMOD, BackendError, CancelToken, Cancelled, EntryKind, RemoteEntry, RemoteFileBackend,
)
from ...transfer.browse import (  # noqa: E402
    breadcrumbs, filter_entries, format_mtime, format_perms, human_size, isolate, local_breadcrumbs,
    normalize_remote, parent_of, sort_entries, History,
)
from ...transfer.dnd import (  # noqa: E402
    PAYLOAD_TARGET, URI_TARGET, decode_payload, encode_payload, parse_uri_list, validate_payload,
)
from ...transfer.local import LocalBackend  # noqa: E402
from ..constants import Icons  # noqa: E402
from ..dialogs.confirm import confirm  # noqa: E402
from ..dialogs.prompt import prompt_text  # noqa: E402
from ..i18n import _, ngettext  # noqa: E402
from ..menu_spec import MenuItemSpec, MenuSection  # noqa: E402
from ..theme.tokens import Space  # noqa: E402
from ..widgets.empty_state import EmptyState  # noqa: E402
from ..widgets.icon_button import IconButton, IconToggleButton, set_accessible_name  # noqa: E402
from ..widgets.menus import build_context_menu, popup_menu  # noqa: E402
from .worker import Worker  # noqa: E402

MAX_ROWS = 20000  # rendered rows; more entries are kept and filterable
BATCH = 500  # rows delivered to the UI at a time while listing
SPINNER_DELAY_MS = 300
SPRING_MS = 800  # hover over a folder while dragging to open it
_INFO_PAYLOAD, _INFO_URIS = 1, 2
PANES: "weakref.WeakValueDictionary[str, FilePane]" = weakref.WeakValueDictionary()  # drag sources by token

(COL_GICON, COL_NAME, COL_SIZE, COL_MTIME, COL_PERMS, COL_OWNER, COL_ENTRY, COL_TOOLTIP) = range(8)


def _gicon(entry: RemoteEntry) -> Gio.Icon:
    if entry.kind is EntryKind.DIR:
        return Gio.ThemedIcon.new("folder-symbolic")
    if entry.kind is EntryKind.SYMLINK:
        return Gio.ThemedIcon.new_from_names(["emblem-symbolic-link-symbolic", "text-x-generic-symbolic"])
    content_type, _uncertain = Gio.content_type_guess(entry.name, None)
    return Gio.content_type_get_symbolic_icon(content_type or "application/octet-stream")


def valid_name(name: str) -> Optional[str]:
    """Why ``name`` cannot be a new file / folder name (``None`` when fine)."""

    text = name.strip()
    if not text:
        return _("Enter a name.")
    if text in (".", "..") or "/" in text or "\0" in text or "\n" in text or "\r" in text:
        return _("A name cannot contain “/”, line breaks or be “.” / “..”.")
    if len(text.encode("utf-8", "surrogateescape")) > 255:
        return _("The name is too long.")
    return None


class FilePane(Gtk.Box):
    """A navigable file list on one side (``side`` = ``"local"`` or ``"remote"``)."""

    def __init__(self, side: str, worker: Worker, backend: Callable[[], Optional[RemoteFileBackend]], *,
                 read_only: bool = False, show_hidden: bool = False, owner_columns: bool = False,
                 transient_for: Callable[[], Optional[Gtk.Window]] = lambda: None) -> None:
        Gtk.Box.__init__(self, orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.get_style_context().add_class("file-pane")
        self.side = side
        self.worker = worker
        self._backend = backend
        self.read_only = read_only
        self.show_hidden = show_hidden
        self._transient_for = transient_for
        self._owner_columns = owner_columns
        self.current_path: Optional[str] = None
        self.history = History()
        self.entries: List[RemoteEntry] = []
        self._sort = ("name", False)
        self._token: Optional[CancelToken] = None
        self._spinner_source = 0
        self._generation = 0
        self._editing_path: Optional[str] = None
        self.connected = side == "local"
        self.destroyed = False
        self.token = uuid.uuid4().hex  # names this pane in drag payloads
        self.owner: Any = None  # the tab
        # (source pane or None for desktop files, entries, target folder, copy modifier) -> accepted
        self.on_drop: Callable[[Optional["FilePane"], List[RemoteEntry], str, bool], bool] = \
            lambda _s, _e, _t, _c: False
        # Callbacks (the tab wires them).
        self.on_open_file: Callable[[RemoteEntry], None] = lambda _e: None
        self.on_transfer: Callable[[List[RemoteEntry]], None] = lambda _e: None
        self.on_paste: Callable[[], None] = lambda: None
        self.on_navigated: Callable[[str], None] = lambda _p: None
        self.on_error: Callable[[BackendError], None] = lambda _e: None
        self.on_properties: Callable[[RemoteEntry], None] = lambda _e: None
        self.on_open_entry: Callable[[RemoteEntry], None] = lambda _e: None
        self.extra_menu: Callable[[], List[MenuItemSpec]] = lambda: []
        self._build()
        self.connect("destroy", lambda *_: self._on_destroy())

    # ---------------------------------------------------------- building
    def _build(self) -> None:
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.XS)
        bar.get_style_context().add_class("file-pane-toolbar")
        local = self.side == "local"
        self.back_button = IconButton(Icons.CHEVRON_LEFT, _("Back (Alt+Left)"), self.go_back)
        self.forward_button = IconButton(Icons.CHEVRON_RIGHT, _("Forward (Alt+Right)"), self.go_forward)
        self.up_button = IconButton("go-up-symbolic", _("Up one folder (Backspace)"), self.go_up)
        self.home_button = IconButton("go-home-symbolic", _("Home folder"), self.go_home)
        self.refresh_button = IconButton(Icons.RECONNECT, _("Refresh (F5)"), self.refresh)
        self.new_folder_button = IconButton(Icons.NEW, _("New folder (F7)"), self.new_folder)
        self.rename_button = IconButton(Icons.EDIT, _("Rename (F2)"), self.start_rename)
        self.delete_button = IconButton(Icons.DELETE, _("Delete (Delete)"), self.delete_selected)
        self.hidden_toggle = IconToggleButton(Icons.EYE, _("Show hidden files"), self.set_show_hidden)
        self.hidden_toggle.set_active(self.show_hidden)
        for widget in (self.back_button, self.forward_button, self.up_button, self.home_button, self.refresh_button):
            bar.pack_start(widget, False, False, 0)
        sep = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        bar.pack_start(sep, False, False, Space.XS)
        for widget in (self.new_folder_button, self.rename_button, self.delete_button):
            bar.pack_start(widget, False, False, 0)
        bar.pack_end(self.hidden_toggle, False, False, 0)
        self.side_label = Gtk.Label(label=_("This computer") if local else _("Server"))
        self.side_label.get_style_context().add_class("section-title")
        bar.pack_end(self.side_label, False, False, Space.S)
        self.pack_start(bar, False, False, 0)

        # path bar: breadcrumbs, or an entry (Ctrl+L)
        self.path_stack = Gtk.Stack()
        self.crumb_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self.crumb_box.get_style_context().add_class("path-bar")
        set_accessible_name(self.crumb_box, _("Location"))
        crumb_scroller = Gtk.ScrolledWindow()
        crumb_scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
        crumb_scroller.add(self.crumb_box)
        edit_button = IconButton(Icons.EDIT, _("Type a location (Ctrl+L)"), self.start_path_entry)
        crumbs_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        crumbs_row.pack_start(crumb_scroller, True, True, 0)
        crumbs_row.pack_start(edit_button, False, False, 0)
        self.path_entry = Gtk.Entry()
        set_accessible_name(self.path_entry, _("Location"))
        self.path_entry.connect("activate", lambda *_: self._path_entry_done())
        self.path_entry.connect("key-press-event", self._on_path_key)
        self._completion_store = Gtk.ListStore(str)
        completion = Gtk.EntryCompletion()
        completion.set_model(self._completion_store)
        completion.set_text_column(0)
        completion.set_inline_completion(True)
        self.path_entry.set_completion(completion)
        self.path_stack.add_named(crumbs_row, "crumbs")
        self.path_stack.add_named(self.path_entry, "entry")
        self.path_error = Gtk.Label()
        self.path_error.set_xalign(0.0)
        self.path_error.get_style_context().add_class("form-error")
        self.path_error.set_no_show_all(True)
        path_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        path_box.pack_start(self.path_stack, False, False, 0)
        path_box.pack_start(self.path_error, False, False, 0)
        path_box.set_margin_start(Space.S)
        path_box.set_margin_end(Space.S)
        self.pack_start(path_box, False, False, 0)

        self.filter_entry = Gtk.SearchEntry()
        self.filter_entry.set_placeholder_text(_("Filter by name (Ctrl+F)"))
        set_accessible_name(self.filter_entry, _("Filter files"))
        self.filter_entry.connect("search-changed", lambda *_: self.render())
        self.filter_entry.set_margin_start(Space.S)
        self.filter_entry.set_margin_end(Space.S)
        self.filter_entry.set_margin_top(Space.XS)
        self.pack_start(self.filter_entry, False, False, 0)

        # the list
        self.store = Gtk.ListStore(Gio.Icon, str, str, str, str, str, GObject.TYPE_PYOBJECT, str)
        self.view = Gtk.TreeView(model=self.store)
        self.view.get_style_context().add_class("file-list")
        set_accessible_name(self.view, _("Files on this computer") if local else _("Files on the server"))
        self.view.get_selection().set_mode(Gtk.SelectionMode.MULTIPLE)
        self.view.set_rubber_banding(True)
        self.view.set_enable_search(True)
        self.view.set_search_column(COL_NAME)
        self.view.set_tooltip_column(COL_TOOLTIP)
        self.view.connect("row-activated", self._on_row_activated)
        self.view.connect("key-press-event", self._on_key)
        self.view.connect("button-press-event", self._on_button)
        self.view.get_selection().connect("changed", lambda *_: self._update_footer())
        self.columns: Dict[str, Gtk.TreeViewColumn] = {}
        self._add_name_column()
        for column_id, title, index, xalign in (("size", _("Size"), COL_SIZE, 1.0),
                                                ("mtime", _("Modified"), COL_MTIME, 0.0),
                                                ("perms", _("Permissions"), COL_PERMS, 0.0),
                                                ("owner", _("Owner"), COL_OWNER, 0.0)):
            renderer = Gtk.CellRendererText()
            renderer.set_property("xalign", xalign)
            column = Gtk.TreeViewColumn(title, renderer, text=index)
            column.set_resizable(True)
            column.set_clickable(True)
            column.connect("clicked", lambda _c, cid=column_id: self.sort_by(cid))
            self.view.append_column(column)
            self.columns[column_id] = column
        self.columns["owner"].set_visible(self._owner_columns)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)
        scroller.add(self.view)

        self.state_stack = Gtk.Stack()
        self.empty_state = EmptyState(Icons.FOLDER, _("This folder is empty"), compact=True)
        self.error_state = EmptyState(Icons.WARNING, _("Cannot open this folder"), "", action_label=_("_Retry"),
                                      on_action=self.refresh, compact=True)
        self.waiting_state = EmptyState(Icons.TRANSFER, _("Not connected"), compact=True)
        self.spinner = Gtk.Spinner()
        loading = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=Space.S)
        loading.set_valign(Gtk.Align.CENTER)
        loading.pack_start(self.spinner, False, False, 0)
        loading_label = Gtk.Label(label=_("Loading…"))
        loading.pack_start(loading_label, False, False, 0)
        self.state_stack.add_named(scroller, "list")
        self.state_stack.add_named(self.empty_state, "empty")
        self.state_stack.add_named(self.error_state, "error")
        self.state_stack.add_named(self.waiting_state, "waiting")
        self.state_stack.add_named(loading, "loading")
        self.pack_start(self.state_stack, True, True, 0)

        self.cap_hint = Gtk.Label()
        self.cap_hint.get_style_context().add_class("form-help")
        self.cap_hint.set_no_show_all(True)
        self.pack_start(self.cap_hint, False, False, 0)
        self.footer = Gtk.Label()
        self.footer.set_xalign(0.0)
        self.footer.get_style_context().add_class("file-pane-footer")
        self.footer.set_margin_start(Space.S)
        self.footer.set_margin_top(Space.XS)
        self.footer.set_margin_bottom(Space.XS)
        self.pack_start(self.footer, False, False, 0)
        self.set_read_only(self.read_only)
        self._sync_nav()
        if not self.connected:
            self.state_stack.set_visible_child_name("waiting")

    def _on_destroy(self) -> None:
        self.destroyed = True
        self._cancel()
        self._cancel_spring()
        PANES.pop(self.token, None)

    def _add_name_column(self) -> None:
        column = Gtk.TreeViewColumn(_("Name"))
        icon = Gtk.CellRendererPixbuf()
        column.pack_start(icon, False)
        column.add_attribute(icon, "gicon", COL_GICON)
        self.name_renderer = Gtk.CellRendererText()
        self.name_renderer.set_property("ellipsize", Pango.EllipsizeMode.MIDDLE)
        self.name_renderer.connect("edited", self._on_name_edited)
        self.name_renderer.connect("editing-canceled", lambda *_: setattr(self, "_editing_path", None))
        column.pack_start(self.name_renderer, True)
        column.add_attribute(self.name_renderer, "text", COL_NAME)
        column.set_expand(True)
        column.set_resizable(True)
        column.set_clickable(True)
        column.connect("clicked", lambda _c: self.sort_by("name"))
        self.view.append_column(column)
        self.columns["name"] = column

    def set_read_only(self, read_only: bool) -> None:
        """Browse / download only (host permissions): no new folder, rename, delete, upload."""

        self.read_only = read_only
        tip = _("This host is read-only (local permissions).")
        for button, normal in ((self.new_folder_button, _("New folder (F7)")), (self.rename_button, _("Rename (F2)")),
                               (self.delete_button, _("Delete (Delete)"))):
            button.set_sensitive(not read_only and self.connected)
            button.set_tooltip_text(tip if read_only else normal)

    # -------------------------------------------------------- navigation
    def backend(self) -> Optional[RemoteFileBackend]:
        return self._backend()

    def set_connected(self, connected: bool, message: str = "") -> None:
        self.connected = connected
        if not connected:
            self._cancel()
            self.waiting_state.set_text(message or _("Not connected"))
            self.state_stack.set_visible_child_name("waiting")
        self.set_read_only(self.read_only)
        self._sync_nav()

    def load(self, path: str, *, remember: bool = True) -> None:
        """Navigate to ``path`` (in the worker); the old listing stays until the new one arrives."""

        backend = self.backend()
        if backend is None or not self.connected or self.destroyed:
            return
        target = normalize_remote(path) if self.side == "remote" else os.path.normpath(path or os.sep)
        self._cancel()
        token = CancelToken()
        self._token = token
        self._generation += 1
        generation = self._generation
        self._spinner_source = GLib.timeout_add(SPINNER_DELAY_MS, self._show_spinner, generation)
        collected: List[RemoteEntry] = []

        def deliver(batch: List[RemoteEntry]) -> None:
            if generation == self._generation and not token.cancelled:
                collected.extend(batch)

        def work() -> int:
            batch: List[RemoteEntry] = []
            count = 0
            for entry in backend.list(target, token):
                batch.append(entry)
                count += 1
                if len(batch) >= BATCH:
                    self.worker.post(deliver, batch)
                    batch = []
            self.worker.post(deliver, batch)
            return count

        def done(_count: int) -> None:
            if generation != self._generation or self.destroyed:
                return
            self._stop_spinner()
            self.entries = list(collected)
            self.path_error.hide()
            if remember and target != self.current_path:
                self.history.visit(target)
            elif not remember:
                self.history.current = target
            self.current_path = target
            self._update_crumbs()
            self._sync_nav()
            self.render()
            self.on_navigated(target)

        def failed(exc: BaseException) -> None:
            if generation != self._generation or isinstance(exc, Cancelled) or self.destroyed:
                return
            self._stop_spinner()
            error = exc if isinstance(exc, BackendError) else BackendError(technical=type(exc).__name__)
            if self.current_path is not None and self.entries:
                self.path_error.set_text(_("Cannot open “{path}”: {error}").format(path=target, error=error.message))
                self.path_error.show()
                self.render()
            else:
                self.error_state.set_text(_("Cannot open this folder"), error.message)
                self.state_stack.set_visible_child_name("error")
            self.on_error(error)

        self.worker.submit(work, done, failed)

    def _show_spinner(self, generation: int) -> bool:
        self._spinner_source = 0
        if generation == self._generation and not self.destroyed:
            self.spinner.start()
            if not self.entries:
                self.state_stack.set_visible_child_name("loading")
        return GLib.SOURCE_REMOVE

    def _stop_spinner(self) -> None:
        if self._spinner_source:
            GLib.source_remove(self._spinner_source)
            self._spinner_source = 0
        self.spinner.stop()

    def _cancel(self) -> None:
        if self._token is not None:
            self._token.cancel()
            self._token = None
        self._stop_spinner()

    def refresh(self) -> None:
        if self.current_path is not None:
            self.load(self.current_path, remember=False)

    def go_up(self) -> None:
        if self.current_path is None:
            return
        parent = parent_of(self.current_path) if self.side == "remote" else os.path.dirname(self.current_path)
        if parent and parent != self.current_path:
            self.load(parent)

    def go_home(self) -> None:
        backend = self.backend()
        if backend is not None:
            self.load(backend.home())

    def go_back(self) -> None:
        path = self.history.back()
        if path:
            self.load(path, remember=False)

    def go_forward(self) -> None:
        path = self.history.forward()
        if path:
            self.load(path, remember=False)

    def _sync_nav(self) -> None:
        enabled = self.connected and self.current_path is not None
        self.back_button.set_sensitive(enabled and self.history.can_back())
        self.forward_button.set_sensitive(enabled and self.history.can_forward())
        for button in (self.up_button, self.home_button, self.refresh_button):
            button.set_sensitive(self.connected)

    def child_path(self, name: str) -> str:
        base = self.current_path or ("/" if self.side == "remote" else os.sep)
        return posixpath.join(base, name) if self.side == "remote" else os.path.join(base, name)

    # ---------------------------------------------------------- path bar
    def _update_crumbs(self) -> None:
        for child in list(self.crumb_box.get_children()):
            self.crumb_box.remove(child)
        crumbs = breadcrumbs(self.current_path or "/") if self.side == "remote" else \
            local_breadcrumbs(self.current_path or os.sep)
        for index, crumb in enumerate(crumbs):
            if index > 1:
                separator = Gtk.Label(label="›")
                separator.get_style_context().add_class("dim-label")
                self.crumb_box.pack_start(separator, False, False, 0)
            button = Gtk.Button(label=crumb.label)  # plain text label: never markup
            button.set_relief(Gtk.ReliefStyle.NONE)
            button.get_style_context().add_class("flat")
            button.set_tooltip_text(crumb.path)
            set_accessible_name(button, _("Go to {path}").format(path=crumb.path))
            button.connect("clicked", lambda _b, p=crumb.path: self.load(p))
            self.crumb_box.pack_start(button, False, False, 0)
        self.crumb_box.show_all()

    def start_path_entry(self) -> None:
        self.path_entry.set_text(self.current_path or "")
        self._completion_store.clear()
        for entry in self.entries:
            if entry.kind is EntryKind.DIR:
                self._completion_store.append([entry.path])
        self.path_stack.set_visible_child_name("entry")
        self.path_entry.grab_focus()

    def _path_entry_done(self) -> None:
        text = self.path_entry.get_text().strip()
        self.path_stack.set_visible_child_name("crumbs")
        if text and "\n" not in text and "\r" not in text:
            if self.side == "remote":
                self.load(normalize_remote(text, self.current_path or "/"))
            else:
                self.load(os.path.expanduser(text))
        elif text:
            self.path_error.set_text(_("That is not a valid location."))
            self.path_error.show()
        self.view.grab_focus()

    def _on_path_key(self, _entry: Gtk.Entry, event: Gdk.EventKey) -> bool:
        if event.keyval == Gdk.KEY_Escape:
            self.path_stack.set_visible_child_name("crumbs")
            self.view.grab_focus()
            return True
        return False

    # ------------------------------------------------------------ render
    def set_show_hidden(self, show: bool) -> None:
        self.show_hidden = bool(show)
        if self.hidden_toggle.get_active() != self.show_hidden:
            self.hidden_toggle.set_active(self.show_hidden)
        self.render()

    def sort_by(self, column: str) -> None:
        current, descending = self._sort
        self._sort = (column, not descending if column == current else False)
        for column_id, view_column in self.columns.items():
            view_column.set_sort_indicator(column_id == column)
            if column_id == column:
                view_column.set_sort_order(Gtk.SortType.DESCENDING if self._sort[1] else Gtk.SortType.ASCENDING)
        self.render()

    def visible_entries(self) -> List[RemoteEntry]:
        shown = filter_entries(self.entries, self.filter_entry.get_text(), show_hidden=self.show_hidden)
        return sort_entries(shown, self._sort[0], descending=self._sort[1])

    def render(self) -> None:
        if not self.connected or self.current_path is None or self.destroyed:
            return
        selected = {e.name for e in self.selected_entries()}
        entries = self.visible_entries()
        self.view.freeze_child_notify()
        self.view.set_model(None)
        self.store.clear()
        now = GLib.get_real_time() / 1_000_000
        for entry in entries[:MAX_ROWS]:
            mtime, iso = format_mtime(entry.mtime, now=now)
            name = entry.name + ("  →" if entry.kind is EntryKind.SYMLINK else "")
            tooltip = entry.name if entry.kind is not EntryKind.SYMLINK else f"{entry.name} → {entry.link_target}"
            if iso:
                tooltip += f"\n{iso}"
            self.store.append([_gicon(entry), isolate(name), human_size(entry.size), mtime, format_perms(entry),
                               entry.owner + (f":{entry.group}" if entry.group else ""), entry,
                               GLib.markup_escape_text(tooltip)])
        self.view.set_model(self.store)
        self.view.thaw_child_notify()
        hidden_count = len(entries) - MAX_ROWS
        self.cap_hint.set_visible(hidden_count > 0)
        if hidden_count > 0:
            self.cap_hint.set_text(ngettext("{n} more item is not shown; refine the filter.",
                                            "{n} more items are not shown; refine the filter.",
                                            hidden_count).format(n=hidden_count))
        self.state_stack.set_visible_child_name("list" if entries or self.filter_entry.get_text() else "empty")
        if selected:
            selection = self.view.get_selection()
            for row in self.store:
                if row[COL_ENTRY].name in selected:
                    selection.select_iter(row.iter)
        self._update_footer()

    def _update_footer(self) -> None:
        selected = self.selected_entries()
        total = len(self.entries)
        text = ngettext("{n} item", "{n} items", total).format(n=total)
        if selected:
            size = sum(e.size or 0 for e in selected if e.kind is not EntryKind.DIR)
            text += " · " + ngettext("{n} selected", "{n} selected", len(selected)).format(n=len(selected))
            if size:
                text += f" · {human_size(size)}"
        self.footer.set_text(text)
        has_selection = bool(selected)
        self.rename_button.set_sensitive(len(selected) == 1 and not self.read_only and self.connected)
        self.delete_button.set_sensitive(has_selection and not self.read_only and self.connected)

    def selected_entries(self) -> List[RemoteEntry]:
        model, paths = self.view.get_selection().get_selected_rows()
        if model is None:
            return []
        return [model[path][COL_ENTRY] for path in paths]

    def select_name(self, name: str) -> None:
        if self.destroyed:
            return
        selection = self.view.get_selection()
        selection.unselect_all()
        for row in self.store:
            if row[COL_ENTRY].name == name:
                selection.select_iter(row.iter)
                self.view.scroll_to_cell(row.path, None, False, 0, 0)
                self.view.set_cursor(row.path, None, False)
                return

    # ------------------------------------------------------------ events
    def _on_row_activated(self, _view, path, _column) -> None:
        entry = self.store[path][COL_ENTRY]
        self.activate(entry)

    def activate(self, entry: RemoteEntry) -> None:
        if entry.kind is EntryKind.DIR:
            self.load(entry.path)
        elif entry.kind is EntryKind.SYMLINK:
            self.on_open_entry(entry)  # the tab resolves the link (folder or file)
        else:
            self.on_open_file(entry)

    def _on_key(self, _view, event: Gdk.EventKey) -> bool:
        mods = event.state & Gtk.accelerator_get_default_mod_mask()
        key = event.keyval
        ctrl = mods == Gdk.ModifierType.CONTROL_MASK
        alt = mods == Gdk.ModifierType.MOD1_MASK
        if key == Gdk.KEY_BackSpace and not mods or (alt and key == Gdk.KEY_Up):
            self.go_up()
        elif alt and key == Gdk.KEY_Left:
            self.go_back()
        elif alt and key == Gdk.KEY_Right:
            self.go_forward()
        elif key == Gdk.KEY_F2 and not mods:
            self.start_rename()
        elif key in (Gdk.KEY_Delete, Gdk.KEY_KP_Delete) and not mods:
            self.delete_selected()
        elif key == Gdk.KEY_F5 and not mods:
            self.refresh()
        elif key == Gdk.KEY_F7 and not mods:
            self.new_folder()
        elif ctrl and key in (Gdk.KEY_l, Gdk.KEY_L):
            self.start_path_entry()
        elif ctrl and key in (Gdk.KEY_f, Gdk.KEY_F):
            self.filter_entry.grab_focus()
        elif ctrl and key in (Gdk.KEY_a, Gdk.KEY_A):
            self.view.get_selection().select_all()
        elif ctrl and key in (Gdk.KEY_c, Gdk.KEY_C):
            self.copy_selection()
        elif ctrl and key in (Gdk.KEY_v, Gdk.KEY_V):
            self.on_paste()
        else:
            return False
        return True

    def _on_button(self, view: Gtk.TreeView, event: Gdk.EventButton) -> bool:
        if event.button != 3 or event.type != Gdk.EventType.BUTTON_PRESS:
            return False
        hit = view.get_path_at_pos(int(event.x), int(event.y))
        selection = view.get_selection()
        if hit and not selection.path_is_selected(hit[0]):
            selection.unselect_all()
            selection.select_path(hit[0])
        elif not hit:
            selection.unselect_all()
        popup_menu(build_context_menu(self.menu_spec(), attach_to=self), event)
        return True

    # -------------------------------------------------------- operations
    clipboard: List[RemoteEntry] = []  # shared between panes: Ctrl+C here, Ctrl+V on the other side

    def copy_selection(self) -> None:
        FilePane.clipboard = [*self.selected_entries()]
        FilePane.clipboard_side = self.side  # type: ignore[attr-defined]

    def menu_spec(self) -> list:
        selected = self.selected_entries()
        one = len(selected) == 1
        writable = not self.read_only and self.connected
        transfer_label = _("_Upload") if self.side == "local" else _("_Download")
        items = [
            MenuSection("open", [
                MenuItemSpec(_("_Open"), lambda: self.activate(selected[0]), Icons.OPEN_FOLDER, "Enter",
                             sensitive=one),
                MenuItemSpec(transfer_label, lambda: self.on_transfer(selected), Icons.SAVE,
                             sensitive=bool(selected) and self.connected),
            ]),
            MenuSection("edit", [
                MenuItemSpec(_("New _Folder…"), self.new_folder, Icons.NEW, "F7", sensitive=writable),
                MenuItemSpec(_("_Rename…"), self.start_rename, Icons.EDIT, "F2", sensitive=one and writable),
                MenuItemSpec(_("Copy _Path"), self.copy_paths, Icons.COPY, sensitive=bool(selected)),
                MenuItemSpec(_("P_roperties"), lambda: self.on_properties(selected[0]), Icons.INFO, sensitive=one),
            ]),
            MenuSection("extra", self.extra_menu()),
            MenuSection("danger", [
                MenuItemSpec(_("_Delete…"), self.delete_selected, Icons.DELETE, "Delete",
                             sensitive=bool(selected) and writable, destructive=True),
            ]),
        ]
        return [section for section in items if section.items]

    def copy_paths(self) -> None:
        paths = "\n".join(e.path for e in self.selected_entries())
        Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(paths, -1)

    def _run(self, work: Callable[[RemoteFileBackend], Any], done: Callable[[Any], None] = lambda _r: None) -> None:
        backend = self.backend()
        if backend is None:
            return

        def failed(exc: BaseException) -> None:
            error = exc if isinstance(exc, BackendError) else BackendError(technical=type(exc).__name__)
            self.on_error(error)

        self.worker.submit(lambda: work(backend), done, failed)

    def new_folder(self) -> None:
        if self.read_only or not self.connected or self.current_path is None:
            return
        name = prompt_text(self._transient_for(), _("New Folder"), _("Name of the new folder:"),
                           validator=valid_name, accept_label=_("C_reate"))
        if not name:
            return
        path = self.child_path(name.strip())
        self._run(lambda b: b.mkdir(path), lambda _r: self._refresh_and_select(name.strip()))

    def _refresh_and_select(self, name: str) -> None:
        self.refresh()
        self._select_after_load(name)

    def _select_after_load(self, name: str) -> None:
        def later() -> bool:
            self.select_name(name)
            return GLib.SOURCE_REMOVE
        GLib.timeout_add(150, later)

    def start_rename(self) -> None:
        selected = self.selected_entries()
        if self.read_only or len(selected) != 1:
            return
        model, paths = self.view.get_selection().get_selected_rows()
        self._editing_path = selected[0].path
        self.name_renderer.set_property("editable", True)
        self.view.set_cursor(paths[0], self.columns["name"], True)

    def _on_name_edited(self, _renderer, path: str, text: str) -> None:
        self.name_renderer.set_property("editable", False)
        old = self._editing_path
        self._editing_path = None
        if old is None:
            return
        entry = self.store[path][COL_ENTRY]
        new_name = text.replace("⁨", "").replace("⁩", "").strip()
        if new_name == entry.name:
            return
        problem = valid_name(new_name)
        if problem:
            self.path_error.set_text(problem)
            self.path_error.show()
            return
        target = self.child_path(new_name)
        self._run(lambda b: b.rename(entry.path, target), lambda _r: self._refresh_and_select(new_name))

    def delete_selected(self) -> None:
        selected = self.selected_entries()
        if self.read_only or not selected or not self.connected:
            return
        names = "\n".join(f"• {e.name}" for e in selected[:5]) + ("\n…" if len(selected) > 5 else "")
        folders = any(e.kind is EntryKind.DIR for e in selected)
        if not confirm(
            self._transient_for(),
            ngettext("Delete {n} item?", "Delete {n} items?", len(selected)).format(n=len(selected)),
            names + ("\n\n" + _("Folders are deleted with everything in them.") if folders else ""),
            confirm_label=_("_Delete"),
            destructive=True,
        ):
            return
        token = CancelToken()
        self._delete_token = token

        def work(backend: RemoteFileBackend) -> int:
            for entry in selected:
                token.raise_if_cancelled()
                if entry.kind is EntryKind.DIR:
                    backend.delete_dir(entry.path, recursive=True, cancel=token)
                else:
                    backend.delete_file(entry.path)  # a symlink itself, never its target
            return len(selected)

        self._run(work, lambda _r: self.refresh())

    def can_chmod(self) -> bool:
        backend = self.backend()
        return bool(backend and F_CHMOD in backend.features() and not self.read_only)

    # ------------------------------------------------------- drag and drop
    def enable_dnd(self) -> None:
        """Drag entries out (payload; local files also as ``text/uri-list``) and accept drops in.

        ``on_drop(source pane or None, entries, target folder, copy modifier)``
        decides what happens; a drop is validated first (payload paths must be
        listed in the source pane; URIs must be existing local files).
        """

        targets = [Gtk.TargetEntry.new(PAYLOAD_TARGET, Gtk.TargetFlags.SAME_APP, _INFO_PAYLOAD),
                   Gtk.TargetEntry.new(URI_TARGET, 0, _INFO_URIS)]
        actions = Gdk.DragAction.COPY | Gdk.DragAction.MOVE
        self.view.enable_model_drag_source(Gdk.ModifierType.BUTTON1_MASK, targets, actions)
        self.view.enable_model_drag_dest(targets, actions)
        self.view.connect("drag-begin", self._on_drag_begin)
        self.view.connect("drag-data-get", self._on_drag_data_get)
        self.view.connect("drag-data-delete", lambda widget, _ctx: widget.stop_emission_by_name("drag-data-delete"))
        self.view.connect("drag-motion", self._on_drag_motion)
        self.view.connect("drag-leave", self._on_drag_leave)
        self.view.connect("drag-drop", self._on_drag_drop)
        self.view.connect("drag-data-received", self._on_drag_data_received)
        PANES[self.token] = self

    def _on_drag_begin(self, _view, context) -> None:
        count = len(self.selected_entries())
        label = Gtk.Label(label=ngettext("{n} item", "{n} items", count).format(n=count))
        label.get_style_context().add_class("drag-badge")
        label.show()
        Gtk.drag_set_icon_widget(context, label, 0, 0)

    def _on_drag_data_get(self, view, _context, data, info, _time) -> None:
        view.stop_emission_by_name("drag-data-get")  # never GtkTreeView's row-reordering data
        entries = self.selected_entries()
        if info == _INFO_PAYLOAD:
            data.set(data.get_target(), 8, encode_payload(self.token, self.side, [e.path for e in entries]))
        elif info == _INFO_URIS and self.side == "local":
            data.set_uris([Gio.File.new_for_path(e.path).get_uri() for e in entries])

    def _drop_folder(self, x: int, y: int) -> Tuple[Optional[str], bool]:
        """``(target folder, onto a file)`` for a drop at ``(x, y)``: a folder row or the current folder."""

        hit = self.view.get_dest_row_at_pos(x, y)
        if hit is not None:
            entry = self.store[hit[0]][COL_ENTRY]
            if entry.kind is EntryKind.DIR:
                return entry.path, False
        return self.current_path, False

    def _on_drag_motion(self, view, context, x: int, y: int, time_: int) -> bool:
        view.stop_emission_by_name("drag-motion")
        if not self.connected or self.current_path is None or (self.side == "remote" and self.read_only):
            Gdk.drag_status(context, 0, time_)
            return True
        hit = view.get_dest_row_at_pos(x, y)
        folder_path = None
        if hit is not None and self.store[hit[0]][COL_ENTRY].kind is EntryKind.DIR:
            view.set_drag_dest_row(hit[0], Gtk.TreeViewDropPosition.INTO_OR_BEFORE)
            folder_path = self.store[hit[0]][COL_ENTRY].path
        else:
            view.set_drag_dest_row(None, Gtk.TreeViewDropPosition.BEFORE)
        self._spring(folder_path)
        has_state, modifiers = Gtk.get_current_event_state()
        copy = bool(has_state and modifiers & Gdk.ModifierType.CONTROL_MASK)  # Ctrl: copy instead of move
        same_pane = Gtk.drag_get_source_widget(context) is view
        action = Gdk.DragAction.MOVE if same_pane and not copy else Gdk.DragAction.COPY
        Gdk.drag_status(context, action, time_)
        return True

    def _spring(self, folder: Optional[str]) -> None:
        """Hovering a folder for 800 ms while dragging opens it (spring-loaded folders)."""

        if folder == getattr(self, "_spring_folder", None):
            return
        self._cancel_spring()
        self._spring_folder = folder
        if folder is not None:
            self._spring_source = GLib.timeout_add(SPRING_MS, self._spring_open, folder)

    def _spring_open(self, folder: str) -> bool:
        self._spring_source = 0
        if not self.destroyed and folder == self._spring_folder:
            self.load(folder)
        return GLib.SOURCE_REMOVE

    def _cancel_spring(self) -> None:
        source = getattr(self, "_spring_source", 0)
        if source:
            GLib.source_remove(source)
        self._spring_source = 0
        self._spring_folder = None

    def _on_drag_leave(self, view, _context, _time) -> None:
        self._cancel_spring()
        view.set_drag_dest_row(None, Gtk.TreeViewDropPosition.BEFORE)

    def _on_drag_drop(self, view, context, _x, _y, time_: int) -> bool:
        view.stop_emission_by_name("drag-drop")
        self._cancel_spring()
        names = [str(target) for target in context.list_targets()]
        wanted = PAYLOAD_TARGET if PAYLOAD_TARGET in names else (URI_TARGET if URI_TARGET in names else None)
        if wanted is None:
            Gtk.drag_finish(context, False, False, time_)
            return True
        view.drag_get_data(context, Gdk.Atom.intern(wanted, False), time_)
        return True

    def _on_drag_data_received(self, view, context, x: int, y: int, data, info: int, time_: int) -> None:
        view.stop_emission_by_name("drag-data-received")  # never GtkTreeView's row insertion
        ok = self.accept_drop(data.get_data() if info == _INFO_PAYLOAD else None,
                              data.get_uris() if info == _INFO_URIS else None, self._drop_folder(x, y)[0],
                              copy=bool(context.get_selected_action() & Gdk.DragAction.COPY))
        Gtk.drag_finish(context, ok, False, time_)

    def accept_drop(self, payload: Optional[bytes], uris: Optional[List[str]], target_folder: Optional[str], *,
                    copy: bool) -> bool:
        """Validate a drop and hand it to ``on_drop``; ``False`` (and nothing happens) when invalid."""

        if target_folder is None or not self.connected or (self.side == "remote" and self.read_only):
            return False
        if payload is not None:
            decoded = decode_payload(payload)
            source = PANES.get(decoded.token) if decoded is not None else None
            if decoded is None or source is None or source.destroyed or source.side != decoded.side:
                return False
            listed = {entry.path: entry for entry in source.entries}
            if not validate_payload(decoded, list(listed)):
                return False  # a path that is not listed in the source pane right now
            return self.on_drop(source, [listed[path] for path in decoded.paths], target_folder, copy)
        paths = parse_uri_list("\n".join(uris or []))
        if not paths:
            return False
        local = LocalBackend()
        entries = []
        for path in paths:
            try:
                entries.append(local.stat(path))
            except BackendError:
                continue
        return bool(entries) and self.on_drop(None, entries, target_folder, True)
