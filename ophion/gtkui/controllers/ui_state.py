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
from ..favorites import clean_keys
from ..tab_labels import TabLabelMode
from ..theme.density import COMFORTABLE, DEFAULT_DENSITY, Density
from ..theme.accent import normalize_accent, normalize_color


def _color(value: object) -> str:
    """``#RRGGBB`` or ``""`` (automatic): anything else is automatic."""

    return (normalize_color(value) or "") if isinstance(value, str) else ""


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


def _color_maps(value: object) -> Dict[str, Dict[str, str]]:
    """``{"groups": {key: colour}, "hosts": {key: colour}}``; anything else is empty."""

    value = value if isinstance(value, dict) else {}
    return {"groups": _str_map(value.get("groups")), "hosts": _str_map(value.get("hosts"))}


def _inventory_colors(value: object) -> Dict[str, Dict[str, Dict[str, str]]]:
    if not isinstance(value, dict):
        return {}
    return {scope: _color_maps(maps) for scope, maps in value.items() if isinstance(scope, str)}


def _favorites(value: object) -> Dict[str, List[str]]:
    """``{inventory: [key, …]}``; anything else, and empty lists, are dropped."""

    if not isinstance(value, dict):
        return {}
    lists = {scope: clean_keys(keys) for scope, keys in value.items() if isinstance(scope, str) and isinstance(keys, list)}
    return {scope: keys for scope, keys in lists.items() if keys}


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
    accent_color: str = "default"  # a preset id or #RRGGBB (gtkui/theme/accent.py); invalid → "default"
    # Tree icon colours when an item and its groups have none: "" = automatic (the theme's icon colour).
    default_group_color: str = ""
    default_host_color: str = ""
    prefer_dark: Optional[bool] = None
    font_scale: float = 1.0
    ui_font_size: int = 11
    details_expanded: bool = True
    filter_query: str = ""
    # Per-item tree colours of each inventory (scope as for favorites); ColorController uses the active one's.
    # group_colors / host_colors are the active inventory's, still written for older versions.
    inventory_colors: Dict[str, Dict[str, Dict[str, str]]] = field(default_factory=dict)
    # Colours of a file from before inventory_colors (one set for every inventory): an inventory opened for
    # the first time takes the ones of its own items.
    # None: not split up yet; ColorController takes group_colors / host_colors as they were at the start.
    unscoped_colors: Optional[Dict[str, Dict[str, str]]] = None
    group_colors: Dict[str, str] = field(default_factory=dict)
    host_colors: Dict[str, str] = field(default_factory=dict)
    recent_inventories: List[str] = field(default_factory=list)
    latency_probe: bool = True
    density: str = DEFAULT_DENSITY.value  # "compact" | "comfortable"
    tab_label_mode: str = TabLabelMode.NAME.value  # "name" | "ip" | "name_ip"
    # Starred hosts and groups per inventory (gtkui/favorites.py) and whether their sidebar section is open.
    favorites: Dict[str, List[str]] = field(default_factory=dict)
    favorites_expanded: bool = True
    # Older files may carry an active content tab ("page_tab", Terminal/Details/
    # Activity); those tabs are gone, so the key is ignored and not written back.

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
            accent_color=normalize_accent(data.get("accent_color")),
            default_group_color=_color(data.get("default_group_color")),
            default_host_color=_color(data.get("default_host_color")),
            prefer_dark=prefer_dark if isinstance(prefer_dark, bool) else None,
            font_scale=_float(data.get("font_scale"), 1.0),
            ui_font_size=_int(data.get("ui_font_size"), 11),
            details_expanded=data.get("details_expanded") is not False,
            filter_query=_str(data.get("filter_query"), ""),
            inventory_colors=_inventory_colors(data.get("inventory_colors")),
            unscoped_colors=_color_maps(data.get("unscoped_colors")) if "inventory_colors" in data else None,
            group_colors=_str_map(data.get("group_colors")),
            host_colors=_str_map(data.get("host_colors")),
            recent_inventories=_str_list(data.get("recent_inventories")),
            latency_probe=_bool(data.get("latency_probe"), True),
            density=Density.coerce(data.get("density")).value,
            tab_label_mode=TabLabelMode.coerce(data.get("tab_label_mode")).value,
            favorites=_favorites(data.get("favorites")),
            favorites_expanded=_bool(data.get("favorites_expanded"), True),
        )

    def to_dict(self) -> Dict[str, object]:
        prefer_dark = self.theme_mode == "dark" if self.theme_mode else bool(self.prefer_dark)
        return {
            "window_size": list(self.window_size) if self.window_size else None,
            "is_maximized": self.is_maximized,
            "sidebar_width": self.sidebar_width,
            "prefer_dark": prefer_dark,
            "theme_mode": self.theme_mode or ("dark" if prefer_dark else "light"),
            "accent_color": normalize_accent(self.accent_color),
            "default_group_color": _color(self.default_group_color),
            "default_host_color": _color(self.default_host_color),
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
            "tab_label_mode": TabLabelMode.coerce(self.tab_label_mode).value,
            "host_colors": dict(self.host_colors),
            "inventory_colors": {scope: _color_maps(maps) for scope, maps in self.inventory_colors.items()},
            "unscoped_colors": _color_maps(self.unscoped_colors if self.unscoped_colors is not None else
                                           {"groups": self.group_colors, "hosts": self.host_colors}),
            "favorites": {scope: list(keys) for scope, keys in self.favorites.items() if keys},
            "favorites_expanded": self.favorites_expanded,
        }
