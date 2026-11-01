"""The local shell tab ("New tab"): opening, the shell itself, exit / close behaviour, no leftovers."""

import os
import re
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.gtk_window_fixture import display_available

ROOT = Path(__file__).resolve().parents[1]


def pump(seconds=0.0, until=None):
    from gi.repository import Gtk  # type: ignore

    deadline = time.monotonic() + seconds
    while True:
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        if until is not None and until():
            return True
        if time.monotonic() >= deadline:
            return until() if until is not None else True
        time.sleep(0.01)


def zombies_of(pid: int):
    found = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            stat = Path(f"/proc/{entry}/stat").read_text()
        except OSError:
            continue
        fields = stat[stat.rindex(")") + 2:].split()
        if fields[0] == "Z" and int(fields[1]) == pid:
            found.append(int(entry))
    return found


@unittest.skipUnless(display_available(), "needs a display")
class LocalTerminalTests(unittest.TestCase):
    def setUp(self) -> None:
        from tests.gtk_window_fixture import WindowSandbox

        self.secret = mock.patch.dict(os.environ, {"SSHPASS": "must-not-leak", "SSH_MANAGER_TEST_X": "1"})
        self.secret.start()
        self.addCleanup(self.secret.stop)
        self.sandbox = WindowSandbox(store=True)
        self.addCleanup(self.sandbox.close)
        self.win = self.sandbox.window
        self.win.realize()
        self.tabs = self.win.terminal_tabs
        self.home = os.environ["HOME"]
        os.makedirs(self.home, exist_ok=True)

    def tearDown(self) -> None:
        for tab in list(self.tabs.tabs()):
            with mock.patch("ophion.gtkui.local_terminal.confirm", return_value=True):
                self.tabs.close_tab(tab)
        pump(0.3)

    # ---------------------------------------------------------------- helpers
    def open(self, **settings):
        for name, value in settings.items():
            setattr(self.win._settings.local_shell, name, value)
        tab = self.tabs.new_local_terminal()
        self.assertTrue(pump(5, until=lambda: tab._shell_pid is not None), "the shell did not start")
        return tab

    def shell(self, tab, command: str) -> str:
        from ophion.gtk_terminal import terminal_text

        marker = f"DONE{time.monotonic_ns()}"
        tab._terminal.feed_child(f"{command}; echo {marker}\n".encode())
        pump(8, until=lambda: terminal_text(tab._terminal).count(marker) >= 2)  # the echo, then the output
        return terminal_text(tab._terminal)

    # ------------------------------------------------------------------ tests
    def test_plus_and_the_shortcut_open_a_local_tab_not_quick_connect(self) -> None:
        from gi.repository import Gtk  # type: ignore

        toplevels = len([w for w in Gtk.Window.list_toplevels() if w.get_visible()])
        with mock.patch("ophion.gtkui.dialogs.prompt.PromptDialog") as prompt:
            self.tabs._strip.new_button.clicked()
            self.win.lookup_action("new-local-tab").activate(None)
            self.tabs._placeholder  # the empty state offers it too (checked below)
            pump(0.5)
        prompt.assert_not_called()
        self.assertEqual(len([t for t in self.tabs.tabs() if getattr(t, "is_local", False)]), 2)
        self.assertEqual(len([w for w in Gtk.Window.list_toplevels() if w.get_visible()]), toplevels)
        app = self.win.get_application()
        self.assertEqual(app.get_accels_for_action("win.new-local-tab"), ["<Primary><Shift>t"])
        self.assertEqual(app.get_accels_for_action("win.quick-connect"), ["<Primary><Alt>t"])

    def test_the_shell_runs_in_home_with_a_clean_environment(self) -> None:
        tab = self.open(start_in="home")
        text = self.shell(tab, 'echo "R:$(pwd):$TERM:$COLORTERM:${SSHPASS-unset}:${SSH_MANAGER_TEST_X-unset}"')
        self.assertIn(f"R:{os.path.realpath(self.home)}:xterm-256color:truecolor:unset:unset", text.replace(self.home, os.path.realpath(self.home)))
        self.assertTrue(os.path.isabs(tab.shell_path))
        children = Path(f"/proc/{tab._shell_pid}/task/{tab._shell_pid}/children").read_text().split()
        names = [Path(f"/proc/{c}/comm").read_text().strip() for c in children if Path(f"/proc/{c}").exists()]
        self.assertNotIn("sshpass", names)
        self.assertIs(tab.status.value != "failed", True)
        self.assertIsNone(self.win.tree.find_host_by_alias("Local"))  # never an inventory host
        self.assertEqual(tab.title, "Local")

    def test_start_in_the_current_tabs_folder(self) -> None:
        first = self.open(start_in="current")
        self.shell(first, "cd /tmp")
        pump(0.3)
        second = self.tabs.new_local_terminal()
        self.assertTrue(pump(5, until=lambda: second._shell_pid is not None))
        self.assertEqual(os.path.realpath(second.current_directory()), os.path.realpath("/tmp"))
        duplicate_target = []
        with mock.patch.object(self.tabs, "new_local_terminal", side_effect=lambda cwd=None: duplicate_target.append(cwd)):
            self.tabs.duplicate_tab(first)
        self.assertEqual(os.path.realpath(duplicate_target[0]), os.path.realpath("/tmp"))

    def test_exit_closes_the_tab_or_holds_it(self) -> None:
        tab = self.open(on_exit="close")
        tab._terminal.feed_child(b"exit\n")
        self.assertTrue(pump(5, until=lambda: tab not in self.tabs.tabs()), "exit did not close the tab")
        held = self.open(on_exit="hold")
        held._terminal.feed_child(b"exit 3\n")
        self.assertTrue(pump(5, until=lambda: held._shell_pid is None))
        pump(0.3)
        self.assertIn(held, self.tabs.tabs())
        self.assertIn("3", held._status_label.get_text())
        self.assertNotEqual(held.status.value, "failed")
        held.reconnect()  # Restart
        self.assertTrue(pump(5, until=lambda: held._shell_pid is not None))

    def test_closing_asks_only_while_a_job_runs(self) -> None:
        tab = self.open()
        with mock.patch("ophion.gtkui.local_terminal.confirm") as ask:
            self.tabs.close_tab(tab)
        ask.assert_not_called()
        self.assertNotIn(tab, self.tabs.tabs())
        busy = self.open()
        busy._terminal.feed_child(b"sleep 60\n")
        self.assertTrue(pump(5, until=busy.foreground_job_running))
        with mock.patch("ophion.gtkui.local_terminal.confirm", return_value=False) as ask:
            self.tabs.close_tab(busy)
        ask.assert_called_once()
        self.assertIn(busy, self.tabs.tabs())  # kept
        self.assertTrue(self.win._on_delete_event.__self__ is self.win)

    def test_no_zombies_after_many_tabs(self) -> None:
        tabs = [self.open() for _ in range(20)]
        shells = [tab._shell_pid for tab in tabs]
        for tab in tabs:
            self.tabs.close_tab(tab)
        self.assertTrue(pump(10, until=lambda: not zombies_of(os.getpid())), zombies_of(os.getpid()))
        # Closing hangs the shells up: none keeps running as an orphan.
        self.assertTrue(pump(10, until=lambda: not [p for p in shells if Path(f"/proc/{p}").exists()]),
                        [p for p in shells if Path(f"/proc/{p}").exists()])

    def test_quick_connect_still_works(self) -> None:
        with mock.patch("ophion.gtkui.dialogs.prompt.PromptDialog") as prompt:
            prompt.return_value.ask.return_value = None
            prompt.return_value.run.return_value = 0
            items = self.tabs._strip.new_menu.get_children()
            next(i for i in items if "Quick" in i.get_label()).activate()
            self.win.lookup_action("quick-connect").activate(None)
        self.assertEqual(prompt.call_count, 2)

    def test_the_local_tab_has_everything_the_workspace_calls(self) -> None:
        from ophion.gtk_terminal import TerminalTab

        source = (ROOT / "ophion" / "gtk_terminal.py").read_text()
        notebook = source[source.index("class TerminalNotebook"):]
        used = set(re.findall(r"\btab\.([a-zA-Z_][a-zA-Z0-9_]*)", notebook))
        host = self.win.tree.find_host_by_alias("web-01")
        ssh_tab = TerminalTab(host, lambda *_a: None, autostart=False)
        self.addCleanup(ssh_tab.destroy)
        local = self.open()
        expected = sorted(name for name in used if hasattr(ssh_tab, name))
        self.assertGreater(len(expected), 30)
        missing = [name for name in expected if not hasattr(local, name)]
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(display_available(), "needs a display")
class LocalShellPreferencesTests(unittest.TestCase):
    def test_preferences_edit_the_shared_settings(self) -> None:
        from ophion.gtkui.controllers.app_dialogs import AppDialogs
        from ophion.gtkui.dialogs.preferences import PreferencesDialog
        from ophion.gtkui.theme.loader import ThemeMode
        from ophion.settings import AppSettings

        settings = AppSettings()
        shared = settings.local_shell
        dialog = PreferencesDialog(None, ThemeMode.LIGHT, 1.0, 11, settings)
        self.addCleanup(dialog.destroy)
        dialog._shell_entry.set_text("/bin/sh")
        dialog._login_shell_check.set_active(True)
        dialog._start_in_combo.set_active_id("home")
        dialog._on_exit_combo.set_active_id("hold_on_error")
        win = mock.MagicMock()
        win._settings = settings
        with mock.patch.object(settings, "save"):
            AppDialogs(win).apply_preferences(dialog.values())
        self.assertIs(settings.local_shell, shared)
        self.assertEqual((shared.command, shared.login_shell, shared.start_in, shared.on_exit),
                         ("/bin/sh", True, "home", "hold_on_error"))
