"""Dark theme stylesheet, generated from :data:`tokens.DARK`."""

from __future__ import annotations

from .stylesheet import build_css
from .tokens import DARK

PALETTE = DARK


def css(*, animations: bool = True, tokens=None, font_pt: float = 11) -> str:
    """Return the dark stylesheet."""

    return build_css(DARK, animations=animations, tokens=tokens, font_pt=font_pt)
