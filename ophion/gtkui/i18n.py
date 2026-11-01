"""Translation hook for user-visible strings.

All new UI strings are wrapped in ``_()`` so the GTK front-end is ready for
gettext catalogs; no translations ship yet, so ``_`` currently returns the
original string.
"""

from __future__ import annotations

import gettext

DOMAIN = "ssh-manager"

_translation = gettext.translation(DOMAIN, fallback=True)


def _(message: str) -> str:
    """Translate ``message`` (identity until catalogs are installed)."""

    return _translation.gettext(message)


def ngettext(singular: str, plural: str, count: int) -> str:
    return _translation.ngettext(singular, plural, count)
