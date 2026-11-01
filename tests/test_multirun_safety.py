"""Run Command: the dangerous-command matcher (advisory) and the confirmation dialog."""

import time
import unittest

from ophion.multirun import safety as S

from tests.gtk_window_fixture import display_available


class MatcherTests(unittest.TestCase):
    POSITIVE = [
        "rm -rf /var/www", "sudo rm -fr /", "RM   -Rf  ~/x", "rm -r -f build", "mkfs.ext4 /dev/sdb1",
        "dd if=/dev/zero of=/dev/sda", "shutdown -h now", "sudo reboot", "poweroff", "halt",
        "init 0", ":(){ :|:& };:", "echo x > /dev/sda", "cat img >/dev/sdb", "chmod -R 777 /",
        "chown -R nobody /srv", "systemctl stop nginx", "systemctl   DISABLE sshd", "systemctl mask ssh",
        "iptables -F", "kill -9 1", "uptime; reboot",
    ]
    NEGATIVE = [
        "uptime", "rm file.txt", "rm -r build", "ls -rf", "df -h", "echo shutdown-notes > notes.txt",
        "cat asphalt.txt", "kill -9 1234", "systemctl status nginx", "systemctl restart nginx",
        "chmod 644 file", "iptables -L", "grep reboots /var/log/x", "dd --help", "init 3",
        "echo x > /dev/null",
    ]

    def test_table(self) -> None:
        for text in self.POSITIVE:
            with self.subTest(text=text):
                self.assertTrue(S.dangerous_matches(text), text)
        for text in self.NEGATIVE:
            with self.subTest(text=text):
                self.assertEqual(S.dangerous_matches(text), [], text)

    def test_reports_what_matched(self) -> None:
        self.assertEqual(S.dangerous_matches("rm -rf /tmp/x && reboot"), ["rm -rf", "reboot"])

    def test_no_catastrophic_backtracking(self) -> None:
        for text in ("rm -" + "r" * 100_000, "a " * 50_000, "systemctl " * 10_000, ":(" * 50_000):
            started = time.perf_counter()
            S.dangerous_matches(text)
            self.assertLess(time.perf_counter() - started, 0.5)

    def test_patterns_live_in_one_tuple(self) -> None:
        self.assertIsInstance(S.DANGEROUS_PATTERNS, tuple)
        self.assertGreaterEqual(len(S.DANGEROUS_PATTERNS), 15)


@unittest.skipUnless(display_available(), "needs a display")
class ConfirmDialogTests(unittest.TestCase):
    def targets(self, count):
        from ophion.multirun.engine import Target

        return [Target(key=f"k{i}", alias=f"host-{i:04d}", hostname="x", group="Prod/web") for i in range(count)]

    def test_needs_confirmation(self) -> None:
        self.assertFalse(S.needs_confirmation(5, "uptime"))
        self.assertTrue(S.needs_confirmation(6, "uptime"))
        self.assertTrue(S.needs_confirmation(1, "reboot"))

    def test_dangerous_command_needs_the_tick(self) -> None:
        from ophion.gtkui.dialogs.multirun_confirm import RunConfirmDialog

        dialog = RunConfirmDialog(None, self.targets(3), "sudo reboot")
        self.addCleanup(dialog.destroy)
        self.assertFalse(dialog.primary_button.get_sensitive())
        self.assertIn("reboot", dialog.matched_label.get_text())
        dialog.understand.set_active(True)
        self.assertTrue(dialog.primary_button.get_sensitive())
        self.assertIn("never", dialog.advisory_label.get_text())  # it claims nothing

    def test_twelve_hundred_hosts_fit_the_screen_and_are_searchable(self) -> None:
        from gi.repository import Gdk, Gtk  # type: ignore

        from ophion.gtkui.dialogs.multirun_confirm import RunConfirmDialog

        dialog = RunConfirmDialog(None, self.targets(1200), "uptime")
        self.addCleanup(dialog.destroy)
        self.assertTrue(dialog.primary_button.get_sensitive())  # not dangerous: no tick needed
        self.assertEqual(dialog.title_label.get_text(), "Run the command on 1200 hosts?")
        self.assertNotIn("host-0001", dialog.title_label.get_text())
        _minimum, natural = dialog.get_preferred_height()
        monitor = Gdk.Display.get_default().get_primary_monitor() or Gdk.Display.get_default().get_monitor(0)
        self.assertLess(natural, 0.7 * monitor.get_geometry().height)
        dialog.hosts.entry.set_text("host-0042")
        dialog.hosts.refilter()
        self.assertEqual(len(dialog.hosts.filter), 1)
        self.assertEqual(tuple(dialog.hosts.filter[0])[:2], ("host-0042   Prod/web", "k42"))  # name shown, key kept
        self.assertIsNotNone(Gtk)


if __name__ == "__main__":
    unittest.main()
