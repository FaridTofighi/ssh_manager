"""Session tree widget (``Gtk.TreeView`` + ``Gtk.TreeStore``), Relay style.

Rows are painted by :class:`RelayRowRenderer` (a ``Gtk.CellRendererText``
subclass, so accessibility still reads the row text):

* top-level groups: chevron, folder icon (user color or token amber), bold
  label, host count;
* nested groups: chevron, label, count;
* hosts: monitor icon, label and a status dot at the right edge.

The selected row gets an inset, rounded ``accent_tint`` background with a 4px
accent bar on its left; hover gets ``bg_hover``. GTK's own expanders and
indentation are off: the renderer indents by depth and clicking the chevron
(or Left/Right) collapses/expands. The row tooltip spells the status out, so
color is never the only cue.
"""

from __future__ import annotations

import math
from typing import Callable, Dict, Optional, Sequence, Set, Tuple

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Gdk, GObject, Gtk, Pango, PangoCairo  # type: ignore  # noqa: E402

from ..constants import RowKind, TreeColumn  # noqa: E402
from ..controllers.tree_controller import TreeRow  # noqa: E402
from ..i18n import _  # noqa: E402
from ..theme.loader import current_palette  # noqa: E402
from ..constants import Icons  # noqa: E402
from ..theme.tokens import STATUS_GLYPHS, ConnectionStatus, Layout, Size  # noqa: E402
from ..widgets.icon_button import set_accessible_name  # noqa: E402
from ..widgets.icons import render_svg_surface, tinted_icon_surface  # noqa: E402

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


# Geometry measured on the reference (sidebar 407px + 1px separator).
ROW_HEIGHT = Size.TREE_ROW  # 41
GROUP_GAP = Layout.Tree.GROUP_GAP
INSET = Size.TREE_INSET  # selection inset from both sidebar edges
INDENT = Size.TREE_INDENT  # 25 per level
LEAD = Layout.Tree.LEAD
CHEVRON = Layout.Tree.CHEVRON
FOLDER = Layout.Tree.FOLDER
HOST_ICON = Layout.Tree.HOST_ICON
BAR_WIDTH, BAR_HEIGHT = Layout.Tree.BAR_WIDTH, Layout.Tree.BAR_HEIGHT
COUNT_RIGHT = Layout.Tree.COUNT_RIGHT
DOT_RIGHT = Layout.Tree.DOT_RIGHT
DOT = Layout.Tree.DOT
COUNT_SCALE = 13 / 16  # count font (13px) relative to the row font (16px)
RADIUS = Layout.Tree.RADIUS


def row_geometry(kind: object, depth: int) -> Dict[str, int]:
    """x positions (widget coordinates) of the chevron, icon and label for a row."""

    base = LEAD + INDENT * max(0, depth)
    row_kind = RowKind.coerce(kind)
    if row_kind is RowKind.HOST:
        return {"chevron": -1, "icon": base + 5, "text": base + 32}
    if depth == 0:
        return {"chevron": base, "icon": base + 26, "text": base + 53}
    return {"chevron": base, "icon": -1, "text": base + 26}


def _rgba(hex_color: str) -> Gdk.RGBA:
    rgba = Gdk.RGBA()
    if not hex_color or not rgba.parse(hex_color):
        rgba.parse("#888888")
    return rgba


def _rounded_rect(cr, x: float, y: float, w: float, h: float, r: float) -> None:
    r = min(r, w / 2, h / 2)
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


class RelayRowRenderer(Gtk.CellRendererText):
    """Paints a whole tree row; ``text`` stays the accessible name."""

    kind = GObject.Property(type=str, default="")
    depth = GObject.Property(type=int, default=0)
    count = GObject.Property(type=int, default=0)
    status = GObject.Property(type=str, default="")
    color = GObject.Property(type=str, default="")
    expandable = GObject.Property(type=bool, default=False)
    expanded = GObject.Property(type=bool, default=False)
    gap = GObject.Property(type=int, default=0)

    def __init__(self, icon_cache: dict) -> None:
        Gtk.CellRendererText.__init__(self)
        self._icon_cache = icon_cache

    # ------------------------------------------------------------- sizing
    def do_get_request_mode(self) -> Gtk.SizeRequestMode:
        return Gtk.SizeRequestMode.CONSTANT_SIZE

    def do_get_preferred_width(self, _widget):
        return 120, 200

    def do_get_preferred_height(self, widget):
        # TreeView adds its vertical-separator to every row; keep the pitch exact.
        try:
            separator = int(widget.style_get_property("vertical-separator"))
        except (TypeError, ValueError):
            separator = 0
        height = ROW_HEIGHT + self.props.gap - separator
        return height, height

    def do_get_preferred_height_for_width(self, widget, _width):
        return self.do_get_preferred_height(widget)

    def do_get_preferred_width_for_height(self, widget, _height):
        return self.do_get_preferred_width(widget)

    def do_get_size(self, widget, _cell_area):
        return 0, 0, 120, self.do_get_preferred_height(widget)[0]

    # ------------------------------------------------------------ drawing
    def do_render(self, cr, widget, background_area, cell_area, flags) -> None:
        palette = current_palette()
        width = widget.get_allocated_width()
        top = background_area.y + self.props.gap
        height = background_area.height - self.props.gap
        selected = bool(flags & Gtk.CellRendererState.SELECTED)
        prelit = bool(flags & Gtk.CellRendererState.PRELIT)
        focused = bool(flags & Gtk.CellRendererState.FOCUSED) and widget.has_focus()

        rect_x, rect_w = INSET, max(0, width - 2 * INSET)
        if selected or prelit:
            cr.set_source_rgba(*self._channels(palette.bg_selected if selected else palette.bg_hover))
            _rounded_rect(cr, rect_x, top, rect_w, height, RADIUS)
            cr.fill()
        if selected:
            cr.set_source_rgba(*self._channels(palette.accent))
            bar_y = top + (height - BAR_HEIGHT + 1) // 2
            cr.new_sub_path()
            cr.move_to(rect_x, bar_y)
            cr.line_to(rect_x + BAR_WIDTH - 2, bar_y)
            cr.arc(rect_x + BAR_WIDTH - 2, bar_y + 2, 2, -math.pi / 2, 0)
            cr.arc(rect_x + BAR_WIDTH - 2, bar_y + BAR_HEIGHT - 2, 2, 0, math.pi / 2)
            cr.line_to(rect_x, bar_y + BAR_HEIGHT)
            cr.close_path()
            cr.fill()
        if focused:
            cr.set_source_rgba(*self._channels(palette.focus_ring, 0.9))
            cr.set_line_width(2)
            _rounded_rect(cr, rect_x + 1, top + 1, rect_w - 2, height - 2, RADIUS - 1)
            cr.stroke()

        kind = RowKind.coerce(self.props.kind)
        geometry = row_geometry(kind, self.props.depth)
        mid = top + height / 2.0
        scale = widget.get_scale_factor()
        text_color = palette.accent_text if selected else palette.text_primary

        if geometry["chevron"] >= 0 and self.props.expandable:
            name = Icons.CHEVRON_DOWN if self.props.expanded else Icons.CHEVRON_RIGHT
            self._paint_icon(cr, name, palette.text_secondary, CHEVRON, geometry["chevron"], mid, scale)
        if geometry["icon"] >= 0:
            if kind is RowKind.HOST:
                color = palette.accent_text if selected else (self.props.color or palette.text_secondary)
                self._paint_icon(cr, Icons.HOST, color, HOST_ICON, geometry["icon"], mid, scale)
            else:
                self._paint_icon(cr, Icons.GROUP, self.props.color or palette.icon_group, FOLDER, geometry["icon"], mid, scale)

        right_reserved = 0
        if kind is RowKind.HOST:
            status = ConnectionStatus.coerce(self.props.status)
            if status is not ConnectionStatus.UNKNOWN:
                cr.set_source_rgba(*self._channels(palette.status_color(status)))
                cr.arc(width - DOT_RIGHT, mid, DOT / 2.0, 0, 2 * math.pi)
                cr.fill()
            right_reserved = DOT_RIGHT + DOT
        else:
            count_layout = widget.create_pango_layout(str(self.props.count))
            font = widget.get_style_context().get_font(Gtk.StateFlags.NORMAL).copy()
            font.set_size(int(font.get_size() * COUNT_SCALE))
            count_layout.set_font_description(font)
            ink, logical = count_layout.get_pixel_extents()
            cr.set_source_rgba(*self._channels(palette.text_faint))
            cr.move_to(width - COUNT_RIGHT - logical.width, mid - logical.height / 2.0)
            PangoCairo.show_layout(cr, count_layout)
            right_reserved = COUNT_RIGHT + logical.width + 8

        layout = widget.create_pango_layout(self.props.text or "")
        font = widget.get_style_context().get_font(Gtk.StateFlags.NORMAL).copy()
        bold = selected or (kind is not RowKind.HOST and self.props.depth == 0)
        font.set_weight(Pango.Weight.SEMIBOLD if bold else Pango.Weight.NORMAL)
        layout.set_font_description(font)
        layout.set_ellipsize(Pango.EllipsizeMode.END)
        available = max(10, width - geometry["text"] - right_reserved)
        layout.set_width(available * Pango.SCALE)
        _ink, logical = layout.get_pixel_extents()
        cr.set_source_rgba(*self._channels(text_color))
        cr.move_to(geometry["text"], mid - logical.height / 2.0)
        PangoCairo.show_layout(cr, layout)

    def _paint_icon(self, cr, name: str, color: str, size: int, x: float, mid: float, scale: int) -> None:
        surface = render_svg_surface(name, color, size, scale, self._icon_cache)
        if surface is None:
            return
        cr.set_source_surface(surface, x, round(mid - size / 2.0))
        cr.paint()

    @staticmethod
    def _channels(hex_color: str, alpha: float = 1.0) -> Tuple[float, float, float, float]:
        rgba = _rgba(hex_color)
        return rgba.red, rgba.green, rgba.blue, rgba.alpha * alpha


class SessionTreeView(Gtk.TreeView):
    """Tree of groups and hosts. Holds no inventory logic."""

    def __init__(self, store: Optional[Gtk.TreeStore] = None) -> None:
        Gtk.TreeView.__init__(self, model=store or new_tree_store())
        self.get_style_context().add_class("relay-tree")
        self.set_headers_visible(False)
        self.set_enable_tree_lines(False)
        self.set_show_expanders(False)
        self.set_level_indentation(0)
        self.collapsed_keys: Set[Tuple[str, str, str]] = set()
        self.set_tooltip_column(TreeColumn.TOOLTIP)
        self.set_search_column(TreeColumn.TEXT)
        self.set_enable_search(True)
        self.set_search_equal_func(self._search_equal)
        self.get_selection().set_mode(Gtk.SelectionMode.MULTIPLE)
        set_accessible_name(self, _("Hosts and groups"))
        self.icon_cache: dict = {}
        self.on_context_menu: Callable[[Optional[Gdk.Event]], None] = lambda _e: None
        self.connect("popup-menu", self._on_popup_menu)
        # Connected before the window's handlers, so chevron clicks and
        # Left/Right expand/collapse win.
        self.connect("button-press-event", self._on_button_press)
        self.connect("key-press-event", self._on_key_press)
        self.connect("row-collapsed", lambda _v, it, _p: self.collapsed_keys.add(self._row_key(it)))
        self.connect("row-expanded", lambda _v, it, _p: self.collapsed_keys.discard(self._row_key(it)))
        self._build_column()

    @property
    def store(self) -> Gtk.TreeStore:
        return self.get_model()

    def _build_column(self) -> None:
        column = Gtk.TreeViewColumn()
        column.set_expand(True)
        column.set_spacing(0)
        self.row_renderer = RelayRowRenderer(self.icon_cache)
        self.row_renderer.set_padding(0, 0)
        column.pack_start(self.row_renderer, True)
        column.set_cell_data_func(self.row_renderer, self._row_data)
        self.append_column(column)

    def _row_data(self, _column, cell, model, tree_iter, _data=None) -> None:
        path = model.get_path(tree_iter)
        depth = path.get_depth() - 1
        indices = path.get_indices()
        cell.set_property("text", model.get_value(tree_iter, TreeColumn.TEXT) or "")
        cell.set_property("kind", model.get_value(tree_iter, TreeColumn.KIND) or "")
        cell.set_property("depth", depth)
        cell.set_property("count", int(model.get_value(tree_iter, TreeColumn.COUNT) or 0))
        cell.set_property("status", model.get_value(tree_iter, TreeColumn.STATUS) or "")
        cell.set_property("color", model.get_value(tree_iter, TreeColumn.COLOR) or "")
        cell.set_property("expandable", model.iter_has_child(tree_iter))
        cell.set_property("expanded", self.row_expanded(path))
        cell.set_property("gap", GROUP_GAP if depth == 0 and indices and indices[0] > 0 else 0)

    # ------------------------------------------------------------------ api
    def populate(self, rows: Sequence[TreeRow]) -> Dict[str, Gtk.TreeIter]:
        """Replace the model content with ``rows``; return ``{host_key: iter}``."""

        store = self.store
        collapsed = set(self.collapsed_keys)  # expand_all() below emits row-expanded
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
        self._restore_collapsed(collapsed)
        return host_iters

    def _row_key(self, tree_iter) -> Tuple[str, str, str]:
        model = self.store
        return (
            model.get_value(tree_iter, TreeColumn.KIND) or "",
            model.get_value(tree_iter, TreeColumn.PARENT) or "",
            model.get_value(tree_iter, TreeColumn.CHILD) or "",
        )

    def _restore_collapsed(self, wanted: Set[Tuple[str, str, str]]) -> None:
        self.collapsed_keys = set(wanted)
        if not wanted:
            return
        rows = []
        self.store.foreach(lambda model, path, it: rows.append((path.copy(), self._row_key(it))) and False)
        # Collapse deepest first so parents keep their own state.
        for path, key in sorted(rows, key=lambda item: -item[0].get_depth()):
            if key in wanted:
                self.collapse_row(path)
        self.collapsed_keys = wanted

    def set_row_collapsed(self, kind: str, parent: str, child: str = "", collapsed: bool = True) -> None:
        """Collapse/expand a group row by its key (used by tests and snapshots)."""

        target = (kind, parent, child)
        found = []
        self.store.foreach(lambda model, path, it: found.append(path.copy()) if self._row_key(it) == target else False)
        for path in found:
            if collapsed:
                self.collapse_row(path)
            else:
                self.expand_row(path, False)

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

    def chevron_hit(self, x: float, y: float) -> Optional[Gtk.TreePath]:
        """Path of the group row whose chevron is at (x, y), if any."""

        pos = self.get_path_at_pos(int(x), int(y))
        if not pos:
            return None
        path = pos[0]
        model = self.store
        tree_iter = model.get_iter(path)
        if not model.iter_has_child(tree_iter):
            return None
        geometry = row_geometry(model.get_value(tree_iter, TreeColumn.KIND), path.get_depth() - 1)
        left = geometry["chevron"]
        if left < 0 or not (left - 6 <= x <= left + CHEVRON + 6):
            return None
        return path

    def _on_button_press(self, _widget, event: Gdk.EventButton) -> bool:
        if event.button != 1 or event.type != Gdk.EventType.BUTTON_PRESS:
            return False
        path = self.chevron_hit(event.x, event.y)
        if path is None:
            return False
        if self.row_expanded(path):
            self.collapse_row(path)
        else:
            self.expand_row(path, False)
        return True

    def _on_key_press(self, _widget, event: Gdk.EventKey) -> bool:
        if event.state & Gtk.accelerator_get_default_mod_mask():
            return False
        if event.keyval not in (Gdk.KEY_Left, Gdk.KEY_Right, Gdk.KEY_KP_Left, Gdk.KEY_KP_Right):
            return False
        path, _column = self.get_cursor()
        if path is None:
            return False
        rtl = self.get_direction() == Gtk.TextDirection.RTL
        forward = event.keyval in (Gdk.KEY_Right, Gdk.KEY_KP_Right)
        if rtl:
            forward = not forward
        has_children = self.store.iter_has_child(self.store.get_iter(path))
        if forward:
            if has_children and not self.row_expanded(path):
                self.expand_row(path, False)
            elif has_children:
                child = path.copy()
                child.down()
                self.set_cursor(child, None, False)
            return True
        if has_children and self.row_expanded(path):
            self.collapse_row(path)
        elif path.get_depth() > 1:
            parent = path.copy()
            parent.up()
            self.set_cursor(parent, None, False)
        return True

    def _on_popup_menu(self, _widget) -> bool:
        self.on_context_menu(None)
        return True
