"""Compatibility path: the translation hook lives in :mod:`ophion.i18n` (the same objects)."""

from __future__ import annotations

from ..i18n import DOMAIN, _, _translation, gettext, ngettext  # noqa: F401
