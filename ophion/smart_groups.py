"""Lightweight storage for smart filters/groups (optional, additive)."""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import List

from ophion.persistence import write_json_atomic
from ophion.settings import _default_config_dir


@dataclass
class SmartGroup:
    name: str
    query: str


def smart_groups_path() -> Path:
    return _default_config_dir() / "smart_groups.json"


def load_smart_groups(path: Path | None = None) -> List[SmartGroup]:
    target = path or smart_groups_path()
    if not target.exists():
        return []
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        return []
    groups: List[SmartGroup] = []
    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            query = item.get("query")
            if not isinstance(name, str) or not isinstance(query, str):
                continue
            name = name.strip()
            query = query.strip()
            if name and query:
                groups.append(SmartGroup(name=name, query=query))
    return groups


def save_smart_groups(groups: List[SmartGroup], path: Path | None = None) -> bool:
    target = path or smart_groups_path()
    try:
        payload = [asdict(g) for g in groups]
        write_json_atomic(target, payload, mode=0o600)
    except OSError:
        return False
    return True
