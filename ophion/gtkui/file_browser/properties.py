"""Properties of one file or folder: size (a folder's on demand), modification time, permissions.

Permissions are editable (octal entry and read / write / execute check boxes,
kept in sync) when the backend supports ``chmod`` and the host is not
read-only. A folder's size is computed in the worker on request and can be
cancelled by closing the dialog.
"""

from __future__ import annotations

from typing import Optional

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # type: ignore  # noqa: E402

from ...transfer.backend import F_CHMOD, BackendError, CancelToken, EntryKind, RemoteEntry  # noqa: E402
from ...transfer.browse import format_mtime, human_size  # noqa: E402
from ..dialogs.base import BaseDialog, DialogSize  # noqa: E402
from ..i18n import _  # noqa: E402
from ..theme.tokens import Space  # noqa: E402
from ..widgets.icon_button import set_accessible_name  # noqa: E402

MAX_SCAN_DEPTH = 64
MAX_SCAN_ENTRIES = 200_000


def folder_size(backend, path: str, cancel: CancelToken, depth: int = 0, counter: Optional[list] = None) -> int:
    """Sum of file sizes below ``path`` (symlinks not followed; depth and entry caps)."""

    counter = counter if counter is not None else [0]
    if depth > MAX_SCAN_DEPTH:
        return 0
    total = 0
    for entry in backend.list(path, cancel):
        counter[0] += 1
        if counter[0] > MAX_SCAN_ENTRIES:
            raise BackendError(_("The folder is too large to measure."))
        if entry.kind is EntryKind.DIR:
            total += folder_size(backend, entry.path, cancel, depth + 1, counter)
        elif entry.kind is EntryKind.FILE:
            total += entry.size or 0
    return total


class PropertiesDialog(BaseDialog):
    def __init__(self, parent: Optional[Gtk.Window], pane, entry: RemoteEntry) -> None:
        BaseDialog.__init__(self, parent, _("Properties of “{name}”").format(name=entry.name), size=DialogSize.SMALL,
                            resizable=False, modal=False)
        self.pane = pane
        self.entry = entry
        self._token = CancelToken()
        grid = Gtk.Grid(column_spacing=Space.M, row_spacing=Space.XS)
        rows = [(_("Name"), entry.name), (_("Location"), entry.path),
                (_("Kind"), {EntryKind.DIR: _("Folder"), EntryKind.FILE: _("File"),
                             EntryKind.SYMLINK: _("Link to {target}").format(target=entry.link_target)}.get(
                    entry.kind, _("Other"))),
                (_("Modified"), format_mtime(entry.mtime)[1] or "—")]
        if entry.owner:
            rows.append((_("Owner"), entry.owner + (f":{entry.group}" if entry.group else "")))
        for index, (label, value) in enumerate(rows):
            self._row(grid, index, label, Gtk.Label(label=value))
        self.size_label = Gtk.Label(label=human_size(entry.size) if entry.kind is not EntryKind.DIR else "—")
        size_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.S)
        size_box.pack_start(self.size_label, False, False, 0)
        if entry.kind is EntryKind.DIR:
            self.size_button = Gtk.Button.new_with_mnemonic(_("_Calculate"))
            self.size_button.connect("clicked", lambda *_: self.calculate())
            size_box.pack_start(self.size_button, False, False, 0)
        self._row(grid, len(rows), _("Size"), size_box)

        self.can_chmod = (not pane.read_only and pane.backend() is not None
                          and F_CHMOD in pane.backend().features() and entry.mode is not None)
        self.mode_entry = Gtk.Entry()
        self.mode_entry.set_width_chars(5)
        self.mode_entry.set_max_length(4)
        set_accessible_name(self.mode_entry, _("Permissions (octal)"))
        self.checks = []
        checks = Gtk.Grid(column_spacing=Space.S, row_spacing=0)
        for column, who in enumerate((_("Owner"), _("Group"), _("Others"))):
            checks.attach(Gtk.Label(label=who), column + 1, 0, 1, 1)
        for row, what in enumerate((_("Read"), _("Write"), _("Execute"))):
            checks.attach(Gtk.Label(label=what), 0, row + 1, 1, 1)
            for column in range(3):
                check = Gtk.CheckButton()
                bit = 1 << (8 - (column * 3 + row))
                set_accessible_name(check, f"{(_('Owner'), _('Group'), _('Others'))[column]} {what}")
                check.connect("toggled", lambda *_: self._checks_to_entry())
                checks.attach(check, column + 1, row + 1, 1, 1)
                self.checks.append((bit, check))
        self.mode_entry.connect("changed", lambda *_: self._entry_to_checks())
        perms_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=Space.XS)
        perms_box.pack_start(self.mode_entry, False, False, 0)
        perms_box.pack_start(checks, False, False, 0)
        self._syncing = False
        self.mode_entry.set_text(f"{(entry.mode or 0) & 0o7777:o}" if entry.mode is not None else "")
        for widget in [self.mode_entry] + [check for _bit, check in self.checks]:
            widget.set_sensitive(self.can_chmod)
        if not self.can_chmod:
            perms_box.set_tooltip_text(_("Permissions cannot be changed here."))
        self._row(grid, len(rows) + 1, _("Permissions"), perms_box)
        self.error = Gtk.Label()
        self.error.get_style_context().add_class("form-error")
        self.error.set_no_show_all(True)
        self.add_body(grid)
        self.add_body(self.error)
        self.add_cancel(_("_Close"))
        if self.can_chmod:
            self.add_primary(_("_Apply"))
        self.connect("response", self._on_response_done)
        self.connect("destroy", lambda *_: self._token.cancel())
        self.show_all()

    @staticmethod
    def _row(grid: Gtk.Grid, row: int, label: str, widget: Gtk.Widget) -> None:
        key = Gtk.Label(label=label)
        key.set_xalign(1.0)
        key.set_valign(Gtk.Align.START)
        key.get_style_context().add_class("dim-label")
        if isinstance(widget, Gtk.Label):
            widget.set_xalign(0.0)
            widget.set_selectable(True)
            widget.set_line_wrap(True)
            widget.set_max_width_chars(48)
        grid.attach(key, 0, row, 1, 1)
        grid.attach(widget, 1, row, 1, 1)

    def mode(self) -> Optional[int]:
        text = self.mode_entry.get_text().strip()
        try:
            value = int(text, 8)
        except ValueError:
            return None
        return value if 0 <= value <= 0o7777 else None

    def _entry_to_checks(self) -> None:
        if self._syncing:
            return
        value = self.mode()
        if value is None:
            return
        self._syncing = True
        for bit, check in self.checks:
            check.set_active(bool(value & bit))
        self._syncing = False

    def _checks_to_entry(self) -> None:
        if self._syncing:
            return
        high = (self.mode() or 0) & 0o7000
        value = high | sum(bit for bit, check in self.checks if check.get_active())
        self._syncing = True
        self.mode_entry.set_text(f"{value:o}")
        self._syncing = False

    def calculate(self) -> None:
        backend = self.pane.backend()
        if backend is None:
            return
        self.size_button.set_sensitive(False)
        self.size_label.set_text(_("Calculating…"))
        self.pane.worker.submit(lambda: folder_size(backend, self.entry.path, self._token),
                                lambda total: self.size_label.set_text(human_size(total) or "0 B"),
                                lambda exc: self.size_label.set_text(
                                    exc.message if isinstance(exc, BackendError) else _("Unknown")))

    def _on_response_done(self, _dialog, response: int) -> None:
        if response != Gtk.ResponseType.OK:
            self.destroy()
            return
        mode = self.mode()
        if mode is None:
            self.error.set_text(_("Enter permissions as an octal number, for example 644."))
            self.error.show()
            return
        backend = self.pane.backend()
        self.pane.worker.submit(lambda: backend.chmod(self.entry.path, mode),
                                lambda _r: (self.pane.refresh(), self.destroy()),
                                lambda exc: (self.error.set_text(exc.message if isinstance(exc, BackendError)
                                                                 else _("Could not change the permissions.")),
                                             self.error.show()))
