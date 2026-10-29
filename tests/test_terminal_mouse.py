"""gnome-terminal mouse behaviour on real VTE terminals: menu, paste, mouse reporting, selection copy."""

import time
import unittest
from unittest import mock

from tests.gtk_window_fixture import display_available


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


@unittest.skipUnless(display_available(), "needs a display")
class TerminalMouseTests(unittest.TestCase):
    def setUp(self) -> None:
        import gi

        gi.require_version("Gtk", "3.0")
        gi.require_version("Gdk", "3.0")
        gi.require_version("Vte", "2.91")
        from gi.repository import Gdk, Gtk  # type: ignore

        from ssh_manager import gtk_terminal
        from ssh_manager.config import HostEntry
        from ssh_manager.settings import TerminalInputSettings

        self.Gdk, self.Gtk, self.gt = Gdk, Gtk, gtk_terminal
        self.tab = gtk_terminal.TerminalTab(HostEntry("mouse-test", {"hostname": "example.invalid"}),
                                            lambda *_a: None, autostart=False)
        self.window = Gtk.OffscreenWindow()
        self.window.set_default_size(640, 400)
        self.window.add(self.tab)
        self.window.show_all()
        self.addCleanup(self.window.destroy)
        self.term = self.tab._terminal
        self.settings = TerminalInputSettings()
        self.tab.set_input_settings(self.settings)
        self.menus = []
        patcher = mock.patch.object(gtk_terminal, "popup_menu", side_effect=lambda menu, *a, **k: self.menus.append(menu))
        patcher.start()
        self.addCleanup(patcher.stop)
        pump(0.3)

    # ------------------------------------------------------------------ helpers
    def run_child(self, script: str) -> None:
        from gi.repository import GLib, Vte  # type: ignore

        self.term.spawn_async(Vte.PtyFlags.DEFAULT, None, ["/bin/sh", "-c", script], None, GLib.SpawnFlags.DEFAULT,
                              None, None, -1, None, None, None)
        pump(0.6)

    def click(self, button: int, shift: bool = False) -> None:
        Gdk = self.Gdk
        seat = Gdk.Display.get_default().get_default_seat()
        window = self.term.get_window()
        for kind in (Gdk.EventType.BUTTON_PRESS, Gdk.EventType.BUTTON_RELEASE):
            event = Gdk.Event.new(kind)
            event.button.window = window
            event.button.button = button
            event.button.x, event.button.y = 30.0, 12.0
            event.button.state = Gdk.ModifierType.SHIFT_MASK if shift else 0
            event.set_device(seat.get_pointer())
            self.term.event(event)
            pump(0.05)

    def text(self) -> str:
        return self.gt.terminal_text(self.term)

    def wait_text(self, needle: str) -> bool:
        return pump(3.0, until=lambda: needle in self.text())

    def menu_labels(self):
        labels = []

        def walk(widget):
            if isinstance(widget, self.Gtk.Label) and widget.get_use_underline():
                labels.append(widget.get_label())
            if isinstance(widget, self.Gtk.Container):
                for child in widget.get_children():
                    walk(child)

        walk(self.menus[-1])
        return labels

    def set_clipboard(self, selection, text: str) -> None:
        self.Gtk.Clipboard.get(selection).set_text(text, -1)
        pump(0.1)

    # -------------------------------------------------------------------- tests
    def test_shift_right_click_opens_the_menu(self) -> None:
        self.run_child("exec cat")
        self.click(3, shift=True)
        self.assertEqual(len(self.menus), 1)
        labels = self.menu_labels()
        for label in ("_Copy", "_Paste", "Select _All", "_Preferences…"):
            self.assertIn(label, labels)

    def test_right_click_follows_the_setting(self) -> None:
        self.run_child("exec cat")
        self.click(3)
        self.assertEqual(len(self.menus), 1)  # default: the menu
        self.settings.right_click = "paste_clipboard"
        self.set_clipboard(self.Gdk.SELECTION_CLIPBOARD, "clip-from-right-click")
        self.click(3)
        self.assertEqual(len(self.menus), 1)
        self.assertTrue(self.wait_text("clip-from-right-click"), self.text())

    def test_middle_click_pastes_the_primary_selection(self) -> None:
        self.run_child("exec cat")
        self.set_clipboard(self.Gdk.SELECTION_PRIMARY, "primary-from-middle")
        self.click(2)
        self.assertTrue(self.wait_text("primary-from-middle"), self.text())
        self.settings.middle_click = "paste_clipboard"
        self.set_clipboard(self.Gdk.SELECTION_CLIPBOARD, "clipboard-from-middle")
        self.click(2)
        self.assertTrue(self.wait_text("clipboard-from-middle"), self.text())

    def test_mouse_reporting_programs_get_the_right_click(self) -> None:
        self.run_child(r"printf '\033[?1000h'; exec cat")  # what vim / tmux / htop do
        self.click(3)
        self.assertEqual(self.menus, [], "plain right-click opened the menu although the program reads the mouse")
        self.click(3, shift=True)
        self.assertEqual(len(self.menus), 1, "Shift+right-click must still open the menu")

    def test_selection_goes_to_primary_and_optionally_the_clipboard(self) -> None:
        Gdk, Gtk = self.Gdk, self.Gtk
        self.run_child("echo selected-words-here; exec cat")
        self.assertTrue(self.wait_text("selected-words-here"))
        self.set_clipboard(Gdk.SELECTION_CLIPBOARD, "keep-me")
        self.tab.select_all_terminal(self.term)
        pump(0.2)
        primary = Gtk.Clipboard.get(Gdk.SELECTION_PRIMARY).wait_for_text() or ""
        self.assertIn("selected-words-here", primary)
        self.assertEqual(Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).wait_for_text(), "keep-me")  # off by default
        self.settings.copy_on_select_clipboard = True
        self.term.unselect_all()
        pump(0.2)
        self.assertEqual(Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).wait_for_text(), "keep-me")  # empty: kept
        self.tab.select_all_terminal(self.term)
        pump(0.2)
        self.assertIn("selected-words-here", Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).wait_for_text() or "")

    def test_menu_items_act_on_the_terminal_under_the_pointer(self) -> None:
        with mock.patch.object(self.tab, "_spawn_secondary"):  # no ssh: the pane would close when it fails
            self.tab.toggle_split()
        pump(0.3)
        second = self.tab._secondary_terminal
        self.assertIsNotNone(second)
        calls = []
        with mock.patch.object(type(second), "paste_primary", lambda term: calls.append(term)):
            spec = self.tab.terminal_menu_spec(second)
            item = next(i for section in spec for i in section.items if i.label == "Paste _Selection")
            item.callback()
        self.assertEqual(calls, [second])
        reconnect = [i for section in spec for i in section.items if i.label == "_Reconnect"]
        self.assertTrue(reconnect and reconnect[0].visible)  # not connected (autostart=False)


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(display_available(), "needs a display")
class MousePreferencesTests(unittest.TestCase):
    def test_preferences_edit_the_shared_settings_in_place(self) -> None:
        from ssh_manager.gtkui.controllers.app_dialogs import AppDialogs
        from ssh_manager.gtkui.dialogs.preferences import PreferencesDialog
        from ssh_manager.gtkui.theme.loader import ThemeMode
        from ssh_manager.settings import AppSettings

        settings = AppSettings()
        shared = settings.terminal_input  # what every open terminal holds
        dialog = PreferencesDialog(None, ThemeMode.LIGHT, 1.0, 11, settings)
        self.addCleanup(dialog.destroy)
        dialog._middle_click_combo.set_active_id("none")
        dialog._right_click_combo.set_active_id("paste_clipboard")
        dialog._copy_select_clipboard_check.set_active(True)
        values = dialog.values()
        self.assertEqual((values.terminal_input.middle_click, values.terminal_input.right_click,
                          values.terminal_input.copy_on_select_clipboard), ("none", "paste_clipboard", True))
        win = mock.MagicMock()
        win._settings = settings
        with mock.patch.object(settings, "save"):
            AppDialogs(win).apply_preferences(values)
        self.assertIs(settings.terminal_input, shared)
        self.assertEqual((shared.middle_click, shared.right_click), ("none", "paste_clipboard"))
