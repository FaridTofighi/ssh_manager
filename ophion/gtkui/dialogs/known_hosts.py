"""Known Hosts manager: the entries of ``~/.ssh/known_hosts``, matched to the inventory.

Views: every entry (malformed lines greyed), inventory hosts without a known
key, hosts with several keys, and hashed entries no inventory host matches.
Actions: copy fingerprints, delete (confirmed, with a backup and *Undo*),
import another file, export, show the backup folder, and verify a host's key
now (key exchange only, nothing written). Parsing and matching run in a
worker thread; rows are added in batches so the dialog never freezes.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk, Pango  # type: ignore  # noqa: E402

from ... import known_hosts as KH  # noqa: E402
from ..i18n import _, ngettext  # noqa: E402
from ..theme.tokens import Level, Space  # noqa: E402
from ..widgets.icon_button import set_accessible_name  # noqa: E402
from .base import BaseDialog, DialogSize  # noqa: E402
from .confirm import ConfirmDialog, confirm, show_message  # noqa: E402

(COL_HOST, COL_ADDRESS, COL_TYPE, COL_FINGERPRINT, COL_LINE, COL_MARKER, COL_COMMENT, COL_SENSITIVE,
 COL_ENTRY, COL_HOST_KEY, COL_TOOLTIP) = range(11)
VIEW_ALL, VIEW_MISSING, VIEW_DUPLICATES, VIEW_HASHED = "all", "missing", "duplicates", "hashed"
BATCH = 500
UNDO_SECONDS = 30.0

Notify = Callable[..., None]


def _views() -> List[Tuple[str, str]]:
    return [(VIEW_ALL, _("All entries")), (VIEW_MISSING, _("Inventory hosts without a known key")),
            (VIEW_DUPLICATES, _("Several keys per host")), (VIEW_HASHED, _("Unmatched hashed entries"))]


class KnownHostsDialog(BaseDialog):
    """``hosts``: ``(key, alias, hostname, port)`` of the inventory; ``host_key`` opens it on one host."""

    def __init__(self, parent: Optional[Gtk.Window], path: Optional[Path] = None,
                 hosts: Sequence[KH.InventoryHost] = (), *, host_key: str = "",
                 notify: Optional[Notify] = None) -> None:
        BaseDialog.__init__(self, parent, _("Known Hosts"), size=DialogSize.LARGE, height=560)
        self.file = KH.KnownHostsFile(path)
        self.hosts = list(hosts)
        self.host_key = host_key
        self._notify = notify
        self.items: List[KH.Item] = []
        self.entries: List[KH.Entry] = []
        self.mapping: Dict[str, List[KH.Entry]] = {}
        self.view = VIEW_ALL
        self._generation = 0
        self.loading = False
        self._filling = False  # rows arriving in batches: shown unfiltered until the last one is in
        self._closed = False
        self.connect("destroy", lambda *_a: setattr(self, "_closed", True))  # late worker results are dropped
        # Test hooks: confirmations, messages, file choosers and the network probe.
        self.confirm: Callable[..., bool] = self._confirm  # (title, text, label, listing=)
        self.message: Callable[[str, str], None] = lambda title, text: show_message(self, title, text)
        self.choose_open: Callable[[], Optional[str]] = self._choose_open
        self.choose_save: Callable[[], Optional[str]] = self._choose_save
        self.probe: Callable[..., Tuple[str, str]] = KH.probe_host_key
        self._by_alias = {key: (alias, hostname, port) for key, alias, hostname, port in self.hosts}
        self._build()
        self.reload()

    # ----------------------------------------------------------------- build
    def _build(self) -> None:
        # The views, then the filter: on one line with unwrapped names they made the dialog 1124 px wide.
        top = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=Space.S)
        group: Optional[Gtk.RadioButton] = None
        self.view_buttons: Dict[str, Gtk.RadioButton] = {}
        switcher = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        switcher.get_style_context().add_class("linked")
        for view, label in _views():
            button = Gtk.RadioButton.new_with_label_from_widget(group, label)
            button.set_mode(False)  # a toggle button, not a radio dot
            text = button.get_child()
            text.set_line_wrap(True)
            text.set_max_width_chars(20)
            text.set_justify(Gtk.Justification.CENTER)
            button.connect("toggled", self._on_view_toggled, view)
            switcher.pack_start(button, False, False, 0)
            self.view_buttons[view] = button
            group = group or button
        top.pack_start(switcher, False, False, 0)
        self.search = Gtk.SearchEntry()
        self.search.set_placeholder_text(_("Filter"))
        set_accessible_name(self.search, _("Filter known hosts"))
        self.search.connect("search-changed", lambda *_a: self._refilter())
        top.pack_start(self.search, False, False, 0)
        self.add_body(top)

        self.store = Gtk.ListStore(str, str, str, str, int, str, str, bool, int, str, str)
        self.filtered = self.store.filter_new()
        self.filtered.set_visible_func(self._visible)
        self.tree = Gtk.TreeView(model=self.filtered)
        self.tree.get_selection().set_mode(Gtk.SelectionMode.MULTIPLE)
        self.tree.set_tooltip_column(COL_TOOLTIP)
        set_accessible_name(self.tree, _("Known hosts entries"))
        for title, column, mono, expand in ((_("Host"), COL_HOST, False, True), (_("Address"), COL_ADDRESS, False, True),
                                            (_("Key type"), COL_TYPE, False, False),
                                            (_("SHA256 fingerprint"), COL_FINGERPRINT, True, True),
                                            (_("Line"), COL_LINE, False, False), (_("Marker"), COL_MARKER, False, False),
                                            (_("Comment"), COL_COMMENT, False, True)):
            cell = Gtk.CellRendererText()
            cell.set_property("ellipsize", Pango.EllipsizeMode.END)
            if mono:
                cell.set_property("family", "monospace")
            col = Gtk.TreeViewColumn(title, cell, text=column, sensitive=COL_SENSITIVE)
            col.set_resizable(True)
            col.set_expand(expand)
            col.set_sort_column_id(column)
            self.tree.append_column(col)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.add(self.tree)
        self.add_body(scroller, expand=True)

        self.status = Gtk.Label()
        self.status.set_xalign(0.0)
        self.status.get_style_context().add_class("form-help")
        self.add_body(self.status)

        # Two lines (the selection, then files): seven buttons on one line were wider than the dialog.
        actions = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=Space.XS)
        lines = [Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.S) for _line in range(2)]
        for line in lines:
            actions.pack_start(line, False, False, 0)

        def button(label: str, callback: Callable[[], object], tooltip: str = "", line: int = 0) -> Gtk.Button:
            widget = Gtk.Button.new_with_mnemonic(label)
            if tooltip:
                widget.set_tooltip_text(tooltip)
            widget.connect("clicked", lambda *_a: callback())
            lines[line].pack_start(widget, False, False, 0)
            return widget

        self.copy_button = button(_("_Copy Fingerprint"), self.copy_fingerprints)
        self.delete_button = button(_("_Delete Selected…"), self.delete_selected)
        self.forget_button = button(_("_Forget Key for This Host…"), self.forget_host)
        self.forget_button.set_no_show_all(not self.host_key)
        self.verify_button = button(_("_Verify Host Key Now…"), self.verify_selected,
                                    _("Connect without logging in and compare the server's key with the stored one"),
                                    line=1)
        button(_("_Import…"), self.import_file, _("Add the entries of another known_hosts file"), line=1)
        button(_("E_xport Selected…"), self.export_selected, line=1)
        button(_("_Backup Folder"), self.show_backup_folder, line=1)
        self.add_body(actions)
        self.add_primary(_("_Close"), Gtk.ResponseType.CLOSE)
        self.tree.get_selection().connect("changed", lambda *_a: self._sync_buttons())
        self._sync_buttons()

    # -------------------------------------------------------------- loading
    def reload(self) -> None:
        """Parse and match in a worker thread; the rows arrive in batches."""

        self._generation += 1
        generation = self._generation
        self.loading = True
        self.status.set_text(_("Reading {path}…").format(path=self.file.path))

        def progress(done: int, total: int) -> None:
            GLib.idle_add(self._progress, generation, done, total)

        def work() -> None:
            try:
                items = self.file.load()
                entries = [i for i in items if isinstance(i, KH.Entry)]
                mapping = KH.cached_map(self.file, entries, self.hosts, progress=progress)
                result: object = (items, entries, mapping)
            except OSError as exc:
                result = exc
            GLib.idle_add(self._loaded, generation, result)

        threading.Thread(target=work, daemon=True, name="sshm-known-hosts").start()

    def _progress(self, generation: int, done: int, total: int) -> bool:
        if not self._closed and generation == self._generation and self.loading and total > 1:
            self.status.set_text(_("Matching hashed entries with the inventory… {done} of {total}").format(
                done=done, total=total))
        return False

    def _loaded(self, generation: int, result: object) -> bool:
        if self._closed or generation != self._generation:
            return False
        if isinstance(result, OSError):
            self.loading = False
            self.status.set_text(_("{path} cannot be read: {error}").format(path=self.file.path,
                                                                            error=result.strerror or result))
            return False
        self.items, self.entries, self.mapping = result  # type: ignore[misc]
        self._fill()
        return False

    def _host_for(self, entry: KH.Entry) -> Tuple[str, str]:
        """``(alias, inventory key)`` of the first inventory host this entry belongs to."""

        for key, found in self.mapping.items():
            if any(e.line_no == entry.line_no for e in found):
                return self._by_alias[key][0], key
        return "", ""

    def _rows(self) -> List[tuple]:
        rows: List[tuple] = []
        if self.view == VIEW_MISSING:
            for key, alias, hostname, port in self.hosts:
                if not self.mapping.get(key):
                    rows.append((alias, KH.host_pattern(hostname, port), "", "", 0, "", _("never connected"),
                                 True, -1, key, ""))
            return rows
        index_of = {entry.line_no: index for index, entry in enumerate(self.entries)}
        if self.view == VIEW_DUPLICATES:
            wanted = []
            for key, found in self.mapping.items():
                plain = [e for e in found if not e.marker]
                if len({(e.key_type, e.key_b64) for e in plain}) > 1:
                    wanted.extend(plain)
            chosen = sorted({e.line_no: e for e in wanted}.values(), key=lambda e: e.line_no)
        elif self.view == VIEW_HASHED:
            chosen = KH.unmatched_hashed(self.entries, self.mapping)
        else:
            chosen = []
        items = self.items if self.view == VIEW_ALL else chosen
        for item in items:
            if isinstance(item, KH.Unparsed):
                text = item.raw_line.rstrip("\r\n")
                if text.strip() and not text.lstrip().startswith("#"):
                    rows.append((_("(not an entry)"), "", "", "", item.line_no, "", text, False, -1, "", text))
                continue
            entry = item
            alias, host_key = self._host_for(entry)
            if entry.hashed:
                label = alias or _("(hashed)")
                address = KH.host_pattern(*self._by_alias[host_key][1:]) if host_key else _("hashed")
            else:
                label = alias or entry.hosts_field
                address = entry.hosts_field
            fingerprint = entry.fingerprint
            bits = KH.key_bits(entry.key_type, entry.key_b64)
            tooltip = f"{fingerprint}\n{KH.fingerprint_md5(entry.key_b64)}" + (f"\n{bits} bits" if bits else "")
            rows.append((label, address, KH.key_type_label(entry.key_type), fingerprint, entry.line_no, entry.marker,
                         entry.comment, True, index_of.get(entry.line_no, -1), host_key, tooltip))
        return rows

    def _fill(self) -> None:
        self._generation += 1
        generation = self._generation
        self.store.clear()
        rows = self._rows()
        # No filtering while the rows arrive. A filter keeps its first visible function (GTK refuses another
        # with a critical), so _visible itself passes every row until the last batch is in.
        self._filling = True

        def add(start: int) -> bool:
            if self._closed or generation != self._generation:
                return False
            for row in rows[start:start + BATCH]:
                self.store.append(row)
            if start + BATCH < len(rows):
                GLib.idle_add(add, start + BATCH)
            else:
                self.loading = False
                self._filling = False
                self._refilter()
                self._select_host()
            return False

        add(0)

    def _select_host(self) -> None:
        if not self.host_key:
            return
        selection = self.tree.get_selection()
        selection.unselect_all()
        for row in self.filtered:
            if row[COL_HOST_KEY] == self.host_key:
                selection.select_iter(row.iter)

    def _visible(self, model, tree_iter, _data=None) -> bool:
        if self._filling:
            return True
        text = self.search.get_text().strip().lower()
        if not text:
            return True
        return any(text in str(model[tree_iter][column]).lower()
                   for column in (COL_HOST, COL_ADDRESS, COL_TYPE, COL_FINGERPRINT, COL_COMMENT))

    def _refilter(self) -> None:
        self.filtered.refilter()
        shown = len(self.filtered)
        self.status.set_text(ngettext("{n} row · {path}", "{n} rows · {path}", shown).format(
            n=shown, path=self.file.path))

    def _on_view_toggled(self, button: Gtk.RadioButton, view: str) -> None:
        if button.get_active() and view != self.view:
            self.view = view
            if not self.loading:
                self._fill()

    def set_view(self, view: str) -> None:
        self.view_buttons[view].set_active(True)

    # ------------------------------------------------------------ selection
    def selected_rows(self) -> List[Gtk.TreeModelRow]:
        model, paths = self.tree.get_selection().get_selected_rows()
        return [model[path] for path in paths]

    def selected_entries(self) -> List[KH.Entry]:
        return [self.entries[row[COL_ENTRY]] for row in self.selected_rows() if row[COL_ENTRY] >= 0]

    def _selected_host(self) -> str:
        keys = {row[COL_HOST_KEY] for row in self.selected_rows() if row[COL_HOST_KEY]}
        return next(iter(keys)) if len(keys) == 1 else ""

    def _sync_buttons(self) -> None:
        entries = bool(self.selected_entries()) if self.entries else False
        for widget in (self.copy_button, self.delete_button):
            widget.set_sensitive(entries)
        self.verify_button.set_sensitive(bool(self._selected_host()))

    # -------------------------------------------------------------- actions
    def copy_fingerprints(self) -> str:
        text = "\n".join(e.fingerprint for e in self.selected_entries())
        if text:
            Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(text, -1)
            self._toast(_("Fingerprint copied."), Level.SUCCESS)
        return text

    def delete_selected(self) -> bool:
        return self._delete(self.selected_entries())

    def forget_host(self) -> bool:
        return self._delete(list(self.mapping.get(self.host_key, [])))

    def _delete(self, entries: List[KH.Entry]) -> bool:
        if not entries:
            return False
        listing = "\n".join(f"{e.line_no}: {self._host_for(e)[0] or e.hosts_field} {KH.key_type_label(e.key_type)} "
                            f"{e.fingerprint}" for e in entries)
        count = len(entries)
        if not self.confirm(
                ngettext("Delete {n} known host key?", "Delete {n} known host keys?", count).format(n=count),
                _("The next connection to these hosts asks again whether to trust their key. A backup of "
                  "known_hosts is kept."),
                ngettext("_Delete {n} Key", "_Delete {n} Keys", count).format(n=count), listing):
            return False
        try:
            backup = self.file.remove_entries(entries)
        except (KH.KnownHostsError, OSError) as exc:
            self.message(_("Nothing was deleted"), str(exc))
            self.reload()
            return False
        self.reload()
        if backup is not None:
            self._toast(ngettext("Deleted {n} known host key.", "Deleted {n} known host keys.", count).format(n=count),
                        Level.SUCCESS, action_label=_("Undo"), action=lambda: self.undo(backup),
                        timeout=UNDO_SECONDS)
        return True

    def undo(self, backup: Path) -> None:
        KH.restore_backup(self.file.path, backup)
        if not self._closed:
            self.reload()

    def import_file(self) -> bool:
        name = self.choose_open()
        if not name:
            return False
        incoming = KH.KnownHostsFile(Path(name)).entries()
        new, duplicates = KH.merge_preview(self.entries, incoming)
        if not new:
            self.message(_("Nothing to import"), ngettext("The {n} entry is already known.",
                                                          "All {n} entries are already known.",
                                                          duplicates).format(n=duplicates))
            return False
        if not self.confirm(ngettext("Add {n} entry?", "Add {n} entries?", new).format(n=new),
                            ngettext("{n} duplicate is skipped.", "{n} duplicates are skipped.",
                                     duplicates).format(n=duplicates), _("_Add")):
            return False
        try:
            self.file.merge(incoming)
        except (KH.KnownHostsError, OSError) as exc:
            self.message(_("Nothing was imported"), str(exc))
            return False
        self.reload()
        return True

    def export_selected(self) -> bool:
        entries = self.selected_entries()
        if not entries:
            return False
        name = self.choose_save()
        if not name:
            return False
        data = "".join(KH.entry_line(e.hosts_field, e.key_type, e.key_b64, marker=e.marker, comment=e.comment)
                       for e in entries).encode("utf-8")
        KH._write_atomic(Path(name), data)
        self._toast(ngettext("Exported {n} entry.", "Exported {n} entries.", len(entries)).format(n=len(entries)),
                    Level.SUCCESS)
        return True

    def show_backup_folder(self) -> None:
        found = KH.backups(self.file.path)
        detail = "\n".join(p.name for p in found) or _("No backups yet.")
        self.message(_("Backups are kept in {folder}").format(folder=self.file.path.parent), detail)

    def verify_selected(self) -> None:
        key = self._selected_host()
        if not key:
            return
        alias, hostname, port = self._by_alias[key]
        stored = list(self.mapping.get(key, []))
        self.status.set_text(_("Asking {host} for its key…").format(host=alias))
        self.verify_button.set_sensitive(False)

        def work() -> None:
            try:
                presented = self.probe(hostname, port, prefer=[e.key_type for e in stored])
                result: object = presented
            except Exception as exc:  # network errors are explained, never a traceback
                result = exc
            GLib.idle_add(self._verified, alias, stored, result)

        threading.Thread(target=work, daemon=True, name="sshm-verify-host-key").start()

    def _verified(self, alias: str, stored: List[KH.Entry], result: object) -> bool:
        if self._closed:
            return False
        self._sync_buttons()
        self._refilter()
        self.last_verification = result
        if isinstance(result, Exception):
            self.message(_("{host} could not be checked").format(host=alias), str(result) or type(result).__name__)
            return False
        key_type, key_b64 = result  # type: ignore[misc]
        new = KH.fingerprint_sha256(key_b64)
        verdict = KH.compare_host_key(stored, key_type, key_b64)
        self.last_verdict = verdict
        if verdict == KH.MATCH:
            self.message(_("The key of {host} matches").format(host=alias),
                         _("The server presented {type} {fp}, the stored key.").format(
                             type=KH.key_type_label(key_type), fp=new))
        elif verdict == KH.DIFFERENT:
            old = "\n".join(f"{KH.key_type_label(e.key_type)} {e.fingerprint} (line {e.line_no})" for e in stored
                            if not e.marker)
            self.message(_("The key of {host} is DIFFERENT").format(host=alias),
                         _("Stored:\n{old}\n\nPresented now:\n{type} {new}\n\nNothing was changed. Find out why "
                           "before you connect (see Known Hosts in the documentation).").format(
                             old=old, type=KH.key_type_label(key_type), new=new))
        else:
            self.message(_("No key of {host} is stored").format(host=alias),
                         _("The server presented {type} {fp}. ssh asks whether to trust it at the first "
                           "connection.").format(type=KH.key_type_label(key_type), fp=new))
        return False

    # ------------------------------------------------------------- helpers
    def _toast(self, text: str, level: Level, **kwargs) -> None:
        if self._notify is not None:
            self._notify(text, level, **kwargs)

    def _confirm(self, title: str, text: str, label: str, listing: str = "") -> bool:
        if not listing:
            return confirm(self, title, text, confirm_label=label, destructive=True)
        dialog = ConfirmDialog(self, title, text, confirm_label=label, destructive=True)  # Cancel is the default
        lines = Gtk.Label(label=listing)
        lines.set_xalign(0.0)
        lines.set_yalign(0.0)
        lines.set_selectable(True)
        lines.get_style_context().add_class("mono")
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.set_min_content_height(min(240, 22 * (listing.count("\n") + 1)))
        scroller.add(lines)
        dialog.add_body(scroller, expand=True)
        return dialog.ask()

    def _choose_open(self) -> Optional[str]:
        from .files import choose_open_file

        return choose_open_file(self, _("Import known_hosts"))

    def _choose_save(self) -> Optional[str]:
        from .files import choose_save_file

        return choose_save_file(self, _("Export known_hosts entries"), current_name="known_hosts")

    def ask(self) -> None:
        self.run_and_destroy()


__all__ = ["KnownHostsDialog", "VIEW_ALL", "VIEW_MISSING", "VIEW_DUPLICATES", "VIEW_HASHED"]
