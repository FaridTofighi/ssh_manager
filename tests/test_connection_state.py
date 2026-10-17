import unittest

from ssh_manager.connection_state import ConnectionState, ConnectionStateMachine


class ConnectionStateMachineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.machine = ConnectionStateMachine()
        self.transitions = []
        self.errors = []
        self.machine.on_state_change(self.transitions.append)
        self.machine.on_error(self.errors.append)

    def test_initial_state(self) -> None:
        self.assertEqual(self.machine.state, ConnectionState.DISCONNECTED)

    def test_valid_transitions(self) -> None:
        sequence = [
            ConnectionState.CONNECTING,
            ConnectionState.AUTHENTICATING,
            ConnectionState.AUTH_FAILED,
            ConnectionState.AUTHENTICATING,
            ConnectionState.AUTH_SUCCEEDED,
            ConnectionState.CONNECTED,
        ]
        for state in sequence:
            self.machine.transition_to(state)
        self.assertEqual(self.machine.state, ConnectionState.CONNECTED)
        self.assertEqual(self.transitions, sequence)

    def test_invalid_transition_raises(self) -> None:
        with self.assertRaises(ValueError):
            self.machine.transition_to(ConnectionState.AUTH_SUCCEEDED)
        self.assertEqual(self.machine.state, ConnectionState.DISCONNECTED)

    def test_emit_error_notifies_listeners(self) -> None:
        self.machine.emit_error("failure")
        self.assertEqual(self.errors, ["failure"])

    def test_reset_sets_disconnected(self) -> None:
        self.machine.transition_to(ConnectionState.CONNECTING)
        self.machine.reset()
        self.assertEqual(self.machine.state, ConnectionState.DISCONNECTED)
        self.assertEqual(
            self.transitions,
            [ConnectionState.CONNECTING, ConnectionState.DISCONNECTED],
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
