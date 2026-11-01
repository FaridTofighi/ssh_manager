"""The remote stats probe helpers (GTK-free module; also importable from ``gtk_terminal``)."""

import unittest

from ophion import remote_stats
from ophion.remote_stats import _REMOTE_STATS_SCRIPT, build_remote_stats_command, parse_remote_stats_output


class ParseTests(unittest.TestCase):
    def test_valid_json_object(self) -> None:
        payload = '{"cpu": 12.5, "mem_used": 512.0, "mem_total": 2048.0, "disk_used": 3.0, "disk_total": 20.0}'
        self.assertEqual(parse_remote_stats_output(payload)["cpu"], 12.5)
        self.assertEqual(parse_remote_stats_output("\n  " + payload + "\r\n")["disk_total"], 20.0)

    def test_invalid_and_non_object_json(self) -> None:
        for text in ("not json", "{broken", "[]", "[1, 2]", "42", '"text"', "null"):
            with self.subTest(text=text):
                self.assertIsNone(parse_remote_stats_output(text))

    def test_ansi_noise_is_not_accepted(self) -> None:
        # The output is parsed as-is: escape sequences around the JSON make it unreadable (current behavior).
        self.assertIsNone(parse_remote_stats_output('\x1b[0m{"cpu": 1.0}\x1b[0m'))

    def test_empty_output(self) -> None:
        for text in ("", "   ", "\n\n"):
            self.assertIsNone(parse_remote_stats_output(text))


class CommandTests(unittest.TestCase):
    def test_password_goes_to_the_environment_only(self) -> None:
        command, env = build_remote_stats_command(["ssh", "-p", "2222", "alice@10.0.0.5"], password="hunter2",
                                                  sshpass_path="/usr/bin/sshpass")
        self.assertEqual(command, ["/usr/bin/sshpass", "-e", "ssh", "-o", "ConnectTimeout=6", "-p", "2222",
                                   "alice@10.0.0.5", "python3", "-"])
        self.assertFalse(any("hunter2" in arg for arg in command))
        self.assertNotIn("BatchMode=yes", command)
        self.assertEqual(env["SSHPASS"], "hunter2")

    def test_batch_mode_without_password_or_sshpass(self) -> None:
        for kwargs in ({}, {"password": "hunter2"}, {"sshpass_path": "/usr/bin/sshpass"}):
            with self.subTest(**kwargs):
                command, env = build_remote_stats_command(["ssh", "alice@10.0.0.5"], **kwargs)
                self.assertEqual(command, ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6", "alice@10.0.0.5",
                                           "python3", "-"])
                self.assertIsNone(env)
                self.assertFalse(any("hunter2" in arg for arg in command))

    def test_empty_ssh_command(self) -> None:
        self.assertEqual(build_remote_stats_command([], password="x", sshpass_path="/usr/bin/sshpass"), ([], None))

    def test_the_script_prints_one_json_object(self) -> None:
        self.assertIn("print(json.dumps(", _REMOTE_STATS_SCRIPT)


class ReexportTests(unittest.TestCase):
    def test_gtk_terminal_keeps_the_same_objects(self) -> None:
        try:
            from ophion import gtk_terminal
        except (ImportError, ValueError) as exc:  # no GTK / VTE on this machine
            self.skipTest(f"gtk_terminal unavailable: {exc}")
        self.assertIs(gtk_terminal.build_remote_stats_command, remote_stats.build_remote_stats_command)
        self.assertIs(gtk_terminal.parse_remote_stats_output, remote_stats.parse_remote_stats_output)
        self.assertIs(gtk_terminal._REMOTE_STATS_SCRIPT, remote_stats._REMOTE_STATS_SCRIPT)


if __name__ == "__main__":
    unittest.main()
