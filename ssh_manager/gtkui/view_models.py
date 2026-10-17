"""GTK-free data passed from controllers to views."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

from .i18n import _
from .theme.tokens import ConnectionStatus


@dataclass
class DetailAction:
    """A button in the details action row."""

    label: str
    callback: Callable[[], None]
    icon: str = ""
    primary: bool = False
    destructive: bool = False
    overflow: bool = False
    sensitive: bool = True
    tooltip: str = ""


@dataclass
class DetailContent:
    """What the details drawer should display."""

    title: str
    subtitle: str = ""
    status: Optional[ConnectionStatus] = None
    chips: List[str] = field(default_factory=list)
    accent_chips: List[str] = field(default_factory=list)
    facts: List[Tuple[str, str]] = field(default_factory=list)
    actions: List[DetailAction] = field(default_factory=list)


def placeholder_content() -> DetailContent:
    return DetailContent(
        title=_("Nothing selected"),
        subtitle=_("Select a host to see its details. Double-click or press Enter to connect."),
    )
