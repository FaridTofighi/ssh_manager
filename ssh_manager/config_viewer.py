"""Read-only SSH config viewer with lightweight validation."""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple
import shutil
import time

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Pango  # type: ignore  # noqa: E402

from .config import split_config_directive
from .persistence import write_text_atomic


@dataclass
class ValidationResult:
    severity: str  # "warning" | "error" | "info"
    message: str
    line: int


def _safe_read(path: Path) -> Tuple[str, str]:
    if not path.exists():
        return "", f"File not found: {path}"
    try:
        return path.read_text(encoding="utf-8"), ""
    except OSError as exc:
        return "", f"Unable to read {path}: {exc}"


def validate_config(text: str) -> List[ValidationResult]:
    """Lightweight, non-blocking lint: duplicate Host, unknown tokens, malformed lines."""
    results: List[ValidationResult] = []
    seen_hosts = {}

    for idx, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, value = split_config_directive(line)
        if not key:
            continue
        key_lower = key.lower()
        if value is None and key_lower != "match":
            results.append(ValidationResult("warning", f"Directive '{key}' has no value.", idx))
            continue
        if key_lower == "host":
            try:
                aliases = shlex.split(value or "")
            except ValueError:
                aliases = (value or "").split()
            for alias in aliases:
                if alias in seen_hosts:
                    results.append(ValidationResult("warning", f"Duplicate Host alias '{alias}' (first at line {seen_hosts[alias]}).", idx))
                else:
                    seen_hosts[alias] = idx
        else:
            # Simple check: warn on obviously odd directives (non-alnum starting)
            if not key_lower.replace("-", "").isalpha():
                results.append(ValidationResult("warning", f"Unrecognized directive '{key}'.", idx))
        # Very basic malformed token detection
        try:
            shlex.split(raw_line)
        except ValueError:
            results.append(ValidationResult("error", "Malformed line (unbalanced quotes).", idx))
    return results


def save_config_text(path: Path, text: str) -> Path:
    """Persist edited config text atomically and return the backup path used."""

    if not text.strip():
        raise ValueError("Cannot save an empty config.")
    backup = path.with_suffix(path.suffix + ".bak")
    try:
        if path.exists():
            shutil.copy2(path, backup)
    except OSError:
        pass
    write_text_atomic(path, text)
    return backup


def export_path_for_directory(directory: Path, filename: str) -> Path:
    """Return a safe export path constrained to the selected directory."""

    if any(ord(ch) < 32 for ch in filename):
        raise ValueError("Export filename must not contain path separators or control characters.")
    name = filename.strip()
    if not name:
        raise ValueError("Export filename cannot be empty.")
    if any(ch in name for ch in ("/", "\\")):
        raise ValueError("Export filename must not contain path separators or control characters.")
    if name in {".", ".."} or Path(name).is_absolute():
        raise ValueError("Export filename must be a file name, not a path.")
    return directory / name


class ConfigViewerDialog(Gtk.Dialog):
    """Modal dialog that shows SSH config with lint results and optional editing."""

    def __init__(self, parent: Gtk.Window, path: Path) -> None:
        Gtk.Dialog.__init__(self, title="SSH Config Viewer", transient_for=parent, modal=True)
        self.add_button("_Close", Gtk.ResponseType.CLOSE)
        self.set_default_size(800, 600)

        box = self.get_content_area()
        box.set_border_width(10)
        box.set_spacing(8)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        header_label = Gtk.Label(label=str(path))
        header_label.set_xalign(0.0)
        header.pack_start(header_label, True, True, 0)
        box.pack_start(header, False, False, 0)

        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        box.pack_start(paned, True, True, 0)

        self._text_view = Gtk.TextView()
        self._text_view.set_editable(False)
        self._text_view.set_monospace(True)
        self._text_view.modify_font(Pango.FontDescription("Monospace 10"))
        text_scroller = Gtk.ScrolledWindow()
        text_scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        text_scroller.add(self._text_view)

        paned.pack1(text_scroller, resize=True, shrink=False)

        warning_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        warning_label = Gtk.Label(label="Warnings")
        warning_label.set_xalign(0.0)
        warning_box.pack_start(warning_label, False, False, 0)
        self._list_store = Gtk.ListStore(str, int)  # message, line
        self._tree = Gtk.TreeView(model=self._list_store)
        renderer = Gtk.CellRendererText()
        column = Gtk.TreeViewColumn("Warnings", renderer, text=0)
        self._tree.append_column(column)
        self._tree.connect("row-activated", self._on_row_activated)
        warn_scroller = Gtk.ScrolledWindow()
        warn_scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        warn_scroller.add(self._tree)
        warn_scroller.set_min_content_width(280)
        warning_box.pack_start(warn_scroller, True, True, 0)
        paned.pack2(warning_box, resize=False, shrink=False)

        self._buffer = self._text_view.get_buffer()
        self._path = path
        self._editing = False

        action_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._edit_toggle = Gtk.ToggleButton(label="Edit")
        self._edit_toggle.connect("toggled", self._on_edit_toggled)
        self._save_button = Gtk.Button(label="Save")
        self._save_button.connect("clicked", self._on_save_clicked)
        self._save_button.set_sensitive(False)
        export_btn = Gtk.Button(label="Export…")
        export_btn.connect("clicked", self._on_export_clicked)
        export_dir_btn = Gtk.Button(label="Export to Directory…")
        export_dir_btn.connect("clicked", self._on_export_dir_clicked)
        import_btn = Gtk.Button(label="Import…")
        import_btn.connect("clicked", self._on_import_clicked)
        import_dir_btn = Gtk.Button(label="Import from Directory…")
        import_dir_btn.connect("clicked", self._on_import_dir_clicked)
        action_row.pack_start(self._edit_toggle, False, False, 0)
        action_row.pack_start(self._save_button, False, False, 0)
        action_row.pack_start(export_btn, False, False, 0)
        action_row.pack_start(export_dir_btn, False, False, 0)
        action_row.pack_start(import_btn, False, False, 0)
        action_row.pack_start(import_dir_btn, False, False, 0)
        box.pack_start(action_row, False, False, 0)

        self._status_label = Gtk.Label()
        self._status_label.set_xalign(0.0)
        box.pack_start(self._status_label, False, False, 0)

        self._load()
        self.show_all()

    def _load(self) -> None:
        text, error = _safe_read(self._path)
        if error:
            self._buffer.set_text(error)
            return
        if not text.strip():
            self._buffer.set_text(f"No content found in {self._path}")
            return
        self._buffer.set_text(text)
        self._populate_results(validate_config(text))

    def _populate_results(self, results: List[ValidationResult]) -> None:
        self._list_store.clear()
        for res in results:
            prefix = {"warning": "⚠ ", "error": "⛔ ", "info": "ℹ "}.get(res.severity, "")
            self._list_store.append([f"{prefix}{res.message} (line {res.line})", res.line])

    def _on_row_activated(self, _view, path, _column) -> None:
        model = self._tree.get_model()
        tree_iter = model.get_iter(path)
        if not tree_iter:
            return
        line = model.get_value(tree_iter, 1)
        if not isinstance(line, int):
            return
        self._scroll_to_line(line)

    def _scroll_to_line(self, line: int) -> None:
        buf = self._buffer
        iter_ = buf.get_iter_at_line(max(0, line - 1))
        mark = buf.create_mark(None, iter_, left_gravity=True)
        self._text_view.scroll_to_mark(mark, 0.25, False, 0, 0)

    # -------------------------------------------------------------- editing
    def _on_edit_toggled(self, button: Gtk.ToggleButton) -> None:
        self._editing = button.get_active()
        self._text_view.set_editable(self._editing)
        self._save_button.set_sensitive(self._editing)
        self._status_label.set_text("Editing…" if self._editing else "")

    def _on_save_clicked(self, _button: Gtk.Button) -> None:
        if not self._editing:
            return
        start, end = self._buffer.get_bounds()
        text = self._buffer.get_text(start, end, False)
        if not text.strip():
            self._status_label.set_text("Cannot save an empty config.")
            return
        # Validate (non-blocking)
        self._populate_results(validate_config(text))
        try:
            backup = save_config_text(self._path, text)
            self._status_label.set_text(f"Saved. Backup at {backup.name}.")
            self._editing = False
            self._edit_toggle.set_active(False)
            self._text_view.set_editable(False)
            self._save_button.set_sensitive(False)
        except Exception as exc:
            self._status_label.set_text(f"Save failed: {exc}")

    def _on_export_clicked(self, _button: Gtk.Button) -> None:
        start, end = self._buffer.get_bounds()
        text = self._buffer.get_text(start, end, False)
        chooser = Gtk.FileChooserDialog(
            title="Export SSH Config",
            parent=self,
            action=Gtk.FileChooserAction.SAVE,
            buttons=(
                Gtk.STOCK_CANCEL,
                Gtk.ResponseType.CANCEL,
                Gtk.STOCK_SAVE,
                Gtk.ResponseType.OK,
            ),
        )
        ts_suffix = time.strftime("%Y%m%d-%H%M%S")
        chooser.set_current_name(f"ssh_config_export_{ts_suffix}.conf")
        response = chooser.run()
        target = chooser.get_filename() if response == Gtk.ResponseType.OK else None
        chooser.destroy()
        if not target:
            return
        try:
            write_text_atomic(Path(target), text)
            self._status_label.set_text(f"Exported to {target}.")
        except Exception as exc:
            self._status_label.set_text(f"Export failed: {exc}")

    def _on_export_dir_clicked(self, _button: Gtk.Button) -> None:
        start, end = self._buffer.get_bounds()
        text = self._buffer.get_text(start, end, False)
        chooser = Gtk.FileChooserDialog(
            title="Export SSH Config to Directory",
            parent=self,
            action=Gtk.FileChooserAction.SELECT_FOLDER,
            buttons=(
                Gtk.STOCK_CANCEL,
                Gtk.ResponseType.CANCEL,
                Gtk.STOCK_SAVE,
                Gtk.ResponseType.OK,
            ),
        )
        response = chooser.run()
        directory = chooser.get_filename() if response == Gtk.ResponseType.OK else None
        chooser.destroy()
        if not directory:
            return
        default_name = f"ssh_config_export_{time.strftime('%Y%m%d-%H%M%S')}.conf"
        save_dialog = Gtk.Dialog(title="File Name", transient_for=self, modal=True)
        save_dialog.add_button("_Cancel", Gtk.ResponseType.CANCEL)
        save_dialog.add_button("_OK", Gtk.ResponseType.OK)
        entry = Gtk.Entry()
        entry.set_text(default_name)
        box = save_dialog.get_content_area()
        box.set_border_width(8)
        box.pack_start(Gtk.Label(label="Export filename"), False, False, 0)
        box.pack_start(entry, False, False, 0)
        save_dialog.show_all()
        resp = save_dialog.run()
        name = entry.get_text().strip()
        save_dialog.destroy()
        if resp != Gtk.ResponseType.OK or not name:
            return
        try:
            target_path = export_path_for_directory(Path(directory), name)
        except ValueError as exc:
            self._status_label.set_text(str(exc))
            return
        if target_path.exists():
            confirm = Gtk.MessageDialog(
                transient_for=self,
                modal=True,
                buttons=Gtk.ButtonsType.OK_CANCEL,
                message_type=Gtk.MessageType.QUESTION,
                text=f"Overwrite existing file '{target_path.name}'?",
            )
            resp2 = confirm.run()
            confirm.destroy()
            if resp2 != Gtk.ResponseType.OK:
                return
        try:
            write_text_atomic(target_path, text)
            extra = ""
            if (Path(directory) / ".git").exists():
                extra = " (Git repository detected; commit manually.)"
            self._status_label.set_text(f"Exported to {target_path}.{extra}")
        except Exception as exc:
            self._status_label.set_text(f"Export failed: {exc}")

    def _on_import_clicked(self, _button: Gtk.Button) -> None:
        chooser = Gtk.FileChooserDialog(
            title="Import SSH Config",
            parent=self,
            action=Gtk.FileChooserAction.OPEN,
            buttons=(
                Gtk.STOCK_CANCEL,
                Gtk.ResponseType.CANCEL,
                Gtk.STOCK_OPEN,
                Gtk.ResponseType.OK,
            ),
        )
        response = chooser.run()
        src = chooser.get_filename() if response == Gtk.ResponseType.OK else None
        chooser.destroy()
        if not src:
            return
        try:
            text = Path(src).read_text(encoding="utf-8")
        except Exception as exc:
            self._status_label.set_text(f"Import failed: {exc}")
            return
        buf = self._text_view.get_buffer()
        buf.set_text(text)
        self._editing = True
        self._edit_toggle.set_active(True)
        self._text_view.set_editable(True)
        self._save_button.set_sensitive(True)
        self._status_label.set_text(f"Imported from {src}. Review and Save to apply.")

    def _on_import_dir_clicked(self, _button: Gtk.Button) -> None:
        chooser = Gtk.FileChooserDialog(
            title="Select Directory to Import From",
            parent=self,
            action=Gtk.FileChooserAction.SELECT_FOLDER,
            buttons=(
                Gtk.STOCK_CANCEL,
                Gtk.ResponseType.CANCEL,
                Gtk.STOCK_OPEN,
                Gtk.ResponseType.OK,
            ),
        )
        response = chooser.run()
        directory = chooser.get_filename() if response == Gtk.ResponseType.OK else None
        chooser.destroy()
        if not directory:
            return
        dir_path = Path(directory)
        default_file = dir_path / "ssh_config_export.conf"
        if default_file.exists():
            target_file = default_file
        else:
            file_chooser = Gtk.FileChooserDialog(
                title="Pick Config File",
                parent=self,
                action=Gtk.FileChooserAction.OPEN,
                buttons=(
                    Gtk.STOCK_CANCEL,
                    Gtk.ResponseType.CANCEL,
                    Gtk.STOCK_OPEN,
                    Gtk.ResponseType.OK,
                ),
            )
            file_chooser.set_current_folder(str(dir_path))
            resp = file_chooser.run()
            target = file_chooser.get_filename() if resp == Gtk.ResponseType.OK else None
            file_chooser.destroy()
            if not target:
                return
            target_file = Path(target)
        try:
            text = target_file.read_text(encoding="utf-8")
        except Exception as exc:
            self._status_label.set_text(f"Import failed: {exc}")
            return
        buf = self._text_view.get_buffer()
        buf.set_text(text)
        self._editing = True
        self._edit_toggle.set_active(True)
        self._text_view.set_editable(True)
        self._save_button.set_sensitive(True)
        extra = ""
        if (dir_path / ".git").exists():
            extra = " (Git repository detected; apply via Save then commit manually.)"
        self._status_label.set_text(f"Imported from {target_file}.{extra}")
