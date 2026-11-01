"""The terminal's host-key banner, end to end: the real ``ssh`` against an in-process server.

Each test host sets ``UserKnownHostsFile`` to a temporary file (ssh finds ``~`` through the passwd
entry, not ``$HOME``): the user's own known_hosts is never read or changed.
"""

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.gtk_window_fixture import display_available
from tests.test_local_terminal import pump


@unittest.skipUnless(display_available() and shutil.which("ssh"), "needs a display and ssh")
class HostKeyBannerTests(unittest.TestCase):
    def setUp(self) -> None:
        import paramiko

        from tests.fake_sftp import FakeSftpServer
        from tests.gtk_window_fixture import WindowSandbox

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.server = FakeSftpServer(Path(self.tmp.name))
        self.server.__enter__()
        self.addCleanup(self.server.__exit__)
        self.known = Path(self.tmp.name) / "known_hosts"
        self.name = f"[127.0.0.1]:{self.server.port}"
        self.wrong = paramiko.ECDSAKey.generate().get_base64()
        self.sandbox = WindowSandbox(store=True)
        self.addCleanup(self.sandbox.close)
        self.win = self.sandbox.window
        self.win.realize()
        self.tabs = self.win.terminal_tabs
        os.makedirs(os.environ["HOME"], exist_ok=True)
        from ophion.gtk_terminal import TerminalTab

        # No stored password: the tab would ask for one first; these tests go straight to ssh.
        patcher = mock.patch.object(TerminalTab, "_password_for_attempt", return_value=(True, None))
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        for tab in list(self.tabs.tabs()):
            with mock.patch("ophion.gtkui.local_terminal.confirm", return_value=True):
                self.tabs.close_tab(tab)
        pump(0.3)

    def host(self, alias="web"):
        from ophion.config import HostEntry

        return HostEntry(alias=alias, options={
            "hostname": "127.0.0.1", "port": str(self.server.port), "user": "alice",
            "UserKnownHostsFile": str(self.known), "GlobalKnownHostsFile": "/dev/null",
            "StrictHostKeyChecking": "ask", "PreferredAuthentications": "password",
            "NumberOfPasswordPrompts": "0"})

    def changed_file(self) -> bytes:
        self.known.write_text(f"# mine\nkeep.example ssh-ed25519 {self.wrong[:0]}AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0"
                              f"SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl\n{self.name} ecdsa-sha2-nistp256 {self.wrong}\n")
        return self.known.read_bytes()

    def open(self, host):
        tab = self.tabs.new_terminal(host)
        self.addCleanup(lambda: setattr(tab, "_closed_by_test", True))
        return tab

    def test_changed_key_banner_remove_and_reconnect(self) -> None:
        from ophion.gtk_terminal import _RESPONSE_HOST_KEY_REMOVE

        before = self.changed_file()
        tab = self.open(self.host())
        self.assertTrue(pump(15, until=lambda: tab.host_key_banner == "changed"), "no banner")
        change = tab._host_key_change
        self.assertEqual((Path(change.known_hosts_path), change.line_no), (self.known, 3))
        self.assertEqual(change.new_fingerprint.split(":")[0], "SHA256")
        self.assertTrue(tab.session_ended)  # r / q keep working on the ended tab
        self.assertEqual(tab._host_key_bar.get_accessible().get_name(), "SSH host key")
        self.assertTrue(tab._host_key_icon.get_icon_name()[0].startswith("dialog-warning"))
        with mock.patch("ophion.gtkui.dialogs.host_key.confirm_remove", return_value=False) as ask, \
                mock.patch.object(tab, "reconnect") as reconnect:
            tab._host_key_bar.response(_RESPONSE_HOST_KEY_REMOVE)  # Cancel
        ask.assert_called_once()
        reconnect.assert_not_called()
        self.assertEqual(self.known.read_bytes(), before)
        with mock.patch("ophion.gtkui.dialogs.host_key.confirm_remove", return_value=True), \
                mock.patch.object(tab, "reconnect") as reconnect:
            tab._host_key_bar.response(_RESPONSE_HOST_KEY_REMOVE)
        reconnect.assert_called_once()
        self.assertEqual(self.known.read_bytes(),
                         before.replace(f"{self.name} ecdsa-sha2-nistp256 {self.wrong}\n".encode(), b""))
        self.assertEqual(tab.host_key_banner, "")

    def test_a_line_that_is_no_longer_the_hosts_is_refused(self) -> None:
        self.changed_file()
        tab = self.open(self.host())
        self.assertTrue(pump(15, until=lambda: tab.host_key_banner == "changed"))
        self.known.write_text(f"{self.name} ecdsa-sha2-nistp256 {self.wrong}\n{self.name} ssh-rsa "
                              "AAAAB3NzaC1yc2EAAAADAQABAAAAgQC7\n")  # moved, and two candidates now
        before = self.known.read_bytes()
        messages = []
        with mock.patch("ophion.gtkui.dialogs.host_key.show_message",
                        side_effect=lambda _p, title, text: messages.append(text)), \
                mock.patch("ophion.gtkui.dialogs.host_key.confirm_remove", return_value=True), \
                mock.patch.object(tab, "reconnect") as reconnect:
            self.assertFalse(tab.remove_host_key(None, tab._host_key_change))
        reconnect.assert_not_called()
        self.assertEqual(self.known.read_bytes(), before)
        self.assertTrue(messages)

    def test_unknown_key_prompt_shows_the_fingerprint(self) -> None:
        self.known.write_text("")
        tab = self.open(self.host())
        self.assertTrue(pump(15, until=lambda: tab.host_key_banner == "unknown"), "no prompt banner")
        self.assertRegex(tab._host_key_prompt.fingerprint, r"^SHA256:")
        self.assertIn(tab._host_key_prompt.fingerprint, tab._host_key_label.get_text())
        self.assertFalse(tab._host_key_buttons["remove"].get_visible())  # no "trust" either: ssh asks
        self.assertEqual(self.known.read_text(), "")

    def test_broadcast_only_the_affected_tab(self) -> None:
        self.changed_file()
        local = self.tabs.new_local_terminal()
        self.assertTrue(pump(5, until=lambda: local._shell_pid is not None))
        self.tabs._on_broadcast_include_toggled(local, True)
        tab = self.open(self.host())
        self.tabs.set_broadcast_mode(True)
        self.assertTrue(pump(15, until=lambda: tab.host_key_banner == "changed"))
        self.assertEqual(local.host_key_banner, "")
        with mock.patch("ophion.gtkui.dialogs.host_key.confirm_remove", return_value=True), \
                mock.patch.object(tab, "reconnect") as reconnect, mock.patch.object(local, "reconnect") as other:
            tab._host_key_bar.response(13)  # Copy (nothing to copy here) and Remove act on this tab only
            from ophion.gtk_terminal import _RESPONSE_HOST_KEY_REMOVE
            tab._host_key_bar.response(_RESPONSE_HOST_KEY_REMOVE)
        reconnect.assert_called_once()
        other.assert_not_called()


if __name__ == "__main__":
    unittest.main()
