"""Non-blocking TCP connect timing for the host header's LATENCY stat.

Probes run on a worker thread and report back through ``dispatch`` (GLib's
idle queue in the app), results are cached for ``ttl`` seconds, and
:meth:`LatencyProbe.shutdown` drops every pending callback. Clock, connector
and dispatcher are injectable so tests run without sockets or a main loop.
"""

from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Set, Tuple

Key = Tuple[str, int]
Callback = Callable[["LatencyResult"], None]

DEFAULT_TTL = 30.0
DEFAULT_TIMEOUT = 3.0
REFRESH_SECONDS = 60


@dataclass(frozen=True)
class LatencyResult:
    key: Key
    ms: Optional[float]
    failed: bool
    measured_at: float


def _tcp_connect(host: str, port: int, timeout: float) -> None:
    with socket.create_connection((host, port), timeout=timeout):
        pass


def _thread_runner(target: Callable[[], None]) -> None:
    threading.Thread(target=target, name="sshm-latency", daemon=True).start()


class LatencyProbe:
    def __init__(
        self,
        *,
        ttl: float = DEFAULT_TTL,
        timeout: float = DEFAULT_TIMEOUT,
        clock: Callable[[], float] = time.monotonic,
        connect: Callable[[str, int, float], None] = _tcp_connect,
        run: Callable[[Callable[[], None]], None] = _thread_runner,
        dispatch: Optional[Callable[[Callable[[], None]], None]] = None,
    ) -> None:
        self.ttl = ttl
        self.timeout = timeout
        self.enabled = True
        self._clock = clock
        self._connect = connect
        self._run = run
        self._dispatch = dispatch or (lambda fn: fn())
        self._cache: Dict[Key, LatencyResult] = {}
        self._pending: Set[Key] = set()
        self._lock = threading.Lock()
        self._generation = 0

    def cached(self, key: Key) -> Optional[LatencyResult]:
        with self._lock:
            return self._cache.get(key)

    def is_fresh(self, key: Key) -> bool:
        result = self.cached(key)
        return result is not None and self._clock() - result.measured_at < self.ttl

    def inject(self, key: Key, ms: Optional[float], *, failed: bool = False) -> LatencyResult:
        result = LatencyResult(key, ms, failed, self._clock())
        with self._lock:
            self._cache[key] = result
        return result

    def request(self, key: Key, callback: Callback, *, force: bool = False) -> bool:
        """Probe ``key`` unless a fresh result exists; ``callback`` runs via ``dispatch``.

        Returns True when a probe was started.
        """

        if not self.enabled or not key[0]:
            return False
        with self._lock:
            if key in self._pending:
                return False
            fresh = key in self._cache and self._clock() - self._cache[key].measured_at < self.ttl
            if fresh and not force:
                return False
            self._pending.add(key)
            generation = self._generation

        def work() -> None:
            start = self._clock()
            failed = False
            try:
                self._connect(key[0], key[1], self.timeout)
            except OSError:
                failed = True
            elapsed = (self._clock() - start) * 1000.0
            result = LatencyResult(key, None if failed else elapsed, failed, self._clock())

            def deliver() -> bool:
                with self._lock:
                    self._pending.discard(key)
                    if generation != self._generation:
                        return False
                    self._cache[key] = result
                callback(result)
                return False

            self._dispatch(deliver)

        self._run(work)
        return True

    def shutdown(self) -> None:
        """Forget pending probes; late results are discarded."""

        with self._lock:
            self._generation += 1
            self._pending.clear()
