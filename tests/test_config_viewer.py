import os
import stat
import tempfile
import unittest
from pathlib import Path

from ssh_manager.config_viewer import export_path_for_directory, save_config_text, validate_config


class ConfigViewerTests(unittest.TestCase):
    def test_validate_config_reports_duplicate_hosts_and_bad_quotes(self) -> None:
        results = validate_config(
            "Host app\n"
            "  HostName app.internal\n"
            "Host=app\n"
            "  ProxyCommand \"unterminated\n"
        )

        messages = [result.message for result in results]
        self.assertTrue(any("Duplicate Host alias 'app'" in msg for msg in messages))
        self.assertTrue(any("Malformed line" in msg for msg in messages))

    def test_save_config_text_creates_backup_and_preserves_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "config"
            path.write_text("Host old\n  HostName old.internal\n", encoding="utf-8")
            os.chmod(path, 0o644)

            backup = save_config_text(path, "Host new\n  HostName new.internal\n")

            self.assertEqual(path.read_text(encoding="utf-8"), "Host new\n  HostName new.internal\n")
            self.assertEqual(backup.read_text(encoding="utf-8"), "Host old\n  HostName old.internal\n")
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o644)

    def test_save_config_text_rejects_empty_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "config"

            with self.assertRaises(ValueError):
                save_config_text(path, "   \n")

    def test_export_path_for_directory_rejects_paths(self) -> None:
        base = Path("/tmp/export-root")

        self.assertEqual(export_path_for_directory(base, "config.conf"), base / "config.conf")
        for name in ("../config.conf", "/tmp/config.conf", "nested/config.conf", "nested\\config.conf", "\n.conf"):
            with self.assertRaises(ValueError):
                export_path_for_directory(base, name)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
