"""Moving the removed Credential Vault into Credentials (``ssh_manager.legacy_vault``)."""

import base64
import hashlib
import hmac
import json
import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ssh_manager import legacy_vault as LV
from ssh_manager.config import HostEntry
from ssh_manager.credentials import CredentialStore
from ssh_manager.inventory.manager import StoreConfigManager
from ssh_manager.inventory.store import InventoryStore, NewHost
from ssh_manager.secure_store import InMemoryKeyring, SecureStore

SECRET = "V4ULT-sentinel-password"


def write_old_vault(path: Path, password: str, entries) -> None:
    """The removed vault's file format, written here so this test outlives the old module."""

    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    salt = os.urandom(16)
    key = base64.urlsafe_b64encode(PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt,
                                              iterations=200_000).derive(password.encode()))
    token = Fernet(key).encrypt(json.dumps([dict(id=i, label=lb, username=u, password=p)
                                            for i, lb, u, p in entries]).encode())
    path.write_text(json.dumps({"version": 1, "salt": base64.b64encode(salt).decode(),
                                "master_hash": base64.b64encode(hmac.new(key, b"vault", hashlib.sha256).digest())
                                .decode(), "data": base64.b64encode(token).decode()}))


class ConversionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.store = InventoryStore(root / "inventory.db")
        self.addCleanup(self.store.close)
        self.manager = StoreConfigManager(self.store)
        self.keyring = InMemoryKeyring()
        self.creds = CredentialStore(SecureStore(self.keyring), root / "credentials.json")
        group = self.store.create_group(None, "G")
        self.hosts = {}
        for alias, vault_id in (("plain", "v1"), ("shared-too", "v1"), ("private", "v2"), ("dangling", "gone"),
                                ("other", "v3"), ("untouched", None)):
            metadata = {"tags": ["x"]}
            if vault_id:
                metadata["vault_id"] = vault_id
            self.hosts[alias] = self.store.add_host(NewHost(group.id, alias, f"{alias}.example", 22, "u",
                                                            metadata=metadata)).id
        self.manager.refresh()
        self.vault = root / "vault.json"
        self.entries = [("v1", "Ops", "deploy", SECRET), ("v2", "DB", "postgres", "pg-secret"),
                        ("v3", "Ops", "root", "root-secret")]
        write_old_vault(self.vault, "vault-pw", self.entries)

    def host(self, alias):
        return next(h for c in self.manager.tree.values() for g in c.values() for h in g.hosts if h.alias == alias)

    def resolve(self, host):
        cred = self.creds.credential_for_host(LV.binding_key(host))
        return (self.creds.get_password(cred.id), "credential") if cred else (None, "none")

    def convert(self, **kw):
        entries = LV.read_legacy_vault(self.vault, "vault-pw")
        return LV.convert_vault(entries, self.creds, self.manager, resolve=self.resolve, **kw)

    def test_wrong_password_changes_nothing(self) -> None:
        before = self.vault.read_bytes()
        self.assertIsNone(LV.read_legacy_vault(self.vault, "wrong"))
        self.assertEqual(self.vault.read_bytes(), before)
        self.assertEqual(self.creds.list(), [])
        entries = LV.read_legacy_vault(self.vault, "vault-pw")
        self.assertNotIn(SECRET, repr(entries))
        broken = self.vault.with_name("broken.json")
        broken.write_text("{nope")
        with self.assertRaises(LV.LegacyVaultError):
            LV.read_legacy_vault(broken, "vault-pw")

    def test_conversion_reuse_collisions_private_and_dangling(self) -> None:
        reused = self.creds.add("Existing", "deploy", SECRET)  # same username and password: reused
        self.creds.add("ops", "someone", "different")  # name collision (any case) with the "Ops" label
        shared = self.creds.add("Shared", "u", "shared-pw")
        self.creds.bind_host(self.hosts["shared-too"], shared.id)  # the vault entry wins
        private = self.creds.create_private(self.hosts["private"], "postgres", "own-password")
        report = self.convert()
        self.assertTrue(report.verified)
        self.assertEqual(report.reused, ["Existing"])
        self.assertEqual(report.created, ["DB", "Ops (2)"])
        self.assertEqual(self.creds.binding_for_host(self.hosts["plain"]), reused.id)
        self.assertEqual(self.creds.binding_for_host(self.hosts["shared-too"]), reused.id)
        self.assertEqual(self.creds.binding_for_host(self.hosts["private"]), private.id)  # kept
        self.assertEqual(self.creds.get_password(private.id), "own-password")
        self.assertEqual(report.hosts_kept_private, ["private"])
        self.assertEqual(report.dangling, ["dangling"])
        other = self.creds.credential_for_host(self.hosts["other"])
        self.assertEqual((other.name, other.username, self.creds.get_password(other.id)),
                         ("Ops (2)", "root", "root-secret"))
        for alias in ("plain", "shared-too", "private", "dangling", "other"):
            self.assertNotIn("vault_id", self.store.get_host(self.hosts[alias]).metadata, alias)
            self.assertEqual(self.store.get_host(self.hosts[alias]).metadata["tags"], ["x"])
        self.assertEqual(self.resolve(self.host("plain")), (SECRET, "credential"))
        self.assertTrue(self.vault.exists())  # deleting it is a separate, confirmed step
        self.assertNotIn(SECRET, report.summary())

    def test_running_twice_changes_nothing(self) -> None:
        self.convert()
        state = (sorted((c.id, c.name, c.username) for c in self.creds.list(include_private=True)),
                 dict(self.creds._bindings))
        again = self.convert()
        self.assertEqual(again.created, [])
        self.assertEqual(sorted(again.reused), ["DB", "Ops", "Ops (2)"])
        self.assertEqual((sorted((c.id, c.name, c.username) for c in self.creds.list(include_private=True)),
                          dict(self.creds._bindings)), state)

    def test_an_interrupted_conversion_resumes_without_duplicates(self) -> None:
        with mock.patch.object(self.creds, "bind_many", side_effect=OSError("crash")):
            with self.assertRaises(OSError):
                self.convert()
        self.assertIn("vault_id", self.store.get_host(self.hosts["plain"]).metadata)  # nothing removed yet
        report = self.convert()
        self.assertEqual(report.created, [])
        self.assertEqual(len(self.creds.list()), 3)
        self.assertEqual(self.resolve(self.host("other"))[0], "root-secret")

    def test_a_failed_verification_removes_nothing(self) -> None:
        with mock.patch.object(self.creds, "get_password", return_value="tampered"):
            with self.assertRaises(LV.LegacyVaultError):
                LV.convert_vault(LV.read_legacy_vault(self.vault, "vault-pw"), self.creds, self.manager)
        self.assertIn("vault_id", self.store.get_host(self.hosts["plain"]).metadata)
        self.assertTrue(self.vault.exists())

    def test_secure_delete_and_logs(self) -> None:
        backup = self.vault.with_name("vault.json.bak")
        backup.write_text("old copy")
        with self.assertLogs(level=logging.DEBUG) as logs:
            logging.getLogger("ssh_manager").debug("start")
            self.convert()
            self.assertTrue(LV.secure_delete_vault_files(self.vault))
        self.assertFalse(self.vault.exists())
        self.assertFalse(backup.exists())
        self.assertNotIn(SECRET, "\n".join(logs.output))

    def test_folder_inventories_are_updated_host_by_host(self) -> None:
        manager = mock.Mock(spec=["tree", "update_host"])
        host = HostEntry("legacy", {"hostname": "h"}, {"vault_id": "v1"}, parent_group="P", child_group="c.conf")
        manager.tree = {"P": {"c.conf": mock.Mock(hosts=[host])}}
        manager.update_host.return_value = (True, "ok")
        report = LV.convert_vault(LV.read_legacy_vault(self.vault, "vault-pw"), self.creds, manager)
        self.assertEqual(report.hosts_cleaned, 1)
        self.assertNotIn("vault_id", host.metadata)
        self.assertIsNotNone(self.creds.binding_for_host("P::c.conf::legacy"))


if __name__ == "__main__":
    unittest.main()
