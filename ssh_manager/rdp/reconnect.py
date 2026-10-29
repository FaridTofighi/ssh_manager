"""When and how often to reconnect an RDP session (pure Python, deterministic).

* A dropped network after a working session reconnects **automatically**
  with exponential backoff (2 s, 4 s, 8 s … at most 60 s), at most
  ``reconnect_max`` times (0 in the settings means "as often as needed", which
  is capped at :data:`HARD_CAP` attempts so it can never loop forever).
* An idle timeout or "someone else connected to this session" **asks** first:
  reconnecting could take the session back from the other user.
* The user's own disconnect, a sign-out, sign-in failures, account,
  certificate and security problems **never** reconnect on their own.
* While the network is down, attempts wait (they are not counted).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from .outcome import Kind, RdpOutcome

BASE_S = 2.0
CAP_S = 60.0
HARD_CAP = 20


class Plan(str, Enum):
    AUTO = "auto"  # reconnect after a delay
    ASK = "ask"  # show Reconnect and explain; never automatic
    NEVER = "never"


def plan_for(outcome: RdpOutcome, *, enabled: bool = True) -> Plan:
    if outcome.reason in ("user_disconnect", "logoff", "admin_logoff", "closed"):
        return Plan.NEVER
    if outcome.reason in ("idle_timeout", "replaced", "logon_timeout", "admin_disconnect"):
        return Plan.ASK
    if outcome.kind is Kind.NETWORK and outcome.connected:
        return Plan.AUTO if enabled else Plan.ASK
    return Plan.NEVER if outcome.kind is Kind.OK_DISCONNECT else Plan.ASK


@dataclass
class ReconnectScheduler:
    max_attempts: int = 5  # 0 = up to HARD_CAP
    base_s: float = BASE_S
    cap_s: float = CAP_S
    attempts: int = 0
    cancelled: bool = False

    @property
    def limit(self) -> int:
        return HARD_CAP if self.max_attempts <= 0 else min(self.max_attempts, HARD_CAP)

    def next_delay(self) -> Optional[float]:
        """The delay before the next attempt (and count it), or ``None`` when the attempts are used up."""

        if self.cancelled or self.attempts >= self.limit:
            return None
        delay = min(self.cap_s, self.base_s * (2 ** self.attempts))
        self.attempts += 1
        return delay

    def succeeded(self) -> None:
        self.attempts = 0
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True

    def reset(self) -> None:
        self.attempts = 0
        self.cancelled = False

    def progress(self) -> str:
        """``"attempt 2 of 5"`` (English; the UI translates the pattern)."""

        return f"attempt {self.attempts} of {self.limit}"


__all__ = ["ReconnectScheduler", "Plan", "plan_for", "HARD_CAP"]
