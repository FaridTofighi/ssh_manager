"""A remote-stats refresh must never resize the terminal: the status bar under it keeps one height.

The spinner shown while fetching is 16 px tall (GTK's minimum) while the bar's labels follow the UI
font. With fonts whose line is shorter than that, showing / hiding the spinner made the bar grow and
shrink by a few pixels, so the terminal above lost and regained a row on every refresh ("the content
jumps"). ``SMALL_TEXT`` reproduces such fonts here; the checks also run with the normal theme.
"""

import os
import tempfile
import time
import unittest
from unittest import mock

from tests.gtk_window_fixture import display_available

SMALL_TEXT = b".statusbar label { font-size: 9px; }"
VALUES = {"cpu": 37.0, "mem_used": 3000.0, "mem_total": 7900.0, "disk_used": 20.0, "disk_total": 78.0}
WARN = {"cpu": 85.0, "mem_used": 6500.0, "mem_total": 7900.0, "disk_used": 70.0, "disk_total": 78.0}
CRITICAL = {"cpu": 99.0, "mem_used": 7800.0, "mem_total": 7900.0, "disk_used": 77.0, "disk_total": 78.0}


def pump(seconds: float = 0.05, window=None) -> None:
    from gi.repository import Gtk  # type: ignore

    end = time.monotonic() + seconds
    while True:
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        if window is not None:
            window.check_resize()  # a layout pass now: nothing draws frames for windows nobody sees
        if time.monotonic() >= end:
            return
        time.sleep(0.005)


@unittest.skipUnless(display_available(), "needs a display")
class StatusBarCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.providers = []
        from ssh_manager.gtkui.theme.loader import ThemeMode, get_theme_manager

        get_theme_manager().apply(ThemeMode.DARK)  # the app's stylesheet, as at start-up

    def tearDown(self) -> None:
        from gi.repository import Gdk, Gtk  # type: ignore

        for provider in self.providers:
            Gtk.StyleContext.remove_provider_for_screen(Gdk.Screen.get_default(), provider)

    def small_text(self) -> None:
        from gi.repository import Gdk, Gtk  # type: ignore

        provider = Gtk.CssProvider()
        provider.load_from_data(SMALL_TEXT)
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), provider,
                                                 Gtk.STYLE_PROVIDER_PRIORITY_USER)
        self.providers.append(provider)

    def local_tab(self):
        from ssh_manager.gtkui.local_terminal import LocalTerminalTab
        from ssh_manager.settings import LocalShellSettings

        settings = LocalShellSettings()
        settings.command = "/bin/sh"
        tab = LocalTerminalTab(lambda *_a, **_k: None, 1.0, settings=settings, cwd=self.tmp.name)
        tab._fetch_stats_thread = lambda: None  # no network: the tests apply the results themselves
        self.addCleanup(tab.destroy)
        return tab

    def offscreen(self, tab, height: int):
        from gi.repository import Gtk  # type: ignore

        window = Gtk.OffscreenWindow()
        window.set_size_request(480, height)
        window.add(tab)
        window.show_all()
        self.addCleanup(window.destroy)
        pump(0.3, window)
        tab._session_active = True
        tab._statusbar.set_stats_visible(True)
        pump(0.1, window)
        return window

    @staticmethod
    def cycle(tab, result, window):
        """``(rows, columns, bar height)`` before, while fetching and after a refresh."""

        def now():
            return (tab._terminal.get_row_count(), tab._terminal.get_column_count(),
                    tab._statusbar.get_allocated_height())

        before = now()
        tab._update_stats()
        pump(0.05, window)
        during = now()
        tab._apply_stats_result(result)
        pump(0.05, window)
        return before, during, now()


class StatusBarHeightTests(StatusBarCase):
    def test_1_the_bar_keeps_one_height_in_every_state(self) -> None:
        from ssh_manager.gtkui.views.statusbar import SessionStatusBar

        for small in (False, True):
            if small:
                self.small_text()
            with self.subTest(small_text=small):
                tab = self.local_tab()
                window = self.offscreen(tab, 300)
                bar: SessionStatusBar = tab._statusbar
                heights = {"idle": bar.get_allocated_height()}
                tab._update_stats()
                pump(0.05, window)
                heights["fetching"] = bar.get_allocated_height()
                for name, result in (("n/a", None), ("ok", VALUES), ("warn", WARN), ("critical", CRITICAL)):
                    tab._update_stats()
                    tab._apply_stats_result(result)
                    pump(0.05, window)
                    heights[name] = bar.get_allocated_height()
                bar.set_stats_visible(False)
                pump(0.05, window)
                heights["stats hidden"] = bar.get_allocated_height()
                bar.set_stats_visible(True)
                pump(0.05, window)
                heights["stats shown"] = bar.get_allocated_height()
                self.assertEqual(len(set(heights.values())), 1, heights)

    def test_1_not_taller_than_the_text(self) -> None:
        tab = self.local_tab()
        window = self.offscreen(tab, 300)
        bar = tab._statusbar
        text = max(bar.status_label.get_allocated_height(), bar.size_label.get_allocated_height())
        top, bottom = bar.get_style_context().get_padding(bar.get_state_flags()).top, \
            bar.get_style_context().get_padding(bar.get_state_flags()).bottom
        self.assertLessEqual(bar.get_allocated_height(), text + top + bottom + 1)  # nothing added for the spinner
        self.assertTrue(window.get_visible())

    def test_2_and_3_a_refresh_never_resizes_or_scrolls_the_terminal(self) -> None:
        self.small_text()
        tab = self.local_tab()
        window = self.offscreen(tab, 300)
        tab._terminal.feed_child(b"for i in $(seq 1 100); do echo line $i; done\n")
        pump(1.0, window)
        char = tab._terminal.get_char_height()
        for height in range(300, 300 + char + 1):  # one window height lands just on a row boundary
            window.set_size_request(480, height)
            pump(0.05, window)
            for result in (VALUES, None):
                adjustment = tab._terminal.get_vadjustment()
                adjustment.set_value(adjustment.get_upper() - adjustment.get_page_size())
                scroll = adjustment.get_value()
                with self.subTest(height=height, result=bool(result)):
                    before, during, after = self.cycle(tab, result, window)
                    self.assertEqual(before[:2], during[:2])
                    self.assertEqual(before[:2], after[:2])
                    self.assertEqual(adjustment.get_value(), scroll)

    def test_4_the_spinner_turns_while_fetching_and_is_invisible_when_idle(self) -> None:
        from gi.repository import Atk  # type: ignore

        tab = self.local_tab()
        window = self.offscreen(tab, 300)
        spinner = tab._statusbar.spinner
        role = spinner.get_accessible().get_role()
        self.assertFalse(spinner.get_property("active"))
        self.assertEqual(spinner.get_opacity(), 0.0)
        tab._update_stats()
        pump(0.05, window)
        self.assertTrue(spinner.get_property("active"))
        self.assertEqual(spinner.get_opacity(), 1.0)
        tab._apply_stats_result(VALUES)
        pump(0.05, window)
        self.assertFalse(spinner.get_property("active"))
        self.assertEqual(spinner.get_opacity(), 0.0)
        self.assertTrue(spinner.get_visible())  # keeps its place
        self.assertEqual(spinner.get_accessible().get_role(), role)
        self.assertNotEqual(role, Atk.Role.INVALID)


@unittest.skipUnless(display_available(), "needs a display")
class TileModeTests(StatusBarCase):
    def test_2_five_tiles_with_broadcast(self) -> None:
        from gi.repository import Gtk  # type: ignore

        from tests.gtk_window_fixture import WindowSandbox

        self.small_text()
        sandbox = WindowSandbox(store=True)
        self.addCleanup(sandbox.close)
        win = sandbox.window
        os.makedirs(os.environ["HOME"], exist_ok=True)
        win._settings.local_shell.command = "/bin/sh"
        tabs = win.terminal_tabs
        opened = []
        for _ in range(5):
            tab = tabs.new_local_terminal()
            tab._fetch_stats_thread = lambda: None
            tabs._on_broadcast_include_toggled(tab, True)
            opened.append(tab)
        tabs.set_broadcast_mode(True)
        self.assertTrue(tabs.tile_mode and tabs.broadcast_active)
        holder = tabs.get_parent()  # the real notebook, laid out in an off-screen window
        holder.remove(tabs)
        window = Gtk.OffscreenWindow()
        window.add(tabs)
        window.set_size_request(1280, 800)
        window.show_all()
        window.show()  # the sandbox's show_all() leaves windows themselves hidden
        pump(0.5, window)
        for tab in opened:
            tab._session_active = True
            tab._statusbar.set_stats_visible(True)
        char = opened[0]._terminal.get_char_height()
        try:
            for height in range(800, 800 + 2 * char + 2):  # tiles are half the height: two rows of sizes
                window.set_size_request(1280, height)
                pump(0.05, window)
                sizes = [(t._terminal.get_row_count(), t._terminal.get_column_count()) for t in opened]
                for tab in opened:
                    tab._update_stats()
                pump(0.05, window)
                during = [(t._terminal.get_row_count(), t._terminal.get_column_count()) for t in opened]
                for tab in opened:
                    tab._apply_stats_result(VALUES if height % 2 else None)
                pump(0.05, window)
                after = [(t._terminal.get_row_count(), t._terminal.get_column_count()) for t in opened]
                with self.subTest(height=height):
                    self.assertEqual(sizes, during)
                    self.assertEqual(sizes, after)
        finally:
            for tab in opened:
                tab._session_active = False
            with mock.patch("ssh_manager.gtkui.local_terminal.confirm", return_value=True):
                for tab in list(tabs.tabs()):
                    tabs.close_tab(tab)
            window.remove(tabs)
            holder.add(tabs)
            window.destroy()


if __name__ == "__main__":
    unittest.main()
