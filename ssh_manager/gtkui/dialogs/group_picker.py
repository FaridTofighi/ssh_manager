"""Choose (or create) a parent/child group destination."""

from __future__ import annotations

from typing import Optional, Tuple

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # type: ignore  # noqa: E402

from ...config import ConfigManager  # noqa: E402
from ..i18n import _  # noqa: E402
from ..validation import validate_child_path  # noqa: E402
from ..widgets.forms import FormSection  # noqa: E402
from .base import BaseDialog, DialogSize, is_accept  # noqa: E402


class GroupPickerDialog(BaseDialog):
    """Pick an existing child group, or type the name of a new one."""

    def __init__(
        self,
        parent: Gtk.Window,
        manager: ConfigManager,
        title: str,
        *,
        allow_new_child: bool = False,
        initial_parent: Optional[str] = None,
        initial_child: Optional[str] = None,
        accept_label: str = "",
    ) -> None:
        BaseDialog.__init__(self, parent, title, size=DialogSize.SMALL, resizable=False)
        self._manager = manager
        self._allow_new_child = allow_new_child

        section = FormSection()
        self._parent_combo = Gtk.ComboBoxText()
        parents = sorted(manager.tree.keys(), key=str.lower)
        for name in parents:
            self._parent_combo.append_text(name)
        if initial_parent and initial_parent in parents:
            self._parent_combo.set_active(parents.index(initial_parent))
        elif parents:
            self._parent_combo.set_active(0)
        section.add(_("_Parent group"), self._parent_combo)

        self._child_combo = Gtk.ComboBoxText()
        section.add(_("_Child group"), self._child_combo)

        self._new_child_entry = Gtk.Entry()
        self._new_child_entry.set_placeholder_text(_("new-group.conf"))
        self._new_child_entry.set_activates_default(True)
        self._new_row = None
        if allow_new_child:
            self._new_row = section.add(
                _("_New child group"),
                self._new_child_entry,
                help_text=_("Leave empty to use the selected child group."),
                validator=lambda value: validate_child_path(value) if value.strip() else None,
            )
        self.add_body(section)

        self._error = Gtk.Label()
        self._error.set_xalign(0.0)
        self._error.get_style_context().add_class("form-error")
        self._error.set_no_show_all(True)
        self.add_body(self._error)

        self.add_cancel()
        self.add_primary(accept_label or _("_Apply"))
        self.set_response_validator(self._validate)
        self.set_initial_focus(self._parent_combo)
        self._parent_combo.connect("changed", lambda *_: self._refresh_child_combo(None))
        self._refresh_child_combo(initial_child)
        self.show_all()

    def selected(self) -> Optional[Tuple[str, str, bool]]:
        """``(parent, child, create_child)`` or ``None``."""

        parent = self._parent_combo.get_active_text()
        if not parent:
            return None
        new_child = self._new_child_entry.get_text().strip() if self._allow_new_child else ""
        if new_child:
            return parent, new_child, True
        child = self._child_combo.get_active_text()
        if not child:
            return None
        return parent, child, False

    def _validate(self, response: int) -> bool:
        if not is_accept(response):
            return True
        if self._new_row is not None and not self._new_row.validate():
            return False
        if self.selected() is None:
            message = (
                _("Choose a child group or enter a new name.")
                if self._allow_new_child
                else _("Choose a child group.")
            )
            self._error.set_text(message)
            self._error.show()
            return False
        self._error.hide()
        return True

    def _refresh_child_combo(self, initial_child: Optional[str]) -> None:
        self._child_combo.remove_all()
        parent = self._parent_combo.get_active_text()
        if not parent:
            return
        names = sorted(self._manager.tree.get(parent, {}).keys(), key=str.lower)
        for name in names:
            self._child_combo.append_text(name)
        if initial_child and initial_child in names:
            self._child_combo.set_active(names.index(initial_child))
        elif names:
            self._child_combo.set_active(0)
