"""Persisted window/UI state (GTK-free).

The state lives in ``<config dir>/.ui_state.json`` next to the inventory.
:meth:`UiState.from_dict` tolerates missing keys and wrong types (a corrupt
or hand-edited file must never prevent the window from opening);
:meth:`UiState.to_dict` also writes the legacy ``prefer_dark`` key so older
versions can still read the file.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ...persistence import write_json_atomic
from ..theme.density import COMFORTABLE, DEFAULT_DENSITY, Density

# Stored when the user never resized the sidebar (the Comfortable default; with
# the 1px pane separator the edge is at x = 408). It stands for "the active
# density's default width", see LayoutController.resolve_sidebar_width().
DEFAULT_SIDEBAR_WIDTH = COMFORTABLE.size.SIDEBAR - 1

UI_STATE_FILE = ".ui_state.json"


def ui_state_path_for_base(base_dir: Path) -> Path:
    return (base_dir.parent if base_dir.is_file() else base_dir) / UI_STATE_FILE


def load_ui_state(path: Path) -> Dict[str, object]:
    """Read the raw state dict; ``{}`` when missing, unreadable or not a JSON object."""

    if not path.exists():
        return {}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return state if isinstance(state, dict) else {}


def save_ui_state(path: Path, state: Dict[str, object]) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(path, state, mode=0o600)
    except OSError:
        return False
    return True


def _int(value: object, default: int) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _float(value: object, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value)


def _bool(value: object, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def _str(value: object, default: str) -> str:
    return value if isinstance(value, str) else default


def _str_map(value: object) -> Dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(k): str(v) for k, v in value.items() if isinstance(v, str)}


def _str_list(value: object) -> List[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


@dataclass
class UiState:
    """Typed view of the persisted UI state."""

    window_size: Optional[Tuple[int, int]] = None
    is_maximized: bool = False
    sidebar_width: int = DEFAULT_SIDEBAR_WIDTH
    sidebar_visible: bool = True
    sidebar_prev_visible: bool = True
    terminal_maximized: bool = False
    theme_mode: str = ""  # "", "system", "light" or "dark"; "" = derive from prefer_dark
    prefer_dark: Optional[bool] = None
    font_scale: float = 1.0
    ui_font_size: int = 11
    details_expanded: bool = True
    filter_query: str = ""
    group_colors: Dict[str, str] = field(default_factory=dict)
    host_colors: Dict[str, str] = field(default_factory=dict)
    recent_inventories: List[str] = field(default_factory=list)
    latency_probe: bool = True
    density: str = DEFAULT_DENSITY.value  # "compact" | "comfortable"

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "UiState":
        data = data if isinstance(data, dict) else {}
        size = data.get("window_size")
        window_size = None
        if (
            isinstance(size, list)
            and len(size) == 2
            and all(isinstance(v, int) and not isinstance(v, bool) and v > 0 for v in size)
        ):
            window_size = (int(size[0]), int(size[1]))
        sidebar_visible = _bool(data.get("sidebar_visible"), True)
        prefer_dark = data.get("prefer_dark")
        return cls(
            window_size=window_size,
            is_maximized=data.get("is_maximized") is True,
            sidebar_width=max(0, _int(data.get("sidebar_width"), DEFAULT_SIDEBAR_WIDTH)),
            sidebar_visible=sidebar_visible,
            sidebar_prev_visible=_bool(data.get("sidebar_prev_visible"), sidebar_visible),
            terminal_maximized=data.get("terminal_maximized") is True,
            theme_mode=_str(data.get("theme_mode"), ""),
            prefer_dark=prefer_dark if isinstance(prefer_dark, bool) else None,
            font_scale=_float(data.get("font_scale"), 1.0),
            ui_font_size=_int(data.get("ui_font_size"), 11),
            details_expanded=data.get("details_expanded") is not False,
            filter_query=_str(data.get("filter_query"), ""),
            group_colors=_str_map(data.get("group_colors")),
            host_colors=_str_map(data.get("host_colors")),
            recent_inventories=_str_list(data.get("recent_inventories")),
            latency_probe=_bool(data.get("latency_probe"), True),
            density=Density.coerce(data.get("density")).value,
        )

    def to_dict(self) -> Dict[str, object]:
        prefer_dark = self.theme_mode == "dark" if self.theme_mode else bool(self.prefer_dark)
        return {
            "window_size": list(self.window_size) if self.window_size else None,
            "is_maximized": self.is_maximized,
            "sidebar_width": self.sidebar_width,
            "prefer_dark": prefer_dark,
            "theme_mode": self.theme_mode or ("dark" if prefer_dark else "light"),
            "font_scale": self.font_scale,
            "ui_font_size": self.ui_font_size,
            "sidebar_visible": self.sidebar_visible,
            "sidebar_prev_visible": self.sidebar_prev_visible,
            "terminal_maximized": self.terminal_maximized,
            "details_expanded": self.details_expanded,
            "filter_query": self.filter_query,
            "group_colors": dict(self.group_colors),
            "recent_inventories": list(self.recent_inventories),
            "latency_probe": self.latency_probe,
            "density": Density.coerce(self.density).value,
            "host_colors": dict(self.host_colors),
        }
