import os
import unittest
from unittest import mock

from ssh_manager.launcher import ConnectionLauncher, build_terminal_command


class LauncherTests(unittest.TestCase):
    def test_build_terminal_command_matches_absolute_known_program_with_extra_args(self) -> None:
        self.assertEqual(
            build_terminal_command(
                "/usr/bin/kitty --class ssh-manager",
                "app",
                ["ssh", "app.internal"],
            ),
            [
                "/usr/bin/kitty",
                "--class",
                "ssh-manager",
                "--title",
                "app",
                "ssh",
                "app.internal",
            ],
        )

    def test_build_terminal_command_supports_unknown_program_with_args(self) -> None:
        self.assertEqual(
            build_terminal_command(
                "custom-term --profile ssh",
                "ignored",
                ["ssh", "app.internal"],
            ),
            ["custom-term", "--profile", "ssh", "-e", "ssh", "app.internal"],
        )

    def test_resolve_terminal_command_uses_terminal_env_executable(self) -> None:
        launcher = ConnectionLauncher()

        with mock.patch.dict(os.environ, {"TERMINAL": "custom-term --profile ssh"}):
            with mock.patch("ssh_manager.launcher.shutil.which") as which:
                which.side_effect = lambda program: "/usr/bin/custom-term" if program == "custom-term" else None

                command = launcher._resolve_terminal_command(["ssh", "app"], "app")

        self.assertEqual(command, ["custom-term", "--profile", "ssh", "-e", "ssh", "app"])

    def test_terminal_override_does_not_require_path_lookup(self) -> None:
        launcher = ConnectionLauncher(terminal_override="missing-term --flag")

        with mock.patch("ssh_manager.launcher.shutil.which", return_value=None):
            command = launcher._resolve_terminal_command(["ssh", "app"], "app")

        self.assertEqual(command, ["missing-term", "--flag", "-e", "ssh", "app"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
