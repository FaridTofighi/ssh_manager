"""The local shell's command, start folder, environment and settings (pure)."""

import json
import os
import tempfile
import unittest
from pathlib import Path

from ssh_manager.local_shell import local_shell_env, resolve_shell, shell_argv, start_directory
from ssh_manager.settings import AppSettings, LocalShellSettings


def env_dict(entries):
    return dict(entry.split("=", 1) for entry in entries)


class ResolveShellTests(unittest.TestCase):
    def test_order(self) -> None:
        ok = {"/usr/bin/zsh", "/bin/fish", "/usr/bin/dash", "/bin/bash", "/bin/sh"}
        executable = ok.__contains__
        account = lambda: "/usr/bin/dash"  # noqa: E731
        environ = {"SHELL": "/bin/fish", "PATH": "/usr/bin"}
        self.assertEqual(resolve_shell("/usr/bin/zsh", environ, account_shell=account, executable=executable),
                         "/usr/bin/zsh")
        self.assertEqual(resolve_shell("", environ, account_shell=account, executable=executable), "/bin/fish")
        self.assertEqual(resolve_shell("", {"SHELL": "/missing"}, account_shell=account, executable=executable),
                         "/usr/bin/dash")
        self.assertEqual(resolve_shell("/missing/zsh", {}, account_shell=lambda: "", executable=executable),
                         "/bin/bash")
        self.assertEqual(resolve_shell("", {}, account_shell=lambda: "", executable={"/bin/sh"}.__contains__),
                         "/bin/sh")
        self.assertEqual(resolve_shell("", {"SHELL": "bash"}, account_shell=lambda: "",  # relative $SHELL: not used
                                       executable=lambda p: os.path.isabs(p) and p in ok), "/bin/bash")

    def test_a_command_name_is_found_on_path_and_made_absolute(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            shell = Path(tmp) / "myshell"
            shell.write_text("#!/bin/sh\n")
            shell.chmod(0o755)
            self.assertEqual(resolve_shell("myshell", {"PATH": tmp}), str(shell))
        self.assertTrue(os.path.isabs(resolve_shell("", os.environ)))

    def test_argv(self) -> None:
        self.assertEqual(shell_argv("/bin/bash"), ["/bin/bash"])
        self.assertEqual(shell_argv("/bin/bash", login=True), ["/bin/bash", "-l"])


class StartDirectoryTests(unittest.TestCase):
    def test_fallbacks(self) -> None:
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as work:
            environ = {"HOME": home}
            self.assertEqual(start_directory("current", work, environ), work)
            self.assertEqual(start_directory("home", work, environ), home)
            self.assertEqual(start_directory("current", None, environ), home)
            self.assertEqual(start_directory("current", os.path.join(work, "gone"), environ), home)
            self.assertTrue(os.path.isdir(start_directory("current", None, {"HOME": "/does/not/exist"})))


class EnvironmentTests(unittest.TestCase):
    def test_secrets_and_app_internals_are_removed(self) -> None:
        env = env_dict(local_shell_env({"SSHPASS": "secret", "SSH_MANAGER_DEBUG": "1", "SSHM_X": "y", "HOME": "/home/u",
                                        "USER": "u", "LANG": "C.UTF-8", "LC_TIME": "C", "DISPLAY": ":0",
                                        "WAYLAND_DISPLAY": "wayland-0", "SSH_AUTH_SOCK": "/run/agent",
                                        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/bus", "PATH": "/usr/bin"}))
        for name in ("SSHPASS", "SSH_MANAGER_DEBUG", "SSHM_X"):
            self.assertNotIn(name, env)
        for name in ("HOME", "USER", "LANG", "LC_TIME", "DISPLAY", "WAYLAND_DISPLAY", "SSH_AUTH_SOCK",
                     "DBUS_SESSION_BUS_ADDRESS", "PATH"):
            self.assertIn(name, env)
        self.assertEqual((env["TERM"], env["COLORTERM"]), ("xterm-256color", "truecolor"))
        self.assertNotIn("secret", " ".join(local_shell_env({"SSHPASS": "secret"})))

    def test_appimage_folders_are_removed_from_search_paths(self) -> None:
        app = "/tmp/.mount_SSH_Ma1234"
        env = env_dict(local_shell_env({
            "SSH_MANAGER_APPDIR": app,
            "PATH": f"{app}/usr/bin:/home/u/bin:/usr/bin",
            "LD_LIBRARY_PATH": f"{app}/usr/lib:{app}/usr/lib/x86_64-linux-gnu",
            "XDG_DATA_DIRS": f"{app}/usr/share:/usr/local/share:/usr/share",
            "GI_TYPELIB_PATH": f"{app}/usr/lib/x86_64-linux-gnu/girepository-1.0:/opt/typelibs",
            "PYTHONHOME": f"{app}/usr", "PYTHONPATH": f"{app}/usr/lib/python3/dist-packages",
            "GSETTINGS_SCHEMA_DIR": f"{app}/usr/share/glib-2.0/schemas", "GTK_PATH": f"{app}/usr/lib/gtk-3.0",
            "GIO_MODULE_DIR": f"{app}/usr/lib/gio/modules", "PYTHONNOUSERSITE": "1",
            "GDK_PIXBUF_MODULE_FILE": "/home/u/.cache/ssh-manager-appimage/1/gdk-pixbuf-loaders.cache",
            "SSL_CERT_FILE": "/etc/ssl/certs/ca-certificates.crt", "APPIMAGE": "/home/u/SSH_Manager.AppImage",
        }))
        self.assertEqual(env["PATH"], "/home/u/bin:/usr/bin")
        self.assertEqual(env["XDG_DATA_DIRS"], "/usr/local/share:/usr/share")
        self.assertEqual(env["GI_TYPELIB_PATH"], "/opt/typelibs")
        self.assertEqual(env["SSL_CERT_FILE"], "/etc/ssl/certs/ca-certificates.crt")  # the host's bundle stays
        for name in ("LD_LIBRARY_PATH", "PYTHONHOME", "PYTHONPATH", "GSETTINGS_SCHEMA_DIR", "GTK_PATH",
                     "GIO_MODULE_DIR", "PYTHONNOUSERSITE", "GDK_PIXBUF_MODULE_FILE", "APPIMAGE", "SSH_MANAGER_APPDIR"):
            self.assertNotIn(name, env)

    def test_without_an_appimage_paths_are_kept(self) -> None:
        env = env_dict(local_shell_env({"PATH": "/opt/x/bin:/usr/bin", "PYTHONPATH": "/home/u/lib",
                                        "LD_LIBRARY_PATH": "/opt/lib"}))
        self.assertEqual((env["PATH"], env["PYTHONPATH"], env["LD_LIBRARY_PATH"]),
                         ("/opt/x/bin:/usr/bin", "/home/u/lib", "/opt/lib"))

    def test_term(self) -> None:
        if os.path.exists("/usr/share/terminfo/x/xterm-direct") or os.path.exists("/lib/terminfo/x/xterm-direct"):
            self.assertEqual(env_dict(local_shell_env({"TERM": "xterm-direct"}))["TERM"], "xterm-direct")
        for term in ("dumb", "screen-256color", "xterm-does-not-exist", "../x"):
            self.assertEqual(env_dict(local_shell_env({"TERM": term}))["TERM"], "xterm-256color", term)


class LocalShellSettingsTests(unittest.TestCase):
    def test_validation_and_round_trip(self) -> None:
        self.assertEqual(LocalShellSettings().to_dict(), {"command": "", "login_shell": False, "start_in": "current",
                                                          "on_exit": "close"})
        with self.assertLogs("ssh_manager.settings", "WARNING"):
            bad = LocalShellSettings.from_dict({"command": "zsh\n", "login_shell": "yes", "start_in": "/tmp",
                                                "on_exit": "explode"})
        self.assertEqual(bad, LocalShellSettings())
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text(json.dumps({"host_key_policy": "reject"}))
            settings = AppSettings.load(path)
            self.assertEqual(settings.local_shell, LocalShellSettings())
            settings.local_shell.on_exit = "hold_on_error"
            settings.local_shell.command = "/bin/zsh"
            settings.save()
            loaded = AppSettings.load(path).local_shell
            self.assertEqual((loaded.on_exit, loaded.command), ("hold_on_error", "/bin/zsh"))


if __name__ == "__main__":
    unittest.main()
