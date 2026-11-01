"""The per-user launcher that gives running windows Ophion's icon (temporary XDG_DATA_HOME only)."""

import configparser
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from ophion import desktop_integration as D
from ophion.identity import APP_ID


class LauncherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = {"HOME": str(self.root / "home"), "XDG_DATA_HOME": str(self.root / "data"),
                    "XDG_DATA_DIRS": str(self.root / "system")}
        self.path = self.root / "data" / "applications" / f"{APP_ID}.desktop"

    def entry(self):
        parser = configparser.ConfigParser(interpolation=None)
        parser.optionxform = str
        parser.read(self.path, encoding="utf-8")
        return parser["Desktop Entry"]

    def test_installed_with_icons_then_current(self) -> None:
        self.assertEqual(D.ensure_launcher(self.env, executable="/usr/bin/python3"), "installed")
        entry = self.entry()
        self.assertEqual((entry["Icon"], entry["StartupWMClass"], entry["Name"]), ("ophion", APP_ID, "Ophion"))
        main = Path(D.__file__).resolve().parent.parent / "main.py"
        self.assertEqual(entry["Exec"], f"/usr/bin/python3 {D._quote(str(main))} %U")
        for size in (16, 22, 24, 32, 48, 64, 96, 128, 256, 512):
            icon = self.root / "data" / "icons" / "hicolor" / f"{size}x{size}" / "apps" / "ophion.png"
            self.assertTrue(icon.is_file(), icon)
        if shutil.which("desktop-file-validate"):
            result = subprocess.run(["desktop-file-validate", str(self.path)], capture_output=True, text=True)
            self.assertEqual((result.returncode, result.stdout.strip()), (0, ""))
        self.assertEqual(D.ensure_launcher(self.env, executable="/usr/bin/python3"), "current")

    def test_a_moved_copy_updates_only_its_own_launcher(self) -> None:
        D.ensure_launcher(self.env, executable="/usr/bin/python3")
        self.assertEqual(D.ensure_launcher(self.env, executable="/opt/py/bin/python3"), "updated")
        self.assertTrue(self.entry()["Exec"].startswith("/opt/py/bin/python3 "))
        self.path.write_text("[Desktop Entry]\nType=Application\nName=Mine\nExec=mine\nIcon=ophion\n")
        self.assertEqual(D.ensure_launcher(self.env, executable="/usr/bin/python3"), "user")
        self.assertEqual(self.entry()["Name"], "Mine")  # the user's own file is never touched

    def test_appimage_and_quoting(self) -> None:
        appimage = self.root / "My Apps" / "Ophion-1.0-x86_64.AppImage"
        appimage.parent.mkdir()
        appimage.write_bytes(b"")
        env = dict(self.env, APPIMAGE=str(appimage))
        self.assertEqual(D.ensure_launcher(env), "installed")
        self.assertEqual(self.entry()["Exec"], f'"{appimage}" %U')
        self.assertEqual(D._quote('a$b"c%'), '"a\\$b\\"c%%"')

    def test_an_installed_package_launcher_is_not_duplicated(self) -> None:
        system = self.root / "system" / "applications"
        system.mkdir(parents=True)
        (system / f"{APP_ID}.desktop").write_text("[Desktop Entry]\n")
        self.assertEqual(D.ensure_launcher(self.env), "system")
        self.assertFalse(self.path.exists())
        # ... but the AppImage's own copy (inside APPDIR) is not an installed one
        env = dict(self.env, APPDIR=str(self.root / "system"))
        self.assertEqual(D.ensure_launcher(env, executable="/usr/bin/python3"), "installed")


if __name__ == "__main__":
    unittest.main()
