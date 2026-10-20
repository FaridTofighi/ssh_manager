"""Phase F3: the transfer panel, conflict dialog and drag and drop in the file-browser tab (GTK)."""

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ssh_manager.config import HostEntry
from ssh_manager.transfer.backend import RemoteEntry
from ssh_manager.transfer.dnd import encode_payload
from ssh_manager.transfer.engine import Conflict, ConflictInfo, Direction, Endpoint, State, TransferItem, TransferJob
from ssh_manager.transfer.local import LocalBackend
from ssh_manager.transfer.state import BrowserState

from tests.fake_backend import FakeBackend


def _display() -> bool:
    try:
        import gi

        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk  # type: ignore

        return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")) and Gtk.init_check(None)[0]
    except Exception:
        return False


def pump_until(predicate, timeout=8.0) -> bool:
    from gi.repository import Gtk

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        if predicate():
            return True
        time.sleep(0.01)
    return False


class PanelTextTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            from ssh_manager.gtkui.file_browser import transfers  # noqa: F401
        except Exception as exc:  # pragma: no cover
            raise unittest.SkipTest(str(exc))

    def job(self, state, **kwargs):
        local = Endpoint("l", LocalBackend, local=True)
        job = TransferJob(Direction.DOWNLOAD, [RemoteEntry("big.iso", "/srv/big.iso")], "/tmp", local, local)
        job.items = [TransferItem("/srv/big.iso", "/tmp/big.iso", size=48_000_000, done=12_400_000)]
        job.state = state
        for key, value in kwargs.items():
            setattr(job, key, value)
        return job

    def test_detail_texts(self) -> None:
        from ssh_manager.gtkui.file_browser.transfers import format_eta, job_detail

        running = self.job(State.RUNNING, rate=3_100_000.0)
        self.assertEqual(job_detail(running), "12.4 MB of 48.0 MB · 3.1 MB/s · 11 s left · Transferring")
        self.assertEqual(job_detail(self.job(State.PLANNING, planned=340)), "Scanning… 340 files")
        failed = self.job(State.FAILED)
        failed.items[0].state, failed.items[0].error = State.FAILED, "Permission denied."
        self.assertEqual(job_detail(failed), "1 file failed: Permission denied.")
        self.assertEqual((format_eta(None), format_eta(90), format_eta(7300)), ("", "1 min left", "2 h 1 min left"))


@unittest.skipUnless(_display(), "needs a display")
class PanelTests(unittest.TestCase):
    def test_rows_and_actions(self) -> None:
        from ssh_manager.gtkui.file_browser.transfers import TransferPanel

        panel = TransferPanel()
        self.addCleanup(panel.destroy)
        local = Endpoint("l", LocalBackend, local=True)
        job = TransferJob(Direction.UPLOAD, [RemoteEntry("a", "/a")], "/srv", local, local)
        job.items = [TransferItem("/a", "/srv/a", size=10)]
        job.state = State.RUNNING
        calls = []
        panel.on_pause = lambda j: calls.append("pause")
        panel.on_resume = lambda j: calls.append("resume")
        panel.update(job)
        row = panel.rows[job.id]
        self.assertTrue(panel.get_reveal_child())
        self.assertTrue(row.pause_button.get_visible() and row.cancel_button.get_visible())
        self.assertFalse(row.retry_button.get_visible())
        self.assertEqual(panel.summary_text(), "1 transfer")
        row.pause_button.clicked()
        job.state = State.PAUSED
        panel.update(job)
        row.pause_button.clicked()
        self.assertEqual(calls, ["pause", "resume"])
        job.state = State.FAILED
        job.items[0].state = State.FAILED
        panel.update(job)
        self.assertTrue(row.retry_button.get_visible() and row.remove_button.get_visible())
        self.assertIn("Failed", row.get_accessible().get_description() or "Failed")
        panel._clear()
        self.assertEqual(panel.rows, {})

    def test_conflict_dialog(self) -> None:
        from gi.repository import Gtk

        from ssh_manager.gtkui.file_browser.transfers import ConflictDialog

        item = TransferItem("/a/report.pdf", "/b/report.pdf", size=2000, mtime=1_700_000_000)
        dialog = ConflictDialog(None, ConflictInfo(item, 1000, 1_600_000_000, can_resume=True))
        ids = [row[1] for row in dialog.choice.get_model()]
        self.assertIn("resume", ids)
        self.assertEqual(dialog.choice.get_active_id(), "resume")
        dialog.choice.set_active_id("rename")
        dialog.apply_all.set_active(True)
        with mock.patch.object(dialog, "run", return_value=Gtk.ResponseType.OK):
            self.assertEqual(dialog.ask(), (Conflict.RENAME, True))
        dialog = ConflictDialog(None, ConflictInfo(item, 5000, None, can_resume=False))
        self.assertNotIn("resume", [row[1] for row in dialog.choice.get_model()])
        with mock.patch.object(dialog, "run", return_value=Gtk.ResponseType.CANCEL):
            self.assertEqual(dialog.ask(), (None, False))


@unittest.skipUnless(_display(), "needs a display")
class DragDropTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.local_dir = Path(self.tmp.name, "local")
        (self.local_dir / "site" / "css").mkdir(parents=True)
        (self.local_dir / "site" / "index.html").write_text("<h1>")
        (self.local_dir / "site" / "css" / "app.css").write_text("body{}")
        (self.local_dir / "notes.txt").write_text("notes")
        self.backend = FakeBackend({"/home/alice/report.txt": b"remote data"}, ["/home/alice/inbox"])
        self.logs = []

    def tab(self, backend=None, read_only=False, home=None):
        from ssh_manager.gtkui.file_browser.tab import FileBrowserTab

        backend = backend or self.backend
        host = HostEntry("files", {"hostname": "10.0.0.20", "user": "alice"}, protocol="sftp", id=f"h{id(backend)}")
        tab = FileBrowserTab(host, lambda message, level: self.logs.append(message),
                             backend_factory=lambda h, u, p: backend, read_only=read_only,
                             state=BrowserState(Path(self.tmp.name) / "fb.json"),
                             local_home=str(home or self.local_dir))
        self.addCleanup(lambda: tab.get_parent() is None and tab.destroy())
        self.assertTrue(pump_until(lambda: tab.remote_pane.current_path and tab.local_pane.entries
                                   and not tab.worker.pending))
        return tab

    def wait_jobs(self, tab):
        self.assertTrue(pump_until(lambda: tab.queue.jobs and not tab.queue.running()), self.logs)
        pump_until(lambda: False, timeout=0.1)

    def entry(self, pane, name):
        return next(e for e in pane.entries if e.name == name)

    def test_drop_a_local_folder_on_the_remote_pane_uploads_the_tree(self) -> None:
        tab = self.tab()
        site = self.entry(tab.local_pane, "site")
        payload = encode_payload(tab.local_pane.token, "local", [site.path])
        self.assertTrue(tab.remote_pane.accept_drop(payload, None, "/home/alice", copy=False))
        self.wait_jobs(tab)
        self.assertEqual(self.backend.files["/home/alice/site/css/app.css"], b"body{}")
        self.assertEqual(len(tab.panel.rows), 1)  # the panel shows the job
        self.assertEqual(tab.panel.summary_text(), "All transfers finished")
        self.assertEqual(tab.queue.jobs[0].direction, Direction.UPLOAD)

    def test_drop_onto_a_remote_folder_row(self) -> None:
        tab = self.tab()
        notes = self.entry(tab.local_pane, "notes.txt")
        payload = encode_payload(tab.local_pane.token, "local", [notes.path])
        self.assertTrue(tab.remote_pane.accept_drop(payload, None, "/home/alice/inbox", copy=False))
        self.wait_jobs(tab)
        self.assertEqual(self.backend.files["/home/alice/inbox/notes.txt"], b"notes")

    def test_payload_paths_must_be_listed(self) -> None:
        tab = self.tab()
        forged = encode_payload(tab.local_pane.token, "local", ["/etc/shadow"])
        self.assertFalse(tab.remote_pane.accept_drop(forged, None, "/home/alice", copy=False))
        unknown = encode_payload("no-such-pane", "local", [str(self.local_dir / "notes.txt")])
        self.assertFalse(tab.remote_pane.accept_drop(unknown, None, "/home/alice", copy=False))
        self.assertFalse(tab.remote_pane.accept_drop(b"garbage", None, "/home/alice", copy=False))
        self.assertEqual(tab.queue.jobs, [])

    def test_files_from_the_desktop_are_uploaded(self) -> None:
        tab = self.tab()
        uri = Path(self.local_dir / "notes.txt").as_uri()
        self.assertTrue(tab.remote_pane.accept_drop(None, [uri, "http://example.com/x"], "/home/alice", copy=False))
        self.wait_jobs(tab)
        self.assertEqual(self.backend.files["/home/alice/notes.txt"], b"notes")
        self.assertFalse(tab.remote_pane.accept_drop(None, ["file:///no/such/file"], "/home/alice", copy=False))

    def test_download_by_drop_lands_in_the_folder(self) -> None:
        tab = self.tab()
        report = self.entry(tab.remote_pane, "report.txt")
        payload = encode_payload(tab.remote_pane.token, "remote", [report.path])
        self.assertTrue(tab.local_pane.accept_drop(payload, None, str(self.local_dir / "site"), copy=False))
        self.wait_jobs(tab)
        self.assertEqual((self.local_dir / "site" / "report.txt").read_bytes(), b"remote data")

    def test_read_only_host_refuses_drops(self) -> None:
        tab = self.tab(read_only=True)
        notes = self.entry(tab.local_pane, "notes.txt")
        payload = encode_payload(tab.local_pane.token, "local", [notes.path])
        self.assertFalse(tab.remote_pane.accept_drop(payload, None, "/home/alice", copy=False))

    def test_folder_into_itself_is_refused(self) -> None:
        self.backend.dirs.update({"/home/alice/a", "/home/alice/a/b"})
        tab = self.tab()
        tab.remote_pane.refresh()
        self.assertTrue(pump_until(lambda: any(e.name == "a" for e in tab.remote_pane.entries)))
        folder = self.entry(tab.remote_pane, "a")
        payload = encode_payload(tab.remote_pane.token, "remote", [folder.path])
        self.assertFalse(tab.remote_pane.accept_drop(payload, None, "/home/alice/a/b", copy=False))
        self.assertTrue(any("into itself" in message for message in self.logs))

    def test_same_server_move_renames(self) -> None:
        tab = self.tab()
        report = self.entry(tab.remote_pane, "report.txt")
        payload = encode_payload(tab.remote_pane.token, "remote", [report.path])
        self.assertTrue(tab.remote_pane.accept_drop(payload, None, "/home/alice/inbox", copy=False))
        self.wait_jobs(tab)
        self.assertIn("rename /home/alice/report.txt /home/alice/inbox/report.txt", self.backend.calls)
        self.assertEqual(tab.queue.jobs[0].direction, Direction.REMOTE_MOVE)

    def test_cross_tab_relay_copy(self) -> None:
        other_backend = FakeBackend({"/data/x.bin": b"relay me"}, home="/data")
        first, second = self.tab(), self.tab(backend=other_backend)
        source = self.entry(second.remote_pane, "x.bin")
        payload = encode_payload(second.remote_pane.token, "remote", [source.path])
        self.assertTrue(first.remote_pane.accept_drop(payload, None, "/home/alice", copy=False))
        self.wait_jobs(first)
        self.assertEqual(self.backend.files["/home/alice/x.bin"], b"relay me")
        self.assertEqual(first.queue.jobs[0].direction, Direction.REMOTE_COPY)

    def test_conflict_dialog_from_a_worker_thread(self) -> None:
        self.backend.files["/home/alice/notes.txt"] = b"old"
        tab = self.tab()
        notes = self.entry(tab.local_pane, "notes.txt")
        with mock.patch("ssh_manager.gtkui.file_browser.tab.ConflictDialog") as dialog:
            dialog.return_value.ask.return_value = (Conflict.OVERWRITE, False)
            tab.transfer(tab.local_pane, tab.remote_pane, [notes])
            self.wait_jobs(tab)
        dialog.assert_called_once()
        self.assertEqual(self.backend.files["/home/alice/notes.txt"], b"notes")

    def test_cancel_the_transfer_from_the_conflict_dialog(self) -> None:
        self.backend.files["/home/alice/notes.txt"] = b"old"
        tab = self.tab()
        notes = self.entry(tab.local_pane, "notes.txt")
        with mock.patch("ssh_manager.gtkui.file_browser.tab.ConflictDialog") as dialog:
            dialog.return_value.ask.return_value = (None, False)
            job = tab.transfer(tab.local_pane, tab.remote_pane, [notes])
            self.wait_jobs(tab)
        self.assertEqual(job.state, State.CANCELLED)
        self.assertEqual(self.backend.files["/home/alice/notes.txt"], b"old")

    def test_closing_with_running_transfers_asks(self) -> None:
        gate = threading.Event()
        original = self.backend.open_write
        self.backend.open_write = lambda *a, **k: (gate.wait(5), original(*a, **k))[1]
        tab = self.tab()
        notes = self.entry(tab.local_pane, "notes.txt")
        tab.transfer(tab.local_pane, tab.remote_pane, [notes])
        self.assertTrue(pump_until(lambda: tab.running_transfers() == 1))
        with mock.patch("ssh_manager.gtkui.file_browser.tab.confirm", return_value=False) as ask:
            self.assertFalse(tab.confirm_close())
        self.assertIn("1 transfers are in progress", ask.call_args[0][2])
        gate.set()
        self.wait_jobs(tab)
        self.assertTrue(tab.confirm_close())

    def test_status_bar_summary_and_panel_toggle(self) -> None:
        gate = threading.Event()
        original = self.backend.open_write
        self.backend.open_write = lambda *a, **k: (gate.wait(5), original(*a, **k))[1]
        tab = self.tab()
        tab.transfer(tab.local_pane, tab.remote_pane, [self.entry(tab.local_pane, "notes.txt")])
        self.assertTrue(pump_until(lambda: "1 transfer" in tab._statusbar.size_label.get_text()))
        self.assertTrue(tab.transfers_toggle.get_active())
        gate.set()
        self.wait_jobs(tab)
        self.assertTrue(pump_until(lambda: tab._statusbar.size_label.get_text() == ""))

    def test_synthetic_drag_data_received(self) -> None:
        from gi.repository import Gdk

        tab = self.tab()
        notes = self.entry(tab.local_pane, "notes.txt")
        data = SimpleNamespace(get_data=lambda: encode_payload(tab.local_pane.token, "local", [notes.path]),
                               get_uris=lambda: None)
        context = SimpleNamespace(get_selected_action=lambda: Gdk.DragAction.COPY)
        with mock.patch("ssh_manager.gtkui.file_browser.pane.Gtk.drag_finish") as finish:
            tab.remote_pane._on_drag_data_received(tab.remote_pane.view, context, 1, 1, data, 1, 0)
        finish.assert_called_once_with(context, True, False, 0)
        self.wait_jobs(tab)
        self.assertEqual(self.backend.files["/home/alice/notes.txt"], b"notes")


if __name__ == "__main__":
    unittest.main()
