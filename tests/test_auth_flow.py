"""Phase 1: safe password handling — classification, sentinel, resolution, no silent writes or deletes."""

import logging
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ssh_manager.auth_flow import (
    MAX_ATTEMPTS,
    AuthOutcome,
    PasswordResolution,
    classify_exit,
    ends_with_password_prompt,
    probably_authenticated,
    sentinel_options,
    with_sentinel,
)
from ssh_manager.config import HostEntry
from ssh_manager.credentials import CredentialStore
from ssh_manager.secure_store import InMemoryKeyring, SecureStore

SECRET = "pa ss'w$o`rd%é"


class RecordingKeyring(InMemoryKeyring):
    """Counts every write and delete."""

    def __init__(self) -> None:
        super().__init__()
        self.writes = []

    def set_password(self, service, username, password):
        self.writes.append(("set", username))
        return super().set_password(service, username, password)

    def delete_password(self, service, username):
        self.writes.append(("delete", username))
        return super().delete_password(service, username)


class FakeDialog:
    """Stands in for PasswordRequestDialog / SavePasswordDialog: records the request, answers on demand."""

    def __init__(self, request, on_done) -> None:
        self.request = request
        self.on_done = on_done
        self.dismissed = False

    def dismiss(self) -> None:
        self.dismissed = True


class ClassifyTests(unittest.TestCase):
    TABLE = [
        (5, "", AuthOutcome.AUTH_FAILED),  # sshpass: wrong password
        (255, "Permission denied, please try again.", AuthOutcome.AUTH_FAILED),
        (255, "alice@h: Permission denied (publickey,password).", AuthOutcome.AUTH_FAILED),
        (255, "Permission denied (publickey,keyboard-interactive).\r\nAuthentication failed.", AuthOutcome.AUTH_FAILED),
        (255, "\x1b[1;31mPermission denied, please try again.\x1b[0m", AuthOutcome.AUTH_FAILED),
        (255, "Permission denied (publickey).", AuthOutcome.NO_PASSWORD_METHOD),
        (255, "Permission denied (publickey,gssapi-with-mic).", AuthOutcome.NO_PASSWORD_METHOD),
        (6, "", AuthOutcome.HOSTKEY),
        (255, "Host key verification failed.", AuthOutcome.HOSTKEY),
        (255, "@@@ WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! @@@", AuthOutcome.HOSTKEY),
        (255, "ssh: connect to host h port 22: Connection refused", AuthOutcome.NETWORK),
        (255, "ssh: connect to host h port 22: Connection timed out", AuthOutcome.NETWORK),
        (255, "ssh: connect to host h port 22: No route to host", AuthOutcome.NETWORK),
        (255, "ssh: Could not resolve hostname h: Name or service not known", AuthOutcome.NETWORK),
        (255, "Connection closed by 10.0.0.1 port 22", AuthOutcome.NETWORK),
        (255, "ssh: connect to host otp.example.com port 22: Connection refused", AuthOutcome.NETWORK),
        (255, "Received disconnect from h: Too many authentication failures", AuthOutcome.OTHER),
        (255, "Verification code: ", AuthOutcome.OTP_REQUIRED),
        (255, "One-time password (OATH) for `alice': ", AuthOutcome.OTP_REQUIRED),
        (0, "logout", AuthOutcome.OK),
        (3, "", AuthOutcome.OTHER),
        (4, "", AuthOutcome.OTHER),
    ]

    def test_truth_table(self) -> None:
        for status, output, expected in self.TABLE:
            with self.subTest(status=status, output=output):
                self.assertIs(classify_exit(status, output), expected)

    def test_only_auth_failed_may_prompt(self) -> None:
        prompting = [row for row in self.TABLE if row[2] is AuthOutcome.AUTH_FAILED]
        self.assertTrue(all(status == 5 or "denied" in output.lower() or "failed" in output.lower()
                            for status, output, _o in prompting))

    def test_heuristic(self) -> None:
        self.assertTrue(probably_authenticated("Last login: today\r\n$ ", alive_s=4, output_after_password=True))
        self.assertFalse(probably_authenticated("alice@h's password: ", alive_s=9, output_after_password=True))
        self.assertFalse(probably_authenticated("Permission denied, please try again.", alive_s=9,
                                                output_after_password=True))
        self.assertFalse(probably_authenticated("$ ", alive_s=1, output_after_password=True))
        self.assertFalse(probably_authenticated("$ ", alive_s=9, output_after_password=False))
        self.assertTrue(ends_with_password_prompt("\r\nalice@h's password: "))


class SentinelTests(unittest.TestCase):
    def test_quoting_and_percent(self) -> None:
        marker = Path("/run/user/1000/ssh-manager/a b'%c.ok")
        options, method = sentinel_options({}, marker)
        self.assertEqual(method, "sentinel")
        self.assertEqual(options[:3], ["-o", "PermitLocalCommand=yes", "-o"])
        self.assertEqual(options[3], "LocalCommand=touch -- '/run/user/1000/ssh-manager/a b'\"'\"'%%c.ok'")

    def test_user_local_command_is_kept(self) -> None:
        options, method = sentinel_options({"LocalCommand": "notify-send hi"}, Path("/x/m.ok"))
        self.assertTrue(options[3].endswith("; notify-send hi"))
        self.assertEqual(method, "sentinel")

    def test_permit_local_command_no_skips_the_sentinel(self) -> None:
        self.assertEqual(sentinel_options({"permitlocalcommand": "no"}, Path("/x/m.ok")), ([], "heuristic"))

    def test_inserted_right_after_ssh(self) -> None:
        argv = ["ssh", "-p", "2222", "-J", "gw", "alice@h"]
        self.assertEqual(with_sentinel(argv, ["-o", "X=1"]), ["ssh", "-o", "X=1", "-p", "2222", "-J", "gw", "alice@h"])
        self.assertEqual(with_sentinel(argv, []), argv)

    def test_resolution_repr_hides_the_password(self) -> None:
        text = repr(PasswordResolution(SECRET, "credential", "c1")) + str(PasswordResolution(SECRET, "vault"))
        self.assertNotIn(SECRET, text)


class ResolutionTests(unittest.TestCase):
    """CredentialController.resolve_password: read-only, ordered."""

    def setUp(self) -> None:
        from ssh_manager.gtkui.controllers.credentials_controller import CredentialController

        self.tmp = tempfile.TemporaryDirectory()
        self.keyring = RecordingKeyring()
        self.secure = SecureStore(self.keyring)
        self.creds = CredentialStore(self.secure, path=Path(self.tmp.name) / "credentials.json")
        self.cred = self.creds.add("Ops", "ops", "credential-pw")
        self.win = SimpleNamespace(_credential_store=self.creds, _secure_store=self.secure, _vault=None,
                                   _notify=lambda *a, **k: None, manager=SimpleNamespace())
        self.controller = CredentialController(self.win)
        self.host = HostEntry("web", {"hostname": "web.internal", "user": "alice"}, parent_group="p", child_group="c")
        self.keyring.writes.clear()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_credential_then_legacy_then_none(self) -> None:
        self.assertEqual(self.controller.resolve_password(self.host).source, "none")
        self.secure.set("web.internal", 22, "alice", "legacy-pw")
        self.assertEqual(self.controller.resolve_password(self.host), PasswordResolution("legacy-pw", "legacy"))
        self.creds.bind_host("p::c::web", self.cred.id)
        resolution = self.controller.resolve_password(self.host)
        self.assertEqual((resolution.password, resolution.source, resolution.credential_id),
                         ("credential-pw", "credential", self.cred.id))

    def test_unavailable_credential_notifies_without_the_password(self) -> None:
        from ssh_manager.secure_store import SecureStoreError

        notes = []
        self.win._notify = lambda message, *a, **k: notes.append(message)
        self.creds.bind_host("p::c::web", self.cred.id)
        with mock.patch.object(self.creds, "get_password", side_effect=SecureStoreError("locked")), \
                self.assertLogs("ssh_manager", level=logging.WARNING) as captured:
            self.assertEqual(self.controller.resolve_password(self.host).source, "none")
        self.assertEqual(notes, ["Credential 'Ops' is unavailable."])
        self.assertNotIn("credential-pw", "\n".join(notes + captured.output))

    def test_connecting_twice_never_rewrites_the_legacy_secret(self) -> None:
        self.secure.set("web.internal", 22, "ops", "my-own-password")
        self.creds.bind_host("p::c::web", self.cred.id)
        self.keyring.writes.clear()
        for _ in range(2):
            host, resolution = self.controller.resolve_host_credentials(self.host)
            self.assertEqual(resolution.source, "credential")
        self.assertEqual(self.keyring.writes, [])
        self.assertEqual(self.secure.get("web.internal", 22, "ops"), "my-own-password")
        self.assertEqual(host.options["user"], "ops")


def _display() -> bool:
    try:
        import gi

        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk  # type: ignore

        return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")) and Gtk.init_check(None)[0]
    except Exception:
        return False


@unittest.skipUnless(_display(), "needs a display")
class TerminalAuthTests(unittest.TestCase):
    """The terminal's attempt handling with VTE's spawn mocked (no ssh process)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.runtime = mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": self.tmp.name})
        self.runtime.start()
        self.keyring = RecordingKeyring()
        self.secure = SecureStore(self.keyring)
        self.creds_path = Path(self.tmp.name) / "credentials.json"
        self.creds = CredentialStore(self.secure, path=self.creds_path)
        self.cred = self.creds.add("Ops", "ops", SECRET)
        self.keyring.writes.clear()
        self.logs = []
        self.which = mock.patch("ssh_manager.gtk_terminal.shutil.which", return_value="/usr/bin/sshpass")
        self.which.start()
        self.kill = mock.patch("ssh_manager.gtk_terminal.os.kill")
        self.kill.start()

    def tearDown(self) -> None:
        mock.patch.stopall()
        self.tmp.cleanup()

    def tab(self, resolution=None):
        from ssh_manager.gtk_terminal import TerminalTab

        resolution = resolution or PasswordResolution(SECRET, "credential", self.cred.id)
        tab = TerminalTab(HostEntry("web", {"hostname": "web.internal", "user": "ops"}),
                          lambda message, level: self.logs.append(message), autostart=False,
                          password_store=self.secure, password_provider=lambda: resolution)
        tab._terminal.spawn_async = mock.Mock()
        self.addCleanup(tab.destroy)
        return tab

    @staticmethod
    def finish_spawn(tab, pid=4242):
        call = tab._terminal.spawn_async.call_args[0]
        call[9](tab._terminal, pid, None, None)
        return call[2], call[3]

    def spawn(self, tab, *args, pid=4242, **kwargs):
        tab._spawn(*args, **kwargs)
        call = tab._terminal.spawn_async.call_args[0]
        call[9](tab._terminal, pid, None, None)  # the spawn callback
        return call[2], call[3]  # argv, envv

    def snapshot(self):
        return dict(self.keyring._store), self.creds_path.read_bytes()

    @staticmethod
    def fire_retry(tab):
        """Run the pending retry timer now, as GLib would (its source is consumed, not leaked)."""

        from gi.repository import GLib

        GLib.source_remove(tab._retry_timeout_id)
        tab._prompt_retry_after_failure()

    def exit(self, tab, output, status=255 << 8):
        with mock.patch.object(type(tab), "_auth_failure_output", return_value=output):
            tab._on_child_exited(tab._terminal, status)

    def test_ssh_and_sshpass_are_found_through_path(self) -> None:
        # argv names "sshpass" / "ssh" without a folder: older VTE (0.68, Ubuntu 22.04) only searches PATH when
        # asked, so a bundled or non-/usr/bin ssh would fail with "Failed to execve".
        from gi.repository import GLib

        tab = self.tab()
        tab._spawn()
        flags = tab._terminal.spawn_async.call_args[0][4]
        self.assertTrue(flags & GLib.SpawnFlags.SEARCH_PATH)
        pane = mock.Mock()
        tab._spawn_secondary(pane)
        self.assertTrue(pane.spawn_async.call_args[0][4] & GLib.SpawnFlags.SEARCH_PATH)

    def test_argv_has_the_sentinel_and_never_the_password(self) -> None:
        tab = self.tab()
        argv, envv = self.spawn(tab)
        self.assertEqual(argv[:3], ["sshpass", "-e", "ssh"])
        self.assertIn("PermitLocalCommand=yes", argv)
        self.assertNotIn(SECRET, " ".join(argv))
        self.assertIn(f"SSHPASS={SECRET}", envv)
        self.assertEqual(tab._auth_method, "sentinel")
        self.assertTrue(str(tab._marker).startswith(os.path.join(self.tmp.name, "ssh-manager")))
        self.assertEqual(oct(os.stat(tab._marker.parent).st_mode & 0o777), "0o700")

    def test_rejected_credential_password_changes_no_store(self) -> None:
        tab = self.tab()
        self.spawn(tab)
        before = self.snapshot()
        self.exit(tab, "Permission denied, please try again.")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.keyring.writes, [])
        self.assertTrue(tab._retry_timeout_id)  # asks again (after ≥ 1 s)
        self.assertFalse(tab.authenticated)

    def test_wrong_retry_password_persists_nothing(self) -> None:
        tab = self.tab()
        self.spawn(tab, "typed-wrong", password_origin="retry")
        self.exit(tab, "", status=5 << 8)  # sshpass: wrong password
        self.assertEqual(self.keyring.writes, [])
        self.assertIsNone(tab._session_password)

    def test_verified_success_keeps_a_typed_password_for_this_tab_only(self) -> None:
        tab = self.tab()
        self.spawn(tab, "typed-right", password_origin="retry")
        self.assertFalse(tab.authenticated)  # spawned is not authenticated
        tab._marker.write_text("")  # what LocalCommand does after a successful login
        tab._poll_marker()
        self.assertTrue(tab.authenticated)
        self.assertEqual(tab._session_password, "typed-right")
        self.assertEqual(self.keyring.writes, [])
        argv, envv = self.spawn(tab)  # reconnect in this tab uses it first
        self.assertIn("SSHPASS=typed-right", envv)
        self.assertEqual(tab._password_source, "session")

    def test_max_three_attempts_then_stop(self) -> None:
        tab = self.tab()
        dialogs = []
        with mock.patch("ssh_manager.gtk_terminal.PasswordRequestDialog",
                        side_effect=lambda parent, request, on_done: dialogs.append(FakeDialog(request, on_done))
                        or dialogs[-1]):
            tab._begin_connect()  # attempt with the credential's password (no prompt)
            for attempt in range(1, MAX_ATTEMPTS + 1):
                self.finish_spawn(tab)
                self.exit(tab, "Permission denied, please try again.")
                self.assertTrue(tab._retry_timeout_id, attempt)
                self.fire_retry(tab)
                self.assertEqual(dialogs[-1].request.attempt, attempt)
                dialogs[-1].on_done("again")  # Retry with a typed password
            self.finish_spawn(tab)
            self.exit(tab, "Permission denied, please try again.")
        self.assertEqual(len(dialogs), MAX_ATTEMPTS)
        self.assertEqual(dialogs[0].request.rejected, "saved password (credential)")
        self.assertEqual(dialogs[1].request.rejected, "password you entered")
        self.assertEqual(tab._attempts, MAX_ATTEMPTS)
        self.assertFalse(tab._retry_timeout_id)
        self.assertTrue(any("after 3 attempts" in message for message in self.logs))
        tab._begin_connect()  # a new connect resets the budget
        self.assertEqual(tab._attempts, 0)

    def test_non_auth_failures_never_ask_for_a_password(self) -> None:
        for output, status in (("Host key verification failed.", 255), ("", 6),
                               ("ssh: connect to host h port 22: Connection refused", 255),
                               ("Permission denied (publickey).", 255), ("Verification code: ", 255)):
            with self.subTest(output=output):
                tab = self.tab()
                self.spawn(tab)
                self.exit(tab, output, status=status << 8)
                self.assertFalse(tab._retry_timeout_id)
                self.assertEqual(self.keyring.writes, [])
        self.assertTrue(any("does not accept password login" in message for message in self.logs))

    def test_no_password_in_logs(self) -> None:
        with self.assertLogs("ssh_manager", level=logging.DEBUG) as captured:
            logging.getLogger("ssh_manager").debug("start")
            tab = self.tab()
            self.spawn(tab, "typed", password_origin="retry")
            self.exit(tab, "Permission denied, please try again.")
            tab2 = self.tab()
            self.spawn(tab2)
            tab2._marker.write_text("")
            tab2._poll_marker()
        text = "\n".join(captured.output + self.logs)
        self.assertNotIn(SECRET, text)
        self.assertNotIn("typed", text)

    def test_destroy_removes_every_source_and_the_marker(self) -> None:
        from ssh_manager.gtk_terminal import TerminalTab

        tab = TerminalTab(HostEntry("web", {"hostname": "web.internal"}), lambda *a: None, autostart=False,
                          password_provider=lambda: PasswordResolution(SECRET, "credential"))
        tab._terminal.spawn_async = mock.Mock()
        self.spawn(tab)
        tab._schedule_retry_prompt()
        marker = tab._marker
        marker.write_text("")
        self.assertTrue(tab._auth_watch_source_id and tab._retry_timeout_id)
        tab.destroy()
        self.assertEqual((tab._auth_watch_source_id, tab._retry_timeout_id), (0, 0))
        self.assertFalse(marker.exists())

    def test_stats_use_the_authenticated_password(self) -> None:
        tab = self.tab(PasswordResolution(None, "none"))
        self.spawn(tab, "typed-right", password_origin="retry")
        tab._marker.write_text("")
        tab._poll_marker()
        self.assertEqual(tab._stored_stats_password(), "typed-right")


class ConnectorTests(unittest.TestCase):
    def test_paramiko_never_deletes_or_rewrites_resolved_passwords(self) -> None:
        import time

        from ssh_manager.connection_state import ConnectionStateMachine
        from ssh_manager.connector import Connector, TryResult
        from tests.test_connector import FakePasswordDialog, HostCfg, Settings

        keyring = RecordingKeyring()
        store = SecureStore(keyring)
        store.set("example.com", 22, "alice", "legacy")
        keyring.writes.clear()
        connector = Connector(FakePasswordDialog([("typed", False)]), store, Settings(), ConnectionStateMachine(),
                              main_invoke=lambda fn: fn(), main_queue=lambda cb, a, k: cb(*a, **(k or {})))
        attempts = []

        def fake_try(_cfg, password, _timeouts):
            attempts.append(password)
            ok = password == "typed"
            return TryResult(ok=ok, is_auth_error=not ok, message="", client="c" if ok else None)

        connector._try_paramiko = fake_try
        connector.connect_to_host(HostCfg(), password_provider=lambda: PasswordResolution("from-credential",
                                                                                         "credential", "c1"))
        for _ in range(40):
            if connector._worker_thread is None:
                break
            time.sleep(0.05)
        self.assertEqual(attempts, ["from-credential", "typed"])
        self.assertEqual(keyring.writes, [])  # rejected: not deleted; typed without "remember": not saved
        self.assertEqual(store.get("example.com", 22, "alice"), "legacy")


if __name__ == "__main__":
    unittest.main()
