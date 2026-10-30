"""``ssh_manager.known_hosts``: parsing, matching (plain, patterns, hashed), fingerprints, byte-preserving edits."""

import base64
import fcntl
import hashlib
import hmac
import os
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from ssh_manager import known_hosts as KH



def _keys():
    import paramiko
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    ed = Ed25519PrivateKey.generate().public_key().public_bytes(serialization.Encoding.OpenSSH,
                                                               serialization.PublicFormat.OpenSSH).split()[1].decode()
    return ed, paramiko.RSAKey.generate(2048).get_base64(), paramiko.ECDSAKey.generate().get_base64()


ED25519, RSA, ECDSA = _keys()  # real public keys, made for this test run


def hashed_name(name: str, salt: bytes = b"0123456789abcdef0123") -> str:
    digest = hmac.new(salt, name.encode(), hashlib.sha1).digest()
    return f"|1|{base64.b64encode(salt).decode()}|{base64.b64encode(digest).decode()}"


def fixture_text() -> bytes:
    lines = [
        "# a comment line",
        "",
        f"web.example.org ssh-ed25519 {ED25519} admin@laptop",
        f"[db.example.org]:2222 ecdsa-sha2-nistp256 {ECDSA}",
        f"alpha,beta,10.0.0.5 ssh-rsa {RSA}",
        f"*.corp.example ssh-ed25519 {ED25519}",
        f"*.lab,!secret.lab ssh-ed25519 {ED25519}",
        f"host?? ssh-ed25519 {ED25519}",
        f"{hashed_name('hashed.example.org')} ssh-ed25519 {ED25519}",
        f"{hashed_name('[hashed.example.org]:2200', b'saltsaltsaltsaltsalt')} ssh-ed25519 {ED25519}",
        f"@cert-authority *.ca.example ssh-ed25519 {ED25519} ca key",
        f"@revoked old.example.org ssh-rsa {RSA}",
        "this line is not valid",
        f"spaced.example.org   ssh-ed25519   {ED25519}   ",
    ]
    text = "\n".join(lines) + "\n"
    return text.encode().replace(f"{ECDSA}\n".encode(), f"{ECDSA}\r\n".encode(), 1)  # one CRLF line


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.path = self.dir / "known_hosts"
        self.path.write_bytes(fixture_text())
        os.chmod(self.path, 0o600)
        self.file = KH.KnownHostsFile(self.path)


class ParseTests(Case):
    def test_entries_and_unparsed_lines(self) -> None:
        items = self.file.load()
        entries = [i for i in items if isinstance(i, KH.Entry)]
        unparsed = [i for i in items if isinstance(i, KH.Unparsed)]
        self.assertEqual(len(items), 14)  # comments and blank lines are kept as Unparsed too
        self.assertEqual([u.raw_line for u in unparsed if u.raw_line.strip() and not u.raw_line.startswith("#")],
                         ["this line is not valid\n"])
        self.assertEqual(len(entries), 11)
        by_line = {e.line_no: e for e in entries}
        self.assertEqual(by_line[3].hosts_field, "web.example.org")
        self.assertEqual((by_line[3].key_type, by_line[3].comment), ("ssh-ed25519", "admin@laptop"))
        self.assertEqual(by_line[4].raw_line[-2:], "\r\n")
        self.assertEqual(by_line[11].marker, "@cert-authority")
        self.assertEqual(by_line[12].marker, "@revoked")
        self.assertTrue(by_line[9].hashed)
        self.assertEqual(by_line[14].key_type, "ssh-ed25519")
        self.assertEqual(b"".join(i.raw_line.encode() for i in items), fixture_text())  # lossless

    def test_matching(self) -> None:
        entries = {e.line_no: e for e in self.file.entries()}
        cases = [
            (3, "web.example.org", 22, True), (3, "web.example.org", 2222, False),
            (4, "db.example.org", 2222, True), (4, "db.example.org", 22, False),
            (5, "beta", 22, True), (5, "10.0.0.5", 22, True), (5, "gamma", 22, False),
            (6, "a.corp.example", 22, True), (6, "corp.example", 22, False),
            (7, "x.lab", 22, True), (7, "secret.lab", 22, False),
            (8, "host12", 22, True), (8, "host1", 22, False),
            (9, "hashed.example.org", 22, True), (9, "other.example.org", 22, False),
            (10, "hashed.example.org", 2200, True), (10, "hashed.example.org", 22, False),
            (3, "WEB.example.org", 22, True),  # host names are case-insensitive
        ]
        for line, name, port, expected in cases:
            with self.subTest(line=line, name=name, port=port):
                self.assertEqual(entries[line].matches(name, port), expected)
        self.assertEqual([e.line_no for e in self.file.lookup("web.example.org", 22)], [3])

    def test_fingerprints(self) -> None:
        blob = base64.b64decode(ED25519)
        expected = "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
        self.assertEqual(KH.fingerprint_sha256(ED25519), expected)
        md5 = hashlib.md5(blob).hexdigest()
        self.assertEqual(KH.fingerprint_md5(ED25519), "MD5:" + ":".join(md5[i:i + 2] for i in range(0, 32, 2)))
        self.assertEqual(KH.key_bits("ssh-ed25519", ED25519), 256)
        self.assertEqual(KH.key_bits("ecdsa-sha2-nistp256", ECDSA), 256)
        self.assertEqual(KH.key_bits("ssh-rsa", RSA), 2048)
        self.assertEqual(KH.key_type_label("ecdsa-sha2-nistp256"), "ECDSA")
        self.assertEqual(KH.key_type_label("ssh-ed25519"), "ED25519")
        self.assertEqual(KH.fingerprint_sha256("not base64!"), "")

    @unittest.skipUnless(shutil.which("ssh-keygen"), "needs ssh-keygen")
    def test_against_ssh_keygen(self) -> None:
        line = self.dir / "one.pub"
        line.write_text(f"ssh-ed25519 {ED25519} x\n")
        out = subprocess.run(["ssh-keygen", "-lf", str(line)], capture_output=True, text=True).stdout
        self.assertIn(KH.fingerprint_sha256(ED25519), out)
        hashed = self.dir / "hashed"
        hashed.write_text(f"real.example.org ssh-ed25519 {ED25519}\n[real.example.org]:2022 ssh-ed25519 {ED25519}\n")
        subprocess.run(["ssh-keygen", "-H", "-f", str(hashed)], capture_output=True, check=True)
        entries = KH.KnownHostsFile(hashed).entries()
        self.assertTrue(all(e.hashed for e in entries))
        self.assertTrue(entries[0].matches("real.example.org", 22))
        self.assertTrue(entries[1].matches("real.example.org", 2022))
        self.assertFalse(entries[0].matches("real.example.org", 2022))

    def test_ten_thousand_lines_load_fast(self) -> None:
        big = self.dir / "big"
        big.write_text("".join(f"h{i}.example.org ssh-ed25519 {ED25519}\n" for i in range(10_000)))
        started = time.perf_counter()
        entries = KH.KnownHostsFile(big).entries()
        elapsed = time.perf_counter() - started
        self.assertEqual(len(entries), 10_000)
        self.assertLess(elapsed, 0.3)


class EditTests(Case):
    def untouched(self, before: bytes, after: bytes, removed: bytes) -> None:
        self.assertEqual(after, before.replace(removed, b"", 1))

    def test_remove_keeps_every_other_byte_and_backs_up(self) -> None:
        before = self.path.read_bytes()
        entry = next(e for e in self.file.entries() if e.line_no == 4)  # the CRLF one
        self.file.remove_entries([entry])
        self.untouched(before, self.path.read_bytes(), entry.raw_line.encode())
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        backups = KH.backups(self.path)
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), before)
        self.assertEqual([p.name for p in self.dir.iterdir() if p.name.endswith(".tmp")], [])

    def test_replace_and_add(self) -> None:
        before = self.path.read_bytes()
        entry = next(e for e in self.file.entries() if e.line_no == 3)
        self.file.replace_key(entry, "ssh-rsa", RSA)
        after = self.path.read_bytes()
        new_line = f"web.example.org ssh-rsa {RSA} admin@laptop\n".encode()
        self.assertEqual(after, before.replace(entry.raw_line.encode(), new_line, 1))
        self.file.add_entry("new.example.org", 2022, "ssh-ed25519", ED25519)
        self.assertTrue(self.path.read_bytes().startswith(after))
        self.assertTrue(self.path.read_bytes().endswith(f"[new.example.org]:2022 ssh-ed25519 {ED25519}\n".encode()))
        self.file.add_entry("secret.example.org", 22, "ssh-ed25519", ED25519, hashed=True)
        last = self.file.entries()[-1]
        self.assertTrue(last.hashed and last.matches("secret.example.org", 22))
        self.assertNotIn(b"secret.example.org", self.path.read_bytes())

    def test_add_to_a_file_without_final_newline_and_a_new_file(self) -> None:
        self.path.write_bytes(b"a.example ssh-ed25519 " + ED25519.encode())
        self.file.add_entry("b.example", 22, "ssh-ed25519", ED25519)
        self.assertEqual(self.path.read_bytes().count(b"\n"), 2)
        fresh = KH.KnownHostsFile(self.dir / "ssh" / "known_hosts")
        fresh.add_entry("c.example", 22, "ssh-ed25519", ED25519)
        self.assertEqual(stat.S_IMODE((self.dir / "ssh" / "known_hosts").stat().st_mode), 0o600)

    def test_backups_are_pruned_to_five(self) -> None:
        for index in range(7):
            with mock.patch.object(KH, "_stamp", return_value=f"20260101-00000{index}"):
                self.file.add_entry(f"h{index}.example", 22, "ssh-ed25519", ED25519)
        names = [p.name for p in KH.backups(self.path)]
        self.assertEqual(len(names), 5)
        self.assertEqual(names[0], "known_hosts.bak-20260101-000006")

    def test_a_file_changed_after_reading_is_refused(self) -> None:
        entries = self.file.entries()
        time.sleep(0.01)
        with open(self.path, "ab") as handle:
            handle.write(b"other.example ssh-ed25519 " + ED25519.encode() + b"\n")
        changed = self.path.read_bytes()
        with self.assertRaises(KH.KnownHostsChanged):
            self.file.remove_entries([entries[0]])
        self.assertEqual(self.path.read_bytes(), changed)

    def test_an_entry_that_no_longer_matches_its_line_is_refused(self) -> None:
        entry = self.file.entries()[0]
        stale = KH.Entry(line_no=entry.line_no, marker="", hosts_field="elsewhere", hashed=False,
                         key_type=entry.key_type, key_b64=entry.key_b64, comment="", raw_line="elsewhere x y\n")
        with self.assertRaises(KH.KnownHostsChanged):
            self.file.remove_entries([stale])

    def test_edits_wait_for_the_lock(self) -> None:
        handle = open(self.path, "rb")
        fcntl.flock(handle, fcntl.LOCK_EX)
        done = []

        def edit():
            KH.KnownHostsFile(self.path).add_entry("locked.example", 22, "ssh-ed25519", ED25519)
            done.append(True)

        thread = threading.Thread(target=edit)
        thread.start()
        time.sleep(0.2)
        self.assertEqual(done, [])  # still waiting
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()
        thread.join(5)
        self.assertEqual(done, [True])

    def test_restore_backup(self) -> None:
        before = self.path.read_bytes()
        self.file.remove_entries(self.file.entries()[:3])
        backup = KH.backups(self.path)[0]
        KH.restore_backup(self.path, backup)
        self.assertEqual(self.path.read_bytes(), before)

    def test_merge_skips_duplicates(self) -> None:
        other = self.dir / "other"
        other.write_text(f"web.example.org ssh-ed25519 {ED25519} admin@laptop\nfresh.example ssh-ed25519 {ED25519}\n")
        incoming = KH.KnownHostsFile(other).entries()
        self.assertEqual(KH.merge_preview(self.file.entries(), incoming), (1, 1))
        before = self.path.read_bytes()
        self.assertEqual(self.file.merge(incoming), (1, 1))
        self.assertEqual(self.path.read_bytes(), before + f"fresh.example ssh-ed25519 {ED25519}\n".encode())
        self.assertEqual(self.file.merge(incoming), (0, 2))


class InventoryTests(Case):
    def test_mapping_plain_patterns_and_hashed(self) -> None:
        hosts = [("h1", "web", "web.example.org", 22), ("h2", "db", "db.example.org", 2222),
                 ("h3", "hashed", "hashed.example.org", 22), ("h4", "lab", "x.lab", 22),
                 ("h5", "never", "never.example.org", 22)]
        progress = []
        mapping = KH.map_inventory(self.file.entries(), hosts, progress=lambda done, total: progress.append(done))
        self.assertEqual({k: [e.line_no for e in v] for k, v in mapping.items()},
                         {"h1": [3], "h2": [4], "h3": [9], "h4": [7], "h5": []})
        self.assertTrue(progress)
        self.assertEqual([e.line_no for e in KH.unmatched_hashed(self.file.entries(), mapping)], [10])

    def test_alias_is_tried_too(self) -> None:
        mapping = KH.map_inventory(self.file.entries(), [("h1", "alpha", "192.0.2.1", 22)])
        self.assertEqual([e.line_no for e in mapping["h1"]], [5])


if __name__ == "__main__":
    unittest.main()
