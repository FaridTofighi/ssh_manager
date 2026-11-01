"""The product identity after the rename to Ophion (no GTK), and the old names it still understands."""

import unittest
from pathlib import Path

from ssh_manager import identity as I


class IdentityTests(unittest.TestCase):
    def test_constants(self) -> None:
        self.assertEqual((I.APP_NAME, I.APP_SLUG, I.APP_ID), ("Ophion", "ophion", "io.github.ophion.Ophion"))
        self.assertEqual(I.APP_TAGLINE, "Connection manager for SSH, SFTP, FTP and RDP")
        self.assertEqual((I.LEGACY_APP_NAME, I.LEGACY_SLUG, I.LEGACY_APP_ID),
                         ("SSH Manager", "ssh-manager", "org.codex.SSHManager"))

    def test_environment_new_name_first_then_the_old_one(self) -> None:
        self.assertEqual(I.env("APPDIR", {"OPHION_APPDIR": "/new", "SSH_MANAGER_APPDIR": "/old"}), "/new")
        self.assertEqual(I.env("APPDIR", {"SSH_MANAGER_APPDIR": "/old"}), "/old")
        self.assertEqual(I.env("APPDIR", {}, "x"), "x")

    def test_local_shell_drops_both_prefixes_and_reads_the_new_appdir(self) -> None:
        from ssh_manager.local_shell import local_shell_env

        env = dict(item.split("=", 1) for item in local_shell_env(
            {"PATH": "/usr/bin", "HOME": "/home/u", "OPHION_APPDIR": "/tmp/.mount_x", "SSH_MANAGER_DEBUG": "1",
             "OPHION_DEBUG": "1", "APPIMAGE": "/a/Ophion.AppImage"}))
        self.assertFalse([name for name in env if name.startswith(("OPHION_", "SSH_MANAGER_"))])
        self.assertNotIn("APPIMAGE", env)  # OPHION_APPDIR is recognised: the AppImage's own variables go

    def test_names_written_and_names_still_read(self) -> None:
        from ssh_manager import native_format as F
        from ssh_manager.inventory import openssh

        self.assertEqual(F.MIME_TYPES, ("application/x-ophion+json", "application/x-ssh-manager+json"))
        self.assertEqual((F.FORMAT, F.EXTENSION), ("ssh-manager-export", ".sshm"))  # file format: unchanged
        self.assertEqual(openssh.DEFAULT_EXPORT, Path("~/.config/ophion/ssh_config"))
        self.assertEqual((openssh.INCLUDE_MARK, openssh.LEGACY_INCLUDE_MARK),
                         ("# added by Ophion", "# added by SSH Manager"))
        self.assertTrue(F.default_file_name(0).startswith("ophion-"))


if __name__ == "__main__":
    unittest.main()
