import os
import json
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ssh_manager.secure_store import (
    CRYPTO_AVAILABLE,
    FileKeyring,
    InMemoryKeyring,
    KeyringError,
    SecureStore,
    SecureStoreError,
)


class FailingKeyring(InMemoryKeyring):
    def __init__(self, exc: Exception) -> None:
        super().__init__()
        self._exc = exc

    def set_password(self, service_name: str, username: str, password: str) -> None:  # type: ignore[override]
        raise self._exc

    def get_password(self, service_name: str, username: str):  # type: ignore[override]
        raise self._exc

    def delete_password(self, service_name: str, username: str) -> None:  # type: ignore[override]
        raise self._exc


class SecureStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = InMemoryKeyring()
        self.store = SecureStore(self.backend)
        self.key_args = ("example.com", 22, "alice")

    def test_key_format(self) -> None:
        key = SecureStore.key(*self.key_args)
        self.assertEqual(key, "example.com:22:alice")

    def test_set_and_get_password(self) -> None:
        self.store.set(*self.key_args, secret="pass123")
        self.assertEqual(self.store.get(*self.key_args), "pass123")

    def test_delete_password(self) -> None:
        self.store.set(*self.key_args, secret="pass123")
        self.store.delete(*self.key_args)
        self.assertIsNone(self.store.get(*self.key_args))

    def test_backend_error_raises_secure_store_error(self) -> None:
        failing_store = SecureStore(FailingKeyring(KeyringError("boom")))
        with self.assertRaises(SecureStoreError):
            failing_store.get(*self.key_args)
        with self.assertRaises(SecureStoreError):
            failing_store.set(*self.key_args, secret="x")
        with self.assertRaises(SecureStoreError):
            failing_store.delete(*self.key_args)

    def test_file_keyring_persists_and_restricts_file_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "passwords.json"
            backend = FileKeyring(path)
            backend.set_password("svc", "user", "secret")

            loaded = FileKeyring(path)

            self.assertEqual(loaded.get_password("svc", "user"), "secret")
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_file_keyring_rolls_back_when_save_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "passwords.json"
            backend = FileKeyring(path)
            backend.set_password("svc", "user", "old")

            with mock.patch("ssh_manager.secure_store.write_json_atomic", side_effect=OSError("disk full")):
                with self.assertRaises(KeyringError):
                    backend.set_password("svc", "user", "new")
            self.assertEqual(backend.get_password("svc", "user"), "old")

            store = SecureStore(backend)
            store.set("example.com", 22, "alice", "secret")
            with mock.patch("ssh_manager.secure_store.write_json_atomic", side_effect=OSError("disk full")):
                with self.assertRaises(SecureStoreError):
                    store.delete("example.com", 22, "alice")
            self.assertEqual(store.get("example.com", 22, "alice"), "secret")

    def test_secure_store_converts_file_keyring_save_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SecureStore(FileKeyring(Path(tmpdir) / "passwords.json"))

            with mock.patch("ssh_manager.secure_store.write_json_atomic", side_effect=OSError("disk full")):
                with self.assertRaises(SecureStoreError):
                    store.set("example.com", 22, "alice", "secret")

    def test_file_keyring_ignores_malformed_secret_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "passwords.json"
            path.write_text(
                json.dumps(
                    {
                        "svc": {
                            "valid": "secret",
                            "invalid": ["not", "secret"],
                        },
                    }
                ),
                encoding="utf-8",
            )

            backend = FileKeyring(path)

            self.assertEqual(backend.get_password("svc", "valid"), "secret")
            self.assertIsNone(backend.get_password("svc", "invalid"))

    @unittest.skipUnless(CRYPTO_AVAILABLE, "cryptography is not installed")
    def test_file_keyring_encrypts_with_master_password(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "passwords.json"
            FileKeyring(path, master_password="master").set_password("svc", "user", "hunter2")

            raw = path.read_text(encoding="utf-8")
            self.assertNotIn("hunter2", raw)
            self.assertEqual(json.loads(raw)["version"], FileKeyring.FORMAT_VERSION)
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            self.assertEqual(FileKeyring(path, master_password="master").get_password("svc", "user"), "hunter2")

    @unittest.skipUnless(CRYPTO_AVAILABLE, "cryptography is not installed")
    def test_file_keyring_migrates_plaintext_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "passwords.json"
            path.write_text(json.dumps({"svc": {"user": "legacy"}}), encoding="utf-8")

            backend = FileKeyring(path, master_password="master")

            self.assertEqual(backend.get_password("svc", "user"), "legacy")
            self.assertNotIn("legacy", path.read_text(encoding="utf-8"))

    @unittest.skipUnless(CRYPTO_AVAILABLE, "cryptography is not installed")
    def test_file_keyring_locked_without_correct_master_password(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "passwords.json"
            FileKeyring(path, master_password="master").set_password("svc", "user", "secret")
            before = path.read_text(encoding="utf-8")

            for backend in (FileKeyring(path), FileKeyring(path, master_password="wrong")):
                self.assertTrue(backend.locked)
                self.assertIsNone(backend.get_password("svc", "user"))
                with self.assertRaises(KeyringError):
                    backend.set_password("svc", "other", "x")
            self.assertEqual(path.read_text(encoding="utf-8"), before)

    @unittest.skipUnless(CRYPTO_AVAILABLE, "cryptography is not installed")
    def test_file_keyring_rekey_changes_password_and_can_decrypt(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "passwords.json"
            backend = FileKeyring(path, master_password="old")
            backend.set_password("svc", "user", "secret")

            backend.rekey("new")
            self.assertTrue(FileKeyring(path, master_password="old").locked)
            self.assertEqual(FileKeyring(path, master_password="new").get_password("svc", "user"), "secret")

            backend.rekey(None)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"svc": {"user": "secret"}})

    @unittest.skipUnless(CRYPTO_AVAILABLE, "cryptography is not installed")
    def test_default_backend_falls_back_to_encrypted_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": tmpdir}), mock.patch(
                "ssh_manager.secure_store._system_keyring", return_value=None
            ):
                store = SecureStore.with_default_backend(master_password="master")
            self.assertTrue(store.uses_file_backend)
            self.assertTrue(store.backend.encrypted)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
