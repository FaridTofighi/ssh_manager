"""Terminal color schemes (data) and resolution of the effective terminal colors.

These colors belong to the terminal emulator, not to the application chrome,
so they live apart from the UI palettes in ``tokens.py``. Both the session
terminals and the Preferences preview resolve colors through
:func:`resolve_terminal_colors` so they always agree.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

DEFAULT_PALETTE: List[str] = [
    "#1f2329", "#ef9a9a", "#a5d6a7", "#ffe0b2", "#90caf9", "#c7a4ff", "#80cbc4", "#f5f6f8",
    "#394049", "#f28b82", "#81c995", "#fdd663", "#8ab4f8", "#d7aefb", "#78d9ec", "#ffffff",
]

SOLARIZED_PALETTE: List[str] = [
    "#073642", "#dc322f", "#859900", "#b58900", "#268bd2", "#d33682", "#2aa198", "#eee8d5",
    "#002b36", "#cb4b16", "#586e75", "#657b83", "#839496", "#6c71c4", "#93a1a1", "#fdf6e3",
]

# Built-in dark scheme: neutral gray, darker than the UI surface so the terminal
# sinks into the window (was the bluish #1f2329 / #f5f6f8).
DARK_COLORS: Tuple[str, str] = ("#16171a", "#d6d9de")  # background, foreground
DARK_CURSOR = "#b8bcc2"
DARK_HIGHLIGHT = "#28443a"  # rgba(92, 203, 156, 0.25) — accent_text — over the background
LIGHT_COLORS: Tuple[str, str] = ("#f8fafc", "#0f172a")
SOLARIZED_DARK_COLORS: Tuple[str, str] = ("#002b36", "#93a1a1")
SOLARIZED_LIGHT_COLORS: Tuple[str, str] = ("#fdf6e3", "#586e75")
CURSOR_COLOR = "#b9c4cf"  # every other built-in scheme and user colors
HIGHLIGHT_COLOR = "#2f333a"

# (key, label) pairs shown in Preferences.
SCHEMES: List[Tuple[str, str]] = [
    ("default", "Default"),
    ("dark", "Dark"),
    ("light", "Light"),
    ("solarized-dark", "Solarized Dark"),
    ("solarized-light", "Solarized Light"),
]


@dataclass(frozen=True)
class TerminalColors:
    """Resolved terminal colors as hex strings."""

    foreground: str
    background: str
    cursor: str
    highlight: str
    palette: List[str]


def resolve_terminal_colors(appearance: Optional[object], profile_theme: str = "") -> TerminalColors:
    """Return the colors a terminal should use for ``appearance``.

    ``appearance`` is a :class:`ophion.settings.TerminalAppearanceSettings`
    (or ``None``); ``profile_theme`` is the session profile theme
    (``"dark"``/``"light"``) that applies when the scheme is ``default``.
    """

    background, foreground = DARK_COLORS
    palette = list(DEFAULT_PALETTE)
    profile = (profile_theme or "").lower()

    if appearance is not None:
        scheme = str(getattr(appearance, "color_scheme", "default") or "default").lower()
        fg = getattr(appearance, "fg_color", None)
        bg = getattr(appearance, "bg_color", None)
        if getattr(appearance, "use_custom_colors", False) and fg and bg:
            foreground, background = fg, bg
        elif scheme == "dark":
            background, foreground = DARK_COLORS
        elif scheme == "light":
            background, foreground = LIGHT_COLORS
        elif scheme in {"solarized-dark", "solarized dark"}:
            background, foreground = SOLARIZED_DARK_COLORS
            palette = list(SOLARIZED_PALETTE)
        elif scheme in {"solarized-light", "solarized light"}:
            background, foreground = SOLARIZED_LIGHT_COLORS
            palette = list(SOLARIZED_PALETTE)
        elif scheme == "default" and profile in {"dark", "light"}:
            background, foreground = LIGHT_COLORS if profile == "light" else DARK_COLORS
        custom_palette = getattr(appearance, "palette", None)
        if custom_palette:
            palette = list(custom_palette)

    dark_default = (background, foreground) == DARK_COLORS
    return TerminalColors(
        foreground=foreground,
        background=background,
        cursor=DARK_CURSOR if dark_default else CURSOR_COLOR,
        highlight=DARK_HIGHLIGHT if dark_default else HIGHLIGHT_COLOR,
        palette=palette,
    )
