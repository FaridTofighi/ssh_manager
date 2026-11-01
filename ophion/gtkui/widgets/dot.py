"""Small filled status dot drawn in the widget's CSS ``color``."""

from __future__ import annotations

import math

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # type: ignore  # noqa: E402


class Dot(Gtk.DrawingArea):
    """Filled circle; style it with ``color`` (e.g. ``.status-dot.connected``)."""

    def __init__(self, diameter: int = 8, *, css_class: str = "") -> None:
        Gtk.DrawingArea.__init__(self)
        self._diameter = diameter
        self.set_size_request(diameter, diameter)
        self.set_valign(Gtk.Align.CENTER)
        self.set_halign(Gtk.Align.CENTER)
        if css_class:
            for name in css_class.split():
                self.get_style_context().add_class(name)
        self.connect("draw", self._on_draw)

    def set_state_class(self, state: str, choices=("connected", "connecting", "failed", "unknown", "warning", "error", "success")) -> None:
        ctx = self.get_style_context()
        for name in choices:
            ctx.remove_class(name)
        if state:
            ctx.add_class(state)
        self.queue_draw()

    def _on_draw(self, widget: Gtk.Widget, cr) -> bool:
        ctx = widget.get_style_context()
        color = ctx.get_color(ctx.get_state())
        width, height = widget.get_allocated_width(), widget.get_allocated_height()
        radius = self._diameter / 2.0
        cr.set_source_rgba(color.red, color.green, color.blue, color.alpha)
        cr.arc(width / 2.0, height / 2.0, radius, 0, 2 * math.pi)
        cr.fill()
        return False
