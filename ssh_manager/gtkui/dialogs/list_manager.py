"""Shared list-management dialog (credentials, vault entries, …)."""

from __future__ import annotations

from typing import Callable, List, Optional, Sequence, Tuple

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # type: ignore  # noqa: E402

from ..widgets.icons import make_icon  # noqa: E402
from ..constants import Icons  # noqa: E402
from ..i18n import _  # noqa: E402
from ..theme.tokens import Space  # noqa: E402
from ..widgets.empty_state import EmptyState  # noqa: E402
from ..widgets.icon_button import set_accessible_name  # noqa: E402
from .base import BaseDialog, DialogSize  # noqa: E402


class ListManagerDialog(BaseDialog):
    """Toolbar + list + empty state + selection-aware buttons.

    Subclasses provide the rows via :meth:`load_rows` (``[(id, col1, col2, …)]``)
    and implement :meth:`on_add`, :meth:`on_edit` and :meth:`on_delete`.
    """

    def __init__(
        self,
        parent: Optional[Gtk.Window],
        title: str,
        columns: Sequence[str],
        *,
        empty_icon: str,
        empty_title: str,
        empty_description: str = "",
        add_label: str = "",
        size: DialogSize = DialogSize.MEDIUM,
        height: int = 420,
    ) -> None:
        BaseDialog.__init__(self, parent, title, size=size, height=height)
        self._columns = list(columns)
        self._build_toolbar(add_label)
        self._build_list(title, empty_icon, empty_title, empty_description, add_label)

        self.hint = Gtk.Label()
        self.hint.set_xalign(0.0)
        self.hint.set_line_wrap(True)
        self.hint.get_style_context().add_class("form-help")
        self.hint.set_no_show_all(True)
        self.add_body(self.hint)

        close = self.add_primary(_("_Close"), Gtk.ResponseType.CLOSE)
        close.get_style_context().remove_class("suggested-action")

    def _build_toolbar(self, add_label: str) -> None:
        self.toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.S)
        self.add_button_widget = Gtk.Button.new_with_mnemonic(add_label or _("_Add…"))
        self.add_button_widget.set_image(make_icon(Icons.NEW, 16))
        self.add_button_widget.set_always_show_image(True)
        self.add_button_widget.connect("clicked", lambda *_: self.on_add())
        self.edit_button = Gtk.Button.new_with_mnemonic(_("_Edit…"))
        self.edit_button.connect("clicked", lambda *_: self._edit_selected())
        self.delete_button = Gtk.Button.new_with_mnemonic(_("_Delete…"))
        self.delete_button.get_style_context().add_class("destructive-action")
        self.delete_button.connect("clicked", lambda *_: self._delete_selected())
        for widget in (self.add_button_widget, self.edit_button):
            self.toolbar.pack_start(widget, False, False, 0)
        self.toolbar.pack_end(self.delete_button, False, False, 0)
        self.add_body(self.toolbar)

    def _build_list(self, title: str, empty_icon: str, empty_title: str, empty_description: str, add_label: str) -> None:
        self.store = Gtk.ListStore(*([str] * (len(self._columns) + 1)))
        self.view = Gtk.TreeView(model=self.store)
        set_accessible_name(self.view, title)
        for index, column_title in enumerate(self._columns, start=1):
            renderer = Gtk.CellRendererText()
            column = Gtk.TreeViewColumn(column_title, renderer, text=index)
            column.set_resizable(True)
            column.set_expand(True)
            self.view.append_column(column)
        self.view.connect("row-activated", lambda *_: self._edit_selected())
        self.view.get_selection().connect("changed", lambda *_: self.update_buttons())
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.set_shadow_type(Gtk.ShadowType.IN)
        scroller.add(self.view)

        self.empty_state = EmptyState(
            empty_icon,
            empty_title,
            empty_description,
            action_label=add_label or _("_Add…"),
            on_action=lambda: self.on_add(),
            compact=True,
        )
        self.list_stack = Gtk.Stack()
        self.list_stack.add_named(scroller, "list")
        self.list_stack.add_named(self.empty_state, "empty")
        # Gtk.Stack only switches to visible children, so show them up front.
        scroller.show_all()
        self.empty_state.show_all()
        self.add_body(self.list_stack, expand=True)

    # ----------------------------------------------------------- overrides
    def load_rows(self) -> List[Tuple[str, ...]]:
        return []

    def on_add(self) -> None:
        raise NotImplementedError

    def on_edit(self, item_id: str) -> None:
        raise NotImplementedError

    def on_delete(self, item_id: str) -> None:
        raise NotImplementedError

    def can_modify(self) -> bool:
        return True

    # ------------------------------------------------------------- helpers
    def set_hint(self, text: str) -> None:
        self.hint.set_text(text)
        self.hint.set_visible(bool(text))

    def refresh(self) -> None:
        selected = self.selected_id()
        self.store.clear()
        rows = self.load_rows()
        for row in rows:
            self.store.append(list(row))
        self.list_stack.set_visible_child_name("list" if rows else "empty")
        if selected:
            self.select_id(selected)
        self.update_buttons()

    def selected_id(self) -> Optional[str]:
        model, tree_iter = self.view.get_selection().get_selected()
        if model is None or tree_iter is None:
            return None
        return model.get_value(tree_iter, 0)

    def select_id(self, item_id: str) -> None:
        tree_iter = self.store.get_iter_first()
        while tree_iter is not None:
            if self.store.get_value(tree_iter, 0) == item_id:
                self.view.get_selection().select_iter(tree_iter)
                return
            tree_iter = self.store.iter_next(tree_iter)

    def update_buttons(self) -> None:
        modifiable = self.can_modify()
        has_selection = self.selected_id() is not None
        self.add_button_widget.set_sensitive(modifiable)
        self.edit_button.set_sensitive(modifiable and has_selection)
        self.delete_button.set_sensitive(modifiable and has_selection)
        if self.empty_state.primary_button is not None:
            self.empty_state.primary_button.set_sensitive(modifiable)

    def _edit_selected(self) -> None:
        item_id = self.selected_id()
        if item_id and self.can_modify():
            self.on_edit(item_id)

    def _delete_selected(self) -> None:
        item_id = self.selected_id()
        if item_id and self.can_modify():
            self.on_delete(item_id)


def run_list_dialog(dialog: ListManagerDialog, on_close: Optional[Callable[[], None]] = None) -> None:
    dialog.refresh()
    dialog.run_and_destroy()
    if on_close is not None:
        on_close()
