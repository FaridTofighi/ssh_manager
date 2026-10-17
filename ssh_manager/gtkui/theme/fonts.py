"""Resolve the UI and monospace font families from the design tokens.

The Relay design names Inter / JetBrains Mono first; whichever family in
``tokens.UI_FONT_FAMILIES`` / ``MONO_FONT_FAMILIES`` is installed wins, and
the platform default is the final fallback.
"""

from __future__ import annotations

from functools import lru_cache
from typing import FrozenSet, Iterable, Optional

import gi

gi.require_version("PangoCairo", "1.0")
from gi.repository import PangoCairo  # type: ignore  # noqa: E402

from .tokens import MONO_FONT_FAMILIES, UI_FONT_FAMILIES  # noqa: E402


@lru_cache(maxsize=1)
def installed_families() -> FrozenSet[str]:
    try:
        fontmap = PangoCairo.FontMap.get_default()
        return frozenset(family.get_name().casefold() for family in fontmap.list_families())
    except Exception:  # pragma: no cover - no font backend
        return frozenset()


def first_available(candidates: Iterable[str], installed: Optional[FrozenSet[str]] = None) -> Optional[str]:
    available = installed_families() if installed is None else installed
    for name in candidates:
        if name.casefold() in available:
            return name
    return None


def ui_font_family(fallback: str = "Sans") -> str:
    return first_available(UI_FONT_FAMILIES) or fallback


def mono_font_family(fallback: str = "Monospace") -> str:
    return first_available(MONO_FONT_FAMILIES) or fallback
