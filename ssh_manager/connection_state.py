"""Connection state machine for the SSH manager."""

from __future__ import annotations

from enum import Enum, auto
from typing import Callable, Dict, List, Tuple


class ConnectionState(Enum):
    DISCONNECTED = auto()
    CONNECTING = auto()
    AUTHENTICATING = auto()
    AUTH_FAILED = auto()
    AUTH_SUCCEEDED = auto()
    CONNECTED = auto()
    ABORTED = auto()


_ALLOWED_TRANSITIONS: Dict[ConnectionState, Tuple[ConnectionState, ...]] = {
    ConnectionState.DISCONNECTED: (ConnectionState.CONNECTING,),
    ConnectionState.CONNECTING: (
        ConnectionState.AUTHENTICATING,
        ConnectionState.ABORTED,
    ),
    ConnectionState.AUTHENTICATING: (
        ConnectionState.AUTH_FAILED,
        ConnectionState.AUTH_SUCCEEDED,
        ConnectionState.ABORTED,
    ),
    ConnectionState.AUTH_FAILED: (
        ConnectionState.AUTHENTICATING,
        ConnectionState.ABORTED,
    ),
    ConnectionState.AUTH_SUCCEEDED: (
        ConnectionState.CONNECTED,
        ConnectionState.ABORTED,
    ),
    ConnectionState.CONNECTED: (
        ConnectionState.ABORTED,
        ConnectionState.DISCONNECTED,
    ),
    ConnectionState.ABORTED: (),
}


class ConnectionStateMachine:
    """Lightweight observable state machine enforcing the defined transitions."""

    def __init__(self) -> None:
        self._state = ConnectionState.DISCONNECTED
        self._state_listeners: List[Callable[[ConnectionState], None]] = []
        self._error_listeners: List[Callable[[str], None]] = []

    @property
    def state(self) -> ConnectionState:
        return self._state

    def on_state_change(self, callback: Callable[[ConnectionState], None]) -> None:
        self._state_listeners.append(callback)

    def on_error(self, callback: Callable[[str], None]) -> None:
        self._error_listeners.append(callback)

    def transition_to(self, new_state: ConnectionState) -> None:
        if new_state == self._state:
            return
        allowed = _ALLOWED_TRANSITIONS[self._state]
        if new_state not in allowed:
            raise ValueError(
                f"Invalid transition: {self._state.name} -> {new_state.name}"
            )
        self._state = new_state
        for callback in list(self._state_listeners):
            callback(new_state)

    def emit_error(self, message: str) -> None:
        for callback in list(self._error_listeners):
            callback(message)

    def reset(self) -> None:
        if self._state == ConnectionState.DISCONNECTED:
            return
        self._state = ConnectionState.DISCONNECTED
        for callback in list(self._state_listeners):
            callback(self._state)
