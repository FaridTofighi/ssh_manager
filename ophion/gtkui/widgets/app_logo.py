"""The application icon as a small widget (the header's brand mark).

The artwork is a transparent spiral mark (raster), drawn as it is: no tile, no
clip, no border or shadow (the PNG is never edited). The source is the smallest
packaged size at least as large as the mark at the screen's scale factor,
scaled down with HYPER — never enlarged past a file's own size.

The same transparent mark in light and dark theme (the cream band is weak on
white; that is accepted, not compensated: no plate, outline or glow).
"""

from __future__ import annotations

import logging
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
    def __init__(self, size: int, radius: int = 6) -> None:  # radius: kept for callers; the mark is not clipped
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
        # Device pixels 1:1 (a tile larger than every file shows the file's size, never an enlargement).
        surface = Gdk.cairo_surface_create_from_pixbuf(pixbuf, max(1, int(scale)), None)
        cr.set_source_surface(surface, 0, 0)
        cr.paint()
        return True


__all__ = ["AppLogo", "logo_pixbuf"]
