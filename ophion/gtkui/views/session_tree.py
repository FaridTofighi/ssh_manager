"""Session tree widget (``Gtk.TreeView`` + ``Gtk.TreeStore``), Relay style.

Rows are painted by :class:`RelayRowRenderer` (a ``Gtk.CellRendererText``
subclass, so accessibility still reads the row text):

* groups: chevron, folder / open-folder icon (``*.conf`` groups: file icon),
  label (semibold at the top level) and the host count at the right edge;
* hosts: server icon (file-transfer icon for FTP / FTPS / SFTP) with the
  connection-status badge (and a lock when the host is read-only) composited
  on, then the label; non-SSH hosts get a small protocol pill (``FTP``,
  ``FTPS``, ``SFTP``) at the right edge, where groups show their count.

Icons, colors and overlays come from :mod:`gtkui.tree_icons`; every level has
one icon column (hosts leave the chevron slot empty), so labels of one level
line up. A connecting badge pulses (0.8 Hz) unless animations are off.

The selected row gets an inset, rounded ``accent_tint`` background with an
accent bar on its left (4px Comfortable, 3px Compact); hover gets ``bg_hover``.
All geometry comes from the active density preset at paint/measure time. GTK's own expanders and
indentation are off: the renderer indents by depth and clicking the chevron
(or Left/Right) collapses/expands. The row tooltip spells the status out, so
color is never the only cue.

Expansion state: :attr:`SessionTreeView.expanded_keys` holds the
``(kind, parent, child)`` keys of the groups the user opened in **this
session**. A new view starts empty, so every group is collapsed at startup;
nothing about it is persisted. :meth:`SessionTreeView.populate` rebuilds the
store and expands exactly those rows (or, while a search/filter is active,
a temporary set that never touches ``expanded_keys``). A group whose parent
is collapsed keeps its key, so re-opening the parent restores it.
"""

from __future__ import annotations

import math
import time
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Gdk, GLib, GObject, Gtk, Pango, PangoCairo  # type: ignore  # noqa: E402

from ... import protocols  # noqa: E402
from ..constants import RowKind, TreeColumn  # noqa: E402
from ..controllers.tree_controller import TreeRow  # noqa: E402
from ..i18n import _  # noqa: E402
from ..theme.loader import current_palette  # noqa: E402
from ..constants import Icons  # noqa: E402
from ..theme import density as density_tokens  # noqa: E402
from ..theme.tokens import Layout, Size  # noqa: E402
from ..tree_icons import icon_color, pulse_alpha, quantize_alpha, show_status_badge, status_badge, tree_icon_for  # noqa: E402
from ..widgets.icon_button import set_accessible_name  # noqa: E402
from ..widgets.icons import render_svg_surface, render_tree_icon  # noqa: E402

RowKey = Tuple[str, str, str]  # (kind, parent, child) of a group row

COLUMN_TYPES = (str, str, str, str, str, str, GObject.TYPE_PYOBJECT, str, str, str, int, str, bool, bool)
PULSE_INTERVAL_MS = 100


def new_tree_store() -> Gtk.TreeStore:
    return Gtk.TreeStore(*COLUMN_TYPES)


def _font_tokens():
    return density_tokens.active().font


def style_font(widget: Gtk.Widget) -> Pango.FontDescription:
    """The widget's CSS font (``StyleContext.get_font`` is deprecated)."""

    return widget.get_style_context().get_property("font", Gtk.StateFlags.NORMAL)


def font_pixel_size(widget: Gtk.Widget) -> float:
    font = style_font(widget)
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
    file_backed = GObject.Property(type=bool, default=True)
    gap = GObject.Property(type=int, default=0)
    protocol = GObject.Property(type=str, default="")  # hosts: ssh, ftp, ftps, sftp …
    badge = GObject.Property(type=str, default="")  # protocol pill text ("" for SSH)
    flat = GObject.Property(type=bool, default=False)  # a flat list (Favorites): groups get the host row pitch

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
        group = RowKind.coerce(self.props.kind) is not RowKind.HOST and self.props.depth == 0 and not self.props.flat
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
            if self.props.badge:
                right_reserved += self._paint_badge(cr, widget, self.props.badge, width - COUNT_RIGHT, mid,
                                                    COUNT_SCALE, selected) + 8
        else:
            count_layout = widget.create_pango_layout(str(self.props.count))
            font = style_font(widget).copy()
            font.set_size(int(font.get_size() * COUNT_SCALE))
            count_layout.set_font_description(font)
            ink, logical = count_layout.get_pixel_extents()
            cr.set_source_rgba(*self._channels(palette.text_faint))
            cr.move_to(width - COUNT_RIGHT - logical.width, mid - logical.height / 2.0)
            PangoCairo.show_layout(cr, count_layout)
            right_reserved = COUNT_RIGHT + logical.width + 8

        layout = widget.create_pango_layout(self.props.text or "")
        font = style_font(widget).copy()
        bold = selected or (kind is not RowKind.HOST and self.props.depth == 0 and not self.props.flat)
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
                             self.props.file_backed if kind is RowKind.CHILD else None,
                             selected=selected, has_children=self.props.expandable, protocol=self.props.protocol)
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

    def _paint_badge(self, cr, widget, text: str, right: float, mid: float, scale: float, selected: bool) -> int:
        """A small outlined pill with the protocol name, right-aligned at ``right``; returns its width."""

        palette = current_palette()
        layout = widget.create_pango_layout(text)
        font = style_font(widget).copy()
        font.set_size(int(font.get_size() * scale * 0.9))
        font.set_weight(Pango.Weight.SEMIBOLD)
        layout.set_font_description(font)
        _ink, logical = layout.get_pixel_extents()
        pad_x, pad_y = 4, 1
        w, h = logical.width + 2 * pad_x, logical.height + 2 * pad_y
        x, y = right - w, mid - h / 2.0
        cr.set_source_rgba(*self._channels(palette.border_strong))
        cr.set_line_width(1)
        _rounded_rect(cr, x + 0.5, y + 0.5, w - 1, h - 1, h / 2.0)
        cr.stroke()
        cr.set_source_rgba(*self._channels(palette.accent_text if selected else palette.text_secondary))
        cr.move_to(x + pad_x, y + pad_y)
        PangoCairo.show_layout(cr, layout)
        return int(w)

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
        # Groups the user opened in this session (never persisted; empty = all collapsed).
        self.expanded_keys: Set[RowKey] = set()
        self._tracking = True  # row-expanded / row-collapsed update expanded_keys
        self._temporary = False  # search/filter view: expansions are not the user's
        self._passing_keys: Set[RowKey] = set()  # opened by type-ahead search or drag hover
        self._typeahead_hooked = False
        self._drag_hover = False
        self.expand_calls = 0  # rows expanded by populate() (tests, benchmarks)
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
        self.connect("row-expanded", self._on_row_expanded)
        self.connect("row-collapsed", self._on_row_collapsed)
        # Drag hover may open a row (GTK's auto-expand); it closes again unless a drop went there.
        self.connect("drag-motion", self._on_drag_motion)
        self.connect("drag-leave", self._on_drag_leave)
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
        cell.set_property("file_backed", bool(model.get_value(tree_iter, TreeColumn.FILE_BACKED)))
        host = model.get_value(tree_iter, TreeColumn.HOST)
        cell.set_property("protocol", protocols.protocol_of(host) if host is not None else "")
        cell.set_property("badge", protocols.badge_for(host) if host is not None else "")
        cell.set_property("expandable", model.iter_has_child(tree_iter))
        cell.set_property("expanded", self.row_expanded(path))
        cell.set_property("gap", Layout.Tree.GROUP_GAP if depth == 0 and indices and indices[0] > 0 else 0)

    # ------------------------------------------------------------------ api
    def populate(self, rows: Sequence[TreeRow], *, temporary: Optional[Iterable[RowKey]] = None
                 ) -> Dict[str, Gtk.TreeIter]:
        """Replace the model content with ``rows``; return ``{host_key: iter}``.

        Only the rows in :attr:`expanded_keys` are expanded (keys of groups
        that no longer exist are dropped). With ``temporary`` (search/filter
        results) exactly those rows are expanded instead and
        :attr:`expanded_keys` is left alone, so clearing the search restores
        the user's view.
        """

        store = self.store
        host_iters: Dict[str, Gtk.TreeIter] = {}
        present: Set[RowKey] = set()
        columns = list(range(len(COLUMN_TYPES)))

        def append(parent_iter, row: TreeRow) -> None:
            # insert_with_values: about half the time of append(), whose override converts each value in
            # Python (0.4 s of a 4,000-host rebuild). The host object needs its GValue type spelled out.
            tree_iter = store.insert_with_values(
                parent_iter,
                -1,
                columns,
                [
                    row.icon,
                    row.text,
                    row.kind.value,
                    row.parent,
                    row.child,
                    row.alias,
                    GObject.Value(GObject.TYPE_PYOBJECT, row.host),
                    row.search,
                    row.color,
                    row.status,
                    int(row.count or 0),
                    row.tooltip,
                    bool(row.read_only),
                    bool(row.file_backed),
                ],
            )
            if row.kind is RowKind.HOST:
                host_iters[row.key] = tree_iter
            else:
                present.add((row.kind.value, row.parent, row.child))
            for child in row.children:
                append(tree_iter, child)

        self._tracking = False
        try:
            # Detached while filling: no per-row signals or layout work in the view.
            self.set_model(None)
            store.clear()
            for row in rows:
                append(None, row)
            self.set_model(store)
            self._passing_keys.clear()
            self._temporary = temporary is not None
            if temporary is None:
                self.expanded_keys &= present  # deleted / renamed groups
                wanted: Set[RowKey] = self.expanded_keys
            else:
                wanted = set(temporary)
            self.expand_calls = self._expand_matching(wanted)
        finally:
            self._tracking = True
        return host_iters

    def _row_key(self, tree_iter) -> RowKey:
        model = self.store
        return (
            model.get_value(tree_iter, TreeColumn.KIND) or "",
            model.get_value(tree_iter, TreeColumn.PARENT) or "",
            model.get_value(tree_iter, TreeColumn.CHILD) or "",
        )

    def _expand_matching(self, wanted: Set[RowKey], parent_iter=None) -> int:
        """Expand the rows in ``wanted``, parents before children; a row under a closed parent stays shut."""

        if not wanted:
            return 0
        model = self.store
        count = 0
        tree_iter = model.iter_children(parent_iter)
        while tree_iter is not None:
            if model.iter_has_child(tree_iter) and self._row_key(tree_iter) in wanted:
                self.expand_row(model.get_path(tree_iter), False)
                count += 1 + self._expand_matching(wanted, tree_iter)
            tree_iter = model.iter_next(tree_iter)
        return count

    def _find_path(self, kind: str, parent: str, child: str = "", alias: str = "") -> Optional[Gtk.TreePath]:
        target = (kind, parent, child)
        found: List[Gtk.TreePath] = []

        def visit(model, path, tree_iter) -> bool:
            if self._row_key(tree_iter) == target and (not alias or model.get_value(tree_iter, TreeColumn.ALIAS) == alias):
                found.append(path.copy())
                return True
            return False

        self.store.foreach(visit)
        return found[0] if found else None

    def set_row_collapsed(self, kind: str, parent: str, child: str = "", collapsed: bool = True) -> None:
        """Collapse/expand a group row by its key (tests, snapshots and the controller)."""

        path = self._find_path(kind, parent, child)
        if path is None:
            return
        if collapsed:
            self.collapse_row(path)
            self.expanded_keys.discard((kind, parent, child))
        else:
            self.expand_to_path(path)

    def set_row_expanded(self, kind: str, parent: str, child: str = "", expanded: bool = True) -> None:
        self.set_row_collapsed(kind, parent, child, collapsed=not expanded)

    def expand_all_rows(self) -> None:
        """Open every group (and remember it for this session)."""

        self.expand_all()

    def collapse_all_rows(self) -> None:
        """Close every group; :attr:`expanded_keys` becomes empty."""

        self.collapse_all()
        self.expanded_keys.clear()

    def reveal_path(self, path: Optional[Gtk.TreePath]) -> None:
        """Expand the ancestors of ``path`` (a user action: remembered) and scroll to it."""

        if path is None:
            return
        if path.get_depth() > 1:
            parent = path.copy()
            parent.up()
            self.expand_to_path(parent)
        self.scroll_to_cell(path, None, False, 0.0, 0.0)

    def expand_to_row(self, kind: str, parent: str, child: str = "", alias: str = "") -> Optional[Gtk.TreePath]:
        """Reveal a row by its key: expand all its ancestors and scroll to it. Returns its path."""

        path = self._find_path(kind, parent, child, alias)
        self.reveal_path(path)
        return path

    def is_row_visible(self, path: Gtk.TreePath) -> bool:
        """True when every ancestor of ``path`` is expanded."""

        ancestor = path.copy()
        while ancestor.get_depth() > 1:
            ancestor.up()
            if not self.row_expanded(ancestor):
                return False
        return True

    def remap_keys(self, old_parent: str, new_parent: str, old_child: str = "", new_child: str = "") -> None:
        """A group was renamed or moved: carry its open state (and its subgroups') to the new key.

        ``old_child`` empty = a top-level group (``parent`` renamed). Otherwise
        the subgroup ``old_child`` (and everything under ``old_child/``) of
        ``old_parent`` becomes ``new_child`` under ``new_parent``.
        """

        updated: Set[RowKey] = set()
        for kind, parent, child in self.expanded_keys:
            if parent != old_parent:
                updated.add((kind, parent, child))
            elif not old_child:
                updated.add((kind, new_parent, child))
            elif child == old_child or child.startswith(old_child + "/"):
                updated.add((kind, new_parent, new_child + child[len(old_child):]))
            else:
                updated.add((kind, parent, child))
        self.expanded_keys = updated

    # ------------------------------------------------------- expand state
    def _on_row_expanded(self, _view, tree_iter, path) -> None:
        if not self._tracking:
            return
        key = self._row_key(tree_iter)
        if self._temporary:
            return  # search/filter results: not the user's layout
        if self._drag_hover or self._typeahead_active():
            if key not in self.expanded_keys:
                self._passing_keys.add(key)
            return
        self.expanded_keys.add(key)
        # Subgroups that were open before the parent was closed open again.
        self._tracking = False
        try:
            self._expand_matching(self.expanded_keys, tree_iter)
        finally:
            self._tracking = True

    def _on_row_collapsed(self, _view, tree_iter, _path) -> None:
        if not self._tracking or self._temporary:
            return
        key = self._row_key(tree_iter)
        self.expanded_keys.discard(key)
        self._passing_keys.discard(key)

    def _close_passing_rows(self) -> bool:
        """Close rows opened only by type-ahead search or drag hover (deepest first)."""

        keys, self._passing_keys = self._passing_keys, set()
        paths = [p for p in (self._find_path(*key) for key in keys if key not in self.expanded_keys) if p is not None]
        self._tracking = False
        try:
            for path in sorted(paths, key=lambda p: -p.get_depth()):
                if self.row_expanded(path):
                    self.collapse_row(path)
        finally:
            self._tracking = True
        return GLib.SOURCE_REMOVE

    def _typeahead_active(self) -> bool:
        entry = self.get_search_entry()
        if entry is None:
            return False
        window = entry.get_toplevel()
        if window is None or window is entry or not isinstance(window, Gtk.Window):
            return False
        if not self._typeahead_hooked:
            window.connect("hide", lambda *_: GLib.idle_add(self._close_passing_rows))
            self._typeahead_hooked = True
        return bool(window.get_visible())

    def _on_drag_motion(self, *_args) -> bool:
        self._drag_hover = True
        return False  # GTK and the drag-and-drop controller still handle it

    def _on_drag_leave(self, *_args) -> None:
        # Leave also fires before a drop: by the idle callback the drop has rebuilt the tree.
        self._drag_hover = False
        GLib.idle_add(self._close_passing_rows)

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
