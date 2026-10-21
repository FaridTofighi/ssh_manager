"""The window's RDP runtime: child registry, stale-child reaper, session limit, trust store, stop-all."""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, List, Optional, Set, Tuple

from ...rdp.process import ChildRegistry, RdpProcess, SessionLimiter, reap_stale
from ...trust import TrustStore

LOGGER = logging.getLogger(__name__)


class RdpRuntime:
    """One per window. Tabs register their processes so quitting the app stops every FreeRDP client."""

    def __init__(self, *, max_sessions: int = 8, registry: Optional[ChildRegistry] = None,
                 trust_store: Optional[TrustStore] = None, reap: bool = True,
                 state_path: Optional[Path] = None) -> None:
        self.registry = registry or ChildRegistry()
        self.trust = trust_store or TrustStore()
        self.limiter = SessionLimiter(max_sessions)
        self._processes: List[RdpProcess] = []
        self._lock = threading.Lock()
        base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
        self.state_path = state_path or Path(base) / "ssh-manager" / "rdp-embed.json"
        self.embed_failures: Set[Tuple[str, str]] = self._load_failures()
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

    # ----------------------------------------------- embedding failures
    def _load_failures(self) -> Set[Tuple[str, str]]:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return set()
        if not isinstance(data, list):
            return set()
        return {(str(a), str(b)) for a, b in (item for item in data if isinstance(item, list) and len(item) == 2)}

    def note_embed_failure(self, engine_id: str, session_type: str) -> None:
        """Embedding failed with this engine on this kind of session: use the external window from now on."""

        self.embed_failures.add((engine_id, session_type or "unknown"))
        try:
            self.state_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps(sorted(list(item) for item in self.embed_failures)),
                                       encoding="utf-8")
        except OSError as exc:
            LOGGER.warning("could not remember the embedding failure: %s", type(exc).__name__)

    def embed_failed(self, engine_id: str, session_type: str) -> bool:
        return (engine_id, session_type or "unknown") in self.embed_failures

    def forget_embed_failures(self) -> None:
        self.embed_failures.clear()
        try:
            self.state_path.unlink()
        except OSError:
            pass

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
