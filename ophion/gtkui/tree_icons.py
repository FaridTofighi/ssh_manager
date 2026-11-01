"""Session-tree icons: which icon, color, badge and overlay a row gets (no GTK).

The icon family (``gtkui/assets/icons``, 24-grid, 1.75 outline converted to
filled paths plus a 20 % duotone fill):

========================  =====================
Row                       Icon
========================  =====================
group, collapsed          ``sshm-folder``
group, expanded           ``sshm-folder-open``
config-file group         ``sshm-file-conf``
host                      ``sshm-server`` (FTP / FTPS / SFTP: ``sshm-transfer``; RDP: ``sshm-desktop``)
========================  =====================

Every icon is drawn in one color, ``icon_default`` (white on dark, near-black
on light), in every row state: groups and hosts differ by shape, badge and
label weight, never by icon color. A user-chosen item color replaces it.
Read-only hosts get a lock overlay; hosts get a connection-status badge. Both are separate small SVGs composited onto the
icon with a transparent cut-out ring, so the row's real background (normal,
hover, selected) shows around them.

In the inventory, ``RowKind.CHILD`` is the ``*.conf`` file (file-backed group),
``RowKind.PARENT`` a top-level directory and ``RowKind.CHILD_FOLDER`` a
directory between them.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from .. import protocols
from .constants import Icons, RowKind
from .theme.tokens import ConnectionStatus

#: Badge diameter relative to the icon (≈ 42 %), lock overlay relative to the icon (50 %).
BADGE_RATIO = 0.42
LOCK_RATIO = 0.5
#: Below this icon size the failed badge is a plain dot (the × would be mush).
FAILED_CROSS_MIN = 24


@dataclass(frozen=True)
class TreeIcon:
    """The icon of one row: bundled icon name, palette color role, lock overlay."""

    name: str
    color_role: str = "icon_default"
    lock: bool = False


def file_backed(kind: object) -> bool:
    """Whether a group row is a config file (``*.conf``) rather than a directory."""

    return RowKind.coerce(kind) is RowKind.CHILD


def tree_icon_for(
    kind: object,
    expanded: bool = False,
    read_only: bool = False,
    file_backed_group: Optional[bool] = None,
    *,
    selected: bool = False,
    has_children: bool = True,
    protocol: str = "",
) -> TreeIcon:
    """Icon for a tree row.

    ``file_backed_group`` defaults to "the row is a ``*.conf`` group". An empty
    group keeps the closed folder even when GTK reports it expanded.
    """

    del selected  # the icon color is the same in every row state
    row_kind = RowKind.coerce(kind)
    if row_kind is RowKind.HOST:
        kind = protocols.get(protocol) if protocol else None
        family = kind.family if kind is not None else ""
        icon = {"transfer": Icons.TRANSFER, "desktop": Icons.DESKTOP}.get(family, Icons.HOST)
        return TreeIcon(icon, lock=bool(read_only))
    if file_backed(kind) if file_backed_group is None else file_backed_group:
        return TreeIcon(Icons.FILE_CONF)
    return TreeIcon(Icons.FOLDER_OPEN if expanded and has_children else Icons.FOLDER)


def icon_color(palette, icon: Optional[TreeIcon] = None, user_color: str = "") -> str:
    """The color to draw a group / host icon in: the user's item color, else ``icon_default``."""

    return user_color or getattr(palette, icon.color_role if icon is not None else "icon_default")


@dataclass(frozen=True)
class Badge:
    """A status badge: bundled SVG, palette color role, accessible shape name."""

    name: str
    color_role: str
    shape: str  # "dot", "ring" or "cross" (color is never the only cue)
    pulse: bool = False


def status_badge(status: object, icon_size: int) -> Badge:
    """Badge for a host's connection status at ``icon_size`` px."""

    state = ConnectionStatus.coerce(status)
    if state is ConnectionStatus.CONNECTED:
        return Badge(Icons.BADGE_DOT, "badge_connected", "dot")
    if state is ConnectionStatus.CONNECTING:
        return Badge(Icons.BADGE_RING, "warning", "ring", pulse=True)
    if state is ConnectionStatus.FAILED:
        if icon_size >= FAILED_CROSS_MIN:
            return Badge(Icons.BADGE_FAILED, "danger", "cross")
        return Badge(Icons.BADGE_DOT, "danger", "dot")
    return Badge(Icons.BADGE_DOT, "badge_unknown", "dot")


def badge_diameter(icon_size: int) -> int:
    """Badge diameter in logical px (≈ 42 % of the icon, even, at least 6)."""

    return max(6, int(round(icon_size * BADGE_RATIO / 2.0)) * 2)


def lock_size(icon_size: int) -> int:
    return max(6, int(round(icon_size * LOCK_RATIO)))


def cutout_ring(icon_size: int) -> float:
    """Width of the transparent ring around a badge or the lock (2 px from 20 px up).

    With the even badge diameters this keeps the cut-out circle on whole pixels.
    """

    return 2.0 if icon_size >= 20 else 1.5


@dataclass(frozen=True)
class Overlay:
    """Where an overlay goes inside the icon box (logical px, top-left of its box)."""

    x: float
    y: float
    size: float

    @property
    def center(self):
        return self.x + self.size / 2.0, self.y + self.size / 2.0


def badge_box(icon_size: int) -> Overlay:
    """Status badge: bottom-right corner of the icon box."""

    d = badge_diameter(icon_size)
    return Overlay(icon_size - d, icon_size - d, d)


def lock_box(icon_size: int, with_badge: bool) -> Overlay:
    """Lock overlay: bottom-right, or top-right when a status badge takes that corner."""

    size = lock_size(icon_size)
    return Overlay(icon_size - size, 0 if with_badge else icon_size - size, size)


def show_status_badge(kind: object, status: object = None, read_only: bool = False) -> bool:
    """Hosts carry a status badge (gray while unknown); a read-only host whose
    status is unknown shows only its lock, in the badge's corner."""

    if RowKind.coerce(kind) is not RowKind.HOST:
        return False
    return not (read_only and ConnectionStatus.coerce(status) is ConnectionStatus.UNKNOWN)


def pulse_alpha(phase: float) -> float:
    """Badge opacity for the connecting pulse at ``phase`` seconds (0.8 Hz, 0.45–1.0)."""

    return 0.725 + 0.275 * math.cos(2 * math.pi * 0.8 * phase)


def quantize_alpha(alpha: float, steps: int = 8) -> float:
    """Pulse opacity rounded to ``steps`` levels so composited icons stay cached."""

    return round(max(0.0, min(1.0, alpha)) * steps) / steps
