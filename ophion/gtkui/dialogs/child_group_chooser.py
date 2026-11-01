"""Choose (or create) the child group of a top-level group: a search entry over the list of groups.

Used by *New Host…* and by dropping hosts on a top-level group. Inventories with
a thousand child groups under one top-level group are common, so the groups are
in a list (never in the message) and the dialog keeps a size that fits the
screen. Typing filters the list; a name that matches no group offers "Create new
group “<name>”".
"""

from __future__ import annotations

from typing import Callable, Optional, Sequence, Tuple

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Pango  # type: ignore  # noqa: E402

from ..i18n import _  # noqa: E402
from ..widgets.searchable_list import SearchableList  # noqa: E402
from .base import BaseDialog, DialogSize, is_accept  # noqa: E402

Validator = Callable[[str], Optional[str]]


class ChildGroupChooserDialog(BaseDialog):
    """``selected()`` → ``(child, create_child)`` or ``None``."""

    def __init__(self, parent_window: Optional[Gtk.Window], title: str, message: str, parent_group: str,
                 children: Sequence[str], *, allow_new: bool = True, initial: Optional[str] = None,
                 accept_label: str = "", normalize: Callable[[str], str] = lambda name: name.strip(),
                 validator: Optional[Validator] = None) -> None:
        BaseDialog.__init__(self, parent_window, title, size=DialogSize.MEDIUM, resizable=True)
        self.parent_group = parent_group
        self._children = list(children)
        self._names = set(self._children)
        self._normalize = normalize
        self._name_validator = validator
        if message:
            label = Gtk.Label(label=message)
            label.set_xalign(0.0)
            label.set_line_wrap(True)
            label.set_max_width_chars(60)
            label.set_lines(3)
            label.set_ellipsize(Pango.EllipsizeMode.END)  # a short message, never a list
            self.add_body(label)
        self.list = SearchableList(
            [(name, name) for name in self._children], allow_new=allow_new,
            initial=initial if initial in self._names else None,
            search_label=_("Search the child groups of “{group}”").format(group=parent_group),
            list_label=_("Child groups of “{group}”").format(group=parent_group),
            on_activate=lambda: self.response(Gtk.ResponseType.OK))
        self.add_body(self.list, expand=True)
        self.error = Gtk.Label()
        self.error.set_xalign(0.0)
        self.error.set_line_wrap(True)
        self.error.get_style_context().add_class("form-error")
        self.error.set_no_show_all(True)
        self.add_body(self.error)
        self.add_cancel()
        self.add_primary(accept_label or _("_Choose"))
        self.set_initial_focus(self.list.entry)
        self.set_response_validator(self._validate)
        self.set_default_size(-1, 460)

    def selected(self) -> Optional[Tuple[str, bool]]:
        choice = self.list.selected()
        if choice is None:
            return None
        value, is_new = choice
        if not is_new:
            return value, False
        child = self._normalize(value)
        if not child:
            return None
        return child, child not in self._names

    def error_for(self) -> Optional[str]:
        choice = self.list.selected()
        if choice is None:
            return _("Choose a group, or type the name of a new one.")
        value, is_new = choice
        if is_new:
            if self._name_validator is not None:
                error = self._name_validator(value)
                if error:
                    return error
            if not self._normalize(value):
                return _("A value is required.")
        return None

    def _validate(self, response: int) -> bool:
        if not is_accept(response):
            return True
        error = self.error_for()
        if error:
            self.error.set_text(error)
            self.error.show()
            self.list.entry.grab_focus()
            return False
        self.error.hide()
        return True

    def ask(self) -> Optional[Tuple[str, bool]]:
        self.show_all()
        try:
            response = int(self.run())
            return self.selected() if is_accept(response) else None
        finally:
            self.destroy()


__all__ = ["ChildGroupChooserDialog"]
