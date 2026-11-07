"""Favorites section at the top of the sidebar's tree area::

    ▾ Favorites                              3
      [▣] web-01
      [▣] db-primary
      [▭] prod

Absent (never mapped, no height) while there are no favorites or a search or filter is active. The
header is a toggle that opens and closes the list. The list is a flat ``Gtk.TreeView`` painted by
the session tree's :class:`RelayRowRenderer` (same icons, colours, status badges and row pitch;
groups keep the host row pitch).

At most :data:`MAX_ROWS` rows show (5 Comfortable, 7 Compact); more scroll inside the list. The
main tree keeps at least :data:`MIN_TREE_ROWS` rows: on a short window the list gets fewer rows,
and with no room for one it closes to its header (not remembered). The view owns no selection of
its own: the controller mirrors the tree's selection into it.
"""

from __future__ import annotations

from typing import Callable, Optional, Sequence

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GObject, Gtk, Pango  # type: ignore  # noqa: E402

from ... import protocols  # noqa: E402
from ..constants import Icons, TreeColumn  # noqa: E402
from ..controllers.tree_controller import TreeRow  # noqa: E402
from ..i18n import _, ngettext  # noqa: E402
from ..theme import density as density_tokens  # noqa: E402
from ..theme.tokens import Layout  # noqa: E402
from ..widgets.icon_button import set_accessible_name  # noqa: E402
from ..widgets.icons import make_icon  # noqa: E402
from .session_tree import COLUMN_TYPES, RelayRowRenderer, row_height_for  # noqa: E402

MAX_ROWS = {False: 5, True: 7}  # visible rows by density (compact?) before the list scrolls
MIN_TREE_ROWS = 3  # the main tree never gets less room than this many rows


class FavoritesList(Gtk.TreeView):
    """One flat row per favorite."""

    def __init__(self) -> None:
        Gtk.TreeView.__init__(self, model=Gtk.ListStore(*COLUMN_TYPES))
        self.get_style_context().add_class("relay-tree")
        self.set_headers_visible(False)
        self.set_enable_search(False)
        self.set_tooltip_column(TreeColumn.TOOLTIP)
        self.get_selection().set_mode(Gtk.SelectionMode.SINGLE)
        set_accessible_name(self, _("Favorites"))
        self.icon_cache: dict = {}
        column = Gtk.TreeViewColumn()
        column.set_expand(True)
        self.row_renderer = RelayRowRenderer(self.icon_cache)
        self.row_renderer.set_padding(0, 0)
        self.row_renderer.set_property("flat", True)
        column.pack_start(self.row_renderer, True)
        column.set_cell_data_func(self.row_renderer, self._row_data)
        self.append_column(column)

    @staticmethod
    def _row_data(_column, cell, model, tree_iter, _data=None) -> None:
        cell.set_property("text", model.get_value(tree_iter, TreeColumn.TEXT) or "")
        cell.set_property("kind", model.get_value(tree_iter, TreeColumn.KIND) or "")
        cell.set_property("depth", 0)
        cell.set_property("count", int(model.get_value(tree_iter, TreeColumn.COUNT) or 0))
        cell.set_property("status", model.get_value(tree_iter, TreeColumn.STATUS) or "")
        cell.set_property("color", model.get_value(tree_iter, TreeColumn.COLOR) or "")
        cell.set_property("read_only", bool(model.get_value(tree_iter, TreeColumn.READ_ONLY)))
        cell.set_property("file_backed", bool(model.get_value(tree_iter, TreeColumn.FILE_BACKED)))
        host = model.get_value(tree_iter, TreeColumn.HOST)
        cell.set_property("protocol", protocols.protocol_of(host) if host is not None else "")
        cell.set_property("badge", protocols.badge_for(host) if host is not None else "")
        cell.set_property("expandable", False)
        cell.set_property("expanded", False)
        cell.set_property("gap", 0)

    def fill(self, rows: Sequence[TreeRow]) -> None:
        store = self.get_model()
        self.set_model(None)
        store.clear()
        for row in rows:
            store.insert_with_values(-1, list(range(len(COLUMN_TYPES))), [
                row.icon, row.text, row.kind.value, row.parent, row.child, row.alias,
                GObject.Value(GObject.TYPE_PYOBJECT, row.host), row.search, row.color, row.status,
                int(row.count or 0), row.tooltip, bool(row.read_only), bool(row.file_backed),
            ])
        self.set_model(store)

    def selected_index(self) -> Optional[int]:
        _model, tree_iter = self.get_selection().get_selected()
        return self.get_model().get_path(tree_iter).get_indices()[0] if tree_iter is not None else None

    def clear_icon_cache(self) -> None:
        self.icon_cache.clear()
        self.queue_draw()


class FavoritesSection(Gtk.Box):
    """Header toggle and list; hidden while empty."""

    def __init__(self) -> None:
        Gtk.Box.__init__(self, orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.get_style_context().add_class("favorites-section")
        self.on_expanded: Callable[[bool], None] = lambda _e: None
        self.on_selected: Callable[[int], None] = lambda _i: None
        self.on_activated: Callable[[int], None] = lambda _i: None
        self.on_context_menu: Callable[[Optional[Gdk.Event]], None] = lambda _e: None
        self.on_toggle_favorite: Callable[[int], None] = lambda _i: None
        self._count = 0
        self._expanded = True
        self._room = MAX_ROWS[False]  # rows the window has room for (fit())
        self._silent = False

        self.header = Gtk.ToggleButton()
        self.header.set_relief(Gtk.ReliefStyle.NONE)
        self.header.get_style_context().add_class("favorites-header")
        self.header.set_active(True)
        self._header_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self._chevron = make_icon(Icons.CHEVRON_DOWN, Layout.Tree.CHEVRON)
        self._header_box.pack_start(self._chevron, False, False, 0)
        title = Gtk.Label(label=_("Favorites"))
        title.set_xalign(0.0)
        title.set_ellipsize(Pango.EllipsizeMode.END)
        title.get_style_context().add_class("favorites-title")
        self._header_box.pack_start(title, True, True, 0)
        self.count_label = Gtk.Label()
        self.count_label.get_style_context().add_class("favorites-count")
        self._header_box.pack_end(self.count_label, False, False, 0)
        self.header.add(self._header_box)
        self.header.connect("toggled", self._on_header_toggled)
        self.pack_start(self.header, False, False, 0)

        self.list = FavoritesList()
        self.list.get_selection().connect("changed", self._on_selection_changed)
        self.list.connect("row-activated", lambda _v, path, _c: self.on_activated(path.get_indices()[0]))
        self.list.connect("button-press-event", self._on_button_press)
        self.list.connect("key-press-event", self._on_key_press)
        self.list.connect("popup-menu", lambda *_: self.on_context_menu(None) or True)
        self.scroller = Gtk.ScrolledWindow()
        self.scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scroller.set_overlay_scrolling(True)
        self.scroller.add(self.list)
        self.pack_start(self.scroller, False, False, 0)
        self.set_no_show_all(True)  # set_rows() decides
        self.apply_density()

    # ------------------------------------------------------------- layout
    def apply_density(self) -> None:
        tree = Layout.Tree
        self.set_margin_top(Layout.Sidebar.TREE_TOP_GAP)
        self.header.set_size_request(-1, self.row_height())
        self._header_box.set_spacing(0)
        self._chevron.set_pixel_size(tree.CHEVRON)
        self._chevron.set_margin_start(tree.LEAD)
        self._chevron.set_margin_end(tree.CHEVRON_GAP)
        self.count_label.set_margin_end(tree.COUNT_RIGHT)
        self.list.clear_icon_cache()
        self.list.get_column(0).queue_resize()  # row heights are cached per row: measure them again
        self.list.queue_resize()
        self._apply_height()

    def row_height(self) -> int:
        return row_height_for(self.list)

    def max_rows(self) -> int:
        return MAX_ROWS[density_tokens.active().compact]

    def visible_rows(self) -> int:
        """List rows shown without scrolling (0: closed, by the user or for lack of room)."""

        if not self._expanded:
            return 0
        return max(0, min(self._count, self.max_rows(), self._room))

    def fit(self, tree_area: int) -> None:
        """``tree_area``: the height the list and the main tree share; the tree keeps its minimum."""

        room = max(0, (tree_area - MIN_TREE_ROWS * row_height_for(self.list)) // self.row_height())
        if room != self._room:
            self._room = room
            self._apply_height()

    def _apply_height(self) -> None:
        rows = self.visible_rows()
        height = rows * self.row_height()
        self.scroller.set_min_content_height(-1)  # GTK wants min <= max at every step
        self.scroller.set_max_content_height(height)
        self.scroller.set_min_content_height(height)
        self.scroller.set_visible(rows > 0)
        self._sync_header()

    # ---------------------------------------------------------------- api
    def set_rows(self, rows: Sequence[TreeRow]) -> None:
        self._silent = True
        try:
            self.list.fill(rows)
        finally:
            self._silent = False
        self._count = len(rows)
        self.count_label.set_text(str(self._count))
        if rows:
            self.show()
            self.header.show_all()
            self.list.show()
        else:
            self.hide()
        self._apply_height()

    @property
    def count(self) -> int:
        return self._count

    @property
    def expanded(self) -> bool:
        return self._expanded

    def set_expanded(self, expanded: bool) -> None:
        if self.header.get_active() != expanded:
            self.header.set_active(expanded)  # -> _on_header_toggled
        else:
            self._expanded = expanded
            self._apply_height()

    def select_index(self, index: Optional[int]) -> None:
        """Mirror the tree's selection (no ``on_selected``)."""

        selection = self.list.get_selection()
        self._silent = True
        try:
            if index is None or index >= self._count:
                selection.unselect_all()
            else:
                path = Gtk.TreePath.new_from_indices([index])
                selection.select_path(path)
                if self.list.get_realized():
                    self.list.scroll_to_cell(path, None, False, 0.0, 0.0)
        finally:
            self._silent = False

    def update_row(self, index: int, status: str, tooltip: str) -> None:
        store = self.list.get_model()
        tree_iter = store.iter_nth_child(None, index)
        if tree_iter is not None:
            store.set_value(tree_iter, TreeColumn.STATUS, status)
            store.set_value(tree_iter, TreeColumn.TOOLTIP, tooltip)

    def selected_row_rect(self) -> Optional[Gdk.Rectangle]:
        index = self.list.selected_index()
        if index is None:
            return None
        return self.list.get_cell_area(Gtk.TreePath.new_from_indices([index]), self.list.get_column(0))

    # ----------------------------------------------------------- internal
    def _sync_header(self) -> None:
        shown = self.visible_rows() > 0
        self._chevron.set_icon(Icons.CHEVRON_DOWN if shown else Icons.CHEVRON_RIGHT)
        state = _("expanded") if shown else _("collapsed")
        name = ngettext("Favorites, {n} item, {state}", "Favorites, {n} items, {state}", self._count).format(
            n=self._count, state=state)
        set_accessible_name(self.header, name)
        self.header.set_tooltip_text(_("Hide favorites") if shown else _("Show favorites"))

    def _on_header_toggled(self, button: Gtk.ToggleButton) -> None:
        self._expanded = button.get_active()
        if self._expanded and self._room < 1:
            self._room = 1  # opened by the user on a short window: one row at least
        self._apply_height()
        self.on_expanded(self._expanded)

    def _on_selection_changed(self, _selection) -> None:
        if self._silent:
            return
        index = self.list.selected_index()
        if index is not None:
            self.on_selected(index)

    def _on_button_press(self, view: Gtk.TreeView, event: Gdk.EventButton) -> bool:
        if event.button != Gdk.BUTTON_SECONDARY or event.type != Gdk.EventType.BUTTON_PRESS:
            return False
        pos = view.get_path_at_pos(int(event.x), int(event.y))
        if not pos:
            return False
        view.grab_focus()
        view.set_cursor(pos[0], None, False)  # selects it: the tree follows
        self.on_context_menu(event)
        return True

    def _on_key_press(self, view: Gtk.TreeView, event: Gdk.EventKey) -> bool:
        from ..controllers.tree import is_favorite_key

        if is_favorite_key(event):
            index = self.list.selected_index()
            if index is not None:
                self.on_toggle_favorite(index)
            return True
        return False

