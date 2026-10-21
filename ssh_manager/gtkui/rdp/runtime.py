"""The window's RDP runtime: child registry, stale-child reaper, session limit, trust store, stop-all."""

from __future__ import annotations

import logging
import threading
from typing import Any, List, Optional

from ...rdp.process import ChildRegistry, RdpProcess, SessionLimiter, reap_stale
from ...trust import TrustStore

LOGGER = logging.getLogger(__name__)


class RdpRuntime:
    """One per window. Tabs register their processes so quitting the app stops every FreeRDP client."""

    def __init__(self, *, max_sessions: int = 8, registry: Optional[ChildRegistry] = None,
                 trust_store: Optional[TrustStore] = None, reap: bool = True) -> None:
        self.registry = registry or ChildRegistry()
        self.trust = trust_store or TrustStore()
        self.limiter = SessionLimiter(max_sessions)
        self._processes: List[RdpProcess] = []
        self._lock = threading.Lock()
        if reap:
            threading.Thread(target=self._reap, daemon=True, name="sshm-rdp-reap").start()

    def _reap(self) -> None:
        try:
            handled = reap_stale(self.registry)
        except Exception as exc:  # never fail the app start over a stale file
            LOGGER.warning("RDP stale-child check failed: %s", type(exc).__name__)
            return
        if handled:
            LOGGER.info("stopped %d FreeRDP client(s) left over from a previous run", len(handled))

    def track(self, process: RdpProcess) -> None:
        with self._lock:
            self._processes = [p for p in self._processes if p.running] + [process]

    def running(self) -> List[RdpProcess]:
        with self._lock:
            return [p for p in self._processes if p.running]

    def set_limit(self, limit: int) -> None:
        self.limiter.limit = max(1, int(limit))

    def stop_all(self, grace: float = 2.0) -> None:
        """Application exit: stop every client and wait (bounded) so none outlives the app."""

        for process in self.running():
            try:
                process.stop_now(grace)
            except Exception as exc:
                LOGGER.warning("stopping a FreeRDP client failed: %s", type(exc).__name__)


def any_running(tabs: List[Any]) -> int:
    return sum(1 for tab in tabs if getattr(tab, "rdp_connected", lambda: False)())
