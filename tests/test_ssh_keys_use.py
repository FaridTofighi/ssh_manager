"""Using a key in a host: the host dialog's picker, the saved passphrase at ssh's prompt, Paramiko paths."""

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.gtk_window_fixture import display_available
from tests.test_local_terminal import pump

PASS = "Use-key-passphrase-sentinel"


@unittest.skipUnless(display_available() and shutil.which("ssh"), "needs a display and ssh")
class TerminalPassphraseTests(unittest.TestCase):
    """The real ssh asks for the key's passphrase in the tab; the saved one is typed once."""

    def setUp(self) -> None:
        from tests.fake_sftp import FakeSftpServer
        from tests.gtk_window_fixture import WindowSandbox

        from ssh_manager import ssh_keys as K
        from ssh_manager.gtk_terminal import TerminalTab

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.server = FakeSftpServer(root / "srv", users={"alice": "unused"})
        (root / "srv").mkdir()
        self.server.__enter__()
        self.addCleanup(self.server.__exit__)
        self.key = K.generate_key(root / "keys" / "id_ed25519", comment="t", passphrase=PASS)
        (root / "srv" / ".ssh").mkdir(mode=0o700)
        (root / "srv" / ".ssh" / "authorized_keys").write_text(self.key.public_line + "\n")
        self.known = root / "known_hosts"
        key = self.server.host_key
        self.known.write_text(f"[127.0.0.1]:{self.server.port} {key.get_name()} {key.get_base64()}\n")
        self.sandbox = WindowSandbox(store=True)
        self.addCleanup(self.sandbox.close)
        self.tabs = self.sandbox.window.terminal_tabs
        patcher = mock.patch.object(TerminalTab, "_password_for_attempt", return_value=(True, None))
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        for tab in list(self.tabs.tabs()):
            self.tabs.close_tab(tab)
        pump(0.3)

    def open(self, saved):
        from ssh_manager.config import HostEntry

        host = HostEntry(alias="keyed", options={
            "hostname": "127.0.0.1", "port": str(self.server.port), "user": "alice",
            "IdentityFile": str(self.key.path), "IdentitiesOnly": "yes", "PreferredAuthentications": "publickey",
            "UserKnownHostsFile": str(self.known), "GlobalKnownHostsFile": "/dev/null",
            "StrictHostKeyChecking": "yes", "BatchMode": "no"})
        tab = self.tabs.new_terminal(host, autostart=False)
        asked = []
        tab.passphrase_provider = lambda path: asked.append(path) or saved
        fed = []
        real = tab._terminal.feed_child
        tab._terminal.feed_child = lambda data: (fed.append(data), real(data))
        tab._begin_connect()
        return tab, asked, fed

    def test_the_saved_passphrase_is_typed_once_and_logs_in(self) -> None:
        tab, asked, fed = self.open(PASS)
        self.assertTrue(pump(15, until=lambda: self.server.sessions_opened > 0), "no key login")
        self.assertEqual(asked, [str(self.key.path)])
        self.assertEqual(fed, [(PASS + "\n").encode()])

    def test_a_wrong_saved_passphrase_is_never_typed_twice(self) -> None:
        from ssh_manager.gtk_terminal import terminal_text

        tab, asked, fed = self.open("wrong-passphrase")
        self.assertTrue(pump(15, until=lambda: terminal_text(tab._terminal).count("Enter passphrase") >= 2))
        pump(0.5)
        self.assertEqual(len(fed), 1)  # the second prompt is left to the user
        self.assertEqual(self.server.sessions_opened, 0)


@unittest.skipUnless(display_available(), "needs a display")
class HostDialogPickerTests(unittest.TestCase):
    def setUp(self) -> None:
        from tests.gtk_window_fixture import WindowSandbox

        self.sandbox = WindowSandbox(store=True)
        self.addCleanup(self.sandbox.close)
        self.win = self.sandbox.window
        self.win.ssh_keys.ssh_dir = self.sandbox.root / "home" / ".ssh"

    def test_combo_lists_known_keys_lazily_and_saves_identityfile(self) -> None:
        from ssh_manager import ssh_keys as K

        info = K.generate_key(self.win.ssh_keys.ssh_dir / "id_ed25519", comment="t")
        host = next(h for h in self.win.ssh_keys.hosts() if h.alias == "web-01")
        calls = []
        original = self.win.ssh_keys.keys
        self.win.ssh_keys.keys = lambda: calls.append(1) or original()
        dialog = self.win.hosts.host_dialog(host)
        self.addCleanup(dialog.destroy)
        self.assertEqual(calls, [])  # not scanned while the dialog opens
        dialog._fill_identity_keys()
        self.assertEqual(calls, [1])
        dialog.identity_combo.set_active_id(str(info.path))
        self.assertEqual(dialog.identity_entry.get_text(), str(info.path))
        self.assertFalse(dialog.identity_warning.get_visible())
        built = dialog.build_host(host.parent_group, host.child_group)
        self.assertEqual(built.option("identityfile"), str(info.path))
        dialog.identity_entry.set_text("~/.ssh/gone_key")
        self.assertTrue(dialog.identity_warning.get_visible())
        self.assertEqual(dialog.identity_warning.get_text(), "This file does not exist.")


@unittest.skipUnless(display_available(), "needs a display")
class ParamikoPassphraseTests(unittest.TestCase):
    def test_the_saved_passphrase_reaches_backend_for(self) -> None:
        from tests.gtk_window_fixture import WindowSandbox

        sandbox = WindowSandbox(store=True)
        self.addCleanup(sandbox.close)
        win = sandbox.window
        sandbox.use_keybox("pw")
        key = sandbox.root / "home" / ".ssh" / "k"
        host = next(h for h in win.ssh_keys.hosts() if h.alias == "web-01")
        host.options["IdentityFile"] = str(key)
        win.manager.update_host(host.parent_group, host.child_group, host.alias, host)
        self.assertTrue(win.ssh_keys.remember_passphrase(key, PASS))
        host = next(h for h in win.ssh_keys.hosts() if h.alias == "web-01")
        self.assertEqual(win.sessions.saved_key_passphrase(host), PASS)
        seen = []

        def fake_backend_for(*args, **kwargs):
            seen.append(kwargs.get("passphrase"))
            raise ConnectionError("stop here")

        with mock.patch("ssh_manager.transfer.sftp.backend_for", side_effect=fake_backend_for):
            for connect in (lambda: win.multirun.connect(win.multirun.target(host), 5),
                            lambda: win.tunnels._connect(win.tunnels.host_key(host))):
                with self.assertRaises(ConnectionError):
                    connect()
        self.assertEqual(seen, [PASS, PASS])
        self.assertNotIn(PASS, repr(dict(os.environ)))


if __name__ == "__main__":
    unittest.main()
