"""Per-host "last connected" timestamps and a short activity log.

Stored as JSON next to the UI-state file. Missing or corrupt data is
ignored; writes are atomic. Each host keeps at most ``EVENT_LIMIT`` events.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from ..persistence import write_json_atomic
from .host_facts import ActivityEvent, HostActivity

LOGGER = logging.getLogger(__name__)

EVENT_LIMIT = 50
HOST_LIMIT = 500


class HostHistory:
    def __init__(self, path: Optional[Path], *, clock: Callable[[], float] = time.time) -> None:
        self.path = path
        self._clock = clock
        self._hosts: Dict[str, HostActivity] = {}
        self._listeners: List[Callable[[str], None]] = []
        self._load()

    # ------------------------------------------------------------ queries
    def get(self, key: str) -> HostActivity:
        return self._hosts.get(key) or HostActivity()

    def last_connected(self, key: str) -> Optional[float]:
        return self.get(key).last_connected

    def events(self, key: str) -> List[ActivityEvent]:
        return list(reversed(self.get(key).events))  # newest first

    # ------------------------------------------------------------ updates
    def record_connected(self, key: str, timestamp: Optional[float] = None, *, text: str = "") -> None:
        when = self._clock() if timestamp is None else timestamp
        activity = self._hosts.setdefault(key, HostActivity())
        activity.last_connected = when
        if text:
            self._append(activity, ActivityEvent(when, "success", text))
        self._changed(key)

    def record_event(self, key: str, level: str, text: str, timestamp: Optional[float] = None) -> None:
        activity = self._hosts.setdefault(key, HostActivity())
        self._append(activity, ActivityEvent(self._clock() if timestamp is None else timestamp, level, text))
        self._changed(key)

    def rename(self, old_key: str, new_key: str) -> None:
        if old_key in self._hosts and old_key != new_key:
            self._hosts[new_key] = self._hosts.pop(old_key)
            self._changed(new_key)

    def add_listener(self, callback: Callable[[str], None]) -> None:
        self._listeners.append(callback)

    # ------------------------------------------------------------ storage
    @staticmethod
    def _append(activity: HostActivity, event: ActivityEvent) -> None:
        activity.events.append(event)
        del activity.events[:-EVENT_LIMIT]

    def _changed(self, key: str) -> None:
        self._save()
        for callback in list(self._listeners):
            callback(key)

    def _load(self) -> None:
        if not self.path or not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            LOGGER.warning("ignoring unreadable host history %s: %s", self.path, exc)
            return
        hosts = data.get("hosts") if isinstance(data, dict) else None
        if not isinstance(hosts, dict):
            return
        for key, raw in hosts.items():
            if not isinstance(raw, dict):
                continue
            last = raw.get("last_connected")
            events = []
            for item in raw.get("events") or []:
                if isinstance(item, dict) and isinstance(item.get("ts"), (int, float)) and isinstance(item.get("text"), str):
                    events.append(ActivityEvent(float(item["ts"]), str(item.get("level", "info")), item["text"]))
            self._hosts[str(key)] = HostActivity(
                float(last) if isinstance(last, (int, float)) and not isinstance(last, bool) else None,
                events[-EVENT_LIMIT:],
            )

    def _save(self) -> None:
        if not self.path:
            return
        items = sorted(self._hosts.items(), key=lambda kv: kv[1].last_connected or 0, reverse=True)[:HOST_LIMIT]
        payload = {
            "version": 1,
            "hosts": {
                key: {
                    "last_connected": activity.last_connected,
                    "events": [{"ts": e.timestamp, "level": e.level, "text": e.text} for e in activity.events],
                }
                for key, activity in items
            },
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            write_json_atomic(self.path, payload)
        except OSError as exc:
            LOGGER.warning("could not save host history: %s", exc)
