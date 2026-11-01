"""Workspace (inventory) list for the top-bar switcher."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from ..identity import APP_NAME
from typing import Iterable, List, Optional, Sequence

RECENT_LIMIT = 6


class WorkspaceKind(str, Enum):
    LOCAL = "local"
    SHARED = "shared"


@dataclass(frozen=True)
class Workspace:
    name: str
    path: Path
    kind: WorkspaceKind = WorkspaceKind.LOCAL
    current: bool = False

    @property
    def read_only(self) -> bool:
        return self.kind is WorkspaceKind.SHARED


def workspace_name(path: Path) -> str:
    """Human name for an inventory: its folder (or file stem) name."""

    path = Path(path)
    if path.suffix == ".db":
        return APP_NAME  # the app-owned inventory
    name = path.stem if path.suffix else path.name
    return name or str(path)


def _key(path: Path) -> str:
    try:
        return str(Path(path).expanduser().resolve())
    except OSError:
        return str(path)


def remember_workspace(recent: Sequence[str], path: Path, *, limit: int = RECENT_LIMIT) -> List[str]:
    """Return ``recent`` with ``path`` moved to the front (deduplicated, bounded)."""

    key = _key(path)
    items = [key] + [p for p in recent if p and _key(Path(p)) != key]
    return items[:limit]


def workspace_list(current: Path, recent: Iterable[str] = (), shared: Optional[Path] = None) -> List[Workspace]:
    """The current inventory first, then recently used ones, then a shared one."""

    current_key = _key(current)
    result = [Workspace(workspace_name(current), Path(current), current=True)]
    seen = {current_key}
    for item in recent:
        if not item:
            continue
        key = _key(Path(item))
        if key in seen:
            continue
        seen.add(key)
        result.append(Workspace(workspace_name(Path(item)), Path(item)))
    if shared is not None:
        result.append(Workspace(workspace_name(shared), Path(shared), WorkspaceKind.SHARED, current=True))
    return result
