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
from typing import Dict, Iterator, List, Optional, Tuple


def enum_value(value: object) -> str:
    """Return the raw string of a ``str`` Enum member (``str()`` gives ``Cls.NAME`` on 3.11+)."""

    return str(value.value) if isinstance(value, Enum) else str(value)


# --------------------------------------------------------------------- scales
# Geometry lives in the density presets (``density.py``). ``Space``, ``Radius``,
# ``Size``, ``Layout`` and ``FontScale`` are *live views* of the active preset:
# ``Size.TOPBAR`` is 44 in Compact and 76 in Comfortable, read at the moment it
# is used. Code that lays widgets out reads them in its ``apply_density()``, so
# a density switch at runtime re-applies the geometry.
from . import density as _density  # noqa: E402


class _LiveGroup:
    """Attribute access forwards to one group of the active density preset."""

    def __init__(self, group: str, doc: str = "") -> None:
        self._group = group
        self.__doc__ = doc

    def _tokens(self):
        return getattr(_density.active(), self._group)

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._tokens(), name)

    def __repr__(self) -> str:
        return f"<live {self._group} tokens ({_density.active().density.value})>"


class _LiveLayout:
    """``Layout.Sidebar.SEARCH_TOP`` → ``active().sidebar.SEARCH_TOP``."""

    Topbar = _LiveGroup("topbar", "Top bar offsets.")
    Sidebar = _LiveGroup("sidebar", "Sidebar offsets.")
    Tree = _LiveGroup("tree", "Tree row geometry.")
    Page = _LiveGroup("page", "Host page offsets.")
    Ready = _LiveGroup("ready", "Ready-to-connect state offsets.")


Space = _LiveGroup("space", "Spacing scale (px) of the active density.")
Radius = _LiveGroup("radius", "Corner radius scale (px) of the active density.")
Size = _LiveGroup("size", "Component sizes (px) of the active density.")
Layout = _LiveLayout()


class Motion:
    """Animation durations in milliseconds (ease-out curves)."""

    FAST = 120
    NORMAL = 160
    SLOW = 200
    EASING = "cubic-bezier(0.25, 0.46, 0.45, 0.94)"


BASE_FONT_PX = 11 * 96 / 72  # 1em at the default application font size (11 pt @ 96 dpi)


def em(px: float) -> float:
    """Express a reference-design pixel size relative to the app font size."""

    return round(px / BASE_FONT_PX, 3)


class _LiveFontScale:
    """Relative type scale of the active density (multiples of the app font size)."""

    def __getattr__(self, name: str) -> float:
        if name.startswith("_"):
            raise AttributeError(name)
        tokens = _density.active()
        return em(getattr(tokens.font, name) / tokens.base_font_scale)


FontScale = _LiveFontScale()


UI_FONT_FAMILIES = ("Inter", "Cantarell", "Noto Sans", "DejaVu Sans")
MONO_FONT_FAMILIES = ("JetBrains Mono", "Fira Code", "DejaVu Sans Mono")
CAPS_TRACKING = _density.COMFORTABLE.caps_tracking  # per density: ``density.active().caps_tracking``
BADGE_TRACKING = _density.COMFORTABLE.badge_tracking


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
    bg_canvas: str
    bg_input: str
    bg_page_bar: str
    bg_hover: str
    bg_selected: str
    border_subtle: str
    border_input: str
    border_strong: str
    text_primary: str
    text_secondary: str
    text_muted: str
    text_faint: str
    text_disabled: str
    tab_inactive: str
    accent: str
    accent_hover: str
    accent_pressed: str
    accent_text: str
    accent_tint: str
    accent_soft_border: str
    on_accent: str
    success: str
    warning: str
    danger: str
    danger_strong: str
    on_danger: str
    info: str
    focus_ring: str
    chip_bg: str
    avatar_bg: str
    # Broadcast input has its own hue (violet): unmistakable, but not the red of errors.
    broadcast: str  # the strip's line, the chip, the BROADCAST badge
    broadcast_bg: str
    broadcast_fg: str
    # Group and host icons (gtkui/tree_icons.py), in every row state: white on dark,
    # near-black on light (white would be 1:1 on the light surfaces). The legacy
    # names icon_group / icon_file / icon_host / icon_host_selected alias it.
    icon_default: str
    # Status badge on the host icon (warning/danger for connecting/failed).
    badge_connected: str
    badge_unknown: str
    scrollbar: str
    shadow: str
    shadow_popover: str
    # Terminal panes shown together (split pane, tile mode): a visible border on
    # every side, a darker gutter between panes, a header per pane.
    pane_border: str  # ≥ 3:1 against pane_gutter, ≥ 1.8:1 against the terminal
    pane_border_focus: str
    pane_border_broadcast: str
    pane_header_bg: str
    pane_header_bg_focus: str
    pane_gutter: str
    pane_dim: str  # veil over unfocused panes (overlay, never widget opacity)

    def items(self) -> Iterator[Tuple[str, str]]:
        """Yield ``(role, value)`` for every color role."""

        for field in fields(self):
            if field.name == "name":
                continue
            yield field.name, getattr(self, field.name)

    # Legacy icon roles: one token drives every group / host icon (Phase 9b).
    @property
    def icon_group(self) -> str:
        return self.icon_default

    @property
    def icon_file(self) -> str:
        return self.icon_default

    @property
    def icon_host(self) -> str:
        return self.icon_default

    @property
    def icon_host_selected(self) -> str:
        return self.icon_default

    def surface_colors(self) -> Tuple[str, ...]:
        """Colors a widget background may legitimately use (see :data:`SURFACE_ROLES`)."""

        dark = self.name == "dark"
        badges = tuple(background for background, _fg in ENV_BADGES["dark" if dark else "light"].values())
        return tuple(getattr(self, role) for role in SURFACE_ROLES) + badges

    def status_color(self, status: object) -> str:
        """Return the color used for a connection status."""

        state = ConnectionStatus.coerce(status)
        return {
            ConnectionStatus.CONNECTED: self.success,
            ConnectionStatus.CONNECTING: self.warning,
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


LIGHT = Palette(
    name="light",
    bg_window="#FFFFFF",
    bg_surface="#FFFFFF",
    bg_surface_raised="#FFFFFF",
    bg_canvas="#F5F7F6",
    bg_input="#FAFBFA",
    bg_page_bar="#FCFDFC",
    bg_hover="#F3F6F4",
    bg_selected="#EAF5EF",
    border_subtle="#DFE5E1",
    border_input="#D9E0DC",
    border_strong="#C9D2CD",
    text_primary="#18231F",
    text_secondary="#5B6B64",
    text_muted="#8C9791",
    text_faint="#9BA59F",
    text_disabled="#9BA59F",
    tab_inactive="#7C8882",
    accent="#176B4D",
    accent_hover="#12593F",
    accent_pressed="#0E4A34",
    accent_text="#176B4D",
    accent_tint="#EAF5EF",
    accent_soft_border="#CFE6DB",
    on_accent="#FFFFFF",
    success="#2DA771",
    warning="#B45309",
    danger="#B42318",
    danger_strong="#B42318",
    on_danger="#FFFFFF",
    info="#2563A8",
    focus_ring="#176B4D",
    chip_bg="#F1F4F2",
    avatar_bg="#233B32",
    broadcast="#5B47B8",
    broadcast_bg="#F0EDFB",
    broadcast_fg="#3B2D86",
    icon_default="#18231F",  # = text_primary; a white icon on light would need a dark tile (not done)
    # success / text_muted reach only 2.7:1 on the selected row; nearest shades that reach 3:1.
    badge_connected="#2A9D6A",
    badge_unknown="#848E88",
    scrollbar="#C9D2CD",
    shadow="rgba(24, 35, 31, 0.06)",
    shadow_popover="rgba(24, 35, 31, 0.12)",
    pane_border="#868F8A",
    pane_border_focus="#176B4D",
    pane_border_broadcast="#6A55C9",
    pane_header_bg="#FFFFFF",
    pane_header_bg_focus="#EAF5EF",
    pane_gutter="#F5F7F6",
    pane_dim="rgba(0, 0, 0, 0.12)",
)

DARK = Palette(
    name="dark",
    bg_window="#151619",
    bg_surface="#1A1B1E",
    bg_surface_raised="#212226",
    bg_canvas="#121315",
    bg_input="#16171A",
    bg_page_bar="#1A1B1E",
    bg_hover="#25272B",
    bg_selected="#202A25",
    border_subtle="#2A2C31",
    border_input="#35383E",
    border_strong="#454951",
    text_primary="#E6E8EB",
    text_secondary="#A3A8B0",
    text_muted="#8A9098",
    text_faint="#6B7079",
    text_disabled="#6B7079",
    tab_inactive="#8F959E",
    accent="#1F7A57",
    accent_hover="#1A6E4E",
    accent_pressed="#196A4B",
    accent_text="#5CCB9C",
    accent_tint="#202A25",
    accent_soft_border="#2F4A3E",
    on_accent="#FFFFFF",
    success="#48C48D",
    warning="#E3A548",
    danger="#F07A6E",
    danger_strong="#B42318",
    on_danger="#FFFFFF",
    info="#7DB1E8",
    focus_ring="#5CCB9C",
    chip_bg="#25272B",
    avatar_bg="#3F4A45",
    broadcast="#B3A6F5",
    broadcast_bg="#2A2550",
    broadcast_fg="#E4DFFC",
    icon_default="#FFFFFF",
    badge_connected="#48C48D",
    badge_unknown="#8A9098",
    scrollbar="#454951",
    shadow="rgba(0, 0, 0, 0.35)",
    shadow_popover="rgba(0, 0, 0, 0.45)",
    pane_border="#5E6269",
    pane_border_focus="#1F7A57",
    pane_border_broadcast="#9C8CF0",
    pane_header_bg="#212226",
    pane_header_bg_focus="#202A25",
    pane_gutter="#121315",
    pane_dim="rgba(0, 0, 0, 0.12)",
)


ENV_BADGES: Dict[str, Dict[str, Tuple[str, str]]] = {
    # theme -> environment -> (background, foreground)
    "light": {
        "production": ("#FFF1E5", "#8A4B1F"),
        "staging": ("#E8F1FB", "#1D4F86"),
        "development": ("#EEF1EF", "#46534D"),
        "neutral": ("#F1F3F2", "#4F5C56"),
    },
    "dark": {  # backgrounds derived from the neutral-gray surface
        "production": ("#2E2620", "#F0B27A"),
        "staging": ("#1F2733", "#8DB7E8"),
        "development": ("#25272B", "#A3A8B0"),
        "neutral": ("#25272B", "#A3A8B0"),
    },
}

_ENV_ALIASES = {
    "prod": "production", "production": "production", "prd": "production", "live": "production",
    "stage": "staging", "staging": "staging", "stg": "staging", "uat": "staging", "preprod": "staging",
    "dev": "development", "development": "development", "test": "development", "qa": "development",
    "local": "development",
}


def environment_kind(environment: str) -> str:
    """``production`` / ``staging`` / ``development`` / ``neutral`` for an environment name."""

    return _ENV_ALIASES.get((environment or "").strip().lower(), "neutral")


def environment_badge_style(environment: str, dark: bool = False) -> Tuple[str, str]:
    """``(background, foreground)`` of the environment badge for ``environment``."""

    return ENV_BADGES["dark" if dark else "light"][environment_kind(environment)]


# Roles that may fill a widget background. Text, icon and on-* roles are
# foreground-only; a background matching one of them (e.g. #FFFFFF) is a leak
# from the GTK theme, not a design choice.
SURFACE_ROLES: Tuple[str, ...] = (
    "bg_window", "bg_surface", "bg_surface_raised", "bg_canvas", "bg_input", "bg_page_bar",
    "bg_hover", "bg_selected", "border_subtle", "border_input", "border_strong",
    "accent", "accent_hover", "accent_pressed", "accent_tint", "accent_soft_border",
    "success", "warning", "danger", "danger_strong", "chip_bg", "avatar_bg", "broadcast_bg",
    "scrollbar", "pane_border", "pane_border_focus", "pane_border_broadcast", "pane_header_bg",
    "pane_header_bg_focus", "pane_gutter",
)


PALETTES: Dict[str, Palette] = {"dark": DARK, "light": LIGHT}

# The user's accent colour (gtkui/theme/accent.py derives the accent roles from one of these, or a custom
# colour): (id, English name, base colour). "default" is the palettes' own green, unchanged.
ACCENT_PRESETS: List[Tuple[str, str, Optional[str]]] = [
    ("default", "Green", None),
    ("teal", "Teal", "#0F9D9A"),
    ("blue", "Blue", "#2F6FEB"),
    ("indigo", "Indigo", "#5B5BD6"),
    ("purple", "Purple", "#8E4EC6"),
    ("pink", "Pink", "#D6409F"),
    ("red", "Red", "#D64545"),
    ("orange", "Orange", "#E8590C"),
    ("amber", "Amber", "#D4A017"),
    ("graphite", "Graphite", "#6B7280"),
]
ACCENT_DEFAULT_SWATCH = LIGHT.accent  # what "default" looks like in the picker
ON_ACCENT_LIGHT, ON_ACCENT_DARK = "#FFFFFF", "#0B1210"  # text on an accent button: white, else near-black


def palette_for(dark: bool, accent: object = None) -> Palette:
    """The light or dark palette, with the user's accent colour (``None`` / ``"default"``: the palette itself)."""

    base = DARK if dark else LIGHT
    if accent is None:
        return base
    from .accent import accent_palette

    return accent_palette(base, accent)


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
# text_faint is decorative only (tree counts, captions next to stronger text) and
# is intentionally not checked; caps labels use text_muted at the UI-component
# threshold because they label adjacent, higher-contrast values.
CONTRAST_PAIRS: List[Tuple[str, str, float]] = [
    ("text_primary", "bg_window", AA_TEXT),
    ("text_primary", "bg_surface", AA_TEXT),
    ("text_primary", "bg_canvas", AA_TEXT),
    ("text_primary", "bg_input", AA_TEXT),
    ("text_primary", "bg_hover", AA_TEXT),
    ("text_primary", "bg_selected", AA_TEXT),
    ("text_primary", "chip_bg", AA_TEXT),
    ("text_secondary", "bg_window", AA_TEXT),
    ("text_secondary", "bg_canvas", AA_TEXT),
    ("text_secondary", "bg_page_bar", AA_TEXT),
    ("accent_text", "bg_selected", AA_TEXT),
    ("accent_text", "bg_surface", AA_TEXT),
    ("on_accent", "accent", AA_TEXT),
    ("on_accent", "accent_hover", AA_TEXT),
    ("on_danger", "danger_strong", AA_TEXT),
    ("broadcast_fg", "broadcast_bg", AA_TEXT),
    ("broadcast", "broadcast_bg", AA_UI),
    ("broadcast", "bg_surface", AA_UI),
    ("broadcast", "bg_canvas", AA_UI),
    ("broadcast", "bg_window", AA_UI),
    ("pane_border_broadcast", "bg_surface", AA_UI),
    ("pane_border_broadcast", "bg_canvas", AA_UI),
    ("on_accent", "avatar_bg", AA_TEXT),
    ("tab_inactive", "bg_surface", AA_UI),
    ("text_muted", "bg_surface", AA_UI),
    ("accent", "bg_surface", AA_UI),
    ("focus_ring", "bg_surface", AA_UI),
    ("success", "bg_surface", AA_UI),
    ("warning", "bg_surface", AA_UI),
    ("danger", "bg_surface", AA_UI),
    ("info", "bg_surface", AA_UI),
] + [
    # Tree icons and the host status badge, on every row background they sit on.
    (fg, bg, AA_UI)
    for fg in ("icon_default", "badge_connected", "warning", "danger", "badge_unknown")
    for bg in ("bg_surface", "bg_hover", "bg_selected")
]


def contrast_report(palette: Palette) -> List[Tuple[str, str, float, float]]:
    """Return ``(fg_role, bg_role, ratio, minimum)`` for every checked pair."""

    report = []
    for fg_role, bg_role, minimum in CONTRAST_PAIRS:
        ratio = contrast_ratio(getattr(palette, fg_role), getattr(palette, bg_role))
        report.append((fg_role, bg_role, ratio, minimum))
    return report
