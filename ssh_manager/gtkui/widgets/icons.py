"""Bundled outline icons rendered from SVG in the widget's current color.

GTK 3's symbolic-icon recoloring only overrides ``fill``, which turns stroked
outline icons into solid shapes. The bundled set in ``gtkui/assets/icons`` is
stroke-based (``currentColor``), so it is rendered here with librsvg: the SVG
text gets ``currentColor`` replaced by the requested color and is rendered at
the device scale into a Cairo surface (crisp on HiDPI, no pixel painting).

* :class:`SvgIcon` – a widget that draws an icon in its CSS ``color``, so
  hover/selected/disabled states recolor it automatically.
* :func:`make_icon` – ``SvgIcon`` for bundled names, themed ``Gtk.Image``
  otherwise (fallback when a file is missing).
* :func:`tinted_icon_surface` – a Cairo surface for cell renderers.
* :func:`render_tree_icon` – a session-tree icon with its status badge and lock
  overlay composited on (each a bundled SVG; the overlays sit in a transparent
  cut-out ring, so the row's real background shows around them).

The session-tree family (folder, folder-open, file-conf, server, badges) is
drawn on the 24-grid with a 1.75 outline converted to filled paths plus a 20 %
duotone fill (``opacity`` on a second path). Filled paths survive GTK's own
symbolic recoloring too, should a name ever reach ``Gtk.IconTheme`` directly.
"""

from __future__ import annotations

import functools
import logging
from pathlib import Path
from typing import Dict, Optional, Tuple

import cairo
import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Rsvg", "2.0")
from gi.repository import Gdk, Gtk, Rsvg  # type: ignore  # noqa: E402

LOGGER = logging.getLogger(__name__)
ICON_DIR = Path(__file__).resolve().parent.parent / "assets" / "icons"
PREFIX = "sshm-"

IconCache = Dict[Tuple[str, str, int, int], object]
_SURFACES: IconCache = {}


def register_icon_path() -> None:
    """Add the bundled icons to the default icon theme search path (idempotent)."""

    theme = Gtk.IconTheme.get_default()
    if theme is not None and str(ICON_DIR) not in theme.get_search_path():
        theme.append_search_path(str(ICON_DIR))


APP_ICON_NAME = "ophion"
APP_ICON_DIR = Path(__file__).resolve().parent.parent / "assets" / "app-icon"  # holds hicolor/<size>/apps/
APP_ICON_SIZES = (16, 22, 24, 32, 48, 64, 96, 128, 256, 512)


def app_icon_file(size: int = 256) -> Path:
    return APP_ICON_DIR / "hicolor" / f"{size}x{size}" / "apps" / f"{APP_ICON_NAME}.png"


def install_app_icon(theme: Optional[Gtk.IconTheme] = None, search_dir: Optional[Path] = None) -> str:
    """The default icon of every window: the ``ophion`` theme icon, else the 256 px file.

    Works from a source checkout too (the bundled hicolor folder joins the search path). Returns ``"name"``,
    ``"file"`` or ``""`` (no icon); never raises.
    """

    theme = theme or Gtk.IconTheme.get_default()
    folder = str(search_dir or APP_ICON_DIR)
    try:
        if theme is not None and folder not in theme.get_search_path():
            theme.append_search_path(folder)
        if theme is not None and theme.has_icon(APP_ICON_NAME):
            Gtk.Window.set_default_icon_name(APP_ICON_NAME)
            return "name"
        Gtk.Window.set_default_icon_from_file(str(app_icon_file()))
        return "file"
    except Exception as exc:  # a missing or unreadable icon must never stop the app
        LOGGER.info("app icon: not set (%s)", type(exc).__name__)
        return ""


def _file_for(name: str) -> Optional[Path]:
    base = name[:-len("-symbolic")] if name.endswith("-symbolic") else name
    if not base.startswith(PREFIX):
        base = PREFIX + base
    path = ICON_DIR / f"{base}-symbolic.svg"
    return path if path.exists() else None


def is_bundled(name: str) -> bool:
    return _file_for(name) is not None


@functools.lru_cache(maxsize=None)
def _svg_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _hex(rgba: Gdk.RGBA) -> str:
    return "#{:02x}{:02x}{:02x}".format(*(int(round(c * 255)) for c in (rgba.red, rgba.green, rgba.blue)))


def _parse(color: str) -> Optional[Gdk.RGBA]:
    rgba = Gdk.RGBA()
    return rgba if color and rgba.parse(color) else None


def render_svg_surface(name: str, color: str, size: int = 16, scale: int = 1, cache: Optional[IconCache] = None):
    """Render bundled icon ``name`` in ``color`` at ``size``×``size`` logical px."""

    path = _file_for(name)
    rgba = _parse(color)
    if path is None or rgba is None:
        return None
    store = _SURFACES if cache is None else cache
    key = (path.name, color, size, scale)
    if key in store:
        return store[key]
    svg = _svg_text(path).replace("currentColor", _hex(rgba))
    if rgba.alpha < 1.0:
        svg = svg.replace("<svg ", f'<svg opacity="{rgba.alpha:.3f}" ', 1)
    handle = Rsvg.Handle.new_from_data(svg.encode("utf-8"))
    pixels = size * scale
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, pixels, pixels)
    cr = cairo.Context(surface)
    viewport = Rsvg.Rectangle()
    viewport.x, viewport.y, viewport.width, viewport.height = 0, 0, pixels, pixels
    handle.render_document(cr, viewport)
    surface.set_device_scale(scale, scale)
    store[key] = surface
    return surface


def tinted_icon_surface(
    icon_name: str,
    color: str,
    *,
    widget: Optional[Gtk.Widget] = None,
    size: int = 16,
    cache: Optional[IconCache] = None,
):
    """Cairo surface of ``icon_name`` in ``color`` (bundled SVG, else themed icon)."""

    rgba = _parse(color)
    if rgba is None:
        return None
    scale = widget.get_scale_factor() if widget is not None else 1
    surface = render_svg_surface(icon_name, color, size, scale, cache)
    if surface is not None:
        return surface
    key = (icon_name, color, size, scale)
    if cache is not None and key in cache:
        return cache[key]
    theme = Gtk.IconTheme.get_default()
    info = theme.lookup_icon_for_scale(icon_name, size, scale, Gtk.IconLookupFlags.FORCE_SIZE)
    if info is None:
        return None
    try:
        pixbuf, _was_symbolic = info.load_symbolic(rgba, None, None, None)
    except Exception:
        return None
    window = widget.get_window() if widget is not None else None
    surface = Gdk.cairo_surface_create_from_pixbuf(pixbuf, scale, window)
    if cache is not None:
        cache[key] = surface
    return surface


def render_tree_icon(
    name: str,
    color: str,
    size: int,
    scale: int = 1,
    *,
    badge: str = "",
    badge_color: str = "",
    badge_alpha: float = 1.0,
    lock: bool = False,
    cache: Optional[IconCache] = None,
):
    """``name`` in ``color`` at ``size`` px with an optional status ``badge`` and lock.

    Every layer is a bundled SVG rendered at device pixels; overlays are placed by
    :mod:`gtkui.tree_icons` (badge bottom-right, lock bottom-right or top-right).
    Before an overlay is drawn, a circle ``cutout_ring`` px wider is erased from
    the icon (``DEST_OUT``), so the ring is the row's own background on normal,
    hover and selected rows alike. Results are cached by every input.
    """

    from ..constants import Icons
    from ..tree_icons import badge_box, cutout_ring, lock_box

    store = _SURFACES if cache is None else cache
    alpha = round(float(badge_alpha), 3)
    key = ("tree", name, color, size, scale, badge, badge_color, alpha, bool(lock))
    if key in store:
        return store[key]
    pixels = size * scale
    base = render_svg_surface(name, color, pixels, 1, store)
    if base is None:
        return None
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, pixels, pixels)
    cr = cairo.Context(surface)
    cr.set_source_surface(base, 0, 0)
    cr.paint()
    overlays = []
    if lock:
        overlays.append((Icons.BADGE_LOCK, color, lock_box(size, with_badge=bool(badge)), 1.0))
    if badge and badge_color:
        overlays.append((badge, badge_color, badge_box(size), alpha))
    ring = cutout_ring(size)
    for glyph, glyph_color, box, glyph_alpha in overlays:
        cx, cy = (v * scale for v in box.center)
        cut = int(round((box.size + 2 * ring) * scale))
        mask = render_svg_surface(Icons.BADGE_CUTOUT, glyph_color, cut, 1, store)  # only its alpha is used
        if mask is not None:
            cr.set_operator(cairo.OPERATOR_DEST_OUT)
            cr.mask_surface(mask, cx - cut / 2.0, cy - cut / 2.0)
            cr.set_operator(cairo.OPERATOR_OVER)
        glyph_px = int(round(box.size * scale))
        layer = render_svg_surface(glyph, glyph_color, glyph_px, 1, store)
        if layer is not None:
            cr.set_source_surface(layer, cx - glyph_px / 2.0, cy - glyph_px / 2.0)
            cr.paint_with_alpha(glyph_alpha)
    surface.flush()
    surface.set_device_scale(scale, scale)
    store[key] = surface
    return surface


def add_combo_icon(combo: Gtk.ComboBox, icon_name: str, color_role: str, size: int = 16) -> Gtk.CellRendererPixbuf:
    """Put ``icon_name`` (in palette color ``color_role``) in front of every combo row.

    Used by pickers that list groups (folder / file-conf, as in the tree). The
    color is read from the active palette at draw time, so theme switches apply.
    """

    from ..theme.loader import current_palette

    cell = Gtk.CellRendererPixbuf()
    cell.set_padding(0, 0)
    combo.pack_start(cell, False)
    combo.reorder(cell, 0)

    def data(_layout, renderer, _model, _iter, _user=None) -> None:
        color = getattr(current_palette(), color_role)
        renderer.set_property("surface", render_svg_surface(icon_name, color, size, combo.get_scale_factor()))

    combo.set_cell_data_func(cell, data)
    return cell


class SvgIcon(Gtk.DrawingArea):
    """Bundled icon drawn in the widget's CSS ``color`` (follows states and theme)."""

    def __init__(self, name: str, size: int = 16, *, css_class: str = "") -> None:
        Gtk.DrawingArea.__init__(self)
        self._name = name
        self._size = size
        self.set_size_request(size, size)
        self.set_halign(Gtk.Align.CENTER)
        self.set_valign(Gtk.Align.CENTER)
        self.get_style_context().add_class("svg-icon")
        if css_class:
            self.get_style_context().add_class(css_class)
        self.connect("draw", self._on_draw)

    @property
    def icon_name(self) -> str:
        return self._name

    def set_icon(self, name: str) -> None:
        self._name = name
        self.queue_draw()

    @property
    def pixel_size(self) -> int:
        return self._size

    def set_pixel_size(self, size: int) -> None:
        """Change the drawn size (density switch); same name as ``Gtk.Image``'s setter."""

        if size != self._size:
            self._size = int(size)
            self.set_size_request(self._size, self._size)
            self.queue_draw()

    def _on_draw(self, widget: Gtk.Widget, cr) -> bool:
        color = widget.get_style_context().get_color(widget.get_state_flags())
        hex_color = _hex(color)
        if color.alpha < 1.0:
            hex_color = f"rgba({int(color.red*255)},{int(color.green*255)},{int(color.blue*255)},{color.alpha:.3f})"
        surface = render_svg_surface(self._name, hex_color, self._size, widget.get_scale_factor())
        if surface is None:
            return False
        width = widget.get_allocated_width()
        height = widget.get_allocated_height()
        cr.set_source_surface(surface, (width - self._size) / 2.0, (height - self._size) / 2.0)
        cr.paint()
        return False


def make_icon(name: str, size: int = 16, *, css_class: str = "") -> Gtk.Widget:
    """Icon widget for ``name``: bundled :class:`SvgIcon`, else a themed ``Gtk.Image``."""

    if is_bundled(name):
        return SvgIcon(name, size, css_class=css_class)
    gtk_size = Gtk.IconSize.MENU if size <= 16 else Gtk.IconSize.LARGE_TOOLBAR if size <= 24 else Gtk.IconSize.DND
    image = Gtk.Image.new_from_icon_name(name, gtk_size)
    image.set_pixel_size(size)
    if css_class:
        image.get_style_context().add_class(css_class)
    return image
