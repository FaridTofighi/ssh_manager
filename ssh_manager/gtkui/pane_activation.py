"""Which pane is active: the pure state machine behind pane activation (no GTK).

A split tab (its terminals) and the tile grid (its sessions) each keep one
:class:`ActivePane`. There is exactly one active pane whenever there is at
least one pane, and every way of changing it (a click, Alt+arrows, Ctrl+Tab, a
new split, a closed pane) goes through :meth:`ActivePane.activate`, so the
callers can tell a real change from a no-op and update the UI only once.
"""

from __future__ import annotations

from typing import Generic, Hashable, Iterable, List, Optional, TypeVar

P = TypeVar("P", bound=Hashable)


class ActivePane(Generic[P]):
    """Ordered panes and the one that is active."""

    def __init__(self, panes: Iterable[P] = ()) -> None:
        self._panes: List[P] = []
        self._active: Optional[P] = None
        self.replace(panes)

    @property
    def panes(self) -> List[P]:
        return list(self._panes)

    @property
    def active(self) -> Optional[P]:
        return self._active

    def is_active(self, pane: P) -> bool:
        return pane is self._active

    def activate(self, pane: P) -> bool:
        """Make ``pane`` active. Returns whether anything changed (unknown panes: no)."""

        if pane not in self._panes or pane is self._active:
            return False
        self._active = pane
        return True

    def add(self, pane: P, *, activate: bool = True, index: Optional[int] = None) -> bool:
        """Add a pane (a new split is active by default). Returns whether the active pane changed."""

        if pane not in self._panes:
            if index is None:
                self._panes.append(pane)
            else:
                self._panes.insert(max(0, min(index, len(self._panes))), pane)
        if activate or self._active is None:
            return self.activate(pane)
        return False

    def remove(self, pane: P) -> bool:
        """Remove a pane; when it was active, its neighbor (the next one, else the
        previous one) becomes active. Returns whether the active pane changed."""

        if pane not in self._panes:
            return False
        index = self._panes.index(pane)
        self._panes.remove(pane)
        if pane is not self._active:
            return False
        if not self._panes:
            self._active = None
        else:
            self._active = self._panes[min(index, len(self._panes) - 1)]
        return True

    def replace(self, panes: Iterable[P]) -> bool:
        """New pane list (re-layout, tile ⇄ tabs): the active pane stays active when
        it is still there, else the first pane is. Returns whether it changed."""

        self._panes = []
        for pane in panes:
            if pane not in self._panes:
                self._panes.append(pane)
        previous = self._active
        if previous not in self._panes:
            self._active = self._panes[0] if self._panes else None
        return self._active is not previous
