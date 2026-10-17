"""Themed symbolic icons tinted with a color (HiDPI-safe).

The session tree tints its folder/host icons with per-kind token colors or a
user-chosen color. Instead of painting pixels by hand, this loads the *theme's*
symbolic icon at the widget's scale factor, recolors it with
``Gtk.IconInfo.load_symbolic`` and wraps it in a Cairo surface that carries the
device scale, so icons stay crisp on HiDPI displays and follow icon-theme
changes.
"""

from __future__ import annotations

from typing import Dict, Optional, Tuple

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gtk  # type: ignore  # noqa: E402

IconCache = Dict[Tuple[str, str, int, int], object]


def _rgba(color: str) -> Optional[Gdk.RGBA]:
    rgba = Gdk.RGBA()
    return rgba if color and rgba.parse(color) else None


def tinted_icon_surface(
    icon_name: str,
    color: str,
    *,
    widget: Optional[Gtk.Widget] = None,
    size: int = 16,
    cache: Optional[IconCache] = None,
):
    """Return a Cairo surface of ``icon_name`` recolored with ``color`` (or ``None``)."""

    rgba = _rgba(color)
    if rgba is None:
        return None
    scale = widget.get_scale_factor() if widget is not None else 1
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
