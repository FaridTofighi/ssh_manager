import os
import time
import unittest
from dataclasses import dataclass, field
from typing import List, Optional
from unittest import mock

import paramiko
from ssh_manager.connection_state import ConnectionState, ConnectionStateMachine
from ssh_manager.connector import (
    Connector,
    TryResult,
    _connection_algorithm_overrides,
    _jump_proxy_command,
    _policy_from_settings,
    _proxy_command_for,
)
from ssh_manager.secure_store import InMemoryKeyring, SecureStore


@dataclass
class HostCfg:
    host: str = "example.com"
    port: int = 22
    username: str = "alice"
    timeout: float = 1.0
    parent_window: Optional[object] = None
    options: dict = field(default_factory=dict)


class Settings:
    remember_password_default = True
    strict_host_key_checking = "reject"
    connect_timeout = 5.0
    auth_timeout = 5.0
    banner_timeout = 5.0
    use_paramiko_vte = False


class FakePasswordDialog:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls: List[str] = []
        self.requests = []

    def show_for(self, host_cfg, error_message=None, request=None):
        self.calls.append(error_message or "")
        self.requests.append(request)
        return next(self.responses)


class ConnectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state_machine = ConnectionStateMachine()
        self.store = SecureStore(InMemoryKeyring())
        self.settings = Settings()

    def _make_connector(self, dialog) -> Connector:
        return Connector(
            dialog,
            self.store,
            self.settings,
            self.state_machine,
            main_invoke=lambda fn: fn(),
            main_queue=lambda cb, args, kwargs: cb(*args, **(kwargs or {})),
        )

    def _wait_for_completion(self, connector: Connector) -> None:
        for _ in range(20):
            thread = getattr(connector, "_worker_thread", None)
            if thread is None or not thread.is_alive():
                return
            time.sleep(0.05)
        self.fail("Connector thread did not finish")

    def test_password_retry_until_success(self) -> None:
        responses = [
            ("wrong1", True),
            ("wrong2", True),
            ("correct", True),
        ]
        dialog = FakePasswordDialog(responses)
        connector = self._make_connector(dialog)

        attempts = []

        def fake_try(host_cfg, password, timeouts):
            attempts.append(password)
            if password in {"wrong1", "wrong2"}:
                return TryResult(ok=False, is_auth_error=True, message="bad")
            return TryResult(ok=True, is_auth_error=False, message="", client="client")

        connector._try_paramiko = fake_try  # type: ignore[assignment]

        success = []
        verified = []
        connector.add_connect_success_callback(lambda session: success.append(session))
        connector.add_password_verified_callback(verified.append)

        connector.connect_to_host(HostCfg())
        self._wait_for_completion(connector)

        self.assertEqual(attempts, ["wrong1", "wrong2", "correct"])
        self.assertEqual(success, ["client"])
        # Phase 3: nothing is saved by the connector; the UI is told and asks "Save this password?".
        self.assertIsNone(self.store.get("example.com", 22, "alice"))
        self.assertEqual(verified, ["correct"])
        self.assertEqual([r.attempt for r in dialog.requests], [1, 2, 3])
        self.assertEqual(dialog.requests[1].rejected, "password you entered")
        self.assertEqual(self.state_machine.state, ConnectionState.CONNECTED)

    def test_stored_secret_invalid_then_updated(self) -> None:
        self.store.set("example.com", 22, "alice", "old")
        responses = [("newpass", True)]
        dialog = FakePasswordDialog(responses)
        connector = self._make_connector(dialog)

        attempts = []

        def fake_try(host_cfg, password, timeouts):
            attempts.append(password)
            if password == "old":
                return TryResult(ok=False, is_auth_error=True, message="bad")
            return TryResult(ok=True, is_auth_error=False, message="", client="client")

        connector._try_paramiko = fake_try  # type: ignore[assignment]
        verified = []
        connector.add_password_verified_callback(verified.append)
        connector.connect_to_host(HostCfg())
        self._wait_for_completion(connector)

        self.assertEqual(attempts, ["old", "newpass"])
        self.assertEqual(self.store.get("example.com", 22, "alice"), "old")  # neither rewritten nor deleted
        self.assertEqual(verified, ["newpass"])
        self.assertEqual(dialog.requests[0].rejected, "saved password")
        self.assertEqual(self.state_machine.state, ConnectionState.CONNECTED)

    def test_cancel_aborts_connection(self) -> None:
        dialog = FakePasswordDialog([(None, False)])
        connector = self._make_connector(dialog)

        aborted = []
        connector.add_aborted_callback(lambda reason: aborted.append(reason))

        connector.connect_to_host(HostCfg())
        self._wait_for_completion(connector)

        self.assertTrue(aborted)
        self.assertEqual(self.state_machine.state, ConnectionState.ABORTED)

    def test_network_error(self) -> None:
        dialog = FakePasswordDialog([("irrelevant", True)])
        connector = self._make_connector(dialog)

        messages = []
        connector.add_error_callback(lambda msg: messages.append(msg))

        def fake_try(host_cfg, password, timeouts):
            return TryResult(ok=False, is_auth_error=False, message="Network down")

        connector._try_paramiko = fake_try  # type: ignore[assignment]
        connector.connect_to_host(HostCfg())
        self._wait_for_completion(connector)

        self.assertEqual(messages, ["Network down"])
        self.assertEqual(self.state_machine.state, ConnectionState.ABORTED)

    def test_silent_connect_with_stored_password(self) -> None:
        self.store.set("example.com", 22, "alice", "secret")
        dialog = FakePasswordDialog([("should-not", True)])
        connector = self._make_connector(dialog)

        attempts = []

        def fake_try(host_cfg, password, timeouts):
            attempts.append(password)
            return TryResult(ok=True, is_auth_error=False, message="", client="client")

        connector._try_paramiko = fake_try  # type: ignore[assignment]
        connector.connect_to_host(HostCfg())
        self._wait_for_completion(connector)

        self.assertEqual(attempts, ["secret"])
        self.assertEqual(dialog.calls, [])
        self.assertEqual(self.state_machine.state, ConnectionState.CONNECTED)

    def test_connector_can_be_reused_after_abort(self) -> None:
        dialog = FakePasswordDialog([(None, False), ("secret", True)])
        connector = self._make_connector(dialog)

        connector.connect_to_host(HostCfg())
        self._wait_for_completion(connector)
        self.assertEqual(self.state_machine.state, ConnectionState.ABORTED)

        attempts = []

        def fake_try(host_cfg, password, timeouts):
            attempts.append(password)
            return TryResult(ok=True, is_auth_error=False, message="", client="client")

        connector._try_paramiko = fake_try  # type: ignore[assignment]
        connector.connect_to_host(HostCfg())
        self._wait_for_completion(connector)

        self.assertEqual(attempts, ["secret"])
        self.assertEqual(self.state_machine.state, ConnectionState.CONNECTED)

    def test_policy_supports_current_and_legacy_setting_names(self) -> None:
        class CurrentSettings(Settings):
            host_key_policy = "auto-add"

        class LegacyAutoAddSettings:
            strict_host_key_checking = False

        class LegacyRejectSettings:
            strict_host_key_checking = True

        self.assertEqual(_policy_from_settings(CurrentSettings()), "auto-add")
        self.assertEqual(_policy_from_settings(Settings()), "reject")
        self.assertEqual(_policy_from_settings(LegacyAutoAddSettings()), "auto-add")
        self.assertEqual(_policy_from_settings(LegacyRejectSettings()), "reject")

    def test_algorithm_overrides_use_disabled_algorithms(self) -> None:
        class AlgorithmSettings(Settings):
            allowed_ciphers = [paramiko.Transport._preferred_ciphers[0]]
            allowed_kex = [paramiko.Transport._preferred_kex[0]]

        disabled = _connection_algorithm_overrides({}, AlgorithmSettings())
        self.assertIn("ciphers", disabled)
        self.assertIn("kex", disabled)
        self.assertNotIn(AlgorithmSettings.allowed_ciphers[0], disabled["ciphers"])
        self.assertNotIn(AlgorithmSettings.allowed_kex[0], disabled["kex"])

        host_disabled = _connection_algorithm_overrides(
            {
                "Ciphers": paramiko.Transport._preferred_ciphers[0],
                "KexAlgorithms": paramiko.Transport._preferred_kex[0],
            },
            Settings(),
        )
        self.assertIn("ciphers", host_disabled)
        self.assertIn("kex", host_disabled)

    def test_try_paramiko_uses_app_policy_and_disabled_algorithms_kwarg(self) -> None:
        class AlgorithmSettings(Settings):
            host_key_policy = "auto-add"
            allowed_ciphers = [paramiko.Transport._preferred_ciphers[0]]
            allowed_kex = [paramiko.Transport._preferred_kex[0]]

        class FakeSSHClient:
            instances = []

            def __init__(self):
                self.policy = None
                self.connect_kwargs = None
                FakeSSHClient.instances.append(self)

            def load_system_host_keys(self):
                pass

            def set_missing_host_key_policy(self, policy):
                self.policy = policy

            def connect(self, *args, **kwargs):
                self.connect_kwargs = kwargs

            def close(self):
                pass

        self.settings = AlgorithmSettings()
        connector = self._make_connector(FakePasswordDialog([]))

        with mock.patch("ssh_manager.connector.paramiko.SSHClient", FakeSSHClient):
            result = connector._try_paramiko(HostCfg(), "secret", (1.0, 1.0, 1.0))

        self.assertTrue(result.ok)
        client = FakeSSHClient.instances[-1]
        self.assertIsInstance(client.policy, paramiko.AutoAddPolicy)
        self.assertIsNotNone(client.connect_kwargs)
        self.assertIn("disabled_algorithms", client.connect_kwargs)
        self.assertNotIn("ciphers", client.connect_kwargs)

    def test_try_paramiko_honors_identity_and_boolean_options(self) -> None:
        class FakeSSHClient:
            instances = []

            def __init__(self):
                self.connect_kwargs = None
                FakeSSHClient.instances.append(self)

            def load_system_host_keys(self):
                pass

            def set_missing_host_key_policy(self, policy):
                pass

            def connect(self, *args, **kwargs):
                self.connect_kwargs = kwargs

            def close(self):
                pass

        connector = self._make_connector(FakePasswordDialog([]))
        host_cfg = HostCfg(
            options={
                "IdentityFile": "~/.ssh/id_ed25519",
                "IdentitiesOnly": "yes",
                "Compression": "yes",
            }
        )

        with mock.patch("ssh_manager.connector.paramiko.SSHClient", FakeSSHClient):
            result = connector._try_paramiko(host_cfg, "secret", (1.0, 1.0, 1.0))

        self.assertTrue(result.ok)
        kwargs = FakeSSHClient.instances[-1].connect_kwargs
        self.assertEqual(kwargs["key_filename"], os.path.expanduser("~/.ssh/id_ed25519"))
        self.assertFalse(kwargs["allow_agent"])
        self.assertFalse(kwargs["look_for_keys"])
        self.assertTrue(kwargs["compress"])

    def test_proxy_jump_builds_stdio_forward_command(self) -> None:
        self.assertEqual(
            _jump_proxy_command("bastion", "db.internal", 22),
            "ssh -o BatchMode=yes -W db.internal:22 ssh://bastion",
        )
        self.assertEqual(
            _jump_proxy_command("a@hop1:2222, b@hop2", "db", 2200),
            "ssh -o BatchMode=yes -W db:2200 -J a@hop1:2222 ssh://b@hop2",
        )
        self.assertIsNone(_jump_proxy_command("none", "db", 22))

    def test_proxy_command_takes_precedence_and_expands_tokens(self) -> None:
        options = {"ProxyCommand": "nc -X 5 -x proxy:1080 %h %p # %r %%", "ProxyJump": "bastion"}
        self.assertEqual(
            _proxy_command_for(options, "db", 22, "alice"),
            "nc -X 5 -x proxy:1080 db 22 # alice %",
        )
        self.assertIsNone(_proxy_command_for({"proxycommand": "none"}, "db", 22, "alice"))
        self.assertIsNone(_proxy_command_for({}, "db", 22, "alice"))

    def test_try_paramiko_passes_proxy_socket(self) -> None:
        class FakeSSHClient:
            instances = []

            def __init__(self):
                self.connect_kwargs = None
                FakeSSHClient.instances.append(self)

            def load_system_host_keys(self):
                pass

            def set_missing_host_key_policy(self, policy):
                pass

            def connect(self, *args, **kwargs):
                self.connect_kwargs = kwargs

            def close(self):
                pass

        connector = self._make_connector(FakePasswordDialog([]))
        host_cfg = HostCfg(options={"proxyjump": "bastion"})
        with mock.patch("ssh_manager.connector.paramiko.SSHClient", FakeSSHClient), mock.patch(
            "ssh_manager.connector.paramiko.ProxyCommand", side_effect=lambda cmd: ("proxy", cmd)
        ):
            result = connector._try_paramiko(host_cfg, "secret", (1.0, 1.0, 1.0))

        self.assertTrue(result.ok)
        sock = FakeSSHClient.instances[-1].connect_kwargs["sock"]
        self.assertEqual(sock, ("proxy", "ssh -o BatchMode=yes -W example.com:22 ssh://bastion"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
