"""The user's accent colour: presets, validation and the derived palette roles (pure Python, no GTK).

The accent colours primary buttons and highlights (selected rows, the focused pane, focus rings, links,
checked toggles). :func:`derive_accent` turns one base colour into every accent-dependent role of a theme
variant and guarantees the contrast table (:data:`tokens.CONTRAST_PAIRS`):

* the hue and saturation of the base colour are kept; only the lightness is tuned per theme (the button
  background as close to the chosen colour as the contrast allows), with white or near-black text on it;
* hover and pressed are one and two steps away from that text colour;
* the selected-row tint, the focused pane header and the soft border mix the accent into the theme's own
  surface (low saturation), so text and icons keep their contrast on them;
* a colour that cannot pass by lightness alone (near-white, near-black, pure yellow …) loses saturation
  until it does, and ``adjusted`` says so.

Semantic colours never follow the accent: success / connected, warning, danger, info, broadcast, the
neutral text and surfaces, and the terminal colours. ``"default"`` is today's green: the base palette is
returned unchanged.
"""

from __future__ import annotations

import colorsys
import dataclasses
import re
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from .tokens import (
    AA_TEXT, AA_UI, ACCENT_DEFAULT_SWATCH, ACCENT_PRESETS, CONTRAST_PAIRS, ON_ACCENT_DARK, ON_ACCENT_LIGHT, Palette,
    contrast_ratio, parse_hex,
)

DEFAULT = "default"
PRESETS = ACCENT_PRESETS  # (id, English name, base colour); names are translated where they are shown
PRESET_COLORS: Dict[str, Optional[str]] = {key: color for key, _name, color in PRESETS}
DEFAULT_GREEN = ACCENT_DEFAULT_SWATCH  # what "default" looks like (swatch, hue checks)

# Every role this module may set; everything else comes from the theme unchanged.
ACCENT_ROLES: Tuple[str, ...] = (
    "accent", "accent_hover", "accent_pressed", "accent_text", "accent_tint", "accent_soft_border", "on_accent",
    "bg_selected", "focus_ring", "pane_border_focus", "pane_header_bg_focus", "avatar_bg",
)
WHITE, NEAR_BLACK = ON_ACCENT_LIGHT, ON_ACCENT_DARK
BROADCAST_HUE, BROADCAST_NEAR = 255.0, 25.0
_HEX = re.compile(r"^#?([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
_STEP = 0.005  # lightness search step
_MARGIN = 0.15  # contrast headroom: rounding to 8-bit channels never drops a pair below its minimum


@dataclass(frozen=True)
class AccentRoles:
    roles: Dict[str, str]
    adjusted: bool = False  # the colour lost saturation (or moved far in lightness) to stay readable


# ------------------------------------------------------------------ values
def normalize_color(text: str) -> Optional[str]:
    """``#RGB`` / ``#RRGGBB`` (any case, ``#`` optional) → ``#RRGGBB``; ``None`` when invalid."""

    match = _HEX.match((text or "").strip())
    if not match:
        return None
    digits = match.group(1)
    if len(digits) == 3:
        digits = "".join(ch * 2 for ch in digits)
    return "#" + digits.upper()


def normalize_accent(value: object) -> str:
    """A stored accent value: a preset id or ``#RRGGBB``; anything else is :data:`DEFAULT`."""

    if isinstance(value, str):
        key = value.strip().lower()
        if key in PRESET_COLORS:
            return key
        color = normalize_color(value)
        if color is not None:
            return color
    return DEFAULT


def base_color(value: object) -> Optional[str]:
    """The base colour of an accent value; ``None`` for the default (the palette's own green)."""

    accent = normalize_accent(value)
    return PRESET_COLORS[accent] if accent in PRESET_COLORS else accent


def display_color(value: object) -> str:
    """The colour a swatch shows for ``value``."""

    return base_color(value) or DEFAULT_GREEN


def hue_of(color: str) -> float:
    r, g, b = (channel / 255.0 for channel in parse_hex(color))
    return colorsys.rgb_to_hls(r, g, b)[0] * 360.0


def near_broadcast(value: object) -> bool:
    """The accent's hue is within ±25° of the broadcast violet (a hint, never a block)."""

    color = base_color(value)
    if color is None:
        return False
    r, g, b = (channel / 255.0 for channel in parse_hex(color))
    _h, lightness, saturation = colorsys.rgb_to_hls(r, g, b)
    if saturation < 0.15 or not 0.08 < lightness < 0.92:
        return False  # a grey has no hue to confuse
    distance = abs(hue_of(color) - BROADCAST_HUE) % 360.0
    return min(distance, 360.0 - distance) <= BROADCAST_NEAR


# ------------------------------------------------------------- derivation
def _hls(h: float, lightness: float, s: float) -> str:
    r, g, b = colorsys.hls_to_rgb(h, max(0.0, min(1.0, lightness)), max(0.0, min(1.0, s)))
    return "#{:02X}{:02X}{:02X}".format(*(int(round(c * 255)) for c in (r, g, b)))


def _mix(a: str, b: str, amount: float) -> str:
    """``a`` with ``amount`` (0 … 1) of ``b`` mixed in."""

    ra, ga, ba = parse_hex(a)
    rb, gb, bb = parse_hex(b)
    return "#{:02X}{:02X}{:02X}".format(*(int(round(x + (y - x) * amount)) for x, y in ((ra, rb), (ga, gb), (ba, bb))))


def _passes(fg: str, bg: str, minimum: float) -> bool:
    return contrast_ratio(fg, bg) >= minimum + _MARGIN


def _candidates(start: float):
    """Lightness values nearest to ``start`` first."""

    yield start
    step = _STEP
    while step <= 1.0:
        for value in (start - step, start + step):
            if 0.0 <= value <= 1.0:
                yield value
        step += _STEP


def _button(h: float, s: float, start: float, base: Palette, dark: bool) -> Optional[Tuple[str, str, str, str, float]]:
    """``(accent, hover, pressed, on_accent, lightness)`` nearest to ``start``, or ``None``."""

    surface = base.bg_surface
    for lightness in _candidates(start):
        accent = _hls(h, lightness, s)
        if not _passes(accent, surface, AA_UI):
            continue
        for text in sorted((WHITE, NEAR_BLACK), key=lambda t: -contrast_ratio(t, accent)):
            if not _passes(text, accent, AA_TEXT):
                continue
            away = -1.0 if text == WHITE else 1.0  # hover / pressed move away from the text colour
            step = 0.04 if dark else 0.06
            hover, pressed = _hls(h, lightness + away * step, s), _hls(h, lightness + away * 2 * step, s)
            if _passes(text, hover, AA_TEXT):
                return accent, hover, pressed, text, lightness
    return None


def _shade(h: float, s: float, start: float, direction: float, ok) -> Optional[str]:
    """The first colour from ``start`` towards ``direction`` that satisfies ``ok``."""

    lightness = start
    while 0.0 <= lightness <= 1.0:
        color = _hls(h, lightness, s)
        if ok(color):
            return color
        lightness += direction * _STEP
    return None


def _derive(h: float, s: float, start: float, base: Palette, dark: bool) -> Optional[Dict[str, str]]:
    button = _button(h, s, start, base, dark)
    if button is None:
        return None
    accent, hover, pressed, on_accent, lightness = button
    surface = base.bg_surface
    # The selected row / focused header: the accent mixed into the surface, as long as text and icons keep
    # their contrast on it (the same rows as CONTRAST_PAIRS).
    on_selected = [(getattr(base, fg), minimum) for fg, bg, minimum in CONTRAST_PAIRS
                   if bg == "bg_selected" and fg not in ACCENT_ROLES]
    tint = surface
    for amount in (0.10 if dark else 0.09, 0.07, 0.05, 0.03, 0.0):
        candidate = _mix(surface, accent, amount)
        if all(_passes(fg, candidate, minimum) for fg, minimum in on_selected):
            tint = candidate
            break
    soft_border = _mix(surface, accent, 0.25 if dark else 0.22)
    # Links and selected text: lighter than the button in dark, the button's own shade (or darker) in light.
    text = _shade(h, s, max(lightness, 0.55) if dark else lightness, 1.0 if dark else -1.0,
                  lambda c: _passes(c, surface, AA_TEXT) and _passes(c, tint, AA_TEXT))
    if text is None:
        return None
    focus = text if dark else accent
    if not _passes(focus, surface, AA_UI):
        return None
    # The avatar shows its initials in on_accent: a deep (or, with dark text, a pale) quiet tone of the hue.
    quiet = min(s, 0.30)
    avatar = _shade(h, quiet, 0.24 if on_accent == WHITE else 0.80, -1.0 if on_accent == WHITE else 1.0,
                    lambda c: _passes(on_accent, c, AA_TEXT + 1.5))
    if avatar is None:
        return None
    return {
        "accent": accent, "accent_hover": hover, "accent_pressed": pressed, "accent_text": text,
        "accent_tint": tint, "accent_soft_border": soft_border, "on_accent": on_accent, "bg_selected": tint,
        "focus_ring": focus, "pane_border_focus": accent, "pane_header_bg_focus": tint, "avatar_bg": avatar,
    }


def derive_accent(base_hex: str, dark: bool, base: Optional[Palette] = None) -> AccentRoles:
    """Every accent-dependent role of the light or dark theme for ``base_hex`` (see the module docstring)."""

    from .tokens import palette_for

    color = normalize_color(base_hex)
    if color is None:
        raise ValueError(f"Not a colour: {base_hex!r}")
    base = base or palette_for(dark)
    r, g, b = (channel / 255.0 for channel in parse_hex(color))
    h, lightness, s = colorsys.rgb_to_hls(r, g, b)
    saturation = s
    while True:
        roles = _derive(h, saturation, lightness, base, dark)
        if roles is not None:
            moved = abs(colorsys.rgb_to_hls(*(c / 255.0 for c in parse_hex(roles["accent"])))[1] - lightness)
            return AccentRoles(roles, adjusted=saturation < s - 1e-9 or moved > 0.25)
        if saturation <= 0.0:
            break
        saturation = max(0.0, saturation - 0.1)
    raise AssertionError("no readable accent (a grey always is)")  # pragma: no cover


def readable_color(color: str, backgrounds, minimum: float = AA_UI) -> Tuple[str, bool]:
    """``color`` at the nearest lightness reaching ``minimum`` against every background; ``(drawn, adjusted)``.

    Hue and saturation are kept (saturation drops only if no lightness works). Used for the tree's default
    icon colours: the stored value never changes, only the colour drawn on this theme's surfaces.
    """

    normalized = normalize_color(color)
    if normalized is None:
        raise ValueError(f"Not a colour: {color!r}")
    if all(contrast_ratio(normalized, bg) >= minimum for bg in backgrounds):
        return normalized, False
    h, lightness, s = colorsys.rgb_to_hls(*(c / 255.0 for c in parse_hex(normalized)))
    saturation = s
    while True:
        for candidate in _candidates(lightness):
            drawn = _hls(h, candidate, saturation)
            if all(contrast_ratio(drawn, bg) >= minimum + 0.05 for bg in backgrounds):
                return drawn, True
        if saturation <= 0.0:
            return normalized, False  # pragma: no cover - a grey always reaches 3:1 on one side
        saturation = max(0.0, saturation - 0.1)


def accent_palette(base: Palette, value: object) -> Palette:
    """``base`` with the accent roles for ``value``; the default returns ``base`` itself."""

    color = base_color(value)
    if color is None:
        return base
    return dataclasses.replace(base, **derive_accent(color, base.name == "dark", base).roles)


__all__ = ["DEFAULT", "PRESETS", "PRESET_COLORS", "ACCENT_ROLES", "AccentRoles", "normalize_color", "readable_color",
           "normalize_accent", "base_color", "display_color", "near_broadcast", "hue_of", "derive_accent",
           "accent_palette"]
