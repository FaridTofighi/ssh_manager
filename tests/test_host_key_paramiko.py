"""Changed host keys on the Paramiko paths: the richer ``HostKeyChanged``, the shared dialog, stats probe."""

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import paramiko

from ssh_manager import known_hosts as KH
from ssh_manager.transfer.backend import HostKeyChanged, UnknownHostKey
from ssh_manager.transfer.settings import TransferSettings

from tests.fake_sftp import FakeSftpServer
from tests.gtk_window_fixture import display_available


class ServerCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.server = FakeSftpServer(Path(self.tmp.name))
        self.server.__enter__()
        self.addCleanup(self.server.__exit__)
        self.known = Path(self.tmp.name) / "known_hosts"
        self.name = f"[127.0.0.1]:{self.server.port}"
        self.old = paramiko.ECDSAKey.generate()
        self.known.write_text(f"# first\nother.example ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLs"
                              f"abgH5C9okWi0dh2l9GKJl\n{self.name} {self.old.get_name()} {self.old.get_base64()}\n")

    def backend(self, **kwargs):
        from ssh_manager.transfer.sftp import SftpBackend

        backend = SftpBackend("127.0.0.1", self.server.port, "alice", "s3cret pass", known_hosts=self.known,
                              allow_agent=False, settings=TransferSettings(timeout_read=5), **kwargs)
        self.addCleanup(backend.close)
        return backend


class HostKeyChangedTests(ServerCase):
    def test_the_error_carries_what_the_dialog_needs(self) -> None:
        from ssh_manager.transfer.sftp import key_fingerprint

        with self.assertRaises(HostKeyChanged) as caught:
            self.backend().connect()
        error = caught.exception
        self.assertEqual((error.hostname, error.port, error.key_type), ("127.0.0.1", self.server.port,
                                                                        self.server.host_key.get_name()))
        self.assertEqual(error.fingerprint, key_fingerprint(self.server.host_key))
        self.assertEqual(error.new_fingerprint, error.fingerprint)
        self.assertEqual(error.expected, key_fingerprint(self.old))
        self.assertEqual(error.old_fingerprints, (key_fingerprint(self.old),))
        self.assertEqual((Path(error.known_hosts_path), error.line_no), (self.known, 3))
        self.assertTrue(error.message)  # the existing message is kept

    def test_auto_add_still_refuses_a_changed_key(self) -> None:
        before = self.known.read_bytes()
        with self.assertRaises(HostKeyChanged):
            self.backend(host_key_policy="auto-add").connect()
        self.assertEqual(self.known.read_bytes(), before)

    def test_paramiko_client_auto_add_refuses_a_changed_key_too(self) -> None:
        client = paramiko.SSHClient()
        client.load_host_keys(str(self.known))
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        with self.assertRaises(paramiko.BadHostKeyException):
            client.connect("127.0.0.1", self.server.port, "alice", "s3cret pass", allow_agent=False,
                           look_for_keys=False, timeout=5)
        client.close()

    def test_remove_old_key_then_retry_asks_again(self) -> None:
        from ssh_manager.gtkui.dialogs.host_key import changed_key_from_error, remove_old_key

        with self.assertRaises(HostKeyChanged) as caught:
            self.backend().connect()
        change = changed_key_from_error(caught.exception, "local")
        before = self.known.read_bytes()
        self.assertFalse(remove_old_key(None, change, confirm=lambda c, e: False, message=lambda *a: None))
        self.assertEqual(self.known.read_bytes(), before)
        self.assertTrue(remove_old_key(None, change, confirm=lambda c, e: e.line_no == 3, message=lambda *a: None))
        self.assertEqual(self.known.read_bytes(),
                         before.replace(f"{self.name} {self.old.get_name()} {self.old.get_base64()}\n".encode(), b""))
        self.assertEqual(len(KH.backups(self.known)), 1)
        with self.assertRaises(UnknownHostKey):  # never trusted automatically: the user is asked again
            self.backend().connect()


@unittest.skipUnless(display_available(), "needs a display")
class DialogTests(ServerCase):
    def test_shared_dialog_details_remove_and_retry(self) -> None:
        from gi.repository import Gtk  # type: ignore

        from ssh_manager.gtkui.dialogs import host_key as HK

        with self.assertRaises(HostKeyChanged) as caught:
            self.backend().connect()
        change = HK.changed_key_from_error(caught.exception, "local")
        details = HK.HostKeyDetailsDialog(None, change)
        self.addCleanup(details.destroy)
        self.assertEqual(details.rows["Stored key"].split()[1], KH.fingerprint_sha256(self.old.get_base64()))
        self.assertIn(":3", details.rows["Stored in"])
        responses = iter([HK.RESPONSE_DETAILS, HK.RESPONSE_REMOVE])
        retried = []
        with mock.patch.object(HK.BaseDialog, "run_and_destroy",
                               side_effect=lambda *a: next(responses, Gtk.ResponseType.CLOSE)), \
                mock.patch.object(HK, "show_details") as shown, \
                mock.patch.object(HK, "confirm_remove", return_value=True):
            result = HK.show_host_key_changed(None, change, on_retry=lambda: retried.append(1))
        shown.assert_called_once()
        self.assertEqual((result, retried), ("retried", [1]))
        self.assertNotIn(self.old.get_base64().encode(), self.known.read_bytes())

    def test_the_file_browser_uses_the_shared_dialog(self) -> None:
        from ssh_manager.gtkui.file_browser import tab as FB

        with self.assertRaises(HostKeyChanged) as caught:
            self.backend().connect()
        fake = mock.Mock()
        fake._closed = False
        fake._host.alias = "local"
        fake._window.return_value = None
        with mock.patch("ssh_manager.gtkui.dialogs.host_key.show_host_key_changed") as shared:
            FB.FileBrowserTab._connect_failed(fake, mock.Mock(), caught.exception, "pw")
            FB.FileBrowserTab._host_key_changed(fake, caught.exception, "pw")
        fake._set_status.assert_called()
        self.assertEqual(shared.call_args.args[1].line_no, 3)


@unittest.skipUnless(display_available(), "needs a display")
class StatsProbeTests(unittest.TestCase):
    def test_no_dialog_one_log_per_host(self) -> None:
        from ssh_manager import gtk_terminal as GT
        from ssh_manager.config import HostEntry

        logs = []
        tab = GT.TerminalTab(HostEntry(alias="probe-host", options={"hostname": "127.0.0.1"}),
                             lambda text, level=None: logs.append((text, level)), autostart=False)
        self.addCleanup(tab.destroy)
        GT._HOST_KEY_WARNED.discard("probe-host")
        failed = subprocess.CompletedProcess([], 255, "", "@@@\n@    WARNING: REMOTE HOST IDENTIFICATION HAS "
                                                          "CHANGED!     @\nHost key verification failed.\n")
        with mock.patch.object(GT.subprocess, "run", return_value=failed), \
                mock.patch.object(GT, "build_remote_stats_command", return_value=(["ssh"], {})), \
                mock.patch.object(tab, "_stored_stats_password", return_value=None), \
                mock.patch.object(GT.Gtk.Dialog, "run", side_effect=AssertionError("no dialog")):
            for _ in range(3):
                data = tab._fetch_stats_via_ssh_command()
                self.assertEqual(data, {"error": GT.STATS_HOST_KEY_CHANGED})
                tab._stats_updating = True
                tab._apply_stats_result(data)
        self.assertEqual(tab._cpu_label.get_text(), "Host key changed")
        warnings = [text for text, level in logs if "host key has changed" in text]
        self.assertEqual(len(warnings), 1)


if __name__ == "__main__":
    unittest.main()
