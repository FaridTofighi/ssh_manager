"""SSH keys (no GTK): reading, fingerprints, generating, passphrases, authorized_keys, deployment."""

import base64
import hashlib
import logging
import os
import shutil
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import paramiko
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa

from ssh_manager import ssh_keys as K

PASS = "Key-passphrase-sentinel-7"


def openssh_public(private) -> str:
    return private.public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode()


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name) / ".ssh"

    def write(self, name: str, data: bytes, mode: int = 0o600) -> Path:
        self.dir.mkdir(mode=0o700, exist_ok=True)
        path = self.dir / name
        path.write_bytes(data)
        os.chmod(path, mode)
        return path


class FingerprintTests(Case):
    def test_known_vector_and_line_or_blob(self) -> None:
        private = ed25519.Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
        line = openssh_public(private) + " me@box"
        blob = base64.b64decode(line.split()[1])
        expected = "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
        self.assertEqual(K.fingerprint_sha256(line), expected)
        self.assertEqual(K.fingerprint_sha256(blob), expected)
        self.assertEqual(K.fingerprint_sha256("garbage"), "")

    @unittest.skipUnless(shutil.which("ssh-keygen"), "needs ssh-keygen")
    def test_against_ssh_keygen(self) -> None:
        for private in (ed25519.Ed25519PrivateKey.generate(), rsa.generate_private_key(65537, 2048),
                        ec.generate_private_key(ec.SECP384R1())):
            pub = self.write("k.pub", (openssh_public(private) + " x\n").encode())
            out = subprocess.run(["ssh-keygen", "-lf", str(pub)], capture_output=True, text=True).stdout
            self.assertIn(K.fingerprint_sha256(pub.read_text()), out)
            pub.unlink()


class GenerateTests(Case):
    def test_ed25519_with_passphrase_permissions_and_no_overwrite(self) -> None:
        info = K.generate_key(self.dir / "id_ed25519", comment="me@box", passphrase=PASS)
        private = self.dir / "id_ed25519"
        self.assertEqual(stat.S_IMODE(private.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.dir.stat().st_mode), 0o700)
        self.assertTrue(private.read_bytes().startswith(b"-----BEGIN OPENSSH PRIVATE KEY-----"))
        self.assertNotIn(PASS.encode(), private.read_bytes())
        self.assertEqual((info.key_type, info.bits, info.encrypted, info.comment), ("ed25519", 256, True, "me@box"))
        self.assertTrue((self.dir / "id_ed25519.pub").read_text().startswith("ssh-ed25519 AAAA"))
        paramiko.PKey.from_path(private, PASS.encode())
        with self.assertRaises(K.KeyError_):
            K.generate_key(self.dir / "id_ed25519", comment="again")  # never overwritten
        self.assertEqual(K.suggest_name(self.dir, "id_ed25519"), "id_ed25519_2")

    @unittest.skipUnless(shutil.which("ssh-keygen"), "needs ssh-keygen")
    def test_ssh_keygen_reads_the_generated_keys(self) -> None:
        for name, kind, size in (("e", "ed25519", 0), ("r", "rsa", 3072), ("c", "ecdsa", 384)):
            K.generate_key(self.dir / name, kind=kind, bits=size, comment="t", passphrase=PASS)
            with mock.patch.dict(os.environ, {}):
                # the passphrase on ssh-keygen's argv is only this test's check; the app never does that
                out = subprocess.run(["ssh-keygen", "-y", "-P", PASS, "-f", str(self.dir / name)],
                                     capture_output=True, text=True)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertEqual(out.stdout.split()[1], (self.dir / f"{name}.pub").read_text().split()[1])

    def test_without_bcrypt_falls_back_to_encrypted_pem(self) -> None:
        with mock.patch.object(K, "bcrypt_available", return_value=False):
            info = K.generate_key(self.dir / "fallback", kind="rsa", bits=3072, comment="t", passphrase=PASS)
            with self.assertRaises(K.KeyError_) as caught:
                K.generate_key(self.dir / "fallback_ed", comment="t", passphrase=PASS)
        self.assertIn("bcrypt", str(caught.exception))
        self.assertFalse((self.dir / "fallback_ed").exists())
        data = (self.dir / "fallback").read_bytes()
        self.assertTrue(data.startswith(b"-----BEGIN RSA PRIVATE KEY-----"))
        self.assertIn(b"ENCRYPTED", data)
        self.assertEqual(info.format, "pem")
        paramiko.PKey.from_path(self.dir / "fallback", PASS.encode())

    def test_change_passphrase_and_derive(self) -> None:
        K.generate_key(self.dir / "k", comment="t", passphrase=PASS)
        with self.assertRaises(K.KeyError_) as caught:
            K.change_passphrase(self.dir / "k", "wrong", "new")
        self.assertNotIn(PASS, str(caught.exception))
        K.change_passphrase(self.dir / "k", PASS, "new-pass")
        paramiko.PKey.from_path(self.dir / "k", b"new-pass")
        self.assertEqual(stat.S_IMODE((self.dir / "k").stat().st_mode), 0o600)
        (self.dir / "k.pub").unlink()
        self.assertEqual(K.read_key_info(self.dir / "k").key_type, "ed25519")  # the OpenSSH public section
        self.assertTrue(K.derive_public_line(self.dir / "k", "new-pass").startswith("ssh-ed25519 "))


class ScanTests(Case):
    def test_only_real_keys_and_no_prompt(self) -> None:
        K.generate_key(self.dir / "id_ed25519", comment="a")
        K.generate_key(self.dir / "locked", comment="b", passphrase=PASS)
        (self.dir / "locked.pub").unlink()
        rsa_key = rsa.generate_private_key(65537, 2048)
        self.write("legacy_rsa", rsa_key.private_bytes(serialization.Encoding.PEM,
                                                       serialization.PrivateFormat.TraditionalOpenSSL,
                                                       serialization.NoEncryption()))
        enc = ec.generate_private_key(ec.SECP256R1()).private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.BestAvailableEncryption(b"x"))
        self.write("pkcs8_encrypted", enc)
        self.write("garbage", b"-----BEGIN OPENSSH PRIVATE KEY-----\nnot base64 !!!\n")
        self.write("big", b"-----BEGIN OPENSSH PRIVATE KEY-----\n" + b"A" * 70_000)
        self.write("known_hosts", b"x ssh-ed25519 AAAA\n")
        self.write("config", b"Host *\n")
        self.write("authorized_keys", b"ssh-ed25519 AAAA\n")
        self.write("notes.txt", b"hello\n")
        with mock.patch("getpass.getpass", side_effect=AssertionError("prompted")):
            keys = {k.name: k for k in K.scan_ssh_dir(self.dir)}
        self.assertEqual(sorted(keys), ["garbage", "id_ed25519", "legacy_rsa", "locked", "pkcs8_encrypted"])
        self.assertEqual((keys["id_ed25519"].encrypted, keys["id_ed25519"].has_public_file), (False, True))
        self.assertEqual((keys["locked"].encrypted, keys["locked"].has_public_file), (True, False))
        self.assertEqual(keys["locked"].key_type, "ed25519")  # read without the passphrase
        self.assertEqual((keys["legacy_rsa"].key_type, keys["legacy_rsa"].bits), ("rsa", 2048))
        self.assertTrue(keys["legacy_rsa"].fingerprint.startswith("SHA256:"))
        self.assertEqual((keys["pkcs8_encrypted"].encrypted, keys["pkcs8_encrypted"].fingerprint), (True, ""))
        self.assertEqual(keys["garbage"].fingerprint, "")
        self.assertIsNone(keys["garbage"].encrypted)


class AuthorizedKeysTests(unittest.TestCase):
    LINE = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl new@box"

    def test_cases(self) -> None:
        blob = self.LINE.split()[1]
        cases = {
            "": (self.LINE + "\n", True),
            "ssh-rsa AAAAB3 other": ("ssh-rsa AAAAB3 other\n" + self.LINE + "\n", True),
            "ssh-rsa AAAAB3 other\r\n": ("ssh-rsa AAAAB3 other\r\n" + self.LINE + "\r\n", True),
            f"ssh-ed25519 {blob} old-comment\n": (f"ssh-ed25519 {blob} old-comment\n", False),
            f'from="10.0.0.1",command="echo x y" ssh-ed25519 {blob} c\n': (None, False),
            f"# ssh-ed25519 {blob} commented\n": (None, True),
        }
        for text, (expected, changed) in cases.items():
            with self.subTest(text=text):
                result, did = K.authorized_keys_add(text, self.LINE)
                self.assertEqual(did, changed)
                if expected is not None:
                    self.assertEqual(result, expected)
                if not changed:
                    self.assertEqual(result, text)
                else:
                    self.assertTrue(result.startswith(text.rstrip("\r\n")))

    def test_ten_thousand_lines(self) -> None:
        text = "".join(f"ssh-ed25519 AAAA{i:08d} u{i}\n" for i in range(10_000))
        started = time.perf_counter()
        result, changed = K.authorized_keys_add(text, self.LINE)
        self.assertLess(time.perf_counter() - started, 0.1)
        self.assertTrue(changed)
        self.assertEqual(result, text + self.LINE + "\n")


class SecretTests(Case):
    def test_passphrase_never_reaches_logs_argv_env_or_errors(self) -> None:
        seen = []
        real_popen = subprocess.Popen

        def spy(*args, **kwargs):
            seen.append(repr(args) + repr(kwargs.get("env")))
            return real_popen(*args, **kwargs)

        with self.assertLogs(level=logging.DEBUG) as logs, mock.patch.object(subprocess, "Popen", side_effect=spy):
            logging.getLogger("ssh_manager").debug("start")
            K.generate_key(self.dir / "s", comment="t", passphrase=PASS)
            K.change_passphrase(self.dir / "s", PASS, PASS + "2")
            try:
                K.change_passphrase(self.dir / "s", PASS, "x")
            except K.KeyError_ as exc:
                seen.append(str(exc))
            K.read_key_info(self.dir / "s")
        text = "\n".join(logs.output + seen) + repr(dict(os.environ))
        self.assertNotIn(PASS, text)


class DeployTests(Case):
    def setUp(self) -> None:
        super().setUp()
        from tests.fake_sftp import FakeSftpServer

        self.home = Path(self.tmp.name) / "server-home"
        self.home.mkdir()
        self.server = FakeSftpServer(self.home, users={"alice": "pw"})
        self.server.__enter__()
        self.addCleanup(self.server.__exit__)
        self.known = Path(self.tmp.name) / "known_hosts"
        key = self.server.host_key
        self.known.write_text(f"[127.0.0.1]:{self.server.port} {key.get_name()} {key.get_base64()}\n")
        self.info = K.generate_key(self.dir / "deploy", comment="me@box")

    def backend(self, password="pw", key_file=""):
        from ssh_manager.transfer.settings import TransferSettings
        from ssh_manager.transfer.sftp import SftpBackend

        return SftpBackend("127.0.0.1", self.server.port, "alice", password, known_hosts=self.known,
                           allow_agent=False, key_file=key_file, settings=TransferSettings(timeout_connect=5))

    def target(self, key="h1"):
        from ssh_manager.multirun.engine import Target

        return Target(key=key, alias=key, hostname="127.0.0.1", port=self.server.port, user="alice")

    def test_install_idempotent_and_verified_with_the_key_only(self) -> None:
        line = self.info.public_line
        self.assertEqual(K.deploy_public_key(self.backend().open_transport("t"), line).status, K.INSTALLED)
        path = self.home / ".ssh" / "authorized_keys"
        self.assertEqual(path.read_text().count(line.split()[1]), 1)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.home / ".ssh").stat().st_mode), 0o700)
        self.assertEqual(K.deploy_public_key(self.backend().open_transport("t"), line).status, K.ALREADY_PRESENT)
        self.assertEqual(path.read_text().count(line.split()[1]), 1)
        self.backend(password="", key_file=str(self.dir / "deploy")).open_transport("verify")  # key login works

    def test_runner_statuses(self) -> None:
        import socket

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            dead = probe.getsockname()[1]

        def connect(target, timeout):
            if target.key == "dead":
                from ssh_manager.transfer.sftp import SftpBackend

                return SftpBackend("127.0.0.1", dead, "alice", "pw", known_hosts=self.known).open_transport("t")
            return self.backend(password="wrong" if target.key == "wrong" else "pw").open_transport("t")

        def verify(target, timeout):
            return self.backend(password="", key_file=str(self.dir / "deploy")).open_transport("verify")

        run = K.DeployRun([self.target("ok"), self.target("wrong"), self.target("dead")], self.info.public_line,
                          connect=connect, verify=verify, workers=3, timeout=10)
        run.start()
        self.assertTrue(run.wait(30))
        statuses = {r.key: r.status for r in run.results()}
        self.assertEqual(statuses, {"ok": K.INSTALLED, "wrong": K.AUTH_FAILED, "dead": K.UNREACHABLE})
        self.assertIn("key login works", next(r for r in run.results() if r.key == "ok").message)

    def test_changed_host_key_is_reported_not_bypassed(self) -> None:
        self.known.write_text(f"[127.0.0.1]:{self.server.port} ecdsa-sha2-nistp256 "
                              f"{paramiko.ECDSAKey.generate().get_base64()}\n")
        run = K.DeployRun([self.target()], self.info.public_line,
                          connect=lambda t, timeout: self.backend().open_transport("t"),
                          verify=lambda t, timeout: None)
        run.start()
        self.assertTrue(run.wait(20))
        self.assertEqual(run.results()[0].status, K.HOST_KEY_CHANGED)
        self.assertFalse((self.home / ".ssh" / "authorized_keys").exists())


if __name__ == "__main__":
    unittest.main()
