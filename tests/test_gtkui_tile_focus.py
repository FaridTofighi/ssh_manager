"""Show all sessions (tile mode): the current session keeps keyboard focus when entering and leaving it.

The window's content runs in an offscreen toplevel (every widget realized and mapped); keyboard focus is
the toplevel's focus widget, and a real key press is delivered through ``Gtk.main_do_event`` so the typed
text must reach the expected terminal (VTE's ``commit``).
"""

import unittest
from unittest import mock

from tests.gtk_window_fixture import WindowSandbox
from tests.test_gtkui_pane_clicks import Offscreen, _display_available, _pump, _wait


@unittest.skipUnless(_display_available(), "needs a display")
class TileFocusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sandbox = WindowSandbox()
        self.addCleanup(self.sandbox.close)
        self.win = self.sandbox.window
        self.notebook = self.win.terminal_tabs
        for name in ("_spawn", "_spawn_secondary"):
            patcher = mock.patch(f"ophion.gtk_terminal.TerminalTab.{name}")
            patcher.start()
            self.addCleanup(patcher.stop)
        self.typed = {}  # terminal -> text that reached it

    def open(self, *aliases):
        tabs = []
        for alias in aliases:
            tab = self.notebook.new_terminal(self.win.tree.find_host_by_alias(alias), autostart=False)
            tab._set_status("Connected", True)
            tabs.append(tab)
        self.screen = Offscreen(self.win)
        self.addCleanup(self.screen.restore)
        for tab in tabs:
            self.spy(tab)
        return tabs

    def spy(self, tab):
        for term in tab._terminals():
            if term not in self.typed:
                self.typed[term] = ""
                term.connect("commit", lambda t, text, _size: self.typed.__setitem__(t, self.typed[t] + text))

    def focus(self):
        return self.screen.top.get_focus()

    def type_text(self, text: str) -> None:
        from gi.repository import Gdk, Gtk  # type: ignore

        keymap = Gdk.Keymap.get_for_display(Gdk.Display.get_default())
        for char in text:
            keyval = Gdk.unicode_to_keyval(ord(char))
            found, keys = keymap.get_entries_for_keyval(keyval)
            for kind in (Gdk.EventType.KEY_PRESS, Gdk.EventType.KEY_RELEASE):
                event = Gdk.Event.new(kind)
                event.key.window = self.screen.top.get_window()
                event.key.keyval = keyval
                event.key.hardware_keycode = keys[0].keycode if found and keys else 0
                event.key.time = Gtk.get_current_event_time()
                event.set_device(Gdk.Display.get_default().get_default_seat().get_keyboard())
                Gtk.main_do_event(event)
        _pump()

    def assert_current(self, tab, term=None, label=""):
        term = term or tab.active_terminal
        self.assertTrue(_wait(lambda: self.focus() is term, 3.0),
                        f"{label}: keyboard focus is on {type(self.focus()).__name__}, not the session's terminal")
        self.assertIs(self.notebook.current_tab(), tab, label)
        self.assertIs(self.notebook._last_current_tab, tab, label)
        for other in self.typed:
            self.assertEqual(other is self.focus(), other is term)
        if self.notebook.tile_mode:
            focused = [t for t, frame in self.notebook._tile_wrappers.items() if frame.focused]
            self.assertEqual(focused, [tab], f"{label}: exactly the current tile is focused")
        else:
            self.assertIs(self.notebook._notebook.get_nth_page(self.notebook._notebook.get_current_page()), tab, label)
        for other in self.typed:
            self.typed[other] = ""
        self.type_text("ab")
        self.assertEqual({t: s for t, s in self.typed.items() if s}, {term: "ab"}, f"{label}: typing")

    def toggle(self, active: bool) -> None:
        self.notebook.toggle_tile_mode() if self.notebook.tile_mode != active else None
        _pump()

    # ---------------------------------------------------------------- tests
    def test_entering_and_leaving_keeps_the_current_session(self) -> None:
        tabs = self.open("web-01", "web-02", "db-primary")
        for index in (1, 0, 2):  # the middle one, the first, the last
            with self.subTest(session=index):
                self.notebook.activate_pane(tabs[index])
                self.assert_current(tabs[index], label="before")
                self.toggle(True)
                self.assert_current(tabs[index], label="tile mode on")
                self.toggle(False)
                self.assert_current(tabs[index], label="tile mode off")

    def test_a_split_session_keeps_its_active_pane(self) -> None:
        tabs = self.open("web-01", "web-02", "db-primary")
        tabs[1].toggle_split()
        _pump()
        self.spy(tabs[1])
        second = tabs[1]._terminals()[1]
        self.notebook.activate_pane(tabs[1], second)
        self.assert_current(tabs[1], second, "split before")
        self.toggle(True)
        self.assert_current(tabs[1], second, "split, tile mode on")
        self.toggle(False)
        self.assert_current(tabs[1], second, "split, tile mode off")

    def test_broadcast_enters_and_leaves_tile_mode_with_focus(self) -> None:
        tabs = self.open("web-01", "web-02", "db-primary")
        self.notebook.activate_pane(tabs[1])
        self.notebook.toggle_broadcast()
        _pump()
        self.assertTrue(self.notebook.tile_mode)
        self.assertTrue(_wait(lambda: self.focus() is tabs[1].active_terminal, 3.0), "broadcast on")
        self.assertIs(self.notebook.current_tab(), tabs[1])
        self.notebook.toggle_broadcast()
        self.notebook.toggle_tile_mode()
        _pump()
        self.assert_current(tabs[1], label="broadcast and tile mode off")

    def test_closing_the_current_session_in_tile_mode_then_leaving(self) -> None:
        tabs = self.open("web-01", "web-02", "db-primary")
        self.notebook.activate_pane(tabs[2])
        self.toggle(True)
        self.notebook.close_tab(tabs[2])
        _pump()
        del self.typed[tabs[2].active_terminal]
        self.toggle(False)
        current = self.notebook.current_tab()
        self.assertIn(current, tabs[:2])
        self.assert_current(current, label="after closing the current tile")

    def test_programmatic_change_leaves_focus_in_a_text_entry_outside(self) -> None:
        tabs = self.open("web-01", "web-02", "db-primary")
        self.notebook.activate_pane(tabs[1])
        _wait(lambda: self.focus() is tabs[1].active_terminal, 3.0)
        _wait(lambda: False, 2.5)  # the focus retries queued when the sessions opened (up to 2 s) have run out
        entry = self.win._sidebar.search_entry
        entry.grab_focus()
        _pump()
        self.assertIs(self.focus(), entry)
        self.notebook.set_tile_mode(True)  # not a user toggle of the sessions area: the entry keeps focus
        _pump()
        _wait(lambda: False, 0.3)
        self.assertIs(self.focus(), entry)
        self.assertIs(self.notebook.current_tab(), tabs[1])
        self.notebook.toggle_tile_mode()  # the user's toggle (button / shortcut): the session gets it back
        _pump()
        self.assert_current(tabs[1], label="user toggle after the entry")


if __name__ == "__main__":
    unittest.main()
