"""Choose (or create) a parent/child group destination.

Up to :data:`LARGE` entries the parent and child groups are combo boxes; with more
(a top-level group with hundreds of child groups) a combo's menu would be taller
than the screen, so a :class:`SearchableList` takes its place. ``selected()``
answers the same either way.
"""

from __future__ import annotations

from typing import Optional, Tuple

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # type: ignore  # noqa: E402

from ...config import ConfigManager  # noqa: E402
from ..i18n import _  # noqa: E402
from ..constants import Icons  # noqa: E402
from ..theme.tokens import Size  # noqa: E402
from ..validation import validators_for  # noqa: E402
from ..widgets.icons import add_combo_icon  # noqa: E402
from ..widgets.forms import FormSection  # noqa: E402
from ..widgets.searchable_list import SearchableList  # noqa: E402
from .base import BaseDialog, DialogSize, is_accept  # noqa: E402


LARGE = 30  # more entries than this: a searchable list instead of a combo box
LIST_HEIGHT = 4 * 26  # two lists may be shown: about four rows each (the dialog can be resized)


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
        parents = sorted(manager.tree.keys(), key=str.lower)
        large_parents = len(parents) > LARGE
        large_children = any(len(children) > LARGE for children in manager.tree.values())
        BaseDialog.__init__(self, parent, title, size=DialogSize.MEDIUM if large_parents or large_children
                            else DialogSize.SMALL, resizable=large_parents or large_children)
        self._manager = manager
        self._allow_new_child = allow_new_child

        section = FormSection()
        self._parent_combo = Gtk.ComboBoxText()
        self._parent_list: Optional[SearchableList] = None
        add_combo_icon(self._parent_combo, Icons.FOLDER, "icon_default", Size.ICON)  # as in the tree
        for name in parents:
            self._parent_combo.append_text(name)
        if initial_parent and initial_parent in parents:
            self._parent_combo.set_active(parents.index(initial_parent))
        elif parents:
            self._parent_combo.set_active(0)
        if large_parents:
            self._parent_list = SearchableList(
                [(name, name) for name in parents], initial=self._parent_combo.get_active_text(),
                search_label=_("Search the parent groups"), list_label=_("Parent groups"), min_height=LIST_HEIGHT)
            self._parent_list.view.get_selection().connect("changed", lambda *_: self._refresh_child_combo(None))
            section.add(_("_Parent group"), self._parent_list)
        else:
            section.add(_("_Parent group"), self._parent_combo)

        self._child_combo = Gtk.ComboBoxText()
        self._child_list: Optional[SearchableList] = None
        add_combo_icon(self._child_combo, Icons.FILE_CONF, "icon_default", Size.ICON)
        if large_children:
            self._child_list = SearchableList(search_label=_("Search the child groups"), list_label=_("Child groups"),
                                              min_height=LIST_HEIGHT)
            section.add(_("_Child group"), self._child_list)
        else:
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
                validator=lambda value: validators_for(manager)["child_path"](value) if value.strip() else None,
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
        self.set_initial_focus(self._parent_list.entry if self._parent_list is not None else self._parent_combo)
        self._parent_combo.connect("changed", lambda *_: self._refresh_child_combo(None))
        self._refresh_child_combo(initial_child)
        self.show_all()

    def selected(self) -> Optional[Tuple[str, str, bool]]:
        """``(parent, child, create_child)`` or ``None``."""

        parent = self._active_parent()
        if not parent:
            return None
        new_child = self._new_child_entry.get_text().strip() if self._allow_new_child else ""
        if new_child:
            return parent, new_child, True
        if self._child_list is not None:
            chosen = self._child_list.selected()
            if chosen is None:
                return None
            child = chosen[0]
        elif self._child_combo.get_active() < 0:
            return None
        else:
            child = self._child_combo.get_active_id() or ""  # "" = directly in the top-level group (app store)
        if not child and not getattr(self._manager, "allows_display_names", False):
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

    def _active_parent(self) -> Optional[str]:
        if self._parent_list is not None:
            chosen = self._parent_list.selected()
            return chosen[0] if chosen is not None else None
        return self._parent_combo.get_active_text()

    def _refresh_child_combo(self, initial_child: Optional[str]) -> None:
        self._child_combo.remove_all()
        parent = self._active_parent()
        if not parent:
            if self._child_list is not None:
                self._child_list.set_items([])
            return
        names = sorted((n for n in self._manager.tree.get(parent, {}) if n), key=str.lower)
        if getattr(self._manager, "allows_display_names", False):
            names.insert(0, "")  # hosts may sit directly in a top-level group
        if self._child_list is not None:
            labels = [(name, name or _("(directly in “{group}”)").format(group=parent)) for name in names]
            first = initial_child if initial_child in names else (names[0] if names else None)
            self._child_list.set_items(labels, first)
            return
        for name in names:
            self._child_combo.append(name or "", name or _("(directly in “{group}”)").format(group=parent))
        if initial_child and initial_child in names:
            self._child_combo.set_active(names.index(initial_child))
        elif names:
            self._child_combo.set_active(0)
