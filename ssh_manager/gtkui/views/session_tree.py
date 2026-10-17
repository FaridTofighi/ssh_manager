"""Session tree widget (``Gtk.TreeView`` + ``Gtk.TreeStore``).

Each row shows: a themed symbolic icon (tinted with the user's color or the
token default for its kind), the name, a host-count badge for groups, and a
status glyph for hosts. The glyph's *shape* differs per state (●, ◐, ✖) and
the row tooltip spells the status out, so color is never the only cue.
"""

from __future__ import annotations

from typing import Callable, Dict, Optional, Sequence

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GObject, Gtk, Pango  # type: ignore  # noqa: E402

from ..constants import RowKind, TreeColumn  # noqa: E402
from ..controllers.tree_controller import TreeRow  # noqa: E402
from ..i18n import _  # noqa: E402
from ..theme.loader import current_palette  # noqa: E402
from ..theme.tokens import STATUS_GLYPHS, ConnectionStatus  # noqa: E402
from ..widgets.icon_button import set_accessible_name  # noqa: E402
from ..widgets.icons import tinted_icon_surface  # noqa: E402

COLUMN_TYPES = (str, str, str, str, str, str, GObject.TYPE_PYOBJECT, str, str, str, int, str)


def new_tree_store() -> Gtk.TreeStore:
    return Gtk.TreeStore(*COLUMN_TYPES)


def default_icon_color(kind: object) -> str:
    """Token color for a row kind when the user picked no color."""

    palette = current_palette()
    row_kind = RowKind.coerce(kind)
    if row_kind is RowKind.PARENT:
        return palette.icon_group
    if row_kind in (RowKind.CHILD, RowKind.CHILD_FOLDER):
        return palette.icon_subgroup
    if row_kind is RowKind.HOST:
        return palette.icon_host
    return palette.text_disabled


def render_icon(widget: Optional[Gtk.Widget], cell, model, tree_iter, cache: dict) -> None:
    """Cell-data function body for the icon renderer."""

    icon_name = model.get_value(tree_iter, TreeColumn.ICON)
    color = model.get_value(tree_iter, TreeColumn.COLOR) or default_icon_color(model.get_value(tree_iter, TreeColumn.KIND))
    surface = tinted_icon_surface(icon_name, color, widget=widget, cache=cache) if icon_name else None
    if surface is not None:
        cell.set_property("surface", surface)
    else:
        cell.set_property("surface", None)
        cell.set_property("icon-name", icon_name)


def render_status(cell, model, tree_iter) -> None:
    """Cell-data function body for the status glyph renderer."""

    kind = model.get_value(tree_iter, TreeColumn.KIND)
    if kind != RowKind.HOST.value:
        cell.set_property("text", "")
        cell.set_property("visible", False)
        return
    status = ConnectionStatus.coerce(model.get_value(tree_iter, TreeColumn.STATUS))
    glyph = STATUS_GLYPHS[status]
    cell.set_property("visible", bool(glyph))
    cell.set_property("text", glyph)
    if glyph:
        cell.set_property("foreground", current_palette().status_color(status))
        cell.set_property("foreground-set", True)
    else:
        cell.set_property("foreground-set", False)


def render_count(cell, model, tree_iter) -> None:
    kind = model.get_value(tree_iter, TreeColumn.KIND)
    count = model.get_value(tree_iter, TreeColumn.COUNT)
    show = kind != RowKind.HOST.value
    cell.set_property("visible", show)
    cell.set_property("text", str(count) if show else "")
    cell.set_property("foreground", current_palette().text_secondary)
    cell.set_property("foreground-set", True)


class SessionTreeView(Gtk.TreeView):
    """Tree of groups and hosts. Holds no inventory logic."""

    def __init__(self, store: Optional[Gtk.TreeStore] = None) -> None:
        Gtk.TreeView.__init__(self, model=store or new_tree_store())
        self.set_headers_visible(False)
        self.set_enable_tree_lines(True)
        self.set_tooltip_column(TreeColumn.TOOLTIP)
        self.set_search_column(TreeColumn.TEXT)
        self.set_enable_search(True)
        self.set_search_equal_func(self._search_equal)
        self.get_selection().set_mode(Gtk.SelectionMode.MULTIPLE)
        set_accessible_name(self, _("Hosts and groups"))
        self.icon_cache: dict = {}
        self.on_context_menu: Callable[[Optional[Gdk.Event]], None] = lambda _e: None
        self.connect("popup-menu", self._on_popup_menu)
        self._build_column()

    @property
    def store(self) -> Gtk.TreeStore:
        return self.get_model()

    def _build_column(self) -> None:
        column = Gtk.TreeViewColumn()
        column.set_expand(True)

        icon = Gtk.CellRendererPixbuf()
        icon.set_padding(2, 0)
        column.pack_start(icon, False)
        column.set_cell_data_func(icon, lambda _c, cell, model, it, _d: render_icon(self, cell, model, it, self.icon_cache))

        text = Gtk.CellRendererText()
        text.set_property("ellipsize", Pango.EllipsizeMode.END)
        column.pack_start(text, True)
        column.add_attribute(text, "text", TreeColumn.TEXT)

        count = Gtk.CellRendererText()
        count.set_property("scale", 0.85)
        count.set_property("xalign", 1.0)
        column.pack_start(count, False)
        column.set_cell_data_func(count, lambda _c, cell, model, it, _d: render_count(cell, model, it))

        status = Gtk.CellRendererText()
        status.set_padding(4, 0)
        column.pack_start(status, False)
        column.set_cell_data_func(status, lambda _c, cell, model, it, _d: render_status(cell, model, it))
        self.append_column(column)

    # ------------------------------------------------------------------ api
    def populate(self, rows: Sequence[TreeRow]) -> Dict[str, Gtk.TreeIter]:
        """Replace the model content with ``rows``; return ``{host_key: iter}``."""

        store = self.store
        store.clear()
        host_iters: Dict[str, Gtk.TreeIter] = {}

        def append(parent_iter, row: TreeRow) -> None:
            tree_iter = store.append(
                parent_iter,
                [
                    row.icon,
                    row.text,
                    row.kind.value,
                    row.parent,
                    row.child,
                    row.alias,
                    row.host,
                    row.search,
                    row.color,
                    row.status,
                    row.count,
                    row.tooltip,
                ],
            )
            if row.kind is RowKind.HOST:
                host_iters[row.key] = tree_iter
            for child in row.children:
                append(tree_iter, child)

        for row in rows:
            append(None, row)
        self.expand_all()
        return host_iters

    def clear_icon_cache(self) -> None:
        self.icon_cache.clear()
        self.queue_draw()

    def selected_row_rect(self) -> Optional[Gdk.Rectangle]:
        model, paths = self.get_selection().get_selected_rows()
        if not paths:
            return None
        return self.get_cell_area(paths[0], self.get_column(0))

    # ------------------------------------------------------------- internal
    @staticmethod
    def _search_equal(model, _column, key, tree_iter, _data=None) -> bool:
        # Return False when the row matches (GTK convention).
        text = (model.get_value(tree_iter, TreeColumn.TEXT) or "").lower()
        return key.lower() not in text

    def _on_popup_menu(self, _widget) -> bool:
        self.on_context_menu(None)
        return True
