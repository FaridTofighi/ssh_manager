"""Dark theme stylesheet, generated from :data:`tokens.DARK`."""

from __future__ import annotations

from .stylesheet import build_css
from .tokens import DARK

PALETTE = DARK


def css(*, animations: bool = True, tokens=None, font_pt: float = 11, palette=None) -> str:
    """Return the dark stylesheet (``palette``: DARK with the user's accent, see theme/accent.py)."""

    return build_css(palette or DARK, animations=animations, tokens=tokens, font_pt=font_pt)
