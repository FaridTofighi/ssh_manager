import unittest
import os
import json
import stat
from pathlib import Path
from unittest import mock

from ssh_manager.credentials import CredentialStore
from ssh_manager.secure_store import InMemoryKeyring, KeyringError, SecureStore, SecureStoreError


def make_store(tmp_path: Path) -> CredentialStore:
    keyring = InMemoryKeyring()
    store = SecureStore(keyring)
    return CredentialStore(store, path=tmp_path / "credentials.json")


class CredentialStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.base = Path(self.tmpdir.name)
        self.store = make_store(self.base)

    def test_add_get_update_delete(self) -> None:
        cred = self.store.add("prod", "alice", "secret")
        self.assertEqual(self.store.get_password(cred.id), "secret")
        mode = stat.S_IMODE(os.stat(self.base / "credentials.json").st_mode)
        self.assertEqual(mode, 0o600)

        self.store.update(cred.id, "prod-updated", "bob", password="newpass")
        updated = self.store.get(cred.id)
        self.assertIsNotNone(updated)
        self.assertEqual(updated.name, "prod-updated")
        self.assertEqual(updated.username, "bob")
        self.assertEqual(self.store.get_password(cred.id), "newpass")

        self.store.delete(cred.id)
        self.assertIsNone(self.store.get(cred.id))
        self.assertIsNone(self.store.get_password(cred.id))

    def test_bindings(self) -> None:
        cred = self.store.add("sandbox", "carol", "pw")
        host_key = "p/c/a"
        self.store.bind_host(host_key, cred.id)
        self.assertEqual(self.store.credential_for_host(host_key), cred)

        # Removing binding
        self.store.bind_host(host_key, None)
        self.assertIsNone(self.store.credential_for_host(host_key))

    def test_wrong_json_shape_loads_as_empty_store(self) -> None:
        path = self.base / "credentials.json"
        path.write_text("[]", encoding="utf-8")

        store = CredentialStore(SecureStore(InMemoryKeyring()), path=path)

        self.assertEqual(store.list(), [])

    def test_load_ignores_malformed_credentials_and_bindings(self) -> None:
        path = self.base / "credentials.json"
        path.write_text(
            json.dumps(
                {
                    "credentials": [
                        {"id": "valid", "name": "prod", "username": "alice"},
                        {"id": "bad-name", "name": ["prod"], "username": "bob"},
                        {"id": 42, "name": "numeric", "username": "carol"},
                    ],
                    "bindings": {
                        "prod::hosts.conf::app": "valid",
                        "prod::hosts.conf::bad": ["valid"],
                        42: "valid",
                    },
                }
            ),
            encoding="utf-8",
        )

        store = CredentialStore(SecureStore(InMemoryKeyring()), path=path)

        self.assertEqual([cred.id for cred in store.list()], ["valid"])
        self.assertEqual(store.binding_for_host("prod::hosts.conf::app"), "valid")
        self.assertIsNone(store.binding_for_host("prod::hosts.conf::bad"))

    def test_binding_remap_helpers(self) -> None:
        cred = self.store.add("ops", "alice", "pw")
        self.store.bind_host("old::group.conf::app", cred.id)
        self.store.rebind_host("old::group.conf::app", "new::group.conf::app")
        self.assertIsNone(self.store.binding_for_host("old::group.conf::app"))
        self.assertEqual(self.store.binding_for_host("new::group.conf::app"), cred.id)

        self.store.remap_bindings_prefix("new::", "prod::")
        self.assertIsNone(self.store.binding_for_host("new::group.conf::app"))
        self.assertEqual(self.store.binding_for_host("prod::group.conf::app"), cred.id)

        self.store.unbind_prefix("prod::")
        self.assertIsNone(self.store.binding_for_host("prod::group.conf::app"))

    def test_add_does_not_persist_metadata_when_password_storage_fails(self) -> None:
        class FailingSetKeyring(InMemoryKeyring):
            def set_password(self, service_name: str, username: str, password: str) -> None:  # type: ignore[override]
                raise KeyringError("boom")

        store = CredentialStore(
            SecureStore(FailingSetKeyring()),
            path=self.base / "failing_credentials.json",
        )
        with self.assertRaises(SecureStoreError):
            store.add("broken", "alice", "secret")
        self.assertEqual(store.list(), [])

    def test_update_does_not_persist_metadata_when_password_storage_fails(self) -> None:
        class FailingOnSecondSetKeyring(InMemoryKeyring):
            def __init__(self) -> None:
                super().__init__()
                self.calls = 0

            def set_password(self, service_name: str, username: str, password: str) -> None:  # type: ignore[override]
                self.calls += 1
                if self.calls > 1:
                    raise KeyringError("boom")
                super().set_password(service_name, username, password)

        store = CredentialStore(
            SecureStore(FailingOnSecondSetKeyring()),
            path=self.base / "update_failing_credentials.json",
        )
        cred = store.add("prod", "alice", "secret")

        with self.assertRaises(SecureStoreError):
            store.update(cred.id, "renamed", "bob", password="new")

        unchanged = store.get(cred.id)
        self.assertIsNotNone(unchanged)
        assert unchanged is not None
        self.assertEqual(unchanged.name, "prod")
        self.assertEqual(unchanged.username, "alice")

    def test_add_rolls_back_secret_when_metadata_save_fails(self) -> None:
        keyring = InMemoryKeyring()
        store = CredentialStore(
            SecureStore(keyring),
            path=self.base / "metadata_failing_credentials.json",
        )

        with mock.patch("ssh_manager.credentials.write_json_atomic", side_effect=OSError("disk full")):
            with self.assertRaises(SecureStoreError):
                store.add("prod", "alice", "secret")

        self.assertEqual(store.list(), [])
        self.assertEqual(keyring._store, {})

    def test_update_rolls_back_metadata_and_password_when_metadata_save_fails(self) -> None:
        cred = self.store.add("prod", "alice", "secret")

        with mock.patch("ssh_manager.credentials.write_json_atomic", side_effect=OSError("disk full")):
            with self.assertRaises(SecureStoreError):
                self.store.update(cred.id, "renamed", "bob", password="new")

        unchanged = self.store.get(cred.id)
        self.assertIsNotNone(unchanged)
        assert unchanged is not None
        self.assertEqual(unchanged.name, "prod")
        self.assertEqual(unchanged.username, "alice")
        self.assertEqual(self.store.get_password(cred.id), "secret")

    def test_delete_and_bind_roll_back_when_metadata_save_fails(self) -> None:
        cred = self.store.add("prod", "alice", "secret")
        self.store.bind_host("prod::hosts.conf::app", cred.id)

        with mock.patch("ssh_manager.credentials.write_json_atomic", side_effect=OSError("disk full")):
            with self.assertRaises(SecureStoreError):
                self.store.delete(cred.id)

        self.assertIsNotNone(self.store.get(cred.id))
        self.assertEqual(self.store.binding_for_host("prod::hosts.conf::app"), cred.id)
        self.assertEqual(self.store.get_password(cred.id), "secret")

        with mock.patch("ssh_manager.credentials.write_json_atomic", side_effect=OSError("disk full")):
            with self.assertRaises(SecureStoreError):
                self.store.bind_host("prod::hosts.conf::app", None)

        self.assertEqual(self.store.binding_for_host("prod::hosts.conf::app"), cred.id)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
