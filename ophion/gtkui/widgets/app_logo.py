"""The application icon as a small widget (the header's brand tile).

The artwork is its own dark square tile (raster, opaque), so it is drawn as it
is, with rounded corners clipped here (the PNG is never edited). The source is
the smallest packaged size at least as large as the tile at the screen's scale
factor, scaled down with HYPER — never enlarged past a file's own size.
"""

from __future__ import annotations

import logging
import math
from typing import Dict, Optional

import cairo
import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk  # type: ignore  # noqa: E402

from .icons import APP_ICON_SIZES, app_icon_file  # noqa: E402

LOGGER = logging.getLogger(__name__)


def logo_pixbuf(pixels: int) -> Optional[GdkPixbuf.Pixbuf]:
    """The icon at ``pixels`` px (or a packaged file's size when none is that large); ``None`` if missing."""

    source = next((size for size in APP_ICON_SIZES if size >= pixels), APP_ICON_SIZES[-1])
    try:
        pixbuf = GdkPixbuf.Pixbuf.new_from_file(str(app_icon_file(source)))
    except GLib.Error as exc:
        LOGGER.info("app logo: %s", exc.message)
        return None
    target = min(pixels, source)
    return pixbuf if target == source else pixbuf.scale_simple(target, target, GdkPixbuf.InterpType.HYPER)


class AppLogo(Gtk.DrawingArea):
    def __init__(self, size: int, radius: int = 6) -> None:
        Gtk.DrawingArea.__init__(self)
        self.set_has_window(False)  # draws only: no input window that could cover a button's (see SvgIcon)
        self._cache: Dict[int, Optional[GdkPixbuf.Pixbuf]] = {}
        self.set_logo_size(size, radius)
        self.connect("draw", lambda _w, cr: self.paint(cr, self.get_scale_factor()) or False)
        self.connect("notify::scale-factor", lambda *_a: self.queue_draw())

    def set_logo_size(self, size: int, radius: int) -> None:
        self.size, self.radius = int(size), int(radius)
        self._cache.clear()
        self.set_size_request(self.size, self.size)
        self.queue_draw()

    def pixbuf_for(self, scale: int) -> Optional[GdkPixbuf.Pixbuf]:
        pixels = self.size * max(1, int(scale))
        if pixels not in self._cache:
            self._cache[pixels] = logo_pixbuf(pixels)
        return self._cache[pixels]

    def paint(self, cr: cairo.Context, scale: int) -> bool:
        """Draw the logo at the top left of ``cr`` (``self.size`` logical px); ``False`` when there is none."""

        pixbuf = self.pixbuf_for(scale)
        if pixbuf is None:
            return False
        size, radius = self.size, min(self.radius, self.size / 2)
        cr.new_sub_path()
        cr.arc(size - radius, radius, radius, -math.pi / 2, 0)
        cr.arc(size - radius, size - radius, radius, 0, math.pi / 2)
        cr.arc(radius, size - radius, radius, math.pi / 2, math.pi)
        cr.arc(radius, radius, radius, math.pi, 3 * math.pi / 2)
        cr.close_path()
        cr.clip()
        # Device pixels 1:1 (a tile larger than every file shows the file's size, never an enlargement).
        surface = Gdk.cairo_surface_create_from_pixbuf(pixbuf, max(1, int(scale)), None)
        cr.set_source_surface(surface, 0, 0)
        cr.paint()
        return True


__all__ = ["AppLogo", "logo_pixbuf"]
