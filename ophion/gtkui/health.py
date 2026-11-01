"""Overall health shown in the top bar ("All systems operational")."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .i18n import _
from .theme.tokens import ConnectionStatus, Level


@dataclass(frozen=True)
class Health:
    level: Level
    text: str
    short: str  # compact variant (dot tooltip / narrow windows)


def _plural(count: int, one: str, many: str) -> str:
    return (one if count == 1 else many).format(n=count)


def compute_health(sessions: Iterable[object], *, warnings: int = 0, errors: int = 0) -> Health:
    """Summarise session states and unread problem messages.

    ``sessions`` holds :class:`ConnectionStatus` values (or their strings);
    ``warnings``/``errors`` count unread warning / error messages (auth
    failures, Git or credential-store problems...).
    """

    states = [ConnectionStatus.coerce(s) for s in sessions]
    failed = states.count(ConnectionStatus.FAILED)
    active = states.count(ConnectionStatus.CONNECTED)
    if failed:
        text = _plural(failed, _("{n} session failed"), _("{n} sessions failed"))
        return Health(Level.ERROR, text, text)
    if errors:
        text = _plural(errors, _("{n} error"), _("{n} errors"))
        return Health(Level.ERROR, text, text)
    if warnings:
        text = _plural(warnings, _("{n} warning"), _("{n} warnings"))
        return Health(Level.WARNING, text, text)
    if active:
        text = _plural(active, _("{n} session active"), _("{n} sessions active"))
        return Health(Level.SUCCESS, text, text)
    return Health(Level.SUCCESS, _("All systems operational"), _("Operational"))
