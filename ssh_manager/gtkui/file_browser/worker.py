"""One background thread per connection; results come back on the GTK thread.

A backend object is not thread-safe, so every call for one connection goes
through one single-thread executor (in order). Callbacks run on the GTK main
loop through ``GLib.idle_add`` and are dropped once the worker is shut down
(tab closed), so a late result never touches a destroyed widget.
"""

from __future__ import annotations

import concurrent.futures
import logging
from typing import Any, Callable, Optional

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib  # type: ignore  # noqa: E402

LOGGER = logging.getLogger(__name__)


class Worker:
    def __init__(self, name: str) -> None:
        self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"sshm-{name}")
        self.alive = True
        self.pending = 0

    def post(self, callback: Optional[Callable[[Any], Any]], value: Any = None) -> None:
        """Run ``callback(value)`` on the GTK thread (unless shut down meanwhile)."""

        if callback is None:
            return

        def deliver() -> bool:
            if self.alive:
                try:
                    callback(value)
                except Exception:  # a UI callback must never kill the main loop
                    LOGGER.exception("file browser callback failed")
            return GLib.SOURCE_REMOVE

        GLib.idle_add(deliver)

    def submit(self, work: Callable[[], Any], on_done: Optional[Callable[[Any], Any]] = None,
               on_error: Optional[Callable[[BaseException], Any]] = None) -> Optional[concurrent.futures.Future]:
        if not self.alive:
            return None
        self.pending += 1

        def run() -> None:
            try:
                result = work()
            except BaseException as exc:  # noqa: BLE001 - reported to the UI
                self.post(self._done_error(on_error), exc)
            else:
                self.post(self._done(on_done), result)

        try:
            return self._pool.submit(run)
        except RuntimeError:  # shut down concurrently
            self.pending -= 1
            return None

    def _done(self, callback):
        def finish(value):
            self.pending -= 1
            if callback is not None:
                callback(value)
        return finish

    def _done_error(self, callback):
        def finish(exc):
            self.pending -= 1
            if callback is not None:
                callback(exc)
            else:
                LOGGER.warning("file browser task failed: %s", type(exc).__name__)
        return finish

    def shutdown(self) -> None:
        """Stop delivering results and drop queued work; a running call finishes in the background."""

        self.alive = False
        self._pool.shutdown(wait=False, cancel_futures=True)
