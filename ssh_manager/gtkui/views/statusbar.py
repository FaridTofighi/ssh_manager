"""Per-session status bar shown under each terminal.

``● Connected · 120×32 ···································· [cpu] 37% [mem] 6.0/7.7 GB [disk] 71/78 GB``

Remote statistics use warning/critical classes at 80 % / 90 % and carry the
exact values in tooltips. The stats section can be hidden from the session
toolbar; the connection state and terminal size are always visible.
"""

from __future__ import annotations

from typing import Optional

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Pango  # type: ignore  # noqa: E402

from ..constants import Icons  # noqa: E402
from ..i18n import _  # noqa: E402
from ..theme.tokens import ConnectionStatus, Size, Space  # noqa: E402
from ..view_models import status_text  # noqa: E402
from ..widgets.icon_button import set_accessible_name  # noqa: E402
from ..widgets.icons import make_icon  # noqa: E402
from ..widgets.status_dot import StatusDot  # noqa: E402

WARN_PERCENT = 80.0
CRITICAL_PERCENT = 90.0


def stat_level(percent: Optional[float]) -> str:
    """``""``, ``"stat-warn"`` or ``"stat-critical"`` for a usage percentage."""

    if percent is None:
        return ""
    if percent >= CRITICAL_PERCENT:
        return "stat-critical"
    if percent >= WARN_PERCENT:
        return "stat-warn"
    return ""


def apply_stat_level(label: object, percent: Optional[float]) -> None:
    """Set the threshold class on ``label`` (no-op for objects without a style context)."""

    getter = getattr(label, "get_style_context", None)
    if not callable(getter):
        return
    ctx = getter()
    for css in ("stat-warn", "stat-critical"):
        ctx.remove_class(css)
    level = stat_level(percent)
    if level:
        ctx.add_class(level)


def _stat(icon_name: str, accessible: str) -> tuple:
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.XS)
    image = make_icon(icon_name, Size.ICON_SMALL, css_class="stat-icon")
    label = Gtk.Label()
    label.set_ellipsize(Pango.EllipsizeMode.END)  # let tiles shrink below the text width
    set_accessible_name(label, accessible)
    box.pack_start(image, False, False, 0)
    box.pack_start(label, False, False, 0)
    return box, label


class SessionStatusBar(Gtk.Box):
    """Thin status bar for one terminal session."""

    def __init__(self) -> None:
        Gtk.Box.__init__(self, orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.M)
        self.get_style_context().add_class("statusbar")

        self.dot = StatusDot(size=Size.DOT_LARGE)
        self.pack_start(self.dot, False, False, 0)
        self.status_label = Gtk.Label(label=status_text(ConnectionStatus.UNKNOWN))
        self.status_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.pack_start(self.status_label, False, False, 0)
        self.size_label = Gtk.Label()
        self.size_label.set_tooltip_text(_("Terminal size (columns × rows)"))
        self.pack_start(self.size_label, False, False, 0)

        self.stats_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.M)
        self.spinner = Gtk.Spinner()
        self.spinner.set_no_show_all(True)
        self.stats_box.pack_start(self.spinner, False, False, 0)
        cpu_box, self.cpu_label = _stat(Icons.CPU, _("CPU usage"))
        mem_box, self.mem_label = _stat(Icons.MEMORY, _("Memory usage"))
        disk_box, self.disk_label = _stat(Icons.DISK, _("Disk usage"))
        for box in (cpu_box, mem_box, disk_box):
            box.show_all()
            self.stats_box.pack_start(box, False, False, 0)
        # Visibility is controlled by set_stats_visible(); parents' show_all() must not reveal it.
        self.stats_box.set_no_show_all(True)
        self.pack_end(self.stats_box, False, False, 0)

    def set_status(self, status: object, message: str = "") -> None:
        state = ConnectionStatus.coerce(status)
        self.dot.set_status(state)
        self.status_label.set_text(message or status_text(state))

    def set_size(self, columns: int, rows: int) -> None:
        self.size_label.set_text(f"{columns}×{rows}" if columns and rows else "")

    def set_stats_visible(self, visible: bool) -> None:
        # show_all() is a no-op on a widget with no-show-all set, so toggle visibility directly.
        self.stats_box.set_visible(visible)
