"""install.sh / uninstall.sh: a per-user install, always into a temporary HOME (the real one is never touched)."""

import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.gtk_window_fixture import display_available

REPO = Path(__file__).resolve().parent.parent
INSTALL, UNINSTALL = REPO / "install.sh", REPO / "uninstall.sh"
APP_ID = "io.github.faridtofighi.Ophion"
SIZES = ("16x16", "22x22", "24x24", "32x32", "48x48", "64x64", "96x96", "128x128", "256x256", "512x512")
RELEASE = re.compile(r"releases/[0-9]{8}-[0-9]{6}-[0-9]+")


@unittest.skipUnless(shutil.which("bash"), "bash is not installed")
class InstallerCase(unittest.TestCase):
    def setUp(self) -> None:
        self.home = Path(tempfile.mkdtemp(prefix="ophion-install-"))
        self.addCleanup(shutil.rmtree, self.home, True)
        self.local = self.home / ".local"
        self.opt = self.local / "opt" / "ophion"
        self.command = self.local / "bin" / "ophion"
        self.desktop = self.local / "share" / "applications" / f"{APP_ID}.desktop"

    def env(self, **extra):
        env = {key: value for key, value in os.environ.items() if not key.startswith("XDG_")}
        env.update(HOME=str(self.home), **extra)
        return env

    def run_script(self, script, *args, stdin="", **extra):
        return subprocess.run(["bash", str(script), *args], capture_output=True, text=True, input=stdin,
                              env=self.env(**extra), timeout=300)

    def install(self, *args, **extra):
        done = self.run_script(INSTALL, *args, **extra)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        return done

    def tree(self):
        """Every path under HOME (release folder names made comparable)."""

        paths = (RELEASE.sub("releases/ID", str(path.relative_to(self.home))) for path in self.home.rglob("*"))
        return sorted(path for path in paths if "releases/ID/" not in path)  # not the app copy and venv inside

    def manifest(self):
        return (self.opt / "manifest.txt").read_text(encoding="utf-8").splitlines()


class ScriptTests(InstallerCase):
    def test_syntax_and_shellcheck(self) -> None:
        for script in (INSTALL, UNINSTALL):
            self.assertTrue(os.access(script, os.X_OK), script)
            done = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, done.stderr)
        if shutil.which("shellcheck") is None:
            self.skipTest("shellcheck is not installed (bash -n passed)")
        done = subprocess.run(["shellcheck", "-S", "warning", str(INSTALL), str(UNINSTALL)], capture_output=True,
                              text=True)
        self.assertEqual(done.returncode, 0, done.stdout)

    def test_dry_run_changes_nothing(self) -> None:
        before = self.tree()
        done = self.install("--dry-run")
        self.assertIn("Dry run finished: nothing was changed.", done.stdout)
        self.assertIn("would install " + str(self.command), done.stdout)
        self.assertEqual(self.tree(), before)

    def test_missing_system_libraries_stop_before_any_change(self) -> None:
        fake = self.home.parent / f"{self.home.name}-python"
        fake.write_text("#!/bin/sh\nexec python3 -S \"$@\"\n")  # -S: no dist-packages, so no PyGObject
        fake.chmod(0o755)
        self.addCleanup(fake.unlink)
        before = self.tree()
        done = self.run_script(INSTALL, PYTHON=str(fake))
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn("gi (PyGObject)", done.stdout)
        self.assertRegex(done.stdout, r"sudo (apt|dnf|pacman|zypper) |Install Python 3")
        self.assertEqual(self.tree(), before)


class SourceInstallTests(InstallerCase):
    def test_install_update_and_uninstall(self) -> None:
        self.install()
        # the command, the menu entry and every icon size
        self.assertTrue(self.command.is_file() and os.access(self.command, os.X_OK))
        entry = self.desktop.read_text(encoding="utf-8")
        exec_line = next(line for line in entry.splitlines() if line.startswith("Exec="))
        self.assertEqual(exec_line, f"Exec={self.command} %U")  # absolute, inside this HOME
        for key in ("Name=Ophion", "Icon=ophion", f"StartupWMClass={APP_ID}", "Terminal=false",
                    "X-Ophion-Installed=true"):
            self.assertIn(key, entry.splitlines())
        self.assertNotIn("X-Ophion-Generated=true", entry)  # so the app never rewrites it
        if shutil.which("desktop-file-validate"):
            done = subprocess.run(["desktop-file-validate", str(self.desktop)], capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, done.stdout)
        manifest = self.manifest()
        for size in SIZES:
            icon = self.local / "share" / "icons" / "hicolor" / size / "apps" / "ophion.png"
            self.assertTrue(icon.is_file(), icon)
            self.assertIn(f"f {icon}", manifest)
        self.assertIn(f"f {self.command}", manifest)
        self.assertIn(f"f {self.desktop}", manifest)
        self.assertEqual(len(manifest), len(set(manifest)))
        self.assertTrue((self.opt / "current" / "venv" / "bin" / "python").exists())
        self.assertEqual(stat.S_IMODE(self.command.stat().st_mode), 0o755)

        # running it again: the same files, one release, no duplicate lines
        files, lines = self.tree(), sorted(RELEASE.sub("ID", line) for line in manifest)
        self.install()
        self.assertEqual(self.tree(), files)
        self.assertEqual(sorted(RELEASE.sub("ID", line) for line in self.manifest()), lines)
        self.assertEqual(len(list((self.opt / "releases").iterdir())), 1)

        # uninstall: the manifest's files go, the user's data stays
        config, data = self.home / ".config" / "ophion", self.local / "share" / "ophion"
        for folder in (config, data):
            folder.mkdir(parents=True)
            (folder / "keep.txt").write_text("user data")
        done = self.run_script(UNINSTALL)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("were kept in", done.stdout)
        self.assertEqual(sorted(str(p.relative_to(self.home)) for p in self.home.rglob("*")),
                         [".config", ".config/ophion", ".config/ophion/keep.txt", ".local", ".local/share",
                          ".local/share/ophion", ".local/share/ophion/keep.txt"])

        # --purge: a wrong answer keeps everything, DELETE removes the data folders
        done = self.run_script(UNINSTALL, "--purge", stdin="delete\n")
        self.assertIn("Not confirmed", done.stdout)
        self.assertTrue(config.is_dir() and data.is_dir())
        done = self.run_script(UNINSTALL, "--purge", stdin="DELETE\n")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertFalse(config.exists() or data.exists())
        self.assertIn("secret-tool clear service ssh-manager", done.stdout)  # the keyring is never touched

    @unittest.skipUnless(display_available(), "needs a display (the app imports GTK)")
    def test_the_installed_command_runs(self) -> None:
        self.install()
        done = subprocess.run([str(self.command), "--version"], capture_output=True, text=True, env=self.env(),
                              timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        version = (self.opt / "current" / "app" / "VERSION").read_text().strip()  # the checkout's git describe
        self.assertEqual(done.stdout.strip(), f"Ophion {version}")

    def test_a_launcher_the_user_made_is_not_replaced_without_asking(self) -> None:
        self.desktop.parent.mkdir(parents=True)
        self.desktop.write_text("[Desktop Entry]\nType=Application\nName=Mine\nExec=mine\n")
        done = self.run_script(INSTALL)
        self.assertEqual(done.returncode, 1)
        self.assertIn("--yes", done.stderr)
        self.assertIn("Name=Mine", self.desktop.read_text())
        self.assertFalse(self.opt.exists())  # nothing else happened
        self.install("--yes")
        self.assertIn("X-Ophion-Installed=true", self.desktop.read_text())

    def test_old_ssh_manager_launchers_are_removed_but_not_its_data(self) -> None:
        old_entry = self.local / "share" / "applications" / "org.codex.SSHManager.desktop"
        old_command = self.local / "bin" / "ssh-manager"
        old_data = self.home / ".config" / "ssh-manager" / "settings.json"
        for path in (old_entry, old_command, old_data):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("old")
        done = self.install()
        self.assertIn("SSH Manager", done.stdout)
        self.assertFalse(old_entry.exists() or old_command.exists())
        self.assertEqual(old_data.read_text(), "old")


class AppImageInstallTests(InstallerCase):
    def test_appimage_mode(self) -> None:
        image = self.home.parent / f"{self.home.name}-Ophion-test-x86_64.AppImage"
        image.write_bytes(b"\x7fELF fake AppImage")
        self.addCleanup(image.unlink)
        self.install("--appimage", str(image))
        installed = self.opt / "Ophion.AppImage"
        self.assertEqual(installed.read_bytes(), image.read_bytes())
        self.assertEqual(stat.S_IMODE(installed.stat().st_mode), 0o755)
        self.assertTrue(self.command.is_symlink())
        self.assertEqual(os.readlink(self.command), str(installed))
        self.assertIn(f"Exec={installed} %U", self.desktop.read_text().splitlines())
        self.assertFalse((self.opt / "releases").exists() or (self.opt / "current").exists())  # no virtualenv

        self.install("--source")  # switching modes removes the AppImage
        self.assertFalse(installed.exists())
        self.assertFalse(self.command.is_symlink())
        self.assertIn(f"Exec={self.command} %U", self.desktop.read_text().splitlines())


if __name__ == "__main__":
    unittest.main()
