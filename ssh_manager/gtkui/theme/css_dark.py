"""Dark theme stylesheet, generated from :data:`tokens.DARK`."""

from __future__ import annotations

from .stylesheet import build_css
from .tokens import DARK

PALETTE = DARK


def css(*, animations: bool = True) -> str:
    """Return the dark stylesheet."""

    return build_css(DARK, animations=animations)
