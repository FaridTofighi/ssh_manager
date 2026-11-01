import json
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ophion.smart_groups import SmartGroup, load_smart_groups, save_smart_groups


class SmartGroupTests(unittest.TestCase):
    def test_load_ignores_missing_malformed_and_incomplete_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            missing = Path(tmpdir) / "missing.json"
            self.assertEqual(load_smart_groups(missing), [])

            malformed = Path(tmpdir) / "bad.json"
            malformed.write_text("not json", encoding="utf-8")
            self.assertEqual(load_smart_groups(malformed), [])

            mixed = Path(tmpdir) / "mixed.json"
            mixed.write_text(
                json.dumps(
                    [
                        {"name": "Production", "query": "env:prod"},
                        {"name": "  Staging  ", "query": "  env:stage  "},
                        {"name": "", "query": "owner:ops"},
                        {"name": "Broken", "query": ""},
                        {"name": ["Bad"], "query": "owner:ops"},
                        {"name": "Bad", "query": {"owner": "ops"}},
                        "invalid",
                    ]
                ),
                encoding="utf-8",
            )

            groups = load_smart_groups(mixed)

            self.assertEqual(
                groups,
                [
                    SmartGroup(name="Production", query="env:prod"),
                    SmartGroup(name="Staging", query="env:stage"),
                ],
            )

    def test_save_round_trips_and_restricts_file_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state" / "smart_groups.json"

            saved = save_smart_groups(
                [
                    SmartGroup(name="Prod", query="env:prod"),
                    SmartGroup(name="Owner", query="owner:platform"),
                ],
                path,
            )

            self.assertTrue(saved)
            self.assertEqual(
                load_smart_groups(path),
                [
                    SmartGroup(name="Prod", query="env:prod"),
                    SmartGroup(name="Owner", query="owner:platform"),
                ],
            )
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_save_reports_write_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "smart_groups.json"

            with mock.patch("ophion.smart_groups.write_json_atomic", side_effect=OSError("disk full")):
                saved = save_smart_groups([SmartGroup(name="Prod", query="env:prod")], path)

            self.assertFalse(saved)
            self.assertFalse(path.exists())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
