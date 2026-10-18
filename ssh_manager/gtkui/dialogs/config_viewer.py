"""SSH config viewer/editor with non-blocking lint results."""

from __future__ import annotations

import time
from pathlib import Path
from typing import List

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Pango  # type: ignore  # noqa: E402

from ...config_viewer import (  # noqa: E402
    ValidationResult,
    _safe_read,
    export_path_for_directory,
    save_config_text,
    validate_config,
)
from ...persistence import write_text_atomic  # noqa: E402
from ..constants import Icons  # noqa: E402
from ..i18n import _, ngettext  # noqa: E402
from ..menu_spec import MenuItemSpec, MenuSection  # noqa: E402
from ..theme.tokens import Level, Size, Space  # noqa: E402
from ..widgets.icon_button import IconMenuButton, set_accessible_name  # noqa: E402
from ..widgets.menus import build_menu  # noqa: E402
from ..widgets.toast import LEVEL_ICONS  # noqa: E402
from .base import BaseDialog, DialogSize  # noqa: E402
from .confirm import confirm  # noqa: E402
from .files import choose_folder, choose_open_file, choose_save_file  # noqa: E402
from .prompt import prompt_text  # noqa: E402

_SEVERITY_LEVEL = {"error": Level.ERROR, "warning": Level.WARNING, "info": Level.INFO}


class ConfigViewerDialog(BaseDialog):
    """Show an SSH config file with lint results; optional editing, import and export."""

    def __init__(self, parent: Gtk.Window, path: Path) -> None:
        BaseDialog.__init__(self, parent, _("SSH Config File"), size=DialogSize.LARGE, height=600)
        self._path = path
        self._editing = False
        self._dirty = False

        self.add_body(self._build_toolbar())
        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        paned.pack1(self._build_editor(), resize=True, shrink=False)
        paned.pack2(self._build_issues(), resize=False, shrink=False)
        paned.set_position(int(DialogSize.LARGE) - 300)
        self.add_body(paned, expand=True)

        self._status_label = Gtk.Label()
        self._status_label.set_xalign(0.0)
        self._status_label.set_line_wrap(True)
        self._status_label.get_style_context().add_class("form-help")
        self.add_body(self._status_label)

        close = self.add_primary(_("_Close"), Gtk.ResponseType.CLOSE)
        close.get_style_context().remove_class("suggested-action")
        self.connect("response", self._on_close_response)
        self._load()
        self.show_all()

    # ------------------------------------------------------------- building
    def _build_toolbar(self) -> Gtk.Widget:
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.S)
        path_label = Gtk.Label(label=str(self._path))
        path_label.set_xalign(0.0)
        path_label.set_ellipsize(Pango.EllipsizeMode.START)  # keeps the file name visible
        path_label.set_selectable(True)
        path_label.set_can_focus(False)
        path_label.get_style_context().add_class("dim-label")
        bar.pack_start(path_label, True, True, 0)
        self._edit_toggle = Gtk.ToggleButton.new_with_mnemonic(_("_Edit"))
        self._edit_toggle.connect("toggled", self._on_edit_toggled)
        bar.pack_start(self._edit_toggle, False, False, 0)
        self._save_button = Gtk.Button.new_with_mnemonic(_("_Save"))
        self._save_button.get_style_context().add_class("suggested-action")
        self._save_button.set_sensitive(False)
        self._save_button.connect("clicked", self._on_save_clicked)
        bar.pack_start(self._save_button, False, False, 0)
        menu = build_menu([
            MenuSection("import", [
                MenuItemSpec(_("_Import File…"), self._import_file, Icons.OPEN_FOLDER),
                MenuItemSpec(_("Import from _Directory…"), self._import_directory),
            ]),
            MenuSection("export", [
                MenuItemSpec(_("_Export File…"), self._export_file, Icons.SAVE),
                MenuItemSpec(_("Export to D_irectory…"), self._export_directory),
            ]),
        ])
        bar.pack_start(IconMenuButton(Icons.MORE, _("Import and export"), menu=menu), False, False, 0)
        return bar

    def _build_editor(self) -> Gtk.Widget:
        self._text_view = Gtk.TextView()
        self._text_view.set_editable(False)
        self._text_view.set_monospace(True)
        self._text_view.set_left_margin(Space.S)
        self._text_view.set_top_margin(Space.XS)
        set_accessible_name(self._text_view, _("Config file contents"))
        self._buffer = self._text_view.get_buffer()
        self._buffer.connect("changed", self._on_buffer_changed)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.set_shadow_type(Gtk.ShadowType.IN)
        scroller.add(self._text_view)
        return scroller

    def _build_issues(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=Space.XS)
        box.set_size_request(Size.SIDE_PANEL, -1)
        self._issues_title = Gtk.Label(label=_("Issues"))
        self._issues_title.set_xalign(0.0)
        self._issues_title.get_style_context().add_class("section-title")
        box.pack_start(self._issues_title, False, False, 0)
        self._list_store = Gtk.ListStore(str, str, int)  # icon, message, line
        self._tree = Gtk.TreeView(model=self._list_store)
        self._tree.set_headers_visible(False)
        set_accessible_name(self._tree, _("Issues"))
        column = Gtk.TreeViewColumn()
        icon = Gtk.CellRendererPixbuf()
        column.pack_start(icon, False)
        column.add_attribute(icon, "icon-name", 0)
        text = Gtk.CellRendererText()
        text.set_property("wrap-mode", Pango.WrapMode.WORD_CHAR)
        text.set_property("wrap-width", 220)
        column.pack_start(text, True)
        column.add_attribute(text, "text", 1)
        self._tree.append_column(column)
        self._tree.connect("row-activated", self._on_row_activated)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_shadow_type(Gtk.ShadowType.IN)
        scroller.add(self._tree)
        box.pack_start(scroller, True, True, 0)
        return box

    # --------------------------------------------------------------- content
    def _load(self) -> None:
        text, error = _safe_read(self._path)
        if error:
            self._set_text(error)
            return
        if not text.strip():
            self._set_text("")
            self._status_label.set_text(_("{path} is empty.").format(path=self._path))
            return
        self._set_text(text)
        self._populate_results(validate_config(text))

    def _set_text(self, text: str) -> None:
        self._buffer.set_text(text)
        self._dirty = False

    def _current_text(self) -> str:
        start, end = self._buffer.get_bounds()
        return self._buffer.get_text(start, end, False)

    def _populate_results(self, results: List[ValidationResult]) -> None:
        self._list_store.clear()
        for res in results:
            level = _SEVERITY_LEVEL.get(res.severity, Level.INFO)
            self._list_store.append([LEVEL_ICONS[level], _("Line {line}: {message}").format(line=res.line, message=res.message), res.line])
        count = len(results)
        self._issues_title.set_text(
            ngettext("{n} issue", "{n} issues", count).format(n=count) if count else _("No issues found")
        )

    def _on_row_activated(self, _view, path, _column) -> None:
        tree_iter = self._list_store.get_iter(path)
        line = self._list_store.get_value(tree_iter, 2)
        if isinstance(line, int):
            iter_ = self._buffer.get_iter_at_line(max(0, line - 1))
            self._buffer.place_cursor(iter_)
            self._text_view.scroll_to_iter(iter_, 0.25, False, 0, 0)
            self._text_view.grab_focus()

    def _on_buffer_changed(self, *_args) -> None:
        if self._editing:
            self._dirty = True

    # ---------------------------------------------------------------- editing
    def _set_editing(self, editing: bool) -> None:
        self._editing = editing
        self._text_view.set_editable(editing)
        self._save_button.set_sensitive(editing)
        if self._edit_toggle.get_active() != editing:
            self._edit_toggle.set_active(editing)

    def _on_edit_toggled(self, button: Gtk.ToggleButton) -> None:
        self._set_editing(button.get_active())
        self._status_label.set_text(_("Editing — press Save to write the file (a .bak backup is kept).") if self._editing else "")

    def _on_save_clicked(self, _button: Gtk.Button) -> None:
        if not self._editing:
            return
        text = self._current_text()
        if not text.strip():
            self._status_label.set_text(_("Cannot save an empty config."))
            return
        self._populate_results(validate_config(text))
        try:
            backup = save_config_text(self._path, text)
        except Exception as exc:
            self._status_label.set_text(_("Save failed: {error}").format(error=exc))
            return
        self._dirty = False
        self._set_editing(False)
        self._status_label.set_text(_("Saved. Backup at {name}.").format(name=backup.name))

    def _on_close_response(self, dialog: Gtk.Dialog, response: int) -> None:
        if self._dirty and response in (Gtk.ResponseType.CLOSE, Gtk.ResponseType.DELETE_EVENT):
            if not confirm(
                self,
                _("Discard unsaved changes?"),
                _("Your edits to {path} have not been saved.").format(path=self._path),
                confirm_label=_("_Discard Changes"),
                destructive=True,
            ):
                dialog.stop_emission_by_name("response")

    # --------------------------------------------------------- import/export
    def _load_imported(self, text: str, source: str, *, git_hint: bool = False) -> None:
        self._set_editing(True)
        self._buffer.set_text(text)
        self._dirty = True
        self._populate_results(validate_config(text))
        extra = _(" (Git repository detected; save, then commit manually.)") if git_hint else ""
        self._status_label.set_text(_("Imported from {source}. Review and Save to apply.").format(source=source) + extra)

    def _import_file(self) -> None:
        src = choose_open_file(self, _("Import SSH Config"))
        if not src:
            return
        try:
            text = Path(src).read_text(encoding="utf-8")
        except Exception as exc:
            self._status_label.set_text(_("Import failed: {error}").format(error=exc))
            return
        self._load_imported(text, src)

    def _import_directory(self) -> None:
        directory = choose_folder(self, _("Import from Directory"))
        if not directory:
            return
        dir_path = Path(directory)
        target = dir_path / "ssh_config_export.conf"
        if not target.exists():
            picked = choose_open_file(self, _("Pick Config File"), current_folder=str(dir_path))
            if not picked:
                return
            target = Path(picked)
        try:
            text = target.read_text(encoding="utf-8")
        except Exception as exc:
            self._status_label.set_text(_("Import failed: {error}").format(error=exc))
            return
        self._load_imported(text, str(target), git_hint=(dir_path / ".git").exists())

    def _export_file(self) -> None:
        name = f"ssh_config_export_{time.strftime('%Y%m%d-%H%M%S')}.conf"
        target = choose_save_file(self, _("Export SSH Config"), current_name=name)
        if not target:
            return
        try:
            write_text_atomic(Path(target), self._current_text())
            self._status_label.set_text(_("Exported to {path}.").format(path=target))
        except Exception as exc:
            self._status_label.set_text(_("Export failed: {error}").format(error=exc))

    def _export_directory(self) -> None:
        directory = choose_folder(self, _("Export to Directory"))
        if not directory:
            return

        def validate(name: str):
            try:
                export_path_for_directory(Path(directory), name)
            except ValueError as exc:
                return str(exc)
            return None

        name = prompt_text(
            self,
            _("Export File Name"),
            _("File name inside {path}:").format(path=directory),
            f"ssh_config_export_{time.strftime('%Y%m%d-%H%M%S')}.conf",
            validator=validate,
            accept_label=_("_Export"),
        )
        if not name:
            return
        target_path = export_path_for_directory(Path(directory), name)
        if target_path.exists() and not confirm(
            self,
            _("Replace “{name}”?").format(name=target_path.name),
            _("A file with this name already exists in {path}.").format(path=directory),
            confirm_label=_("_Replace"),
            destructive=True,
        ):
            return
        try:
            write_text_atomic(target_path, self._current_text())
        except Exception as exc:
            self._status_label.set_text(_("Export failed: {error}").format(error=exc))
            return
        extra = _(" (Git repository detected; commit manually.)") if (Path(directory) / ".git").exists() else ""
        self._status_label.set_text(_("Exported to {path}.").format(path=target_path) + extra)
