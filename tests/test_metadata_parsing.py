import tempfile
import unittest
from pathlib import Path

from ssh_manager.config import parse_ssh_config_file


class MetadataParsingTests(unittest.TestCase):
    def test_metadata_round_trip_in_comments(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = Path(tmpdir) / "hosts.conf"
            cfg.write_text(
                "Host app\n"
                "# meta environment=prod\n"
                "# meta owner=team\n"
                "  HostName app.prod\n",
                encoding="utf-8",
            )
            entries = parse_ssh_config_file(cfg, "p", "c")
            self.assertEqual(len(entries), 1)
            host = entries[0]
            self.assertEqual(host.metadata.get("environment"), "prod")
            self.assertEqual(host.metadata.get("owner"), "team")

    def test_metadata_comments_accept_case_and_spaced_equals(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = Path(tmpdir) / "hosts.conf"
            cfg.write_text(
                "Host app\n"
                "# Meta owner = platform\n"
                "# META permissions = can_connect=false can_edit=false\n"
                "  HostName app.prod\n",
                encoding="utf-8",
            )

            [host] = parse_ssh_config_file(cfg, "p", "c")

            self.assertEqual(host.metadata.get("owner"), "platform")
            self.assertEqual(
                host.metadata.get("permissions"),
                {"can_view": True, "can_connect": False, "can_edit": False},
            )

    def test_multi_alias_metadata_is_not_shared_between_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = Path(tmpdir) / "hosts.conf"
            cfg.write_text(
                "Host app app-alt\n"
                "# meta permissions=can_edit=false\n"
                "  HostName app.prod\n",
                encoding="utf-8",
            )

            entries = parse_ssh_config_file(cfg, "p", "c")
            self.assertEqual(len(entries), 2)

            entries[0].metadata["permissions"]["can_edit"] = True

            self.assertFalse(entries[1].metadata["permissions"]["can_edit"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
