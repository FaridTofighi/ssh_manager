"""Notification logic: toast lifetime rules and the in-memory message history.

This module has no GTK widget code. The toast view (``widgets/toast.py``)
subscribes to :class:`ToastController` and renders whatever it is told to show,
which keeps the rules testable with a fake clock and scheduler.

Rules:

* ``info`` and ``success`` toasts auto-dismiss (5 s / 4 s by default);
* ``warning`` and ``error`` toasts are sticky until dismissed;
* at most :attr:`ToastController.MAX_VISIBLE` toasts are on screen; older ones
  are dismissed but every message stays in the history.
"""

from __future__ import annotations

import itertools
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Deque, Dict, List, Optional, Protocol

from ..theme.tokens import Level


@dataclass
class Message:
    """One user-facing message."""

    id: int
    text: str
    level: Level
    timestamp: float
    action_label: str = ""
    action: Optional[Callable[[], None]] = field(default=None, repr=False)
    read: bool = False

    @property
    def sticky(self) -> bool:
        return self.level in (Level.WARNING, Level.ERROR)


class Scheduler(Protocol):
    """Timer interface (GLib in the app, a fake in tests)."""

    def schedule(self, seconds: float, callback: Callable[[], None]) -> int: ...

    def cancel(self, handle: int) -> None: ...


class GLibScheduler:
    """:class:`Scheduler` backed by ``GLib.timeout_add``."""

    def schedule(self, seconds: float, callback: Callable[[], None]) -> int:
        from gi.repository import GLib  # type: ignore

        def fire() -> bool:
            callback()
            return GLib.SOURCE_REMOVE

        return GLib.timeout_add(int(seconds * 1000), fire)

    def cancel(self, handle: int) -> None:
        from gi.repository import GLib  # type: ignore

        if handle:
            GLib.source_remove(handle)


class MessageHistory:
    """Bounded, newest-first history of every message shown to the user."""

    def __init__(self, limit: int = 200) -> None:
        self._items: Deque[Message] = deque(maxlen=limit)
        self._listeners: List[Callable[[], None]] = []

    def add(self, message: Message) -> None:
        self._items.appendleft(message)
        self._emit()

    def items(self) -> List[Message]:
        return list(self._items)

    def clear(self) -> None:
        self._items.clear()
        self._emit()

    def unread_count(self, *, levels: Optional[set] = None) -> int:
        return sum(1 for m in self._items if not m.read and (levels is None or m.level in levels))

    def mark_all_read(self) -> None:
        changed = False
        for message in self._items:
            if not message.read:
                message.read = True
                changed = True
        if changed:
            self._emit()

    def add_listener(self, callback: Callable[[], None]) -> None:
        self._listeners.append(callback)

    def _emit(self) -> None:
        for callback in list(self._listeners):
            callback()

    def __len__(self) -> int:
        return len(self._items)


class ToastController:
    """Decides which toasts are visible and for how long."""

    MAX_VISIBLE = 3
    AUTO_DISMISS_SECONDS: Dict[Level, Optional[float]] = {
        Level.INFO: 5.0,
        Level.SUCCESS: 4.0,
        Level.WARNING: None,
        Level.ERROR: None,
    }

    def __init__(
        self,
        history: Optional[MessageHistory] = None,
        scheduler: Optional[Scheduler] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.history = history if history is not None else MessageHistory()
        self._scheduler: Scheduler = scheduler if scheduler is not None else GLibScheduler()
        self._clock = clock
        self._ids = itertools.count(1)
        self._visible: List[Message] = []
        self._timers: Dict[int, int] = {}
        self._show_listeners: List[Callable[[Message], None]] = []
        self._dismiss_listeners: List[Callable[[Message], None]] = []

    # --------------------------------------------------------- listeners
    def on_show(self, callback: Callable[[Message], None]) -> None:
        self._show_listeners.append(callback)

    def on_dismiss(self, callback: Callable[[Message], None]) -> None:
        self._dismiss_listeners.append(callback)

    # --------------------------------------------------------------- api
    @property
    def visible(self) -> List[Message]:
        return list(self._visible)

    def notify(
        self,
        text: str,
        level: Level = Level.INFO,
        *,
        action_label: str = "",
        action: Optional[Callable[[], None]] = None,
        timeout: Optional[float] = -1.0,
    ) -> Message:
        """Show a toast. ``timeout=-1`` uses the level default, ``None`` is sticky."""

        level = Level(level) if not isinstance(level, Level) else level
        message = Message(
            id=next(self._ids),
            text=text,
            level=level,
            timestamp=self._clock(),
            action_label=action_label if action else "",
            action=action,
        )
        self.history.add(message)
        self._visible.append(message)
        for callback in list(self._show_listeners):
            callback(message)
        while len(self._visible) > self.MAX_VISIBLE:
            self.dismiss(self._visible[0].id)
        seconds = self.AUTO_DISMISS_SECONDS.get(level) if timeout == -1.0 else timeout
        if seconds:
            self._timers[message.id] = self._scheduler.schedule(seconds, lambda mid=message.id: self._expire(mid))
        return message

    def dismiss(self, message_id: int) -> None:
        message = next((m for m in self._visible if m.id == message_id), None)
        if message is None:
            return
        self._visible.remove(message)
        timer = self._timers.pop(message_id, None)
        if timer:
            self._scheduler.cancel(timer)
        message.read = True
        for callback in list(self._dismiss_listeners):
            callback(message)

    def activate(self, message_id: int) -> None:
        """Run a toast's action and dismiss it."""

        message = next((m for m in self._visible if m.id == message_id), None)
        if message is None:
            return
        action = message.action
        self.dismiss(message_id)
        if action is not None:
            action()

    def dismiss_all(self) -> None:
        for message in list(self._visible):
            self.dismiss(message.id)

    def shutdown(self) -> None:
        """Cancel all pending timers (call on window destroy)."""

        for timer in list(self._timers.values()):
            self._scheduler.cancel(timer)
        self._timers.clear()
        self._visible.clear()

    # ----------------------------------------------------------- internal
    def _expire(self, message_id: int) -> None:
        self._timers.pop(message_id, None)
        self.dismiss(message_id)
