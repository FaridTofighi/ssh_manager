import json
import stat
import tempfile
import unittest
from pathlib import Path

from ssh_manager.persistence import append_text, write_json_atomic, write_text_atomic


class PersistenceTests(unittest.TestCase):
    def test_write_text_atomic_replaces_existing_file_and_sets_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            path.write_text("old", encoding="utf-8")

            write_text_atomic(path, "new", mode=0o600)

            self.assertEqual(path.read_text(encoding="utf-8"), "new")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(list(Path(tmpdir).glob("*.tmp")), [])
            self.assertEqual(list(Path(tmpdir).glob(".*.tmp")), [])

    def test_write_json_atomic_creates_parent_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "nested" / "settings.json"

            write_json_atomic(path, {"enabled": True}, mode=0o600)

            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"enabled": True})
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_append_text_creates_private_file_and_appends(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "logs" / "audit.log"

            append_text(path, "first\n")
            append_text(path, "second\n")

            self.assertEqual(path.read_text(encoding="utf-8"), "first\nsecond\n")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
