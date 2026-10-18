"""Light theme stylesheet, generated from :data:`tokens.LIGHT`."""

from __future__ import annotations

from .stylesheet import build_css
from .tokens import LIGHT

PALETTE = LIGHT


def css(*, animations: bool = True, tokens=None, font_pt: float = 11) -> str:
    """Return the light stylesheet."""

    return build_css(LIGHT, animations=animations, tokens=tokens, font_pt=font_pt)
