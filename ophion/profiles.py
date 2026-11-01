"""Session profiles for terminal appearance/behavior."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List


@dataclass(frozen=True)
class SessionProfile:
    id: str
    name: str
    font_family: str | None = None
    font_size: float | None = None
    theme: str | None = None
    scrollback_lines: int | None = None
    bell_enabled: bool | None = None
    env_vars: Dict[str, str] | None = None


def built_in_profiles() -> List[SessionProfile]:
    return [
        SessionProfile(
            id="default",
            name="Default",
            scrollback_lines=None,
            bell_enabled=None,
            env_vars=None,
        ),
        SessionProfile(
            id="high_scrollback",
            name="High Scrollback",
            scrollback_lines=100_000,
            bell_enabled=None,
            env_vars=None,
        ),
        SessionProfile(
            id="dark_ops",
            name="Dark Ops",
            theme="dark",
            bell_enabled=False,
            scrollback_lines=50_000,
            env_vars=None,
        ),
    ]
