"""File chooser helpers (replace repeated ``Gtk.FileChooserDialog`` boilerplate)."""

from __future__ import annotations

from typing import Optional, Sequence, Tuple

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # type: ignore  # noqa: E402

from ..i18n import _  # noqa: E402


def _chooser(parent: Optional[Gtk.Window], title: str, action: Gtk.FileChooserAction, accept: str) -> Gtk.FileChooserDialog:
    dialog = Gtk.FileChooserDialog(title=title, transient_for=parent, modal=True, action=action)
    dialog.add_button(_("_Cancel"), Gtk.ResponseType.CANCEL)
    button = dialog.add_button(accept, Gtk.ResponseType.ACCEPT)
    button.get_style_context().add_class("suggested-action")
    dialog.set_default_response(Gtk.ResponseType.ACCEPT)
    dialog.get_style_context().add_class("sm-dialog")
    return dialog


def _run(dialog: Gtk.FileChooserDialog) -> Optional[str]:
    try:
        response = dialog.run()
        return dialog.get_filename() if response == Gtk.ResponseType.ACCEPT else None
    finally:
        dialog.destroy()


def choose_save_file(
    parent: Optional[Gtk.Window],
    title: str,
    *,
    current_name: str = "",
    current_file: str = "",
    accept_label: str = "",
) -> Optional[str]:
    dialog = _chooser(parent, title, Gtk.FileChooserAction.SAVE, accept_label or _("_Save"))
    dialog.set_do_overwrite_confirmation(True)
    if current_file:
        dialog.set_filename(current_file)
    elif current_name:
        dialog.set_current_name(current_name)
    return _run(dialog)


def choose_open_file(
    parent: Optional[Gtk.Window],
    title: str,
    *,
    current_folder: str = "",
    accept_label: str = "",
    filters: Sequence[Tuple[str, Sequence[str]]] = (),
) -> Optional[str]:
    """Pick a file; ``filters`` = ``[(label, ["*.ext", …]), …]`` (an "All files" filter is added)."""

    dialog = _chooser(parent, title, Gtk.FileChooserAction.OPEN, accept_label or _("_Open"))
    if current_folder:
        dialog.set_current_folder(current_folder)
    if filters:
        for label, patterns in list(filters) + [(_("All files"), ["*"])]:
            file_filter = Gtk.FileFilter()
            file_filter.set_name(label)
            for pattern in patterns:
                file_filter.add_pattern(pattern)
            dialog.add_filter(file_filter)
    return _run(dialog)


def choose_folder(
    parent: Optional[Gtk.Window],
    title: str,
    *,
    current_folder: str = "",
    accept_label: str = "",
) -> Optional[str]:
    dialog = _chooser(parent, title, Gtk.FileChooserAction.SELECT_FOLDER, accept_label or _("_Select"))
    if current_folder:
        dialog.set_current_folder(current_folder)
    return _run(dialog)
