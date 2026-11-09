"""OPHION_REMOTE_STATS=off: sessions never fetch remote statistics (no ssh, no live connection).

The test package sets it (tests/__init__.py): a test that marks a fake session connected starts the
stats timer, which ran the real ``ssh`` to made-up hosts every 10 s for the rest of the run.
"""

import os
import unittest
from unittest import mock

from ophion.config import HostEntry
from tests.gtk_window_fixture import display_available


class PackageTests(unittest.TestCase):
    def test_the_test_package_turns_remote_stats_off(self) -> None:
        self.assertEqual(os.environ.get("OPHION_REMOTE_STATS"), "off")


@unittest.skipUnless(display_available(), "needs a display")
class SwitchTests(unittest.TestCase):
    def tab(self):
        from ophion import gtk_terminal as GT

        tab = GT.TerminalTab(HostEntry(alias="nowhere", options={"hostname": "192.0.2.1"}), lambda *_a: None,
                             autostart=False)
        self.addCleanup(tab.destroy)
        return tab

    def test_off_fetches_nothing(self) -> None:
        tab = self.tab()
        never = AssertionError("no remote stats while OPHION_REMOTE_STATS=off")
        with mock.patch.dict(os.environ, {"OPHION_REMOTE_STATS": "off"}), \
                mock.patch.object(tab, "_fetch_stats_via_paramiko", side_effect=never), \
                mock.patch.object(tab, "_fetch_stats_via_ssh_command", side_effect=never), \
                mock.patch("ophion.gtk_terminal.GLib.idle_add") as idle_add:
            tab._fetch_stats_thread()
        idle_add.assert_called_once_with(tab._apply_stats_result, None)  # shown as N/A, as when a host has none

    def test_otherwise_both_ways_are_tried_as_before(self) -> None:
        tab = self.tab()
        environ = {key: value for key, value in os.environ.items() if key != "OPHION_REMOTE_STATS"}
        with mock.patch.dict(os.environ, environ, clear=True), \
                mock.patch.object(tab, "_fetch_stats_via_paramiko", return_value=None) as paramiko, \
                mock.patch.object(tab, "_fetch_stats_via_ssh_command", return_value={"cpu": 1.0}) as command, \
                mock.patch("ophion.gtk_terminal.GLib.idle_add") as idle_add:
            tab._fetch_stats_thread()
        paramiko.assert_called_once_with()
        command.assert_called_once_with()
        idle_add.assert_called_once_with(tab._apply_stats_result, {"cpu": 1.0})


if __name__ == "__main__":
    unittest.main()
