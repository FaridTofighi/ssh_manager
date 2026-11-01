"""Running FreeRDP: the password only through stdin, masked output, exit codes, no orphans, stale reaper."""

import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from ophion.rdp.command import build_launch
from ophion.rdp.engines import ClientBinary, parse_help_options
from ophion.rdp.process import ChildRegistry, RdpProcess, SessionLimiter, _start_time, reap_stale
from ophion.rdp.settings import RdpIdentity, RdpSettings
from tests import fake_freerdp

FIXTURES = Path(__file__).parent / "fixtures" / "freerdp"
PASSWORD = 'pa ss "w,rd" ünï $HOME `id`'


def wait_for(predicate, timeout=10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


class ProcessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.record = self.dir / "record.jsonl"
        self.registry = ChildRegistry(self.dir / "run" / "children.json")

    def client(self, name="xfreerdp3", help_file="help-3.15.txt", version=(3, 15, 0)):
        path = fake_freerdp.install(self.dir, name)
        options = parse_help_options((FIXTURES / help_file).read_text())
        return ClientBinary(name, str(path), version, ".".join(map(str, version)), options)

    def run_plan(self, client, timeout=10, **script):
        plan = build_launch(hostname="10.0.0.50", port=3389, settings=RdpSettings(), identity=RdpIdentity("alice"),
                            password=PASSWORD, client=client, accepted_fingerprints=["AB" * 32])
        exits = []
        done = threading.Event()
        with mock.patch.dict(os.environ, fake_freerdp.env_for(self.record, **script)):
            process = RdpProcess(plan, on_exit=lambda code: (exits.append(code), done.set()), registry=self.registry)
        self.addCleanup(lambda: process.running and process.stop_now(0.5))
        return process, exits, done

    def test_v3_password_only_through_stdin(self) -> None:
        process, exits, done = self.run_plan(self.client())
        self.assertTrue(done.wait(10))
        record = fake_freerdp.records(self.record)[0]
        self.assertEqual(record["argv"][1:], ["/args-from:stdin"])
        for where in ("argv", "env", "cmdline"):
            self.assertNotIn(PASSWORD, json.dumps(record[where]), where)
            self.assertNotIn("pa ss", json.dumps(record[where]), where)
        lines = record["stdin"].split("\n")
        self.assertIn("/p:" + PASSWORD, lines)  # byte-exact, spaces, quotes and Unicode included
        self.assertTrue(record["stdin"].endswith("\n\n"))
        self.assertEqual(exits, [0])

    def test_v2_password_on_stdin_only(self) -> None:
        process, exits, done = self.run_plan(self.client("xfreerdp", "help-2.11.txt", (2, 11, 7)))
        self.assertTrue(done.wait(10))
        record = fake_freerdp.records(self.record)[0]
        self.assertEqual(record["stdin"], PASSWORD + "\n")
        self.assertIn("/from-stdin:force", record["argv"])
        self.assertNotIn(PASSWORD, record["cmdline"] + json.dumps(record["env"]))

    def test_output_is_masked_stripped_and_capped(self) -> None:
        lines = [f"[ERROR][com.freerdp.core] echo {PASSWORD}", "\x1b[31mred\x1b[0m text", "x" * 10000]
        process, exits, done = self.run_plan(self.client(), lines=lines, exit=134)
        seen = []
        process._on_line = seen.append
        self.assertTrue(done.wait(10))
        output = process.output()
        self.assertNotIn(PASSWORD, "\n".join(output))
        self.assertIn("[ERROR][com.freerdp.core] echo ***", output)
        self.assertIn("red text", output)
        self.assertTrue(all(len(line) <= 4096 for line in output))
        self.assertEqual(exits, [134])

    def test_stop_kills_the_whole_group(self) -> None:
        process, exits, done = self.run_plan(self.client(), child=True, sleep=60)
        self.assertTrue(wait_for(lambda: fake_freerdp.records(self.record)))
        child = fake_freerdp.records(self.record)[0]["child"]
        self.assertTrue(fake_freerdp.alive(child))
        self.assertIn(str(process.pid), self.registry.entries())
        process.stop(grace=0.5)
        self.assertTrue(done.wait(10))
        self.assertTrue(process.stopped_by_user)
        self.assertTrue(wait_for(lambda: not fake_freerdp.alive(child), 5), "the child outlived the client")
        self.assertEqual(self.registry.entries(), {})

    def test_sigterm_ignored_then_sigkill(self) -> None:
        script = fake_freerdp.install(self.dir, "stubborn")
        script.write_text(script.read_text().replace("import json, os, subprocess, sys, time",
                                                     "import json, os, subprocess, sys, time, signal\n"
                                                     "signal.signal(signal.SIGTERM, signal.SIG_IGN)"))
        client = ClientBinary("stubborn", str(script), (3, 15, 0), "3.15.0",
                              parse_help_options((FIXTURES / "help-3.15.txt").read_text()))
        process, exits, done = self.run_plan(client, sleep=60)
        self.assertTrue(wait_for(lambda: fake_freerdp.records(self.record)))
        started = time.monotonic()
        process.stop(grace=0.3)
        self.assertTrue(done.wait(10))
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(exits, [-9])

    def test_stop_now_is_synchronous(self) -> None:
        process, exits, done = self.run_plan(self.client(), sleep=60)
        self.assertTrue(wait_for(lambda: fake_freerdp.records(self.record)))
        process.stop_now(0.5)
        self.assertFalse(process.running)


class ReaperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.registry = ChildRegistry(Path(self.tmp.name) / "children.json")
        self.script = fake_freerdp.install(Path(self.tmp.name), "xfreerdp3")

    def spawn(self):
        env = dict(os.environ, **fake_freerdp.env_for(Path(self.tmp.name) / "r.jsonl", sleep=60))
        proc = subprocess.Popen([str(self.script), "/args-from:stdin"], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, env=env)
        self.addCleanup(lambda: (proc.poll() is None and proc.kill(), proc.wait(5)))
        self.assertTrue(wait_for(lambda: _start_time(proc.pid) is not None))
        return proc

    def dead_pid(self) -> int:
        proc = subprocess.Popen(["true"])
        proc.wait()
        return proc.pid

    def test_reaps_children_of_a_crashed_app(self) -> None:
        proc = self.spawn()
        time.sleep(0.2)  # let the interpreter replace the shebang line in cmdline
        data = {str(proc.pid): {"pgid": proc.pid, "start": _start_time(proc.pid), "binary": str(self.script),
                                "owner": self.dead_pid()}}
        self.registry._save(data)
        cmd0 = Path(f"/proc/{proc.pid}/cmdline").read_bytes().split(b"\0")[0].decode()
        data[str(proc.pid)]["binary"] = cmd0  # the interpreter runs the script: record what /proc shows
        self.registry._save(data)
        handled = reap_stale(self.registry)
        self.assertEqual(handled, [proc.pid])
        self.assertTrue(wait_for(lambda: proc.poll() is not None, 5))
        self.assertEqual(self.registry.entries(), {})

    def test_never_kills_a_reused_pid_or_a_live_owner(self) -> None:
        proc = self.spawn()
        self.registry._save({str(proc.pid): {"pgid": proc.pid, "start": 1, "binary": "/usr/bin/xfreerdp3",
                                             "owner": self.dead_pid()}})
        self.assertEqual(reap_stale(self.registry), [])
        self.assertIsNone(proc.poll())  # untouched
        self.registry._save({str(proc.pid): {"pgid": proc.pid, "start": _start_time(proc.pid),
                                             "binary": str(self.script), "owner": os.getppid()}})
        self.assertEqual(reap_stale(self.registry), [])  # another running instance owns it
        self.assertIn(str(proc.pid), self.registry.entries())

    def test_garbage_registry(self) -> None:
        self.registry.path.write_text("not json")
        self.assertEqual(reap_stale(self.registry), [])
        self.registry.path.write_text(json.dumps({"abc": {"pgid": "x"}, "123": "nope"}))
        self.assertEqual(reap_stale(self.registry), [])

    def test_registry_file_is_private(self) -> None:
        self.registry.add(os.getpid(), os.getpgid(0), "/usr/bin/xfreerdp3")
        self.assertEqual(os.stat(self.registry.path).st_mode & 0o777, 0o600)
        self.registry.remove(os.getpid())
        self.assertEqual(self.registry.entries(), {})


class LimiterTests(unittest.TestCase):
    def test_limit(self) -> None:
        limiter = SessionLimiter(2)
        a, b, c = object(), object(), object()
        self.assertTrue(limiter.acquire(a) and limiter.acquire(b) and limiter.acquire(a))
        self.assertFalse(limiter.acquire(c))
        limiter.release(a)
        self.assertTrue(limiter.acquire(c))
        self.assertEqual(limiter.count, 2)


if __name__ == "__main__":
    unittest.main()
