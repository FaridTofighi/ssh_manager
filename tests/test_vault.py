import os
import base64
import json
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ssh_manager.vault import CRYPTO_AVAILABLE, CredentialVault, VaultEntry, VaultError, default_vault_path

if CRYPTO_AVAILABLE:
    from cryptography.fernet import Fernet  # type: ignore


class DefaultVaultPathTests(unittest.TestCase):
    def test_honors_xdg_config_home(self) -> None:
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as xdg:
            with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": xdg}), mock.patch(
                "ssh_manager.vault.Path.home", return_value=Path(home)
            ):
                self.assertEqual(default_vault_path(), Path(xdg) / "ssh-manager" / "vault.json")

    def test_keeps_existing_legacy_vault(self) -> None:
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as xdg:
            legacy = Path(home) / ".config" / "ssh-manager" / "vault.json"
            legacy.parent.mkdir(parents=True)
            legacy.write_text("{}", encoding="utf-8")
            with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": xdg}), mock.patch(
                "ssh_manager.vault.Path.home", return_value=Path(home)
            ):
                self.assertEqual(default_vault_path(), legacy)


@unittest.skipUnless(CRYPTO_AVAILABLE, "cryptography is not installed")
class CredentialVaultTests(unittest.TestCase):
    def test_vault_persists_entries_and_restricts_file_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "vault.json"
            vault = CredentialVault(path)
            vault.create_master("master")
            vault.add_entry(VaultEntry("1", "prod", "alice", "secret"))

            loaded = CredentialVault(path)
            self.assertTrue(loaded.unlock("master"))
            entries = loaded.entries()
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0].password, "secret")

            mode = stat.S_IMODE(os.stat(path).st_mode)
            self.assertEqual(mode, 0o600)

    def test_load_entries_ignores_wrong_json_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "vault.json"
            vault = CredentialVault(path)
            vault.create_master("master")
            vault.add_entry(VaultEntry("1", "prod", "alice", "secret"))
            path.write_text("[]", encoding="utf-8")

            vault._load_entries()

            self.assertEqual(len(vault.entries()), 1)

    def test_load_entries_ignores_malformed_entry_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "vault.json"
            vault = CredentialVault(path)
            vault.create_master("master")
            assert vault._key is not None
            token = Fernet(vault._key).encrypt(
                json.dumps(
                    [
                        {"id": "1", "label": "prod", "username": "alice", "password": "secret"},
                        {"id": "2", "label": ["bad"], "username": "bob", "password": "secret"},
                    ]
                ).encode("utf-8")
            )
            raw = json.loads(path.read_text(encoding="utf-8"))
            raw["data"] = base64.b64encode(token).decode("utf-8")
            path.write_text(json.dumps(raw), encoding="utf-8")

            loaded = CredentialVault(path)
            self.assertTrue(loaded.unlock("master"))

            self.assertEqual([entry.id for entry in loaded.entries()], ["1"])

    def test_create_master_rolls_back_when_save_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "vault.json"
            vault = CredentialVault(path)

            with mock.patch("ssh_manager.vault.write_json_atomic", side_effect=OSError("disk full")):
                with self.assertRaises(VaultError):
                    vault.create_master("master")

            self.assertFalse(vault.initialized)
            self.assertFalse(vault.unlocked)

    def test_entry_mutations_roll_back_when_save_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "vault.json"
            vault = CredentialVault(path)
            vault.create_master("master")
            vault.add_entry(VaultEntry("1", "prod", "alice", "secret"))

            with mock.patch("ssh_manager.vault.write_json_atomic", side_effect=OSError("disk full")):
                with self.assertRaises(VaultError):
                    vault.add_entry(VaultEntry("2", "stage", "bob", "secret"))
            self.assertEqual([entry.id for entry in vault.entries()], ["1"])

            with mock.patch("ssh_manager.vault.write_json_atomic", side_effect=OSError("disk full")):
                with self.assertRaises(VaultError):
                    vault.update_entry(VaultEntry("1", "renamed", "alice", "new"))
            self.assertEqual(vault.get_entry("1").label, "prod")
            self.assertEqual(vault.get_entry("1").password, "secret")

            with mock.patch("ssh_manager.vault.write_json_atomic", side_effect=OSError("disk full")):
                with self.assertRaises(VaultError):
                    vault.delete_entry("1")
            self.assertIsNotNone(vault.get_entry("1"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
