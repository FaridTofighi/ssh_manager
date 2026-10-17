"""Design tokens: the single source of truth for colors, spacing and motion.

Everything visual in the GTK front-end is derived from the values in this
module. Stylesheets are generated from the palettes (see ``stylesheet.py``)
and Python code that needs a color at runtime (tree status glyphs, the
terminal status bar, …) asks this module instead of hard-coding hex values.

The module is pure Python so it can be imported and tested without GTK.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from enum import Enum
from typing import Dict, Iterator, List, Tuple


def enum_value(value: object) -> str:
    """Return the raw string of a ``str`` Enum member (``str()`` gives ``Cls.NAME`` on 3.11+)."""

    return str(value.value) if isinstance(value, Enum) else str(value)


# --------------------------------------------------------------------- scales
class Space:
    """Spacing scale in pixels. Only these values may be used for margins/gaps."""

    XS = 4
    S = 8
    M = 12
    L = 16
    XL = 24

    @classmethod
    def values(cls) -> Tuple[int, ...]:
        return (cls.XS, cls.S, cls.M, cls.L, cls.XL)


class Radius:
    """Corner radius scale in pixels."""

    SMALL = 4  # inputs, small chips
    MEDIUM = 8  # buttons, cards, toasts
    LARGE = 12  # panels
    PILL = 999  # pills, badges, dots


class Motion:
    """Animation durations in milliseconds (ease-out curves)."""

    FAST = 120
    NORMAL = 160
    SLOW = 200
    EASING = "cubic-bezier(0.25, 0.46, 0.45, 0.94)"


class FontScale:
    """Relative type scale (multiples of the application font size)."""

    CAPTION = 0.85
    BODY = 1.0
    SUBTITLE = 1.1
    TITLE = 1.35


class Level(str, Enum):
    """Feedback levels shared by toasts, banners and the message history."""

    INFO = "info"
    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"


class ConnectionStatus(str, Enum):
    """Visual connection states for hosts and sessions."""

    CONNECTED = "connected"
    CONNECTING = "connecting"
    FAILED = "failed"
    UNKNOWN = "unknown"

    @classmethod
    def coerce(cls, value: object) -> "ConnectionStatus":
        try:
            return cls(enum_value(value))
        except ValueError:
            return cls.UNKNOWN


# Shape glyphs so status is never communicated by color alone.
STATUS_GLYPHS: Dict[ConnectionStatus, str] = {
    ConnectionStatus.CONNECTED: "●",
    ConnectionStatus.CONNECTING: "◐",
    ConnectionStatus.FAILED: "✖",
    ConnectionStatus.UNKNOWN: "",
}


# ------------------------------------------------------------------- palettes
@dataclass(frozen=True)
class Palette:
    """Semantic color roles for one theme variant."""

    name: str
    bg_window: str
    bg_surface: str
    bg_surface_raised: str
    bg_hover: str
    bg_selected: str
    border_subtle: str
    border_strong: str
    text_primary: str
    text_secondary: str
    text_disabled: str
    accent: str
    accent_hover: str
    on_accent: str
    success: str
    warning: str
    danger: str
    danger_strong: str
    on_danger: str
    info: str
    focus_ring: str
    chip_bg: str
    broadcast_bg: str
    broadcast_fg: str
    icon_group: str
    icon_subgroup: str
    icon_host: str
    shadow: str

    def items(self) -> Iterator[Tuple[str, str]]:
        """Yield ``(role, value)`` for every color role."""

        for field in fields(self):
            if field.name == "name":
                continue
            yield field.name, getattr(self, field.name)

    def status_color(self, status: object) -> str:
        """Return the color used for a connection status."""

        state = ConnectionStatus.coerce(status)
        return {
            ConnectionStatus.CONNECTED: self.success,
            ConnectionStatus.CONNECTING: self.info,
            ConnectionStatus.FAILED: self.danger,
            ConnectionStatus.UNKNOWN: self.text_disabled,
        }[state]

    def level_color(self, level: object) -> str:
        """Return the color used for a feedback level."""

        try:
            lvl = Level(enum_value(level))
        except ValueError:
            lvl = Level.INFO
        return {
            Level.INFO: self.info,
            Level.SUCCESS: self.success,
            Level.WARNING: self.warning,
            Level.ERROR: self.danger,
        }[lvl]


DARK = Palette(
    name="dark",
    bg_window="#1b1e23",
    bg_surface="#23272e",
    bg_surface_raised="#2b3038",
    bg_hover="#323843",
    bg_selected="#24456e",
    border_subtle="#363c46",
    border_strong="#4d5563",
    text_primary="#e8eaed",
    text_secondary="#aeb4bf",
    text_disabled="#7a828e",
    accent="#1c71d8",
    accent_hover="#2a7de6",
    on_accent="#ffffff",
    success="#57e389",
    warning="#f6d32d",
    danger="#ff7b72",
    danger_strong="#c01c28",
    on_danger="#ffffff",
    info="#78aeed",
    focus_ring="#78aeed",
    chip_bg="#323843",
    broadcast_bg="#5c1a1f",
    broadcast_fg="#ffe1df",
    icon_group="#e5a50a",
    icon_subgroup="#62a0ea",
    icon_host="#aeb4bf",
    shadow="rgba(0, 0, 0, 0.45)",
)

LIGHT = Palette(
    name="light",
    bg_window="#f6f7f9",
    bg_surface="#ffffff",
    bg_surface_raised="#ffffff",
    bg_hover="#eef0f3",
    bg_selected="#d6e4fa",
    border_subtle="#dde1e6",
    border_strong="#b3bac4",
    text_primary="#1f2328",
    text_secondary="#57606a",
    text_disabled="#7d858f",
    accent="#1c71d8",
    accent_hover="#1a64c2",
    on_accent="#ffffff",
    success="#1a7f37",
    warning="#9a6700",
    danger="#c01c28",
    danger_strong="#c01c28",
    on_danger="#ffffff",
    info="#1c71d8",
    focus_ring="#1c71d8",
    chip_bg="#eef0f3",
    broadcast_bg="#ffe3e5",
    broadcast_fg="#82071e",
    icon_group="#9a6700",
    icon_subgroup="#1c71d8",
    icon_host="#57606a",
    shadow="rgba(15, 23, 42, 0.18)",
)

PALETTES: Dict[str, Palette] = {"dark": DARK, "light": LIGHT}


def palette_for(dark: bool) -> Palette:
    return DARK if dark else LIGHT


# ------------------------------------------------------------------ contrast
def _channel(value: int) -> float:
    srgb = value / 255.0
    return srgb / 12.92 if srgb <= 0.03928 else ((srgb + 0.055) / 1.055) ** 2.4


def parse_hex(color: str) -> Tuple[int, int, int]:
    """Parse ``#rgb`` or ``#rrggbb`` into an RGB tuple."""

    text = color.strip().lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    if len(text) != 6:
        raise ValueError(f"Not a hex color: {color!r}")
    return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)


def relative_luminance(color: str) -> float:
    r, g, b = parse_hex(color)
    return 0.2126 * _channel(r) + 0.7152 * _channel(g) + 0.0722 * _channel(b)


def contrast_ratio(foreground: str, background: str) -> float:
    """WCAG 2.x contrast ratio between two opaque hex colors."""

    l1 = relative_luminance(foreground)
    l2 = relative_luminance(background)
    lighter, darker = max(l1, l2), min(l1, l2)
    return (lighter + 0.05) / (darker + 0.05)


AA_TEXT = 4.5
AA_UI = 3.0

# (foreground role, background role, minimum ratio)
CONTRAST_PAIRS: List[Tuple[str, str, float]] = [
    ("text_primary", "bg_window", AA_TEXT),
    ("text_primary", "bg_surface", AA_TEXT),
    ("text_primary", "bg_surface_raised", AA_TEXT),
    ("text_primary", "bg_hover", AA_TEXT),
    ("text_primary", "bg_selected", AA_TEXT),
    ("text_primary", "chip_bg", AA_TEXT),
    ("text_secondary", "bg_window", AA_TEXT),
    ("text_secondary", "bg_surface", AA_TEXT),
    ("text_secondary", "bg_surface_raised", AA_TEXT),
    ("on_accent", "accent", AA_TEXT),
    ("on_danger", "danger_strong", AA_TEXT),
    ("broadcast_fg", "broadcast_bg", AA_TEXT),
    ("accent", "bg_surface", AA_UI),
    ("focus_ring", "bg_surface", AA_UI),
    ("focus_ring", "bg_window", AA_UI),
    ("success", "bg_surface", AA_UI),
    ("warning", "bg_surface", AA_UI),
    ("danger", "bg_surface", AA_UI),
    ("info", "bg_surface", AA_UI),
    ("border_strong", "bg_surface", 1.0),
    ("icon_group", "bg_surface", AA_UI),
    ("icon_subgroup", "bg_surface", AA_UI),
    ("icon_host", "bg_surface", AA_UI),
]


def contrast_report(palette: Palette) -> List[Tuple[str, str, float, float]]:
    """Return ``(fg_role, bg_role, ratio, minimum)`` for every checked pair."""

    report = []
    for fg_role, bg_role, minimum in CONTRAST_PAIRS:
        ratio = contrast_ratio(getattr(palette, fg_role), getattr(palette, bg_role))
        report.append((fg_role, bg_role, ratio, minimum))
    return report
