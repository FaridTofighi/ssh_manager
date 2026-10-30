"""Run Command on hosts: the engine (no GTK) against an in-process SSH server that really runs ``exec``."""

import logging
import tempfile
import threading
import time
import unittest
from pathlib import Path

import paramiko

from ssh_manager.multirun import engine as R
from ssh_manager.multirun.engine import RunOptions, Target
from ssh_manager.transfer.settings import TransferSettings

from tests.fake_sftp import FakeSftpServer

PASSWORD = "Multirun-sentinel-pw"


class EngineCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.server = FakeSftpServer(Path(self.tmp.name), users={"alice": PASSWORD})
        self.server.__enter__()
        self.addCleanup(self.server.__exit__)
        self.known = Path(self.tmp.name) / "known_hosts"
        key = self.server.host_key
        self.known.write_text(f"[127.0.0.1]:{self.server.port} {key.get_name()} {key.get_base64()}\n")
        self.password = PASSWORD

    def connect(self, target: Target, timeout: float):
        from ssh_manager.transfer.sftp import SftpBackend

        port = target.port
        backend = SftpBackend("127.0.0.1", port, "alice", self.password, known_hosts=self.known, allow_agent=False,
                              settings=TransferSettings(timeout_connect=timeout, timeout_read=timeout))
        return backend.open_transport("Run command")

    def targets(self, count: int = 3, port=None):
        return [Target(key=f"h{i}", alias=f"host{i}", hostname="127.0.0.1", port=port or self.server.port,
                       user="alice", group="G/sub") for i in range(count)]

    def run_all(self, command: str, targets=None, **options):
        run = R.MultiRun(targets or self.targets(), command, RunOptions(**options), connect=self.connect)
        run.start()
        self.assertTrue(run.wait(60), "the run did not finish")
        return run

    def by_alias(self, run):
        return {r.alias: r for r in run.results()}


class BasicTests(EngineCase):
    def test_all_succeed_with_variables_and_lang(self) -> None:
        run = self.run_all('echo "{alias} on {host}:{port} as {user} {unknown}"; echo $LANG; echo err >&2')
        for index, result in sorted(self.by_alias(run).items()):
            with self.subTest(host=index):
                self.assertEqual(result.status, R.SUCCESS)
                self.assertEqual(result.exit_code, 0)
                lines = result.stdout.splitlines()
                self.assertEqual(lines[0], f"{index} on 127.0.0.1:{self.server.port} as alice {{unknown}}")
                self.assertEqual(lines[1], "C.UTF-8")
                self.assertEqual(result.stderr, "err\n")
                self.assertGreaterEqual(result.duration, 0.0)
                self.assertEqual(result.first_line, lines[0])
        self.assertEqual(run.summary(), (3, 3, 0))

    def test_mixed_exit_codes_and_script_with_heredoc(self) -> None:
        script = 'cat <<\'EOF\'\nquoted "text" and $NOT_EXPANDED\nEOF\ncase {alias} in host1) exit 3;; esac\n'
        results = self.by_alias(self.run_all(script))
        self.assertEqual(results["host0"].status, R.SUCCESS)
        self.assertEqual(results["host0"].stdout, 'quoted "text" and $NOT_EXPANDED\n')
        self.assertEqual((results["host1"].status, results["host1"].exit_code), (R.FAILED, 3))
        self.assertEqual(R.status_label(results["host1"]), "Failed (exit 3)")
        self.assertTrue(all(cmd.strip() == "sh -s" for cmd in self.server.exec_commands))  # the script went on stdin

    def test_pty_runs_it_as_one_string(self) -> None:
        results = self.by_alias(self.run_all("echo one; echo two", self.targets(1), pty=True))
        self.assertEqual(results["host0"].status, R.SUCCESS)
        self.assertIn("two", results["host0"].stdout)
        self.assertEqual(self.server.pty_requests, 1)
        self.assertIn("-c", self.server.exec_commands[0])

    def test_output_is_capped(self) -> None:
        result = self.by_alias(self.run_all("head -c 300000 /dev/zero | tr '\\0' x", self.targets(1),
                                            max_output=100_000))["host0"]
        self.assertEqual(result.status, R.SUCCESS)
        self.assertEqual(len(result.stdout), 100_000)
        self.assertTrue(result.stdout_truncated)

    def test_custom_shell(self) -> None:
        result = self.by_alias(self.run_all("echo $0", self.targets(1), shell="bash"))["host0"]
        self.assertEqual(result.status, R.SUCCESS)
        self.assertEqual(self.server.exec_commands[0].strip(), "bash -s")


class FailureTests(EngineCase):
    def test_timeout(self) -> None:
        result = self.by_alias(self.run_all("sleep 5", self.targets(1), command_timeout=0.5))["host0"]
        self.assertEqual(result.status, R.TIMEOUT)
        self.assertLess(result.duration, 4)

    def test_unreachable_port(self) -> None:
        import socket

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            dead = probe.getsockname()[1]
        result = self.by_alias(self.run_all("true", self.targets(1, port=dead), connect_timeout=2))["host0"]
        self.assertEqual(result.status, R.UNREACHABLE)
        self.assertTrue(result.message)

    def test_wrong_password_and_no_secret_anywhere(self) -> None:
        self.password = "wrong-" + PASSWORD
        with self.assertLogs(level=logging.DEBUG) as logs:
            logging.getLogger("ssh_manager").debug("start")
            run = self.run_all("true", self.targets(2))
        for result in run.results():
            self.assertEqual(result.status, R.AUTH_FAILED)
            self.assertIn("interactive login", result.message)
        text = "\n".join(logs.output) + repr([r.__dict__ for r in run.results()])
        self.assertNotIn(PASSWORD, text)

    def test_host_key_mismatch(self) -> None:
        self.known.write_text(f"[127.0.0.1]:{self.server.port} ecdsa-sha2-nistp256 "
                              f"{paramiko.ECDSAKey.generate().get_base64()}\n")
        result = self.by_alias(self.run_all("true", self.targets(1)))["host0"]
        self.assertEqual(result.status, R.HOST_KEY_CHANGED)
        self.assertEqual(self.server.exec_commands, [])  # nothing ran

    def test_host_known_by_another_offered_type_runs(self) -> None:
        from tests.test_transfer_backends import _ed25519

        ed, rsa = _ed25519(Path(self.tmp.name)), paramiko.RSAKey.generate(2048)
        server = FakeSftpServer(Path(self.tmp.name), users={"alice": PASSWORD}, host_key=ed,
                                extra_host_keys=(self.server.host_key, rsa))  # ed25519 first, like OpenSSH servers
        server.__enter__()
        self.addCleanup(server.__exit__)
        key = self.server.host_key  # known_hosts has only the ECDSA key
        self.known.write_text(f"[127.0.0.1]:{server.port} {key.get_name()} {key.get_base64()}\n")
        result = self.by_alias(self.run_all("true", self.targets(1, port=server.port)))["host0"]
        self.assertEqual((result.status, result.message), (R.SUCCESS, ""))  # was "Host key changed"

    def test_changed_key_message_names_host_type_and_fingerprints(self) -> None:
        from ssh_manager.transfer.backend import HostKeyChanged
        from ssh_manager.transfer.sftp import key_fingerprint

        stale = paramiko.ECDSAKey.generate()
        self.known.write_text(f"[127.0.0.1]:{self.server.port} ecdsa-sha2-nistp256 {stale.get_base64()}\n")
        result = self.by_alias(self.run_all("true", self.targets(1)))["host0"]
        self.assertEqual(result.status, R.HOST_KEY_CHANGED)
        for part in (f"[127.0.0.1]:{self.server.port}", "ecdsa-sha2-nistp256", key_fingerprint(self.server.host_key),
                     key_fingerprint(stale)):
            self.assertIn(part, result.message)
        self.assertIsInstance(result.host_key_error, HostKeyChanged)

    def test_sudo_hint(self) -> None:
        result = self.by_alias(self.run_all("sudo true; echo 'sudo: a password is required' >&2; exit 1",
                                            self.targets(1)))["host0"]
        self.assertEqual(result.status, R.FAILED)
        self.assertIn("NOPASSWD", result.hint)


class ControlTests(EngineCase):
    def test_concurrency_is_respected(self) -> None:
        run = self.run_all("sleep 0.3", self.targets(12), concurrency=3)
        self.assertEqual(run.max_running, 3)
        self.assertEqual(run.summary(), (12, 12, 0))

    def test_cancel(self) -> None:
        run = R.MultiRun(self.targets(8), "sleep 3", RunOptions(concurrency=2), connect=self.connect)
        run.start()
        time.sleep(0.8)
        started = time.monotonic()
        run.cancel()
        self.assertTrue(run.wait(10))
        self.assertLess(time.monotonic() - started, 3)
        statuses = [r.status for r in run.results()]
        self.assertEqual(statuses.count(R.CANCELLED), 8)
        self.assertFalse([t for t in threading.enumerate() if t.name.startswith(R.THREAD_PREFIX)])

    def test_stop_at_first_failure(self) -> None:
        targets = self.targets(6)
        run = self.run_all("case {alias} in host0) exit 1;; *) sleep 0.5;; esac", targets, concurrency=1,
                           stop_at_first_failure=True)
        statuses = [r.status for r in run.results()]
        self.assertEqual(statuses[0], R.FAILED)
        self.assertTrue(all(s == R.CANCELLED for s in statuses[1:]), statuses)

    def test_events_queue_and_skipped_hosts(self) -> None:
        targets = self.targets(2) + [Target(key="r", alias="desktop", hostname="x", port=3389, protocol="rdp")]
        run = self.run_all("true", targets)
        events = []
        while not run.events.empty():
            events.append(run.events.get_nowait())
        keys = {key for key, _result in events}
        self.assertEqual(keys, {"h0", "h1", "r"})
        self.assertEqual(self.by_alias(run)["desktop"].status, R.SKIPPED)
        self.assertEqual(run.summary(), (3, 2, 0))  # skipped is neither ok nor failed
        self.assertEqual(run.skipped, 1)

    def test_substitution_rules(self) -> None:
        target = Target(key="k", alias="web", hostname="web.example", port=2222, user="ops")
        self.assertEqual(R.substitute("{alias} {host} {user} {port} {other} {{alias}}", target),
                         "web web.example ops 2222 {other} {alias}")


if __name__ == "__main__":
    unittest.main()
