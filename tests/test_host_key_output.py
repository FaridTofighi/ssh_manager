"""Detecting ssh's host-key messages in terminal output (real captures in ``tests/fixtures/ssh_output``)."""

import random
import unittest
from pathlib import Path

from ssh_manager.host_key_output import HostKeyOutputDetector, detect_changed, detect_unknown

FIXTURES = Path(__file__).parent / "fixtures" / "ssh_output"


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def with_ansi(data: bytes) -> bytes:
    """Colour and cursor sequences like a terminal session adds."""

    return b"\x1b[0m\x1b[?2004l" + data.replace(b"WARNING", b"\x1b[1;31mWARNING\x1b[0m").replace(
        b"Host key verification failed.", b"\x1b[31mHost key verification failed.\x1b[0m")


class ChangedKeyTests(unittest.TestCase):
    CASES = {"changed_ecdsa.txt": "ECDSA", "changed_ed25519.txt": "ED25519", "changed_rsa.txt": "RSA"}

    def test_real_outputs_are_parsed(self) -> None:
        for name, key_type in self.CASES.items():
            with self.subTest(name=name):
                change = detect_changed(fixture(name).decode(), exited=True)
                self.assertIsNotNone(change)
                self.assertEqual(change.key_type, key_type)
                self.assertEqual((change.known_hosts_path, change.line_no), ("/home/user/.ssh/known_hosts", 3))
                self.assertRegex(change.new_fingerprint, r"^SHA256:[A-Za-z0-9+/]{43}$")
                self.assertEqual(change.host, "[127.0.0.1]:2222")
                self.assertEqual(change.host_and_port(), ("127.0.0.1", 2222))

    def test_chunks_at_any_byte_boundary_and_with_ansi(self) -> None:
        rng = random.Random(7)
        for name in self.CASES:
            data = with_ansi(fixture(name))
            for _attempt in range(25):
                detector = HostKeyOutputDetector()
                position = 0
                while position < len(data):
                    step = rng.randint(1, 40)
                    detector.feed(data[position:position + step])
                    position += step
                with self.subTest(name=name):
                    change = detector.changed(exited=True)
                    self.assertIsNotNone(change)
                    self.assertEqual(change.line_no, 3)

    def test_decoys_never_trigger(self) -> None:
        self.assertIsNone(detect_changed(fixture("decoy_cat.txt").decode(), exited=True))  # a prompt came after it
        self.assertIsNone(detect_changed(fixture("changed_ecdsa.txt").decode(), exited=False))  # ssh still runs
        partial = fixture("changed_ecdsa.txt").decode().replace("Host key verification failed.", "")
        self.assertIsNone(detect_changed(partial, exited=True))
        loose = fixture("changed_ecdsa.txt").decode().replace("@@@@@@@@", "")  # not the exact ssh block
        self.assertIsNone(detect_changed(loose, exited=True))
        self.assertIsNone(detect_unknown(fixture("changed_ecdsa.txt").decode()))

    def test_bounded_buffer(self) -> None:
        detector = HostKeyOutputDetector()
        detector.feed(b"x" * 1_000_000 + b"\r\n")
        detector.feed(fixture("changed_rsa.txt"))
        self.assertLessEqual(len(detector.text), HostKeyOutputDetector.LIMIT)
        self.assertIsNotNone(detector.changed(exited=True))
        detector.reset()
        self.assertIsNone(detector.changed(exited=True))


class UnknownHostTests(unittest.TestCase):
    def test_the_prompt_is_parsed(self) -> None:
        prompt = detect_unknown(fixture("unknown_host.txt").decode())
        self.assertIsNotNone(prompt)
        self.assertEqual((prompt.host, prompt.key_type), ("[127.0.0.1]:2222", "ECDSA"))
        self.assertRegex(prompt.fingerprint, r"^SHA256:[A-Za-z0-9+/]{43}$")

    def test_only_while_ssh_waits_for_the_answer(self) -> None:
        text = fixture("unknown_host.txt").decode()
        self.assertIsNone(detect_unknown(text + "yes\r\nWarning: Permanently added ...\r\nuser@web:~$ "))
        self.assertIsNone(detect_unknown("user@web:~$ cat notes\r\n" + text + "\r\nuser@web:~$ "))
        old = text.replace("(yes/no/[fingerprint])? ", "(yes/no)? ")
        self.assertIsNotNone(detect_unknown(old))  # older OpenSSH

    def test_chunks(self) -> None:
        data = with_ansi(fixture("unknown_host.txt"))
        for split in range(1, len(data)):
            detector = HostKeyOutputDetector()
            detector.feed(data[:split])
            detector.feed(data[split:])
            self.assertIsNotNone(detector.unknown())


if __name__ == "__main__":
    unittest.main()
