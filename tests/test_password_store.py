import os
import json
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ssh_manager.config import HostEntry
from ssh_manager.password_store import PasswordStore


class PasswordStoreTests(unittest.TestCase):
    def test_set_get_delete_round_trip_and_private_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "passwords.json"
            host = HostEntry(
                alias="app",
                parent_group="prod",
                child_group="web.conf",
            )
            store = PasswordStore(path)

            store.set(host, "secret")
            self.assertEqual(PasswordStore(path).get(host), "secret")
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

            store.delete(host)
            self.assertIsNone(PasswordStore(path).get(host))

    def test_set_and_delete_roll_back_when_save_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "passwords.json"
            host = HostEntry(alias="app", parent_group="prod", child_group="web.conf")
            store = PasswordStore(path)
            store.set(host, "old")

            with mock.patch("ssh_manager.password_store.write_json_atomic", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    store.set(host, "new")
            self.assertEqual(store.get(host), "old")

            with mock.patch("ssh_manager.password_store.write_json_atomic", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    store.delete(host)
            self.assertEqual(store.get(host), "old")

    def test_load_ignores_non_string_secret_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "passwords.json"
            valid = HostEntry(alias="valid", parent_group="prod", child_group="web.conf")
            invalid = HostEntry(alias="invalid", parent_group="prod", child_group="web.conf")
            path.write_text(
                json.dumps(
                    {
                        "prod::web.conf::valid": "secret",
                        "prod::web.conf::invalid": ["not", "a", "password"],
                    }
                ),
                encoding="utf-8",
            )

            store = PasswordStore(path)

            self.assertEqual(store.get(valid), "secret")
            self.assertIsNone(store.get(invalid))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
