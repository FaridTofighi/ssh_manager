"""Keep Python's cycle collector off short worker threads.

The collector runs on whichever thread happens to allocate. On a worker it can
finalize GTK widgets left in reference cycles, and GTK must only be touched
from its own thread: the result is a crash, or a deadlock between GObject's
signal lock and the GIL. Short workers (engine discovery, probes, the stale
child check) therefore pause automatic collection while they run; it resumes
when the last of them ends, and the pending garbage is collected on the next
allocation — normally on the GTK thread, which does most of the allocating.

Long-lived threads (output readers, tunnels) must not use this: collection
would stay off for the whole session.
"""

from __future__ import annotations

import functools
import gc
import threading
from contextlib import contextmanager
from typing import Callable, Iterator, TypeVar

_lock = threading.Lock()
_active = 0
_was_enabled = False

F = TypeVar("F", bound=Callable[..., object])


@contextmanager
def no_collection() -> Iterator[None]:
    """Pause automatic cycle collection for the duration (nests across threads)."""

    global _active, _was_enabled
    with _lock:
        if _active == 0:
            _was_enabled = gc.isenabled()
            gc.disable()
        _active += 1
    try:
        yield
    finally:
        with _lock:
            _active -= 1
            if _active == 0 and _was_enabled:
                gc.enable()


def worker(func: F) -> F:
    """Decorator for a short worker thread's target: :func:`no_collection` around the call."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        with no_collection():
            return func(*args, **kwargs)

    return wrapper  # type: ignore[return-value]


__all__ = ["no_collection", "worker"]
