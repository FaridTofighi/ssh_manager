"""Session tree widget (``Gtk.TreeView`` + ``Gtk.TreeStore``), Relay style.

Rows are painted by :class:`RelayRowRenderer` (a ``Gtk.CellRendererText``
subclass, so accessibility still reads the row text):

* groups: chevron, folder / open-folder icon (``*.conf`` groups: file icon),
  label (semibold at the top level) and the host count at the right edge;
* hosts: server icon with the connection-status badge (and a lock when the
  host is read-only) composited on, then the label.

Icons, colors and overlays come from :mod:`gtkui.tree_icons`; every level has
one icon column (hosts leave the chevron slot empty), so labels of one level
line up. A connecting badge pulses (0.8 Hz) unless animations are off.

The selected row gets an inset, rounded ``accent_tint`` background with an
accent bar on its left (4px Comfortable, 3px Compact); hover gets ``bg_hover``.
All geometry comes from the active density preset at paint/measure time. GTK's own expanders and
indentation are off: the renderer indents by depth and clicking the chevron
(or Left/Right) collapses/expands. The row tooltip spells the status out, so
color is never the only cue.
"""

from __future__ import annotations

import math
import time
from typing import Callable, Dict, Optional, Sequence, Set, Tuple

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Gdk, GLib, GObject, Gtk, Pango, PangoCairo  # type: ignore  # noqa: E402

from ..constants import RowKind, TreeColumn  # noqa: E402
from ..controllers.tree_controller import TreeRow  # noqa: E402
from ..i18n import _  # noqa: E402
from ..theme.loader import current_palette  # noqa: E402
from ..constants import Icons  # noqa: E402
from ..theme import density as density_tokens  # noqa: E402
from ..theme.tokens import ConnectionStatus, Layout, Size  # noqa: E402
from ..tree_icons import icon_color, pulse_alpha, quantize_alpha, show_status_badge, status_badge, tree_icon_for  # noqa: E402
from ..widgets.icon_button import set_accessible_name  # noqa: E402
from ..widgets.icons import render_svg_surface, render_tree_icon  # noqa: E402

COLUMN_TYPES = (str, str, str, str, str, str, GObject.TYPE_PYOBJECT, str, str, str, int, str, bool)
PULSE_INTERVAL_MS = 100


def new_tree_store() -> Gtk.TreeStore:
    return Gtk.TreeStore(*COLUMN_TYPES)


def _font_tokens():
    return density_tokens.active().font


def font_pixel_size(widget: Gtk.Widget) -> float:
    font = widget.get_style_context().get_font(Gtk.StateFlags.NORMAL)
    size = font.get_size() / Pango.SCALE
    if not font.get_size_is_absolute():
        screen = widget.get_screen()
        dpi = screen.get_resolution() if screen is not None else -1
        size *= (dpi if dpi > 0 else 96.0) / 72.0
    return size


def row_height_for(widget: Gtk.Widget, *, group: bool = False) -> int:
    """Row pitch at the default font (41px Comfortable, 30px Compact); taller for larger fonts."""

    base = Size.TREE_GROUP_ROW if group else Size.TREE_ROW
    return max(base, int(round(base * font_pixel_size(widget) / _font_tokens().TREE)))


def row_geometry(kind: object, depth: int) -> Dict[str, int]:
    """x positions (widget coordinates) of the chevron, icon and label for a row.

    One icon column per level: chevron box, ``CHEVRON_GAP``, icon, ``ICON_GAP``,
    label. Hosts have no chevron but keep its slot, so their icon and label line
    up with a group's at the same depth.
    """

    tree = Layout.Tree
    base = tree.LEAD + Size.TREE_INDENT * max(0, depth)
    icon = base + tree.CHEVRON + tree.CHEVRON_GAP
    text = icon + tree.ICON + tree.ICON_GAP
    chevron = -1 if RowKind.coerce(kind) is RowKind.HOST else base
    return {"chevron": chevron, "icon": icon, "text": text}


def _rgba(hex_color: str) -> Gdk.RGBA:
    rgba = Gdk.RGBA()
    if not hex_color or not rgba.parse(hex_color):
        rgba.parse(current_palette().text_disabled)
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
    read_only = GObject.Property(type=bool, default=False)
    gap = GObject.Property(type=int, default=0)

    def __init__(self, icon_cache: dict) -> None:
        Gtk.CellRendererText.__init__(self)
        self._icon_cache = icon_cache
        self.pulse_phase = 0.0  # seconds; the view advances it while a host is connecting
        self.saw_connecting = False  # set while painting a connecting badge

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
        group = RowKind.coerce(self.props.kind) is not RowKind.HOST and self.props.depth == 0
        height = row_height_for(widget, group=group) + self.props.gap - separator
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
        tree, size = Layout.Tree, Size
        INSET, RADIUS, BAR_WIDTH, BAR_HEIGHT = size.TREE_INSET, tree.RADIUS, tree.BAR_WIDTH, tree.BAR_HEIGHT
        CHEVRON, ICON, COUNT_RIGHT = tree.CHEVRON, tree.ICON, tree.COUNT_RIGHT
        COUNT_SCALE = _font_tokens().TREE_COUNT / _font_tokens().TREE
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
            bar_r = min(2, BAR_WIDTH / 2.0)
            cr.line_to(rect_x + BAR_WIDTH - bar_r, bar_y)
            cr.arc(rect_x + BAR_WIDTH - bar_r, bar_y + bar_r, bar_r, -math.pi / 2, 0)
            cr.arc(rect_x + BAR_WIDTH - bar_r, bar_y + BAR_HEIGHT - bar_r, bar_r, 0, math.pi / 2)
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
            self._paint_tree_icon(cr, widget, kind, selected, ICON, geometry["icon"], mid, scale)

        right_reserved = 0
        if kind is RowKind.HOST:
            right_reserved = COUNT_RIGHT  # the status lives on the icon; the right edge is for counts
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

    def _paint_tree_icon(self, cr, widget, kind, selected: bool, size: int, x: float, mid: float, scale: int) -> None:
        palette = current_palette()
        icon = tree_icon_for(kind, self.props.expanded, self.props.read_only,
                             selected=selected, has_children=self.props.expandable)
        color = icon_color(palette, icon, self.props.color)
        badge_name = badge_color = ""
        alpha = 1.0
        if show_status_badge(kind, self.props.status, self.props.read_only):
            badge = status_badge(self.props.status, size)
            badge_name, badge_color = badge.name, getattr(palette, badge.color_role)
            if badge.pulse:
                self.saw_connecting = True
                if animations_enabled(widget):
                    alpha = quantize_alpha(pulse_alpha(self.pulse_phase))
        surface = render_tree_icon(icon.name, color, size, scale, badge=badge_name, badge_color=badge_color,
                                   badge_alpha=alpha, lock=icon.lock, cache=self._icon_cache)
        if surface is not None:
            cr.set_source_surface(surface, x, round(mid - size / 2.0))
            cr.paint()

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


def animations_enabled(widget: Optional[Gtk.Widget] = None) -> bool:
    settings = widget.get_settings() if widget is not None else Gtk.Settings.get_default()
    try:
        return bool(settings.get_property("gtk-enable-animations")) if settings is not None else True
    except TypeError:
        return True


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
        self._pulse_source = 0
        self._pulse_start = time.monotonic()
        self._build_column()
        # A connecting badge pulses: a timer redraws the tree while one was painted.
        self.connect("draw", self._before_draw)
        self.connect_after("draw", self._after_draw)
        self.connect("destroy", lambda *_: self._stop_pulse())

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
        cell.set_property("read_only", bool(model.get_value(tree_iter, TreeColumn.READ_ONLY)))
        cell.set_property("expandable", model.iter_has_child(tree_iter))
        cell.set_property("expanded", self.row_expanded(path))
        cell.set_property("gap", Layout.Tree.GROUP_GAP if depth == 0 and indices and indices[0] > 0 else 0)

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
                    row.read_only,
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

    # ------------------------------------------------------------ pulse
    def _before_draw(self, *_args) -> bool:
        self.row_renderer.saw_connecting = False
        self.row_renderer.pulse_phase = time.monotonic() - self._pulse_start
        return False

    def _after_draw(self, *_args) -> bool:
        if self.row_renderer.saw_connecting and animations_enabled(self):
            if not self._pulse_source:
                self._pulse_source = GLib.timeout_add(PULSE_INTERVAL_MS, self._on_pulse)
        else:
            self._stop_pulse()
        return False

    def _on_pulse(self) -> bool:
        if not self.get_mapped():
            self._pulse_source = 0
            return GLib.SOURCE_REMOVE
        self.queue_draw()
        return GLib.SOURCE_CONTINUE

    def _stop_pulse(self) -> None:
        if self._pulse_source:
            GLib.source_remove(self._pulse_source)
            self._pulse_source = 0

    @property
    def pulsing(self) -> bool:
        return bool(self._pulse_source)

    def clear_icon_cache(self) -> None:
        self.icon_cache.clear()
        self.queue_draw()

    def apply_density(self) -> None:
        """Re-measure every row with the active density (row pitch, indents, icons)."""

        self.icon_cache.clear()
        column = self.get_column(0)
        if column is not None:
            column.queue_resize()
        self.queue_resize()
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
        if left < 0 or not (left - 6 <= x <= left + Layout.Tree.CHEVRON + 6):
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
