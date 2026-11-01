"""Declarative menu descriptions (GTK-free).

A menu is a list of :class:`MenuSection` objects; sections are rendered with
separators between them and empty or fully hidden sections are skipped. The
same spec drives context menus and overflow menus, so the order and grouping
of actions stays consistent across the UI.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence, Tuple, Union


@dataclass
class MenuItemSpec:
    """One activatable menu entry."""

    label: str
    callback: Optional[Callable[[], None]] = None
    icon: str = ""
    accel: str = ""  # display hint such as "Ctrl+T"
    sensitive: bool = True
    destructive: bool = False
    visible: bool = True
    tooltip: str = ""
    submenu: Optional[List["MenuSection"]] = None


@dataclass
class MenuSection:
    """A group of related items (rendered between separators)."""

    name: str
    items: List[MenuItemSpec] = field(default_factory=list)

    def visible_items(self) -> List[MenuItemSpec]:
        return [item for item in self.items if item.visible]


MenuSpec = List[MenuSection]

# Layout entries: ("item", MenuItemSpec) or ("separator", None)
LayoutEntry = Tuple[str, Optional[MenuItemSpec]]


def menu_layout(spec: Sequence[MenuSection]) -> List[LayoutEntry]:
    """Flatten ``spec`` into items and separators, skipping empty sections."""

    layout: List[LayoutEntry] = []
    for section in spec:
        items = section.visible_items()
        if not items:
            continue
        if layout:
            layout.append(("separator", None))
        layout.extend(("item", item) for item in items)
    return layout


def describe(spec: Sequence[MenuSection]) -> List[Union[str, Tuple[str, bool, bool]]]:
    """Compact description used by tests: ``"---"`` or ``(label, sensitive, destructive)``."""

    out: List[Union[str, Tuple[str, bool, bool]]] = []
    for kind, item in menu_layout(spec):
        if kind == "separator" or item is None:
            out.append("---")
        else:
            out.append((item.label, item.sensitive, item.destructive))
    return out
