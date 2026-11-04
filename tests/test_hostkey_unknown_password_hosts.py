"""First connection to a host with a saved password: the host-key trust question instead of a failure.

Measured (tools/diag_hostkey.py against a throw-away server, empty known_hosts): ``sshpass -e ssh`` exits
with status 6 ("host public key is unknown") and prints nothing — sshpass stops at ssh's "Are you sure you
want to continue connecting" before anyone can answer — so every password host whose key is not in
known_hosts yet ended as "Host key verification failed for <alias>; check the server's key."
(``ssh -o BatchMode=yes`` ends with status 255 and "Host key verification failed.")

The in-process test server (tests/fake_sftp.py) and a temporary known_hosts per host
(``UserKnownHostsFile``) keep the user's own ~/.ssh out of it.
"""

import shutil
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.gtk_window_fixture import display_available
from tests.test_local_terminal import pump

PASSWORD = "Hostkey-sentinel-pw"


def status(tab) -> str:
    return tab._status_label.get_text()


class ServerCase(unittest.TestCase):
    def setUp(self) -> None:
        from tests.fake_sftp import FakeSftpServer

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "srv").mkdir()
        self.server = FakeSftpServer(self.root / "srv", users={"alice": PASSWORD})
        self.server.__enter__()
        self.addCleanup(self.server.__exit__)
        self.known = self.root / "ssh" / "known_hosts"  # does not exist yet: a fresh computer
        self.name = f"[127.0.0.1]:{self.server.port}"

    def host(self, **options):
        from ophion.config import HostEntry

        base = {"hostname": "127.0.0.1", "port": str(self.server.port), "user": "alice",
                "UserKnownHostsFile": str(self.known), "GlobalKnownHostsFile": "/dev/null",
                "PubkeyAuthentication": "no", "PreferredAuthentications": "password"}
        base.update(options)
        return HostEntry(alias="pw-host", options=base)

    def argv(self, **options):
        from ophion.ssh_command import build_ssh_command

        return build_ssh_command(self.host(**options))

    def stop_server(self) -> None:
        """Nothing listens on the port any more (the cleanup's own stop then does nothing)."""

        self.server.__exit__(None, None, None)
        self.server.__exit__ = lambda *_args: None

    def server_line(self, key=None) -> str:
        key = key or self.server.host_key
        return f"{key.get_name()} {key.get_base64()}"

    def trusted_by_ssh(self) -> bool:
        found = subprocess.run(["ssh-keygen", "-F", self.name, "-f", str(self.known)], capture_output=True, text=True)
        return self.server.host_key.get_base64() in found.stdout


@unittest.skipUnless(shutil.which("ssh"), "needs ssh")
class PreflightTests(ServerCase):
    def test_unknown_key_is_read_without_logging_in(self) -> None:
        from ophion import host_key_preflight as P
        from ophion.known_hosts import fingerprint_sha256

        result = P.check(self.argv())
        self.assertEqual(result.state, P.UNKNOWN)
        self.assertEqual((result.key_type, result.fingerprint),
                         (self.server.host_key.get_name(), fingerprint_sha256(self.server.host_key.get_base64())))
        self.assertEqual(self.server.password_attempts, 0)  # no credential is ever sent
        self.assertFalse(self.known.exists())

    def test_already_trusted_plain_hashed_and_with_port(self) -> None:
        from ophion import host_key_preflight as P
        from ophion.known_hosts import hashed_host

        self.known.parent.mkdir()
        for line in (f"{self.name} {self.server_line()}",
                     f"{hashed_host('127.0.0.1', self.server.port)} {self.server_line()}"):
            self.known.write_text(line + "\n")
            self.assertEqual(P.check(self.argv()).state, P.KNOWN, line)
        self.known.write_text(f"web-alias {self.server_line()}\n")  # HostKeyAlias: the alias, without the port
        self.assertEqual(P.check(self.argv(HostKeyAlias="web-alias")).state, P.KNOWN)

    def test_users_own_strict_choice_and_jump_hosts_are_left_to_ssh(self) -> None:
        from ophion import host_key_preflight as P

        for value in ("yes", "no", "accept-new"):
            self.assertEqual(P.check(self.argv(StrictHostKeyChecking=value)).state, P.SKIP, value)
        probe = mock.Mock()
        self.assertEqual(P.check(self.argv(ProxyJump="jump.example"), probe=probe).state, P.PROXIED)
        probe.assert_not_called()  # the key cannot be read past the jump host

    def test_an_unreachable_server_is_reported_quickly(self) -> None:
        from ophion import host_key_preflight as P

        self.stop_server()
        started = time.monotonic()
        result = P.check(self.argv())
        self.assertEqual(result.state, P.UNREADABLE)
        self.assertLess(time.monotonic() - started, 8)
        self.assertTrue(result.error)

    def test_trusting_writes_the_key_like_ssh_would(self) -> None:
        from ophion import host_key_preflight as P

        result = P.check(self.argv())
        P.trust(result)
        self.assertTrue(self.trusted_by_ssh())  # ssh-keygen -F finds it (hashed or not, as HashKnownHosts says)
        self.assertEqual(stat.S_IMODE(self.known.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.known.parent.stat().st_mode), 0o700)
        self.assertEqual(P.check(self.argv()).state, P.KNOWN)


class ClassifyTests(unittest.TestCase):
    def test_sshpass_status_6_is_an_unknown_key_not_a_failure(self) -> None:
        from ophion.auth_flow import AuthOutcome, classify_exit

        self.assertIs(classify_exit(6, ""), AuthOutcome.HOSTKEY_UNKNOWN)
        self.assertIs(classify_exit(255, "Host key verification failed.\r\n"), AuthOutcome.HOSTKEY)
        self.assertIs(classify_exit(255, "@ WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! @"), AuthOutcome.HOSTKEY)
        self.assertIs(classify_exit(5, "Permission denied, please try again."), AuthOutcome.AUTH_FAILED)


@unittest.skipUnless(display_available() and shutil.which("ssh") and shutil.which("sshpass"),
                     "needs a display, ssh and sshpass")
class PasswordSessionTests(ServerCase):
    def setUp(self) -> None:
        super().setUp()
        from tests.gtk_window_fixture import WindowSandbox

        from ophion.gtk_terminal import TerminalTab

        self.sandbox = WindowSandbox(store=True)
        self.addCleanup(self.sandbox.close)
        self.tabs = self.sandbox.window.terminal_tabs
        patcher = mock.patch.object(TerminalTab, "_password_for_attempt", return_value=(True, PASSWORD))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.spawned = []
        real = TerminalTab._start_child
        patcher = mock.patch.object(TerminalTab, "_start_child",
                                    lambda tab, argv, envv: (self.spawned.append(list(argv)), real(tab, argv, envv)))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.logs = []

    def tearDown(self) -> None:
        for tab in list(self.tabs.tabs()):
            self.tabs.close_tab(tab)
        pump(0.3)

    def open(self, answer, **options):
        tab = self.tabs.new_terminal(self.host(**options), autostart=False)
        self.asked = []
        tab.confirm_host_key = lambda alias, key_type, fingerprint: self.asked.append((alias, key_type, fingerprint)) \
            or answer
        real_log = tab._log
        tab._log = lambda message, level: (self.logs.append((message, level)), real_log(message, level))
        tab._begin_connect()
        return tab

    def test_trusting_the_key_saves_it_and_the_password_logs_in(self) -> None:
        from ophion.known_hosts import fingerprint_sha256

        tab = self.open(True)
        self.assertTrue(pump(15, until=lambda: self.server.sessions_opened > 0), "no login")
        self.assertEqual(self.asked, [("pw-host", self.server.host_key.get_name(),
                                       fingerprint_sha256(self.server.host_key.get_base64()))])
        self.assertTrue(self.trusted_by_ssh())
        self.assertEqual(stat.S_IMODE(self.known.stat().st_mode), 0o600)
        self.assertEqual(self.spawned[0][:2], ["sshpass", "-e"])
        self.assertFalse(any(PASSWORD in part for argv in self.spawned for part in argv))
        self.assertFalse(any(PASSWORD in message for message, _level in self.logs))
        self.assertIsNotNone(tab)

    def test_cancelling_writes_nothing_and_is_not_an_error(self) -> None:
        from ophion.gtkui.theme.tokens import Level

        tab = self.open(False)
        self.assertTrue(pump(10, until=lambda: status(tab) == "Connection cancelled"), status(tab))
        self.assertEqual(len(self.asked), 1)
        self.assertFalse(self.known.exists())
        self.assertEqual(self.spawned, [])
        self.assertFalse([m for m, level in self.logs if level is Level.ERROR])

    def test_an_already_trusted_key_asks_nothing(self) -> None:
        from ophion.known_hosts import hashed_host

        self.known.parent.mkdir()
        self.known.write_text(f"{hashed_host('127.0.0.1', self.server.port)} {self.server_line()}\n")
        self.open(True)
        self.assertTrue(pump(15, until=lambda: self.server.sessions_opened > 0), "no login")
        self.assertEqual(self.asked, [])

    def test_a_changed_key_is_refused_without_a_trust_offer(self) -> None:
        import paramiko

        self.known.parent.mkdir()
        self.known.write_text(f"{self.name} {self.server_line(paramiko.ECDSAKey.generate())}\n")
        tab = self.open(True)
        self.assertTrue(pump(15, until=lambda: status(tab) == "Host key changed"), status(tab))
        self.assertEqual(self.asked, [])
        self.assertEqual(self.server.password_attempts, 0)

    def test_an_unreadable_server_says_so_and_asks_nothing(self) -> None:
        self.stop_server()
        tab = self.open(True)
        self.assertTrue(pump(12, until=lambda: status(tab) == "Connection failed"), status(tab))
        self.assertEqual(self.asked, [])
        self.assertTrue(any("could not read the server's key" in m.lower() for m, _level in self.logs), self.logs)
        self.assertFalse(self.known.exists())


class AuxiliaryCommandTests(unittest.TestCase):
    def test_background_commands_never_ask_and_say_the_same(self) -> None:
        from ophion.host_key_preflight import untrusted_text
        from ophion.multirun.engine import HOST_KEY_UNKNOWN, _classify
        from ophion.transfer.backend import UnknownHostKey

        status, message = _classify(UnknownHostKey(hostname="h", port=22, key_type="ssh-ed25519", fingerprint="SHA256:x"))
        self.assertEqual(status, HOST_KEY_UNKNOWN)
        self.assertTrue(message.startswith(untrusted_text()), message)
        self.assertEqual(untrusted_text(), "Host key not trusted yet — open the session once to accept it.")

    def test_stats_report_an_untrusted_key(self) -> None:
        from ophion.remote_stats import STATS_HOST_KEY_UNKNOWN, stats_failure

        self.assertEqual(stats_failure(6, ""), STATS_HOST_KEY_UNKNOWN)  # sshpass: unknown key
        self.assertEqual(stats_failure(255, "Host key verification failed.\n"), STATS_HOST_KEY_UNKNOWN)  # BatchMode
        self.assertIsNone(stats_failure(255, "Connection refused"))


if __name__ == "__main__":
    unittest.main()
