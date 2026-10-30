"""Saved commands and the command history: encrypted with the keybox like macros (no GTK)."""

import json
import stat
import tempfile
import unittest
from pathlib import Path

from ssh_manager import keybox as K
from ssh_manager.multirun import store as S

SECRET = "Saved-command-sentinel-token"


class StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.box = K.Keybox.create("pw", self.dir / "keybox.json", scrypt_n=2 ** 15)
        self.path = self.dir / "commands.enc"

    def store(self, box=None) -> S.CommandStore:
        store = S.CommandStore(self.path)
        box = box or self.box
        store.unlock(box.subkey("commands"), box.id)
        return store


class StoreTests(StoreCase):
    def test_saved_commands_round_trip_encrypted_at_rest(self) -> None:
        store = self.store()
        saved = store.save_command("deploy", f"curl -H 'Authorization: {SECRET}' https://x", shell="bash",
                                   options={"concurrency": 5})
        reopened = self.store()
        self.assertEqual([(c.name, c.text, c.shell, c.options) for c in reopened.commands()],
                         [("deploy", saved.text, "bash", {"concurrency": 5})])
        raw = self.path.read_bytes()
        self.assertNotIn(SECRET.encode(), raw)
        self.assertNotIn(b"deploy", raw)
        document = json.loads(raw)
        self.assertEqual((document["format"], document["keybox_id"]), ("ssh-manager-commands", self.box.id))
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        reopened.delete_command(saved.id)
        self.assertEqual(self.store().commands(), [])

    def test_history_last_fifty_text_only_and_clear(self) -> None:
        store = self.store()
        for index in range(60):
            store.add_history(f"echo {index}")
        store.add_history("echo 10")  # again: moves to the top, no duplicate
        history = self.store().history()
        self.assertEqual(len(history), 50)
        self.assertEqual(history[0], "echo 10")
        self.assertEqual(history.count("echo 10"), 1)
        store.add_history("   ")  # blank: ignored
        self.assertEqual(len(self.store().history()), 50)
        store.clear_history()
        self.assertEqual(self.store().history(), [])
        self.assertNotIn(b"echo", self.path.read_bytes())

    def test_damaged_tampered_and_foreign_files_are_kept(self) -> None:
        self.store().add_history(SECRET)
        good = self.path.read_bytes()
        other = K.Keybox.create("pw", self.dir / "other.json", scrypt_n=2 ** 15)
        with self.assertRaises(S.CommandFileError) as caught:
            self.store(other)
        self.assertEqual(caught.exception.kind, "foreign")
        document = json.loads(good)
        document["data"] = document["data"][:-8] + "AAAAAAA="
        self.path.write_text(json.dumps(document))
        with self.assertRaises(S.CommandFileError) as caught:
            self.store()
        self.assertEqual(caught.exception.kind, "tampered")
        self.path.write_bytes(b"{garbage")
        with self.assertRaises(S.CommandFileError):
            self.store()
        self.assertEqual(self.path.read_bytes(), b"{garbage")  # never overwritten

    def test_memory_only_without_a_key(self) -> None:
        store = S.CommandStore(None)  # "Don't save passwords": nothing on disk
        store.add_history("uptime")
        self.assertEqual(store.history(), ["uptime"])
        self.assertFalse(store.persistent)
        with self.assertRaises(S.CommandFileError):
            store.save_command("x", "y")
        self.assertEqual(list(self.dir.glob("commands*")), [])


if __name__ == "__main__":
    unittest.main()
