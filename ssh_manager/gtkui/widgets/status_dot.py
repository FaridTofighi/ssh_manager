"""Connection status indicator.

The dot is drawn with Cairo (vector, HiDPI-safe) in the color the stylesheet
assigns to ``.status-dot.<state>``. Each state also has a different *shape* and
a tooltip, so status is never communicated by color alone:

* connected  – filled circle
* connecting – half-filled ring
* failed     – cross
* unknown    – hollow ring
"""

from __future__ import annotations

import math
from typing import Dict

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # type: ignore  # noqa: E402

from ..i18n import _  # noqa: E402
from ..theme.tokens import ConnectionStatus  # noqa: E402

STATUS_LABELS: Dict[ConnectionStatus, str] = {
    ConnectionStatus.CONNECTED: _("Connected"),
    ConnectionStatus.CONNECTING: _("Connecting…"),
    ConnectionStatus.FAILED: _("Connection failed"),
    ConnectionStatus.UNKNOWN: _("Not connected"),
}


def status_label(status: object) -> str:
    return STATUS_LABELS[ConnectionStatus.coerce(status)]


class StatusDot(Gtk.DrawingArea):
    """Small vector status indicator bound to a :class:`ConnectionStatus`."""

    def __init__(self, status: object = ConnectionStatus.UNKNOWN, *, size: int = 10) -> None:
        Gtk.DrawingArea.__init__(self)
        self._size = size
        self._status = ConnectionStatus.UNKNOWN
        self.set_size_request(size, size)
        self.set_valign(Gtk.Align.CENTER)
        self.get_style_context().add_class("status-dot")
        self.connect("draw", self._on_draw)
        self.set_status(status)

    @property
    def status(self) -> ConnectionStatus:
        return self._status

    def set_status(self, status: object) -> None:
        new_status = ConnectionStatus.coerce(status)
        ctx = self.get_style_context()
        for state in ConnectionStatus:
            ctx.remove_class(state.value)
        ctx.add_class(new_status.value)
        self._status = new_status
        label = status_label(new_status)
        self.set_tooltip_text(label)
        accessible = self.get_accessible()
        if accessible is not None:
            accessible.set_name(label)
        self.queue_draw()

    def _on_draw(self, widget: Gtk.Widget, cr) -> bool:
        width = widget.get_allocated_width()
        height = widget.get_allocated_height()
        color = widget.get_style_context().get_color(widget.get_state_flags())
        cr.set_source_rgba(color.red, color.green, color.blue, color.alpha)
        radius = max(2.0, min(width, height) / 2.0 - 1.0)
        cx, cy = width / 2.0, height / 2.0
        cr.set_line_width(max(1.5, radius / 3.0))
        if self._status is ConnectionStatus.CONNECTED:
            cr.arc(cx, cy, radius, 0, 2 * math.pi)
            cr.fill()
        elif self._status is ConnectionStatus.CONNECTING:
            cr.arc(cx, cy, radius - 0.75, 0, 2 * math.pi)
            cr.stroke()
            cr.move_to(cx, cy)
            cr.arc(cx, cy, radius, -math.pi / 2, math.pi / 2)
            cr.close_path()
            cr.fill()
        elif self._status is ConnectionStatus.FAILED:
            offset = radius * 0.8
            cr.move_to(cx - offset, cy - offset)
            cr.line_to(cx + offset, cy + offset)
            cr.move_to(cx + offset, cy - offset)
            cr.line_to(cx - offset, cy + offset)
            cr.stroke()
        else:
            cr.arc(cx, cy, radius - 0.75, 0, 2 * math.pi)
            cr.stroke()
        return False
