"""Phase F2: the file-browser tab — pure helpers and GTK behaviour with an in-memory backend."""

import os
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ophion.config import HostEntry
from ophion.transfer.backend import AuthError, CancelToken, Cancelled, EntryKind, RemoteEntry
from ophion.transfer.browse import (
    History, breadcrumbs, complete, filter_entries, human_size, natural_key, normalize_remote, parent_of,
    remote_url, sort_entries,
)
from ophion.transfer.copy import copy_file
from ophion.transfer.local import LocalBackend
from ophion.transfer.state import BrowserState

from tests.fake_backend import FakeBackend


def entry(name, kind=EntryKind.FILE, size=None, mtime=None):
    return RemoteEntry(name, f"/d/{name}", kind, size=size, mtime=mtime)


# ------------------------------------------------------------------- pure
class BrowseHelperTests(unittest.TestCase):
    def test_natural_sort_with_folders_first(self) -> None:
        items = [entry("file10"), entry("File2"), entry("b", EntryKind.DIR), entry("file1"), entry("A", EntryKind.DIR)]
        self.assertEqual([e.name for e in sort_entries(items)], ["A", "b", "file1", "File2", "file10"])
        self.assertEqual([e.name for e in sort_entries(items, descending=True)], ["b", "A", "file10", "File2",
                                                                                  "file1"])
        self.assertEqual([e.name for e in sort_entries(items, folders_first=False)][:2], ["A", "b"])
        self.assertLess(natural_key("x2"), natural_key("x10"))

    def test_sort_by_size_and_time_is_stable(self) -> None:
        items = [entry("a", size=5), entry("b", size=1), entry("c"), entry("d", size=5)]
        self.assertEqual([e.name for e in sort_entries(items, "size")], ["c", "b", "a", "d"])
        dated = [entry("old", mtime=1.0), entry("new", mtime=3.0), entry("none")]
        self.assertEqual([e.name for e in sort_entries(dated, "mtime", descending=True)], ["new", "old", "none"])

    def test_filter(self) -> None:
        items = [entry("Report.PDF"), entry(".bashrc"), entry("notes.txt"), entry("report-2.txt")]
        self.assertEqual([e.name for e in filter_entries(items, "report")], ["Report.PDF", "report-2.txt"])
        self.assertEqual([e.name for e in filter_entries(items, "*.txt")], ["notes.txt", "report-2.txt"])
        self.assertEqual([e.name for e in filter_entries(items, "", show_hidden=False)],
                         ["Report.PDF", "notes.txt", "report-2.txt"])

    def test_sizes(self) -> None:
        self.assertEqual([human_size(v) for v in (None, 0, 999, 1000, 1_400_000, 250_000_000, 3 * 10**12)],
                         ["", "0 B", "999 B", "1.0 kB", "1.4 MB", "250 MB", "3.0 TB"])

    def test_path_normalization(self) -> None:
        cases = {
            "/srv//data/": "/srv/data", "/srv/./data/../logs": "/srv/logs", "/../../etc": "/etc", "": "/home",
            "sub/dir": "/home/sub/dir", "C:\\Inetpub\\..\\wwwroot": "/C:/wwwroot", "/c:/": "/C:", "/": "/",
        }
        for path, expected in cases.items():
            with self.subTest(path=path):
                self.assertEqual(normalize_remote(path, "/home"), expected)
        self.assertEqual((parent_of("/srv/data"), parent_of("/"), parent_of("/C:")), ("/srv", "/", "/C:"))

    def test_breadcrumbs_and_completion(self) -> None:
        self.assertEqual([(c.label, c.path) for c in breadcrumbs("/srv/my data")],
                         [("/", "/"), ("srv", "/srv"), ("my data", "/srv/my data")])
        self.assertEqual([c.label for c in breadcrumbs("/C:/Inetpub")], ["/", "C:", "Inetpub"])
        self.assertEqual(complete("/srv/da", ["data", "Dashboards", "logs"]), ["/srv/data", "/srv/Dashboards"])

    def test_remote_url_never_has_a_password(self) -> None:
        self.assertEqual(remote_url("sftp", "10.0.0.20", 22, 22, "alice", "/srv/my data"),
                         "sftp://alice@10.0.0.20/srv/my%20data")
        self.assertEqual(remote_url("ftp", "::1", 2121, 21, "a b@c", "/"), "ftp://a%20b%40c@[::1]:2121/")

    def test_history(self) -> None:
        history = History(limit=3)
        for path in ("/a", "/b", "/c", "/d"):
            history.visit(path)
        self.assertEqual(history.recent, ["/d", "/c", "/b"])
        self.assertEqual(history.back(), "/c")
        self.assertEqual(history.back(), "/b")
        self.assertEqual(history.forward(), "/c")
        history.visit("/e")
        self.assertFalse(history.can_forward())


class CopyAndLocalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.local = LocalBackend(self.tmp.name)
        self.remote = FakeBackend({"/home/alice/a.bin": b"x" * 1000})

    def test_atomic_copy_and_size_check(self) -> None:
        seen = []
        result = copy_file(self.remote, "/home/alice/a.bin", self.local, f"{self.tmp.name}/a.bin", size=1000,
                           atomic=True, chunk=300, progress=seen.append)
        self.assertEqual((result.bytes, seen), (1000, [300, 600, 900, 1000]))
        self.assertFalse(os.path.exists(f"{self.tmp.name}/a.bin.part"))
        with self.assertRaises(Exception):
            copy_file(self.remote, "/home/alice/a.bin", self.local, f"{self.tmp.name}/b.bin", size=5)
        self.assertFalse(os.path.exists(f"{self.tmp.name}/b.bin"))

    def test_cancel_removes_the_partial_file(self) -> None:
        token = CancelToken()

        def progress(done):
            if done >= 300:
                token.cancel()

        with self.assertRaises(Cancelled):
            copy_file(self.remote, "/home/alice/a.bin", self.local, f"{self.tmp.name}/c.bin", cancel=token,
                      chunk=300, progress=progress, atomic=True)
        self.assertEqual(os.listdir(self.tmp.name), [])

    def test_local_backend(self) -> None:
        base = Path(self.tmp.name)
        (base / "keep").mkdir()
        (base / "keep/file.txt").write_text("k")
        (base / "victim").mkdir()
        os.symlink(base / "keep", base / "victim/link")
        self.local.delete_dir(str(base / "victim/link"))  # removes the link, not its target
        self.assertTrue((base / "keep/file.txt").exists())
        self.local.mkdir(str(base / "x/y"), parents=True)
        self.local.rename(str(base / "x"), str(base / "z"))
        names = {e.name: e.kind for e in self.local.list(self.tmp.name)}
        self.assertEqual(names, {"keep": EntryKind.DIR, "victim": EntryKind.DIR, "z": EntryKind.DIR})

    def test_browser_state(self) -> None:
        state = BrowserState(Path(self.tmp.name) / "s" / "fb.json")
        state.add_bookmark("h1", "remote", "/srv")
        state.add_bookmark("h1", "remote", "/srv")
        state.update("h1", remote="/srv/data")
        self.assertEqual(state.bookmarks("h1"), [{"side": "remote", "path": "/srv"}])
        self.assertEqual(state.get("h1")["remote"], "/srv/data")
        self.assertEqual(stat.S_IMODE(os.stat(state.path).st_mode), 0o600)

    def test_file_transfer_preferences(self) -> None:
        from ophion.settings import FileTransferPreferences

        prefs = FileTransferPreferences.from_dict({"conflict_policy": "bogus", "concurrency": 99, "warn_plain_ftp": 0,
                                                   "future": 1})
        self.assertEqual((prefs.conflict_policy, prefs.concurrency, prefs.warn_plain_ftp), ("ask", 16, True))
        self.assertEqual(FileTransferPreferences.from_dict(prefs.to_dict()), prefs)


# -------------------------------------------------------------------- GTK
def _display() -> bool:
    try:
        import gi

        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk  # type: ignore

        return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")) and Gtk.init_check(None)[0]
    except Exception:
        return False


def pump_until(predicate, timeout=5.0) -> bool:
    from gi.repository import Gtk

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        if predicate():
            return True
        time.sleep(0.01)
    return False


@unittest.skipUnless(_display(), "needs a display")
class PaneTests(unittest.TestCase):
    def setUp(self) -> None:
        from ophion.gtkui.file_browser.pane import FilePane
        from ophion.gtkui.file_browser.worker import Worker

        files = {f"/home/alice/file{i}.txt": b"x" * i for i in range(1, 13)}
        files["/home/alice/.hidden"] = b"h"
        files["/home/alice/docs/readme.md"] = b"readme"
        self.backend = FakeBackend(files, ["/home/alice/empty"])
        self.worker = Worker("test")
        self.addCleanup(self.worker.shutdown)
        self.pane = FilePane("remote", self.worker, lambda: self.backend, owner_columns=True)
        self.pane.set_connected(True)
        self.pane.show_all()
        self.addCleanup(self.pane.destroy)

    def load(self, path="/home/alice"):
        self.pane.load(path)
        self.assertTrue(pump_until(lambda: self.pane.current_path == path and not self.worker.pending))

    def names(self):
        return [row[6].name for row in self.pane.store]

    def test_listing_sorting_filtering_hidden(self) -> None:
        self.load()
        self.assertEqual(self.names()[:3], ["docs", "empty", "file1.txt"])
        self.assertNotIn(".hidden", self.names())
        self.assertEqual(self.names()[-1], "file12.txt")  # natural order
        self.pane.sort_by("size")
        self.pane.sort_by("size")  # descending
        self.assertEqual(self.names()[2], "file12.txt")
        self.pane.filter_entry.set_text("file1")
        self.assertTrue(pump_until(lambda: len(self.names()) == 4))
        self.pane.filter_entry.set_text("")
        self.pane.set_show_hidden(True)
        self.assertIn(".hidden", self.names())
        self.assertIn("15 items", self.pane.footer.get_text())

    def test_breadcrumbs_and_navigation(self) -> None:
        self.load()
        labels = [child.get_label() for child in self.pane.crumb_box.get_children() if hasattr(child, "get_label")]
        self.assertEqual([text for text in labels if text and text != "›"], ["/", "home", "alice"])
        self.load("/home/alice/docs")
        self.pane.go_up()
        self.assertTrue(pump_until(lambda: self.pane.current_path == "/home/alice"))
        self.pane.go_back()
        self.assertTrue(pump_until(lambda: self.pane.current_path == "/home/alice/docs"))

    def test_bad_location_keeps_the_old_folder(self) -> None:
        self.load()
        self.pane.load("/nope")
        self.assertTrue(pump_until(lambda: self.pane.path_error.get_visible()))
        self.assertEqual(self.pane.current_path, "/home/alice")
        self.assertIn("file1.txt", self.names())

    def test_empty_and_error_states(self) -> None:
        self.load("/home/alice/empty")
        self.assertEqual(self.pane.state_stack.get_visible_child_name(), "empty")
        fresh_worker = self.worker
        from ophion.gtkui.file_browser.pane import FilePane

        pane = FilePane("remote", fresh_worker, lambda: self.backend)
        self.addCleanup(pane.destroy)
        pane.set_connected(True)
        pane.show_all()
        pane.load("/missing")
        self.assertTrue(pump_until(lambda: pane.state_stack.get_visible_child_name() == "error"))

    def test_a_newer_navigation_cancels_the_older_one(self) -> None:
        self.backend.block["/home/alice"] = threading.Event()
        self.pane.load("/home/alice")
        self.pane.load("/home/alice/docs")
        self.backend.block["/home/alice"].set()
        self.assertTrue(pump_until(lambda: not self.worker.pending))
        self.assertEqual(self.pane.current_path, "/home/alice/docs")
        self.assertEqual(self.names(), ["readme.md"])

    def test_large_folders_are_batched_and_capped(self) -> None:
        for i in range(1300):
            self.backend.files[f"/home/alice/big/item{i:04d}"] = b""
        self.backend.dirs.add("/home/alice/big")
        with mock.patch("ophion.gtkui.file_browser.pane.MAX_ROWS", 1000):
            self.load("/home/alice/big")
            self.assertEqual(len(self.pane.entries), 1300)
            self.assertEqual(len(self.pane.store), 1000)
            self.assertTrue(self.pane.cap_hint.get_visible())
            self.assertIn("300 more items", self.pane.cap_hint.get_text())

    def test_operations(self) -> None:
        self.load()
        with mock.patch("ophion.gtkui.file_browser.pane.prompt_text", return_value="New stuff"):
            self.pane.new_folder()
        self.assertTrue(pump_until(lambda: "/home/alice/New stuff" in self.backend.dirs))
        self.pane.select_name("file1.txt")
        self.pane.start_rename()
        path = next(str(row.path) for row in self.pane.store if row[6].name == "file1.txt")
        self.pane._on_name_edited(None, path, "renamed.txt")
        self.assertTrue(pump_until(lambda: "/home/alice/renamed.txt" in self.backend.files))
        self.assertTrue(pump_until(lambda: "renamed.txt" in self.names()))
        self.pane.select_name("docs")
        with mock.patch("ophion.gtkui.file_browser.pane.confirm", return_value=True) as ask:
            self.pane.delete_selected()
        self.assertIn("docs", ask.call_args[0][2])
        self.assertTrue(pump_until(lambda: "/home/alice/docs" not in self.backend.dirs))

    def test_invalid_names_are_refused(self) -> None:
        from ophion.gtkui.file_browser.pane import valid_name

        for bad in ("", "..", "a/b", "x\ny", "é" * 200):
            self.assertIsNotNone(valid_name(bad), bad)
        self.assertIsNone(valid_name("Report (final).pdf"))

    def test_read_only(self) -> None:
        self.pane.set_read_only(True)
        self.load()
        self.pane.select_name("file1.txt")
        for button in (self.pane.new_folder_button, self.pane.rename_button, self.pane.delete_button):
            self.assertFalse(button.get_sensitive())
        with mock.patch("ophion.gtkui.file_browser.pane.confirm") as ask:
            self.pane.delete_selected()
        ask.assert_not_called()

    def test_keyboard(self) -> None:
        from gi.repository import Gdk

        self.load("/home/alice/docs")

        def press(keyval, state=0):
            event = Gdk.Event.new(Gdk.EventType.KEY_PRESS)
            event.key.keyval = keyval
            event.key.state = state
            return self.pane._on_key(self.pane.view, event.key)

        self.assertTrue(press(Gdk.KEY_BackSpace))
        self.assertTrue(pump_until(lambda: self.pane.current_path == "/home/alice"))
        self.assertTrue(press(Gdk.KEY_l, Gdk.ModifierType.CONTROL_MASK))
        self.assertEqual(self.pane.path_stack.get_visible_child_name(), "entry")
        self.assertTrue(press(Gdk.KEY_F5))
        self.assertFalse(press(Gdk.KEY_q))

    def test_untrusted_names_are_shown_as_text(self) -> None:
        self.backend.files["/home/alice/<b>bold<i> & \u202egnp.exe"] = b""
        self.load()
        row = next(r for r in self.pane.store if r[6].name.startswith("<b>"))
        self.assertIn("&lt;b&gt;", row[7])  # tooltip is escaped markup
        self.assertTrue(row[1].startswith("\u2068") and row[1].endswith("\u2069"))  # isolated for display


@unittest.skipUnless(_display(), "needs a display")
class TabTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = BrowserState(Path(self.tmp.name) / "fb.json")
        Path(self.tmp.name, "local").mkdir()
        Path(self.tmp.name, "local", "upload.txt").write_text("from here")
        self.backend = FakeBackend({"/home/alice/report.txt": b"remote data"})
        self.logs = []

    def tab(self, protocol="sftp", backends=None, service=None, read_only=False, settings=None, **kwargs):
        from ophion.gtkui.file_browser.tab import FileBrowserTab

        queue = list(backends or [self.backend])
        host = HostEntry("files", {"hostname": "10.0.0.20", "user": "alice"}, protocol=protocol,
                         parent_group="Acme", child_group="", id="h-files", metadata={"transfer": settings or {}})
        tab = FileBrowserTab(host, lambda message, level: self.logs.append(message),
                             backend_factory=lambda h, u, p: (self.factory_calls.append((u, p)), queue.pop(0)
                                                              if len(queue) > 1 else queue[0])[1],
                             state=self.state, read_only=read_only, local_home=str(Path(self.tmp.name, "local")),
                             password_service=service, **kwargs)
        self.addCleanup(lambda: tab.get_parent() is None and tab.destroy())
        return tab

    def connected(self, tab):
        self.assertTrue(pump_until(lambda: tab.remote_pane.current_path is not None
                                   and not tab.worker.pending), self.logs)

    def test_connects_lists_and_shows_security(self) -> None:
        self.factory_calls = []
        tab = self.tab()
        self.connected(tab)
        self.assertEqual(tab.remote_pane.current_path, "/home/alice")
        self.assertEqual([row[6].name for row in tab.remote_pane.store], ["report.txt"])
        self.assertTrue(pump_until(lambda: tab.local_pane.current_path is not None))
        self.assertEqual(tab.security_kind, "encrypted")
        self.assertEqual(tab.status.value, "connected")
        self.assertFalse(tab.is_terminal)

    def test_plain_ftp_asks_once_and_is_flagged(self) -> None:
        self.factory_calls = []
        provider = lambda: SimpleNamespace(password="pw", source="credential", credential_id="c1")  # noqa: E731
        with mock.patch("ophion.gtkui.file_browser.tab.confirm", return_value=True) as ask:
            tab = self.tab("ftp", password_provider=provider)
            self.connected(tab)
            self.assertEqual(tab.security_kind, "plain")
            self.assertIn("Not encrypted", tab.security_label.get_text())
            tab.destroy()
            tab = self.tab("ftp", password_provider=provider)
            self.connected(tab)
        self.assertEqual(ask.call_count, 1)  # remembered for this host

    def test_declining_plain_ftp_does_not_connect(self) -> None:
        self.factory_calls = []
        provider = lambda: SimpleNamespace(password="pw", source="credential", credential_id="c1")  # noqa: E731
        with mock.patch("ophion.gtkui.file_browser.tab.confirm", return_value=False):
            tab = self.tab("ftp", password_provider=provider)
            self.assertTrue(pump_until(lambda: tab.status.value == "failed"))
        self.assertEqual(self.factory_calls, [])

    def test_auth_failure_retry_and_save_offer(self) -> None:
        from ophion.auth_flow import Decision, SaveContext

        self.factory_calls = []
        failing = FakeBackend()
        failing.connect_errors = [AuthError()]
        service = mock.Mock()
        service.describe_password_source.return_value = "saved password (credential ‘Ops’)"
        service.password_save_context.return_value = SaveContext(credential_name="Ops")
        service.save_host_password.return_value = ("saved", "Password saved for ‘files’.")
        requests, offers = [], []
        provider = lambda: SimpleNamespace(password="old-pw", source="credential", credential_id="c1")  # noqa: E731
        with mock.patch("ophion.gtkui.file_browser.tab.PasswordRequestDialog",
                        side_effect=lambda p, r, cb: requests.append((r, cb)) or mock.Mock()), \
                mock.patch("ophion.gtkui.file_browser.tab.SavePasswordDialog",
                           side_effect=lambda p, o, cb: offers.append((o, cb)) or mock.Mock()):
            tab = self.tab(backends=[failing, self.backend], service=service, password_provider=provider)
            self.assertTrue(pump_until(lambda: bool(requests)))
            request, answer = requests[0]
            self.assertIn("credential ‘Ops’", request.message)
            answer("typed pw")
            self.connected(tab)
            self.assertTrue(pump_until(lambda: bool(offers)))
            offer, decide = offers[0]
            self.assertEqual(offer.title, "Save this password for ‘files’?")
            decide(Decision.SAVE)
        self.assertEqual([p for _u, p in self.factory_calls], ["old-pw", "typed pw"])
        service.save_host_password.assert_called_once()
        self.assertEqual(service.save_host_password.call_args[0][1], "typed pw")

    def test_untrusted_certificate_flow(self) -> None:
        from ophion.transfer.backend import CertificateInfo, UntrustedCertificate

        self.factory_calls = []
        failing = FakeBackend()
        failing.connect_errors = [UntrustedCertificate(info=CertificateInfo(subject="CN=x", sha256="AA:BB"))]
        failing.trust_certificate = mock.Mock()
        with mock.patch("ophion.gtkui.file_browser.tab.trust_certificate", return_value=True) as ask:
            tab = self.tab("ftps", backends=[failing, self.backend])
            self.connected(tab)
        ask.assert_called_once()
        failing.trust_certificate.assert_called_once()

    def test_upload_download_and_url(self) -> None:
        self.factory_calls = []
        tab = self.tab()
        self.connected(tab)
        self.assertTrue(pump_until(lambda: tab.local_pane.current_path is not None and tab.local_pane.entries))
        upload = next(e for e in tab.local_pane.entries if e.name == "upload.txt")
        tab.transfer(tab.local_pane, tab.remote_pane, [upload])
        self.assertTrue(pump_until(lambda: self.backend.files.get("/home/alice/upload.txt") == b"from here"))
        self.assertFalse(any(p.endswith(".part") for p in self.backend.files))
        report = next(e for e in tab.remote_pane.entries if e.name == "report.txt")
        tab.transfer(tab.remote_pane, tab.local_pane, [report])
        target = Path(self.tmp.name, "local", "report.txt")
        self.assertTrue(pump_until(target.exists))
        self.assertEqual(target.read_bytes(), b"remote data")
        url = tab.copy_remote_url()
        self.assertEqual(url, "sftp://alice@10.0.0.20/home/alice")

    def test_malicious_remote_name_stays_in_the_destination(self) -> None:
        self.factory_calls = []
        tab = self.tab()
        self.connected(tab)
        evil = RemoteEntry("../../escape.txt", "/home/alice/evil", EntryKind.FILE, size=4)
        self.backend.files["/home/alice/evil"] = b"evil"
        self.assertTrue(pump_until(lambda: tab.local_pane.current_path is not None))
        tab.transfer(tab.remote_pane, tab.local_pane, [evil])
        self.assertTrue(pump_until(lambda: not tab.worker.pending))
        self.assertFalse(Path(self.tmp.name, "escape.txt").exists())
        self.assertTrue(Path(self.tmp.name, "local", ".._.._escape.txt").exists())

    def test_read_only_host_cannot_upload(self) -> None:
        self.factory_calls = []
        tab = self.tab(read_only=True)
        self.connected(tab)
        self.assertTrue(pump_until(lambda: tab.local_pane.entries))
        tab.transfer(tab.local_pane, tab.remote_pane, list(tab.local_pane.entries))
        self.assertTrue(any("read-only" in message for message in self.logs))
        self.assertNotIn("/home/alice/upload.txt", self.backend.files)

    def test_open_downloads_privately(self) -> None:
        self.factory_calls = []
        tab = self.tab()
        self.connected(tab)
        report = tab.remote_pane.entries[0]
        with mock.patch("ophion.gtkui.file_browser.tab.Gio.AppInfo.launch_default_for_uri") as launch:
            tab._open_file(tab.remote_pane, report)
            self.assertTrue(pump_until(lambda: launch.called))
        opened = Path(launch.call_args[0][0].replace("file://", ""))
        self.assertEqual(opened.read_bytes(), b"remote data")
        self.assertEqual(stat.S_IMODE(os.stat(opened).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(opened.parent).st_mode), 0o700)
        tab.destroy()
        self.assertFalse(opened.parent.exists())  # cleaned up on close

    def test_closing_during_a_listing_is_safe(self) -> None:
        self.factory_calls = []
        self.backend.block["/home/alice"] = threading.Event()
        tab = self.tab()
        self.assertTrue(pump_until(lambda: "list /home/alice" in self.backend.calls))
        tab.destroy()
        self.backend.block["/home/alice"].set()
        pump_until(lambda: False, timeout=0.3)
        self.assertFalse(tab.worker.alive)

    def test_bookmarks_and_remembered_folders(self) -> None:
        self.factory_calls = []
        tab = self.tab()
        self.connected(tab)
        tab.add_bookmark("remote")
        self.assertEqual(self.state.bookmarks("h-files"), [{"side": "remote", "path": "/home/alice"}])
        self.assertEqual(self.state.get("h-files")["remote"], "/home/alice")


@unittest.skipUnless(_display(), "needs a display")
@unittest.skipIf(os.environ.get("SSHM_SKIP_INTEGRATION") == "1", "integration tests disabled")
class RealFtpTabTests(unittest.TestCase):
    """The tab with the real FTP backend against tests/fake_ftp.py."""

    def test_browse_and_upload_over_ftp(self) -> None:
        from ophion.gtkui.file_browser.tab import FileBrowserTab
        from ophion.transfer.ftp import FtpBackend
        from tests.fake_ftp import FakeFtpServer

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp, "root")
            (root / "pub").mkdir(parents=True)
            (root / "pub" / "hello.txt").write_text("hello")
            local = Path(tmp, "local")
            local.mkdir()
            (local / "up.bin").write_bytes(b"u" * 5000)
            with FakeFtpServer(root) as server:
                host = HostEntry("files", {"hostname": "127.0.0.1", "port": str(server.port), "user": "alice"},
                                 protocol="ftp", metadata={"transfer": {"initial_remote_dir": "/pub"}})
                tab = FileBrowserTab(
                    host, lambda *a: None, state=BrowserState(Path(tmp, "fb.json")), local_home=str(local),
                    password_provider=lambda: SimpleNamespace(password="s3cret pass", source="credential",
                                                              credential_id="c"),
                    backend_factory=lambda h, u, p: FtpBackend("127.0.0.1", server.port, u, p))
                self.addCleanup(tab.destroy)
                with mock.patch("ophion.gtkui.file_browser.tab.confirm", return_value=True):
                    self.assertTrue(pump_until(lambda: tab.remote_pane.current_path == "/pub"
                                               and tab.remote_pane.entries, timeout=10))
                self.assertEqual([e.name for e in tab.remote_pane.entries], ["hello.txt"])
                self.assertTrue(pump_until(lambda: tab.local_pane.entries))
                tab.transfer(tab.local_pane, tab.remote_pane, list(tab.local_pane.entries))
                self.assertTrue(pump_until(lambda: (root / "pub" / "up.bin").exists()
                                           and not tab.worker.pending, timeout=10))
                self.assertEqual((root / "pub" / "up.bin").read_bytes(), b"u" * 5000)
                self.assertFalse((root / "pub" / "up.bin.part").exists())


@unittest.skipUnless(_display(), "needs a display")
class NotebookTests(unittest.TestCase):
    """A file-browser tab in the real window: SSH-only features are off for it."""

    def test_window_integration(self) -> None:
        from tests.gtk_window_fixture import WindowSandbox

        sandbox = WindowSandbox(store=True)
        self.addCleanup(sandbox.close)
        win = sandbox.window
        win.manager.add_host("staging", "hosts.conf", HostEntry("files", {"hostname": "10.0.0.20"}, protocol="sftp"))
        win.tree.rebuild(refresh=True)
        backend = FakeBackend({"/home/alice/a.txt": b"a"})
        with mock.patch("ophion.transfer.factory.make_backend", return_value=backend):
            win.sessions.open_hosts([win.tree.find_host_by_alias("files")])
            tab = win.terminal_tabs.current_tab()
            self.assertEqual(type(tab).__name__, "FileBrowserTab")
            self.assertTrue(pump_until(lambda: tab.remote_pane.current_path == "/home/alice"))
        bar = win.terminal_tabs._toolbar
        self.assertFalse(bar.split.get_sensitive())
        self.assertFalse(bar.stats.get_sensitive())
        self.assertTrue(bar.reconnect.get_sensitive())
        self.assertFalse(bar.broadcast.get_sensitive())
        self.assertEqual(win.tree.host_status.get("staging::hosts.conf::files"), "connected")
        win.terminal_tabs.close_tab(tab)
        self.assertEqual(win.terminal_tabs.tab_count(), 0)


if __name__ == "__main__":
    unittest.main()
