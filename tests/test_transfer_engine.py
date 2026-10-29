"""Phase F3: planner, queue, conflicts, retries, limits, safe names, drag-and-drop rules (pure)."""

import os
import threading
import time
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ssh_manager.transfer import engine as eng
from ssh_manager.transfer.backend import F_REST, Disconnected, EntryKind, RemoteEntry
from ssh_manager.transfer.dnd import (
    DropAction, Payload, decode_payload, encode_payload, into_itself, parse_uri_list, resolve_drop, uri_list,
    validate_payload,
)
from ssh_manager.transfer.engine import (
    Conflict, Direction, Endpoint, RateLimiter, State, TransferJob, TransferQueue, plan, summarize,
)
from ssh_manager.transfer.local import LocalBackend
from ssh_manager.transfer.names import UnsafePath, contained_path, safe_local_name, unique_name

from tests.fake_backend import FakeBackend


class RestBackend(FakeBackend):
    """In-memory backend that can resume and set modification times."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.mtimes = {}
        self.open_count = 0
        self.max_open = 0
        self._lock = threading.Lock()

    def features(self):
        return {F_REST}

    def set_mtime(self, path, mtime):
        self.mtimes[path] = mtime


def wait(job: TransferJob, timeout: float = 10.0) -> TransferJob:
    deadline = time.monotonic() + timeout
    while job.state not in eng.FINISHED and time.monotonic() < deadline:
        time.sleep(0.01)
    return job


def entries(backend, paths):
    return [backend.stat(p) for p in paths]


class NamesTests(unittest.TestCase):
    MALICIOUS = ["../../.bashrc", "/etc/passwd", "a/../../b", "..", ".", "C:\\Windows\\x.dll", "evil\0name",
                 "line\nbreak", "x" * 400 + ".txt", "\u202egnp.exe", "trailing. . ", "CON.txt"]

    def test_every_malicious_name_stays_one_safe_name(self) -> None:
        for name in self.MALICIOUS:
            with self.subTest(name=name[:30]):
                safe, warnings = safe_local_name(name)
                self.assertNotIn("/", safe)
                self.assertNotIn("\\", safe)
                self.assertNotIn("\0", safe)
                self.assertNotIn(safe, ("", ".", ".."))
                self.assertLessEqual(len(safe.encode("utf-8", "surrogateescape")), 255)
                self.assertFalse(any(ch in safe for ch in "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"))
        self.assertEqual(safe_local_name("../../.bashrc")[0], ".._.._.bashrc")
        self.assertEqual(safe_local_name("C:\\Windows\\x.dll")[0], "C__Windows_x.dll")
        self.assertIn("Windows reserved name", safe_local_name("CON.txt")[1])
        long_a, long_b = safe_local_name("a" * 300 + ".txt")[0], safe_local_name("a" * 299 + "b.txt")[0]
        self.assertNotEqual(long_a, long_b)
        self.assertTrue(long_a.endswith(".txt"))

    def test_contained_path_resists_symlinks_out_of_the_destination(self) -> None:
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as outside:
            os.symlink(outside, os.path.join(root, "escape"))
            with self.assertRaises(UnsafePath):
                contained_path(Path(root), "escape", "file.txt")
            self.assertEqual(contained_path(Path(root), "../x"), Path(root).resolve() / ".._x")

    def test_unique_name(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            Path(root, "report.pdf").write_text("x")
            Path(root, "report (1).pdf").write_text("x")
            self.assertEqual(unique_name(Path(root), "report.pdf"), "report (2).pdf")
            self.assertEqual(unique_name(Path(root), "Makefile"), "Makefile (1)")


class PlannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def job(self, direction, sources, destination, source, target, **kwargs):
        return TransferJob(direction, sources, destination, source, target, **kwargs)

    def test_download_plan_is_recursive_safe_and_keeps_empty_folders(self) -> None:
        remote = FakeBackend({"/srv/site/index.html": b"<h1>", "/srv/site/css/app.css": b"x" * 10},
                             ["/srv/site/empty"], home="/srv")
        remote.files["/srv/site/../../evil"] = b"no"  # a lying server listing
        remote.files["/srv/site/..x"] = b"ok"
        remote.links["/srv/site/link"] = "/etc"
        job = self.job(Direction.DOWNLOAD, entries(remote, ["/srv/site"]), str(self.root), Endpoint(
            "r", lambda: remote), Endpoint("l", LocalBackend, local=True))
        items = plan(job, remote, eng.CancelToken())
        targets = sorted(os.path.relpath(i.target, self.root) for i in items)
        self.assertIn("site/empty", targets)
        self.assertIn("site/css/app.css", targets)
        self.assertTrue(all(not t.startswith("..") for t in targets))
        self.assertTrue(any("link" in w for w in job.warnings))  # symlinks skipped by default

    def test_symlink_loops_and_depth(self) -> None:
        base = self.root / "src"
        (base / "a" / "b").mkdir(parents=True)
        (base / "a" / "b" / "f.txt").write_text("f")
        os.symlink(base / "a", base / "a" / "b" / "loop")
        local = LocalBackend()
        job = self.job(Direction.UPLOAD, [local.stat(str(base / "a"))], "/up", Endpoint("l", LocalBackend, local=True),
                       Endpoint("r", FakeBackend), follow_symlinks=True)
        items = plan(job, local, eng.CancelToken())
        self.assertEqual(sorted(i.target for i in items if not i.is_dir), ["/up/a/b/f.txt"])
        self.assertTrue(any("loop" in w for w in job.warnings))
        with mock.patch.object(eng, "MAX_DEPTH", 1):
            job = self.job(Direction.UPLOAD, [local.stat(str(base / "a"))], "/up",
                           Endpoint("l", LocalBackend, local=True), Endpoint("r", FakeBackend))
            plan(job, local, eng.CancelToken())
            self.assertTrue(any("deeper" in w for w in job.warnings))

    def test_planning_can_be_cancelled(self) -> None:
        remote = FakeBackend({f"/d/f{i}": b"" for i in range(10)})
        token = eng.CancelToken()
        token.cancel()
        job = self.job(Direction.DOWNLOAD, entries(remote, ["/d"]), str(self.root), Endpoint("r", lambda: remote),
                       Endpoint("l", LocalBackend, local=True))
        with self.assertRaises(eng.Cancelled):
            plan(job, remote, token)


class QueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "up" / "nested" / "empty").mkdir(parents=True)
        (self.root / "up" / "a.txt").write_text("alpha")
        (self.root / "up" / "nested" / "b.bin").write_bytes(b"b" * 70_000)
        self.remote = RestBackend(home="/srv")
        self.queue = TransferQueue(concurrency=3, sleep=lambda _s: None)
        self.addCleanup(self.queue.shutdown)
        self.local = Endpoint("local", LocalBackend, local=True)
        self.server = Endpoint("server", lambda: self.remote, max_connections=2)

    def upload(self, **kwargs):
        local = LocalBackend()
        job = TransferJob(Direction.UPLOAD, [local.stat(str(self.root / "up"))], "/srv", self.local, self.server,
                          **kwargs)
        return wait(self.queue.add(job))

    def test_upload_folder_tree(self) -> None:
        job = self.upload()
        self.assertEqual(job.state, State.DONE, job.error)
        self.assertEqual(self.remote.files["/srv/up/a.txt"], b"alpha")
        self.assertEqual(len(self.remote.files["/srv/up/nested/b.bin"]), 70_000)
        self.assertIn("/srv/up/nested/empty", self.remote.dirs)
        self.assertFalse([p for p in self.remote.files if p.endswith(".part")])
        self.assertIn("/srv/up/a.txt", self.remote.mtimes)  # preserved
        self.assertEqual(job.done_bytes, job.total)

    def test_download_into_a_local_folder(self) -> None:
        self.remote.files.update({"/srv/x/one.txt": b"1", "/srv/x/sub/two.txt": b"22"})
        self.remote.dirs.update({"/srv/x", "/srv/x/sub"})
        dest = self.root / "down"
        dest.mkdir()
        job = TransferJob(Direction.DOWNLOAD, [self.remote.stat("/srv/x")], str(dest), self.server, self.local)
        self.assertEqual(wait(self.queue.add(job)).state, State.DONE, job.error)
        self.assertEqual((dest / "x" / "sub" / "two.txt").read_bytes(), b"22")
        self.assertFalse(list(dest.rglob("*.part")))

    def test_conflict_policies(self) -> None:
        self.remote.files["/srv/up/a.txt"] = b"old"
        self.remote.dirs.add("/srv/up")
        skipped = self.upload(conflict=Conflict.SKIP)
        self.assertEqual(self.remote.files["/srv/up/a.txt"], b"old")
        self.assertEqual(next(i for i in skipped.items if i.target == "/srv/up/a.txt").state, State.SKIPPED)
        self.upload(conflict=Conflict.RENAME)
        self.assertEqual(self.remote.files["/srv/up/a (1).txt"], b"alpha")
        self.upload(conflict=Conflict.OVERWRITE)
        self.assertEqual(self.remote.files["/srv/up/a.txt"], b"alpha")

    def test_overwrite_if_newer(self) -> None:
        self.remote.files["/srv/up/a.txt"] = b"remote"
        self.remote.dirs.add("/srv/up")
        future = time.time() + 3600
        with mock.patch.object(FakeBackend, "_entry", wraps=self.remote._entry) as stat:
            stat.side_effect = lambda path: RemoteEntry(os.path.basename(path), path, EntryKind.FILE, size=6,
                                                        mtime=future) if path == "/srv/up/a.txt" \
                else FakeBackend._entry(self.remote, path)
            self.upload(conflict=Conflict.OVERWRITE_NEWER)
        self.assertEqual(self.remote.files["/srv/up/a.txt"], b"remote")  # the server's copy is newer

    def test_ask_with_apply_to_all(self) -> None:
        self.remote.files.update({"/srv/up/a.txt": b"x", "/srv/up/nested/b.bin": b"y"})
        self.remote.dirs.update({"/srv/up", "/srv/up/nested"})
        asked = []

        def ask(job, info):
            asked.append(info.item.name)
            return Conflict.SKIP, True

        self.queue._ask = ask
        job = self.upload()
        self.assertEqual(len(asked), 1)  # "apply to all" answered the second one
        self.assertEqual(job.counts().get(State.SKIPPED), 2)

    def test_resume_a_shorter_target(self) -> None:
        self.remote.files["/srv/up/nested/b.bin"] = b"b" * 30_000
        self.remote.dirs.update({"/srv/up", "/srv/up/nested"})
        job = self.upload(conflict=Conflict.RESUME)
        self.assertEqual(job.state, State.DONE)
        self.assertEqual(self.remote.files["/srv/up/nested/b.bin"], b"b" * 70_000)

    def test_transient_errors_are_retried_then_reported(self) -> None:
        original = self.remote.open_write
        failures = {"left": 1}

        def flaky(path, *args, **kwargs):
            if path.endswith("a.txt.part") and failures["left"]:
                failures["left"] -= 1
                raise Disconnected()
            return original(path, *args, **kwargs)

        self.remote.open_write = flaky
        job = self.upload()
        self.assertEqual(job.state, State.DONE)
        item = next(i for i in job.items if i.target.endswith("a.txt"))
        self.assertEqual(item.attempts, 1)
        failures["left"] = 99
        job = self.upload(conflict=Conflict.OVERWRITE)
        self.assertEqual(job.state, State.FAILED)
        failed = job.failures()[0]
        self.assertEqual((failed.attempts, failed.error), (3, "The connection to the server was lost."))
        failures["left"] = 0
        retry = wait(self.queue.retry(job))
        self.assertEqual(retry.state, State.DONE)
        self.assertEqual(self.remote.files["/srv/up/a.txt"], b"alpha")

    def test_cancel_removes_partial_files(self) -> None:
        gate = threading.Event()
        original = self.remote.open_write

        def slow(path, *args, **kwargs):
            gate.wait(5)
            return original(path, *args, **kwargs)

        self.remote.open_write = slow
        local = LocalBackend()
        job = self.queue.add(TransferJob(Direction.UPLOAD, [local.stat(str(self.root / "up"))], "/srv", self.local,
                                         self.server))
        time.sleep(0.05)
        self.queue.cancel(job)
        gate.set()
        self.assertEqual(wait(job).state, State.CANCELLED)
        self.assertFalse([p for p in self.remote.files if p.endswith(".part")])

    def test_connections_per_endpoint_are_limited(self) -> None:
        opened = []
        active = {"now": 0, "max": 0}
        lock = threading.Lock()
        base = self.remote

        class Counting(RestBackend):
            def open_write(self, *args, **kwargs):
                with lock:
                    active["now"] += 1
                    active["max"] = max(active["max"], active["now"])
                time.sleep(0.02)
                writer = base.open_write(*args, **kwargs)
                with lock:
                    active["now"] -= 1
                return writer

            def stat(self, path):
                return base.stat(path)

            def mkdir(self, path, parents=False):
                return base.mkdir(path, parents)

            def is_dir(self, path):
                return base.is_dir(path)

            def rename(self, old, new):
                return base.rename(old, new)

        for i in range(8):
            (self.root / "up" / f"f{i}.txt").write_text(str(i))
        server = Endpoint("server", lambda: opened.append(1) or Counting(), max_connections=2)
        local = LocalBackend()
        job = wait(self.queue.add(TransferJob(Direction.UPLOAD, [local.stat(str(self.root / "up"))], "/srv",
                                              self.local, server)))
        self.assertEqual(job.state, State.DONE, job.error)
        self.assertLessEqual(len(opened), 2)  # never more connections than max_connections
        self.assertLessEqual(active["max"], 2)
        self.assertGreaterEqual(active["max"], 1)

    def test_pause_keeps_the_part_file_and_resume_continues_it(self) -> None:
        payload = bytes(range(256)) * 12_000  # ~3 MB: many chunks
        (self.root / "up" / "big.bin").write_bytes(payload)
        local = LocalBackend()
        job = TransferJob(Direction.UPLOAD, [local.stat(str(self.root / "up" / "big.bin"))], "/srv", self.local,
                          self.server)
        paused = threading.Event()

        def listener(current):
            item = current.items[0] if current.items else None
            if item is not None and item.done > 0 and not paused.is_set():
                paused.set()
                self.queue.pause(current)

        self.queue.listeners.append(listener)
        self.queue.set_limit(1000)  # ~1 MB/s: the pause lands in the middle of the file
        self.queue.add(job)
        self.assertTrue(paused.wait(5))
        deadline = time.monotonic() + 5
        while job.items[0].state is not State.PAUSED and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(job.items[0].state, State.PAUSED)
        self.assertTrue(job.items[0].partial_in_part)
        self.assertIn("/srv/big.bin.part", self.remote.files)  # kept for the resume
        kept = len(self.remote.files["/srv/big.bin.part"])
        self.queue.set_limit(0)
        self.queue.resume(job)
        self.assertEqual(wait(job).state, State.DONE, job.error)
        self.assertEqual(self.remote.files["/srv/big.bin"], payload)
        self.assertGreater(kept, 0)
        self.assertNotIn("/srv/big.bin.part", self.remote.files)

    def test_cancelling_a_paused_job_removes_its_part_file(self) -> None:
        (self.root / "up" / "big.bin").write_bytes(b"q" * 3_000_000)
        local = LocalBackend()
        job = TransferJob(Direction.UPLOAD, [local.stat(str(self.root / "up" / "big.bin"))], "/srv", self.local,
                          self.server)
        paused = threading.Event()
        self.queue.listeners.append(lambda current: current.items and current.items[0].done and not paused.is_set()
                                    and (paused.set(), self.queue.pause(current)))
        self.queue.set_limit(1000)
        self.queue.add(job)
        self.assertTrue(paused.wait(5))
        deadline = time.monotonic() + 5
        while "/srv/big.bin.part" not in self.remote.files and time.monotonic() < deadline:
            time.sleep(0.01)
        self.queue.cancel(job)
        self.assertEqual(wait(job).state, State.CANCELLED)
        self.assertNotIn("/srv/big.bin.part", self.remote.files)
        self.assertNotIn("/srv/big.bin", self.remote.files)

    def test_remote_move_and_relay_copy(self) -> None:
        self.remote.files["/srv/old/x.txt"] = b"move me"
        self.remote.dirs.update({"/srv/old", "/srv/new"})
        job = TransferJob(Direction.REMOTE_MOVE, [self.remote.stat("/srv/old/x.txt")], "/srv/new", self.server,
                          self.server)
        self.assertEqual(wait(self.queue.add(job)).state, State.DONE, job.error)
        self.assertIn("/srv/new/x.txt", self.remote.files)
        other = RestBackend(home="/data")
        job = TransferJob(Direction.REMOTE_COPY, [self.remote.stat("/srv/new/x.txt")], "/data", self.server,
                          Endpoint("other", lambda: other))
        self.assertEqual(wait(self.queue.add(job)).state, State.DONE, job.error)
        self.assertEqual(other.files["/data/x.txt"], b"move me")

    def test_progress_is_throttled_and_summarized(self) -> None:
        events = []
        self.queue.listeners.append(lambda job: events.append(time.monotonic()))
        (self.root / "up" / "big.bin").write_bytes(b"z" * 3_000_000)
        job = self.upload()
        self.assertEqual(job.state, State.DONE)
        self.assertLess(len(events), 60)  # 12 chunks × several files, but ≤ 10 Hz + state changes
        self.assertEqual(summarize([job]), (0, 0.0))


class Boom(FakeBackend):
    """A backend with a programming error: not a BackendError, not an OSError."""

    def open_write(self, *args, **kwargs):
        raise ValueError("unexpected bug inside backend")


class UnexpectedErrorTests(unittest.TestCase):
    """An unexpected exception never leaves a job DONE with an item still RUNNING."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name in ("a.txt", "b.txt", "c.txt"):
            (self.root / name).write_text(name)
        self.queue = TransferQueue(concurrency=3, sleep=lambda _s: None)
        self.addCleanup(self.queue.shutdown)
        self.local = Endpoint("local", LocalBackend, local=True)

    def upload(self, backend, *names):
        local = LocalBackend()
        server = Endpoint("server", lambda: backend, max_connections=3)
        job = TransferJob(Direction.UPLOAD, [local.stat(str(self.root / n)) for n in names], "/srv", self.local,
                          server)
        return wait(self.queue.add(job))

    def assert_all_final(self, job) -> None:
        self.assertEqual([i.target for i in job.items if i.state not in eng.FINISHED], [])

    def test_unexpected_backend_error_fails_the_item_and_the_job(self) -> None:
        job = self.upload(Boom(home="/srv"), "a.txt")
        self.assertEqual(job.state, State.FAILED)
        self.assertEqual(job.error, "1 of 1 failed")
        (item,) = job.items
        self.assertEqual((item.state, item.error), (State.FAILED, "Unexpected error (ValueError)"))
        self.assertEqual(item.technical, "ValueError: unexpected bug inside backend")
        self.assert_all_final(job)

    def test_only_the_broken_file_fails(self) -> None:
        backend = RestBackend(home="/srv")
        original = backend.open_write

        def broken(path, *args, **kwargs):
            if "b.txt" in path:
                raise KeyError(path)
            return original(path, *args, **kwargs)

        backend.open_write = broken
        job = self.upload(backend, "a.txt", "b.txt", "c.txt")
        self.assertEqual(job.state, State.FAILED)
        self.assertEqual(job.error, "1 of 3 failed")
        states = {i.name: i.state for i in job.items}
        self.assertEqual(states, {"a.txt": State.DONE, "b.txt": State.FAILED, "c.txt": State.DONE})
        self.assertEqual(backend.files["/srv/a.txt"], b"a.txt")

    def test_unexpected_error_while_creating_a_folder(self) -> None:
        (self.root / "tree" / "sub").mkdir(parents=True)
        (self.root / "tree" / "f.txt").write_text("f")
        backend = RestBackend(home="/srv")
        backend.mkdir = mock.Mock(side_effect=ValueError("mkdir bug"))
        job = self.upload(backend, "tree")
        self.assertEqual((job.state, job.error), (State.FAILED, "ValueError"))
        self.assertTrue(job.items)
        self.assert_all_final(job)
        self.assertEqual({i.error for i in job.items}, {"Not run"})

    def test_fault_outside_the_items_own_handling(self) -> None:
        with mock.patch.object(TransferQueue, "_run_item", side_effect=RuntimeError("helper fault")):
            job = self.upload(RestBackend(home="/srv"), "a.txt")
        self.assertEqual((job.state, job.error), (State.FAILED, "1 of 1 failed"))
        self.assertEqual(job.items[0].state, State.FAILED)
        self.assertEqual(job.items[0].error, "Unexpected error (RuntimeError)")

    def test_final_job_state(self) -> None:
        def job_with(*states, cancelled=False):
            endpoint = Endpoint("x", FakeBackend)
            job = TransferJob(Direction.UPLOAD, [], "/srv", endpoint, endpoint)
            job.items = [eng.TransferItem(f"/l/{n}", f"/r/{n}", state=s) for n, s in enumerate(states)]
            if cancelled:
                job.cancel.cancel()
            return job

        cases = [
            ((State.DONE, State.DONE), {}, (State.DONE, "")),
            ((State.DONE, State.SKIPPED), {}, (State.DONE, "")),
            ((State.DONE, State.FAILED), {}, (State.FAILED, "1 of 2 failed")),
            ((State.DONE, State.RUNNING), {}, (State.FAILED, "1 of 2 failed")),
            ((State.QUEUED, State.DONE), {}, (State.FAILED, "1 of 2 failed")),
            ((State.FAILED, State.RUNNING), {}, (State.FAILED, "2 of 2 failed")),
            ((State.DONE, State.RUNNING), {"cancelled": True}, (State.CANCELLED, "")),
            ((), {}, (State.DONE, "")),
        ]
        for states, kwargs, expected in cases:
            with self.subTest(states=states, **kwargs):
                job = job_with(*states, **kwargs)
                self.assertEqual(eng.final_job_state(job), expected)
                if expected[0] is State.FAILED:
                    self.assert_all_final(job)
                    self.assertTrue(all(i.error == "Did not complete" for i in job.items
                                        if i.state is State.FAILED and states[job.items.index(i)] is not State.FAILED))


class LimiterTests(unittest.TestCase):
    def test_token_bucket(self) -> None:
        limiter = RateLimiter(1000, burst=1000)
        self.assertEqual(limiter.delay_for(500), 0.0)
        self.assertEqual(limiter.delay_for(500), 0.0)
        self.assertAlmostEqual(limiter.delay_for(500), 0.5, delta=0.05)
        self.assertEqual(RateLimiter(0).delay_for(10**9), 0.0)


class DndTests(unittest.TestCase):
    def test_payload_round_trip_and_validation(self) -> None:
        data = encode_payload("pane-1", "remote", ["/srv/a", "/srv/b c"])
        payload = decode_payload(data)
        self.assertEqual(payload, Payload("pane-1", "remote", ("/srv/a", "/srv/b c")))
        self.assertTrue(validate_payload(payload, ["/srv/a", "/srv/b c", "/srv/other"]))
        self.assertFalse(validate_payload(payload, ["/srv/a"]))  # not listed: rejected
        for bad in (b"", b"not json", b'{"v": 2, "token": "t", "side": "remote", "paths": ["/a"]}',
                    b'{"v": 1, "token": "t", "side": "nowhere", "paths": ["/a"]}',
                    b'{"v": 1, "token": "t", "side": "local", "paths": []}',
                    b'{"v": 1, "token": "t", "side": "local", "paths": ["/a\\u0000b"]}', b"x" * (5 * 1024 * 1024)):
            self.assertIsNone(decode_payload(bad), bad[:40])

    def test_uri_lists(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            spaced = Path(root, "my file ü.txt")
            spaced.write_text("x")
            text = (f"# comment\r\n{uri_list([str(spaced)])}file://localhost{root}/missing\r\n"
                    f"http://example.com/x\r\nfile://otherhost/etc/passwd\r\nsftp://h/a\r\n")
            self.assertEqual(parse_uri_list(text), [str(spaced)])
            self.assertEqual(parse_uri_list(f"file://localhost{spaced}".replace(" ", "%20"), check_files=False),
                             [str(spaced)])

    def test_drop_matrix(self) -> None:
        cases = [
            (("local", "remote", False, False, False, False), DropAction.UPLOAD),
            (("remote", "local", False, False, False, False), DropAction.DOWNLOAD),
            (("remote", "remote", True, True, False, False), DropAction.MOVE),
            (("remote", "remote", True, True, True, False), DropAction.COPY),
            (("remote", "remote", False, False, False, False), DropAction.RELAY_COPY),
            (("local", "local", True, False, False, False), DropAction.MOVE),
            (("local", "local", False, False, False, False), DropAction.COPY),
            (("local", "remote", False, False, False, True), DropAction.REJECT),  # onto a file
        ]
        for (source, target, same_pane, same_server, ctrl, on_file), expected in cases:
            with self.subTest(case=(source, target, same_pane, ctrl)):
                self.assertIs(resolve_drop(source, target, same_pane=same_pane, same_server=same_server,
                                           copy_modifier=ctrl, target_is_file=on_file), expected)
        self.assertIs(resolve_drop("local", "remote", same_pane=False, same_server=False, copy_modifier=False,
                                   target_is_file=False, read_only_target=True), DropAction.REJECT)

    def test_folder_into_itself(self) -> None:
        self.assertTrue(into_itself(["/srv/a"], "/srv/a/b"))
        self.assertTrue(into_itself(["/srv/a"], "/srv/a"))
        self.assertFalse(into_itself(["/srv/a"], "/srv/ab"))


if __name__ == "__main__":
    unittest.main()
