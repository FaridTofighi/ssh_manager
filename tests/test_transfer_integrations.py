"""File-transfer integrations: Quick Connect URLs, "Files" for SSH hosts, import mapping (synthetic data)."""

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ssh_manager.config import HostEntry
from ssh_manager.protocols.urls import ConnectionUrl, looks_like_url, parse_connection_url
from ssh_manager.settings import AppSettings, FileTransferPreferences
from ssh_manager.transfer.protolog import ConnectionLog, RotatingLogFile
from tests.fake_backend import FakeBackend


def _display() -> bool:
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return False
    try:
        import gi

        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk  # type: ignore

        return Gtk.init_check(None)[0]
    except Exception:
        return False


def pump_until(predicate, timeout=5.0) -> bool:
    import time

    from gi.repository import GLib  # type: ignore

    deadline = time.monotonic() + timeout
    context = GLib.MainContext.default()
    while time.monotonic() < deadline:
        while context.pending():
            context.iteration(False)
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class UrlTests(unittest.TestCase):
    def test_valid_urls(self) -> None:
        cases = {
            "sftp://alice@10.0.0.5:2222/var/www": ConnectionUrl("sftp", "10.0.0.5", "alice", 2222, "/var/www"),
            "ftp://10.0.0.5": ConnectionUrl("ftp", "10.0.0.5"),
            "FTPS://files.example.com/": ConnectionUrl("ftps", "files.example.com", path="/"),
            "ftps://bob%40corp@[fe80::1]:990": ConnectionUrl("ftps", "fe80::1", "bob@corp", 990),
            "ssh://deploy@web.example.com:22/ignored": ConnectionUrl("ssh", "web.example.com", "deploy", 22),
            "sftp://alice@10.0.0.5/my%20files": ConnectionUrl("sftp", "10.0.0.5", "alice", None, "/my files"),
        }
        for text, expected in cases.items():
            url, reason = parse_connection_url(text)
            self.assertEqual((url, reason), (expected, ""), text)

    def test_invalid_urls(self) -> None:
        for text in ("http://x", "ftp://", "ftp://h:0", "ftp://h:99999", "ftp://h:x", "sftp://-oProxy@h",
                     "sftp://a b@h", "ftp://h?x=1", "ftp://h#frag", "ftp://-h", "ftp://[fe80::1"):
            url, reason = parse_connection_url(text)
            self.assertIsNone(url, text)
            self.assertTrue(reason, text)

    def test_a_password_is_refused_and_never_echoed(self) -> None:
        url, reason = parse_connection_url("ftp://alice:s3cret-pw@10.0.0.5")
        self.assertIsNone(url)
        self.assertIn("password", reason)
        self.assertNotIn("s3cret", reason)

    def test_label_and_options(self) -> None:
        url, _ = parse_connection_url("sftp://alice@[fe80::1]:2222/srv")
        self.assertEqual(url.label(), "sftp://alice@[fe80::1]:2222/srv")
        self.assertEqual(url.options(), {"hostname": "fe80::1", "user": "alice", "port": "2222"})
        self.assertEqual(parse_connection_url("ftp://h")[0].effective_port, 21)
        self.assertTrue(looks_like_url("sftp://h"))
        self.assertFalse(looks_like_url("alice@h:22"))


class ConnectionLogTests(unittest.TestCase):
    def test_masks_secrets_and_control_characters(self) -> None:
        log = ConnectionLog("files", clock=lambda: 0.0)
        log.set_secret("password", "s3cret pass")
        seen = []
        log.listeners.append(seen.append)
        log("PASS s3cret pass")
        log("< 530 user alice with s3cret pass rejected")
        log("< 220 evil\x1b[31m banner\u202e")
        text = log.text()
        self.assertNotIn("s3cret", text)
        self.assertIn("PASS ********", text)
        self.assertNotIn("\x1b", text)
        self.assertNotIn("\u202e", text)
        self.assertEqual(len(seen), 3)
        log.set_secret("password", "")
        log.clear()
        self.assertEqual(log.text(), "")

    def test_ring_buffer_is_bounded(self) -> None:
        from ssh_manager.transfer import protolog

        log = ConnectionLog()
        for i in range(protolog.MAX_LINES + 10):
            log(f"line {i}")
        self.assertEqual(len(log.lines()), protolog.MAX_LINES)
        self.assertTrue(log.lines()[-1].endswith(f"line {protolog.MAX_LINES + 9}"))

    def test_file_rotates_and_is_private(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "logs" / "protocol.log"
            sink = RotatingLogFile(path, max_bytes=200, backups=2)
            log = ConnectionLog("files", file=sink)
            log.set_secret("password", "hunter2")
            for i in range(40):
                log(f"> USER alice {i} hunter2")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertTrue(path.with_name("protocol.log.1").exists())
            self.assertTrue(path.with_name("protocol.log.2").exists())
            self.assertFalse(path.with_name("protocol.log.3").exists())
            for item in path.parent.iterdir():
                self.assertNotIn("hunter2", item.read_text())
                self.assertLessEqual(item.stat().st_size, 200)


class PreferencesModelTests(unittest.TestCase):
    def test_file_transfer_preferences_round_trip(self) -> None:
        prefs = FileTransferPreferences.from_dict({"concurrency": 99, "conflict_policy": "bogus",
                                                   "protocol_log": "yes", "download_dir": "~/In"})
        self.assertEqual((prefs.concurrency, prefs.conflict_policy, prefs.protocol_log, prefs.download_dir),
                         (16, "ask", False, "~/In"))
        self.assertFalse(FileTransferPreferences().protocol_log)  # off by default
        self.assertEqual(FileTransferPreferences.from_dict(prefs.to_dict()), prefs)


@unittest.skipUnless(_display(), "needs a display")
class QuickConnectHostTests(unittest.TestCase):
    def test_quick_connect_host(self) -> None:
        from ssh_manager.gtkui.controllers.session_controller import quick_connect_host

        host = quick_connect_host("sftp://alice@10.0.0.5:2222/var/www")
        self.assertEqual((host.protocol, host.alias), ("sftp", "sftp://alice@10.0.0.5:2222/var/www"))
        self.assertEqual(host.options, {"hostname": "10.0.0.5", "user": "alice", "port": "2222"})
        self.assertEqual(host.metadata, {"transfer": {"v": 1, "initial_remote_dir": "/var/www"}})
        self.assertEqual(host.id, "")  # unsaved
        implicit = quick_connect_host("ftps://10.0.0.6:990")
        self.assertEqual(implicit.metadata["transfer"]["tls_mode"], "implicit")
        ssh = quick_connect_host("bob@10.0.0.7:2200")
        self.assertEqual((ssh.protocol, ssh.options["port"]), ("ssh", "2200"))
        self.assertIsNone(quick_connect_host("ftp://a:b@h"))


@unittest.skipUnless(_display(), "needs a display")
class WindowTests(unittest.TestCase):
    def setUp(self) -> None:
        from tests.gtk_window_fixture import WindowSandbox

        self.sandbox = WindowSandbox(store=True)
        self.addCleanup(self.sandbox.close)
        self.win = self.sandbox.window

    def close_tabs(self) -> None:
        tabs = self.win.terminal_tabs
        while tabs.tab_count():
            tabs.close_tab(tabs.current_tab())

    def test_quick_connect_url_opens_an_unsaved_file_browser(self) -> None:
        backend = FakeBackend({"/var/www/index.html": b"x", "/home/alice/a.txt": b"a"})
        notes = []
        before = len(self.win.manager.store.list_hosts())
        with mock.patch("ssh_manager.gtkui.dialogs.prompt.PromptDialog.ask",
                        return_value="sftp://alice@10.0.0.5/var/www"), \
                mock.patch("ssh_manager.transfer.factory.make_backend", return_value=backend), \
                mock.patch.object(self.win, "_notify", side_effect=lambda *a, **k: notes.append((a, k))):
            self.win.sessions.quick_connect()
            tab = self.win.terminal_tabs.current_tab()
            self.assertEqual(type(tab).__name__, "FileBrowserTab")
            self.assertTrue(pump_until(lambda: tab.remote_pane.current_path == "/var/www"))
        self.addCleanup(self.close_tabs)
        self.assertEqual(len(self.win.manager.store.list_hosts()), before)  # nothing saved
        save = [k for _a, k in notes if k.get("action_label")]
        self.assertEqual(len(save), 1)
        with mock.patch.object(self.win.hosts, "prompt_new_host") as prompt:
            save[0]["action"]()
        prefill = prompt.call_args.kwargs["prefill"]
        self.assertEqual((prefill.alias, prefill.protocol, prefill.id), ("10.0.0.5", "sftp", ""))
        self.assertEqual(prefill.options["user"], "alice")
        self.assertEqual(prefill.metadata["transfer"]["initial_remote_dir"], "/var/www")

    def test_quick_connect_validator_rejects_a_password(self) -> None:
        captured = {}

        def fake_init(dialog, *args, **kwargs):
            captured["validator"] = kwargs["validator"]

        with mock.patch("ssh_manager.gtkui.dialogs.prompt.PromptDialog.__init__", fake_init), \
                mock.patch("ssh_manager.gtkui.dialogs.prompt.PromptDialog.ask", return_value=None):
            self.win.sessions.quick_connect()
        validate = captured["validator"]
        self.assertIn("password", validate("ftp://alice:pw@10.0.0.5"))
        self.assertIsNone(validate("ftps://alice@10.0.0.5"))
        self.assertIsNone(validate("alice@10.0.0.5:22"))
        self.assertTrue(validate("gopher://x"))

    def test_files_for_an_ssh_host_is_an_sftp_view(self) -> None:
        from ssh_manager.gtkui.controllers import context_menus

        win = self.win
        win.manager.add_host("staging", "hosts.conf", HostEntry("web", {"hostname": "10.0.0.20", "user": "alice",
                                                                      "proxyjump": "bob@10.0.0.254"}))
        win.tree.rebuild(refresh=True)
        web = win.tree.find_host_by_alias("web")
        win.tree.select_host(web.parent_group, web.child_group, web.alias)
        labels = [item.label for section in win.tree.context_menu_spec() for item in section.items
                  if getattr(item, "visible", True)]
        self.assertIn("Browse _Files (SFTP)", labels)
        before = [(h.id, h.protocol) for h in win.manager.store.list_hosts()]
        backend = FakeBackend({"/home/alice/a.txt": b"a"})
        with mock.patch("ssh_manager.transfer.factory.make_backend", return_value=backend) as make:
            win.actions.open_files_selected()
            tab = win.terminal_tabs.current_tab()
            self.addCleanup(self.close_tabs)
            self.assertEqual(type(tab).__name__, "FileBrowserTab")
            self.assertTrue(pump_until(lambda: tab.remote_pane.current_path == "/home/alice"))
        host_arg = make.call_args.args[0]
        self.assertEqual((host_arg.protocol, host_arg.id), ("sftp", web.id))
        self.assertEqual(host_arg.options["proxyjump"], "bob@10.0.0.254")
        self.assertEqual([(h.id, h.protocol) for h in win.manager.store.list_hosts()], before)  # nothing stored
        self.assertEqual(win.tree.find_host_by_alias("web").protocol, "ssh")
        ftp = context_menus.host_menu(win.actions, is_ssh=False)
        hidden = [i.label for s in ftp for i in s.items if not getattr(i, "visible", True)]
        self.assertIn("Browse _Files (SFTP)", hidden)


@unittest.skipUnless(_display(), "needs a display")
class PreferencesPageTests(unittest.TestCase):
    def test_file_transfer_page(self) -> None:
        from ssh_manager.gtkui.dialogs.preferences import PreferencesDialog

        with tempfile.TemporaryDirectory() as tmp:
            settings = AppSettings(config_path=Path(tmp) / "settings.json")
            settings.file_transfer.concurrency = 5
            dialog = PreferencesDialog(None, False, 1.0, 11, settings)
            self.addCleanup(dialog.destroy)
            self.assertIn("file_transfer", dialog.layout._pages)
            self.assertEqual(dialog.values().file_transfer.concurrency, 5)
            self.assertFalse(dialog._protocol_log_check.get_active())
            dialog._protocol_log_check.set_active(True)
            dialog._conflict_combo.set_active_id("skip")
            dialog._download_dir_entry.set_text(" /tmp/in ")
            dialog._bandwidth_spin.set_value(512)
            values = dialog.values().file_transfer
            self.assertEqual((values.protocol_log, values.conflict_policy, values.download_dir,
                              values.bandwidth_limit_kbps), (True, "skip", "/tmp/in", 512))


@unittest.skipUnless(_display(), "needs a display")
class TabDiagnosticsTests(unittest.TestCase):
    """A real FTP login through the tab: the log pane and the protocol file never show the password."""

    def test_log_pane_copy_and_diagnostics(self) -> None:
        from ssh_manager.auth_flow import PasswordResolution
        from ssh_manager.gtkui.file_browser.tab import FileBrowserTab
        from ssh_manager.transfer.state import BrowserState
        from tests.fake_ftp import FakeFtpServer

        if os.environ.get("SSHM_SKIP_INTEGRATION") == "1":
            self.skipTest("integration tests skipped")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / "root"
        (root / "pub").mkdir(parents=True)
        server = FakeFtpServer(root, users={"alice": "s3cret pass"})
        server.__enter__()
        self.addCleanup(server.__exit__, None, None, None)
        settings = AppSettings(config_path=Path(tmp.name) / "settings.json")
        settings.file_transfer.protocol_log = True
        settings.file_transfer.warn_plain_ftp = False
        log_path = Path(tmp.name) / "state" / "protocol.log"
        host = HostEntry("files", {"hostname": "127.0.0.1", "port": str(server.port), "user": "alice"},
                         protocol="ftp", id="h-files")
        with mock.patch("ssh_manager.gtkui.file_browser.tab.default_log_path", return_value=log_path):
            tab = FileBrowserTab(host, lambda *_a: None, app_settings=settings,
                                 password_provider=lambda: PasswordResolution("s3cret pass", "credential"),
                                 state=BrowserState(Path(tmp.name) / "fb.json"),
                                 local_home=tmp.name)
        self.addCleanup(lambda: tab.get_parent() is None and tab.destroy())
        self.assertTrue(pump_until(lambda: tab.remote_pane.current_path is not None and not tab.worker.pending,
                                   timeout=10))
        tab.log_toggle.set_active(True)
        self.assertTrue(pump_until(lambda: "Connected" in tab.log_view.get_buffer().get_property("text")))
        shown = tab.log_view.get_buffer().get_property("text")
        self.assertIn("PASS ********", shown)
        self.assertIn("USER alice", shown)
        with mock.patch("gi.repository.Gtk.Clipboard.get") as clipboard:
            copied = tab.copy_log()
            diagnostics = tab.copy_diagnostics()
        self.assertEqual(clipboard.call_count, 2)
        self.assertIn("Acme test FTP ready", diagnostics)
        self.assertIn("Mode: passive", diagnostics)
        self.assertIn("User: alice", diagnostics)
        for text in (shown, copied, diagnostics, log_path.read_text()):
            self.assertNotIn("s3cret", text)
        self.assertEqual(stat.S_IMODE(log_path.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
