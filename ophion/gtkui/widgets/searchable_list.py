"""A search entry over a scrollable list that stays fast and small with thousands of entries.

One ``Gtk.ListStore`` (built once) behind a ``Gtk.TreeModelFilter`` and a
``Gtk.TreeView``: no widget per entry, filtering is one pass per keystroke, and
the list area has a fixed minimum height (it never sizes itself to its rows).
With ``allow_new`` a first row "Create new group “<text>”" appears while the
typed text matches no entry exactly (case-insensitive); it is selected then.
"""

from __future__ import annotations

from typing import Callable, Optional, Sequence, Tuple

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gtk, Pango  # type: ignore  # noqa: E402

from ..i18n import _, ngettext  # noqa: E402
from ..theme.tokens import Space  # noqa: E402
from .icon_button import set_accessible_name  # noqa: E402

COL_LABEL, COL_VALUE, COL_NEW = 0, 1, 2
LIST_MIN_HEIGHT = 8 * 26  # about eight rows


class SearchableList(Gtk.Box):
    """``[search] / [list] / "N groups"``; :meth:`selected` returns ``(value, is_new)``."""

    def __init__(self, items: Sequence[Tuple[str, str]] = (), *, allow_new: bool = False,
                 initial: Optional[str] = None, search_label: str = "", list_label: str = "",
                 placeholder: str = "", new_label: str = "",
                 on_activate: Optional[Callable[[], None]] = None, min_height: int = LIST_MIN_HEIGHT) -> None:
        Gtk.Box.__init__(self, orientation=Gtk.Orientation.VERTICAL, spacing=Space.XS)
        self.allow_new = allow_new
        self.new_label = new_label or _("Create new group “{name}”")
        self._on_activate = on_activate
        self._exact: dict = {}
        self._needle = ""
        self._quiet = False

        self.entry = Gtk.SearchEntry()
        self.entry.set_placeholder_text(placeholder or _("Search or type a new name") if allow_new
                                        else placeholder or _("Search"))
        self.entry.set_activates_default(True)
        set_accessible_name(self.entry, search_label or _("Search groups"))
        self.entry.connect("search-changed", lambda *_a: self.refilter())
        self.entry.connect("changed", lambda *_a: self.refilter())
        self.entry.connect("key-press-event", self._on_entry_key)
        self.pack_start(self.entry, False, False, 0)

        self.store = Gtk.ListStore(str, str, bool)
        self.filter = self.store.filter_new()
        self.filter.set_visible_func(self._visible)
        self.view = Gtk.TreeView(model=self.filter)
        self.view.set_headers_visible(False)
        self.view.set_enable_search(False)  # the entry above is the search
        self.view.set_fixed_height_mode(True)
        renderer = Gtk.CellRendererText()
        renderer.set_property("ellipsize", Pango.EllipsizeMode.MIDDLE)
        column = Gtk.TreeViewColumn("", renderer, text=COL_LABEL)
        column.set_sizing(Gtk.TreeViewColumnSizing.FIXED)
        column.set_expand(True)
        self.view.append_column(column)
        self.view.get_selection().set_mode(Gtk.SelectionMode.SINGLE)
        self.view.connect("row-activated", lambda *_a: self._activate())
        set_accessible_name(self.view, list_label or _("Groups"))
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_shadow_type(Gtk.ShadowType.IN)
        scroller.set_min_content_height(min_height)
        scroller.set_vexpand(True)
        scroller.add(self.view)
        self.scroller = scroller
        self.pack_start(scroller, True, True, 0)

        self.count = Gtk.Label()
        self.count.set_xalign(0.0)
        self.count.get_style_context().add_class("dim-label")
        self.count.get_style_context().add_class("caption")
        self.pack_start(self.count, False, False, 0)
        self.set_items(items, initial)

    # ------------------------------------------------------------- items
    def set_items(self, items: Sequence[Tuple[str, str]], initial: Optional[str] = None) -> None:
        """``items``: ``(value, label)`` pairs, in display order (the model is built once per call)."""

        self._quiet = True
        self.view.set_model(None)  # no per-row signals while filling
        self.store.clear()
        if self.allow_new:
            self.store.append(["", "", True])
        self._exact = {}
        for value, label in items:
            self.store.append([label, value, False])
            self._exact.setdefault(value.strip().casefold(), value)
        self.view.set_model(self.filter)
        self._quiet = False
        self.refilter(select=initial)

    def __len__(self) -> int:
        return len(self.store) - (1 if self.allow_new else 0)

    # ------------------------------------------------------------ filtering
    def _visible(self, model, it, _data) -> bool:
        if model[it][COL_NEW]:
            return bool(self._needle) and self._needle not in self._exact
        return not self._needle or self._needle in model[it][COL_LABEL].casefold() \
            or self._needle in model[it][COL_VALUE].casefold()

    def refilter(self, select: Optional[str] = None) -> None:
        if self._quiet:
            return
        text = self.entry.get_text().strip()
        self._needle = text.casefold()
        if self.allow_new and len(self.store):
            self.store[0][COL_LABEL] = self.new_label.format(name=text)
        self.filter.refilter()
        shown = self.filter.iter_n_children(None)
        offered = 1 if self.allow_new and self._needle and self._needle not in self._exact else 0
        visible = shown - offered
        self.count.set_text(ngettext("{n} group", "{n} groups", visible).format(n=visible) if not self._needle
                            else ngettext("{n} of {total} group", "{n} of {total} groups", len(self)).format(
                                n=visible, total=len(self)))
        if select is not None:
            self.select_value(select)
        elif self._needle in self._exact:
            self.select_value(self._exact[self._needle])
        elif offered:
            self._select_path(0)
        else:
            self.view.get_selection().unselect_all()

    # ------------------------------------------------------------ selection
    def _select_path(self, index: int) -> None:
        if 0 <= index < self.filter.iter_n_children(None):
            path = Gtk.TreePath.new_from_indices([index])
            self.view.get_selection().select_path(path)
            self.view.scroll_to_cell(path, None, False, 0.0, 0.0)

    def select_value(self, value: str) -> bool:
        for row in self.filter:
            if not row[COL_NEW] and row[COL_VALUE] == value:
                self.view.get_selection().select_iter(row.iter)
                self.view.scroll_to_cell(row.path, None, True, 0.5, 0.0)
                return True
        return False

    def selected(self) -> Optional[Tuple[str, bool]]:
        """``(value, is_new)``: an entry, or the typed text for the "Create" row; ``None``: nothing chosen."""

        model, it = self.view.get_selection().get_selected()
        if it is None:
            return None
        if model[it][COL_NEW]:
            return self.entry.get_text().strip(), True
        return model[it][COL_VALUE], False

    def visible_values(self):
        return [row[COL_VALUE] for row in self.filter if not row[COL_NEW]]

    def _on_entry_key(self, _entry, event: Gdk.EventKey) -> bool:
        if event.keyval not in (Gdk.KEY_Up, Gdk.KEY_Down, Gdk.KEY_Page_Up, Gdk.KEY_Page_Down):
            return False
        total = self.filter.iter_n_children(None)
        if not total:
            return True
        model, it = self.view.get_selection().get_selected()
        index = model.get_path(it).get_indices()[0] if it is not None else -1
        step = {Gdk.KEY_Up: -1, Gdk.KEY_Down: 1, Gdk.KEY_Page_Up: -8, Gdk.KEY_Page_Down: 8}[event.keyval]
        self._select_path(min(total - 1, max(0, index + step)))
        return True

    def _activate(self) -> None:
        if self._on_activate is not None and self.selected() is not None:
            self._on_activate()


__all__ = ["SearchableList"]
