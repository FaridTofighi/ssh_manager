"""Customisable shortcuts in the window: the editor, the Preferences page and the real accelerator path."""

import unittest
from unittest import mock

from tests.gtk_window_fixture import display_available


@unittest.skipUnless(display_available(), "needs a display")
class ShortcutEditorTests(unittest.TestCase):
    def setUp(self) -> None:
        from ophion.gtkui.controllers.shortcut_registry import ShortcutRegistry, canonical
        from ophion.gtkui.dialogs.shortcuts_page import ShortcutEditor

        self.canonical = canonical
        self.registry = ShortcutRegistry()
        self.editor = ShortcutEditor(self.registry)

    def test_assign_conflict_replace_clear_reset(self) -> None:
        c = self.canonical
        result = self.editor.assign("paste-primary", "<Primary><Shift>c")  # Copy's
        self.assertFalse(result.ok)
        self.assertEqual(result.conflicts, ["copy"])
        result = self.editor.assign("paste-primary", "<Primary><Shift>c", replace=True)
        self.assertTrue(result.ok)
        self.assertEqual(self.editor.registry.effective_accels("paste-primary"), (c("<Primary><Shift>c"),))
        self.assertEqual(self.editor.registry.effective_accels("copy"), (c("<Primary>Insert"),))  # taken away
        self.editor.assign("paste-primary", "<Shift>Insert", append=True, force=True)
        self.assertEqual(len(self.editor.registry.effective_accels("paste-primary")), 2)
        self.editor.clear("paste-primary")
        self.assertEqual(self.editor.registry.effective_accels("paste-primary"), ())
        self.editor.reset("paste-primary")
        self.assertEqual(self.editor.registry.effective_accels("paste-primary"), (c("<Shift>Insert"),))
        self.editor.reset_all()
        self.assertEqual(self.editor.to_dict(), {})
        self.assertEqual(self.registry.to_dict(), {})  # the window's registry is untouched until Apply
        self.assertTrue(self.editor.assign("copy", "not a key").invalid)

    def test_keys_programs_need_ask_first(self) -> None:
        from ophion.gtkui.dialogs.shortcuts_page import steals_terminal_input

        for accel, steals in (("a", True), ("<Shift>a", True), ("F5", True), ("<Primary>c", True),
                              ("<Primary><Shift>c", False), ("<Primary><Alt>t", False), ("<Alt>x", False)):
            self.assertEqual(steals_terminal_input(accel), steals, accel)
        result = self.editor.assign("reset-terminal", "<Primary>r")
        self.assertTrue(result.steals_input and not result.ok)
        self.assertTrue(self.editor.assign("reset-terminal", "<Primary>r", force=True).ok)

    def test_page_builds_and_asks_before_replacing(self) -> None:
        from ophion.gtkui.dialogs import shortcuts_page
        from ophion.gtkui.dialogs.preferences import PreferencesDialog
        from ophion.gtkui.theme.loader import ThemeMode
        from ophion.settings import AppSettings

        dialog = PreferencesDialog(None, ThemeMode.LIGHT, 1.0, 11, AppSettings(), shortcuts=self.registry)
        self.addCleanup(dialog.destroy)
        page = dialog.shortcuts_page
        self.assertIn("paste-primary", page.rows)
        with mock.patch.object(shortcuts_page, "confirm", return_value=False):  # Cancel
            self.assertFalse(page.assign_interactively("paste-primary", "<Primary><Shift>v").ok)
        self.assertEqual(page.editor.to_dict(), {})
        with mock.patch.object(shortcuts_page, "confirm", return_value=True):  # Replace
            self.assertTrue(page.assign_interactively("paste-primary", "<Primary><Shift>v").ok)
        self.assertEqual(page.rows["paste"].accel_label.get_text(), "Disabled")
        self.assertIn("paste-primary", dialog.values().shortcuts)


@unittest.skipUnless(display_available(), "needs a display")
class WindowShortcutTests(unittest.TestCase):
    def setUp(self) -> None:
        from tests.gtk_window_fixture import WindowSandbox

        self.sandbox = WindowSandbox(store=True)
        self.addCleanup(self.sandbox.close)
        self.win = self.sandbox.window
        self.win.realize()
        save = mock.patch.object(self.win._settings, "save")
        save.start()
        self.addCleanup(save.stop)
        self.calls = []
        for name in ("paste_primary", "paste_clipboard", "copy_clipboard", "copy_clipboard_format"):
            patcher = mock.patch(f"gi.repository.Vte.Terminal.{name}",
                                 lambda term, *a, n=name: self.calls.append((n, term)), create=True)
            patcher.start()
            self.addCleanup(patcher.stop)

    def key(self, accel: str) -> bool:
        from gi.repository import Gdk, Gtk  # type: ignore

        keyval, mods = Gtk.accelerator_parse(accel)
        event = Gdk.Event.new(Gdk.EventType.KEY_PRESS)
        event.key.keyval, event.key.state = keyval, mods
        entries = Gdk.Keymap.get_for_display(Gdk.Display.get_default()).get_entries_for_keyval(keyval)[1]
        if entries:
            event.key.hardware_keycode, event.key.group = entries[0].keycode, entries[0].group
        event.key.window = self.win.get_window()
        event.set_device(Gdk.Display.get_default().get_default_seat().get_keyboard())
        # What GtkWindow's key-press handler does: accelerators / mnemonics first, then the focused widget.
        # (Called directly: the sandbox window is never mapped, so it is not the "active" window for GTK.)
        return self.win.activate_key(event.key) or self.win.propagate_key_event(event.key)

    def terminal(self):
        host = self.win.tree.find_host_by_alias("web-01")
        tab = self.win.terminal_tabs.new_terminal(host, autostart=False)
        self.win.set_focus(tab._terminal)
        return tab._terminal

    def test_shift_insert_in_a_text_field_pastes_into_the_field_only(self) -> None:
        from gi.repository import Gdk, Gtk  # type: ignore

        self.terminal()  # a terminal exists in the background
        entry = self.win.sidebar.search_entry if hasattr(self.win, "sidebar") else None
        entry = entry or next(w for w in _walk(self.win) if isinstance(w, Gtk.SearchEntry))
        Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text("from-the-clipboard", -1)
        self.win.set_focus(entry)
        del self.calls[:]
        self.key("<Shift>Insert")
        for _ in range(50):
            while Gtk.events_pending():
                Gtk.main_iteration_do(False)
        self.assertIn("from-the-clipboard", entry.get_text())
        self.assertEqual(self.calls, [])

    def test_terminal_shortcuts_and_live_rebinding(self) -> None:
        from ophion.gtkui.controllers.shortcuts import accel_label

        term = self.terminal()
        del self.calls[:]
        self.key("<Shift>Insert")
        self.assertEqual(self.calls, [("paste_primary", term)])
        del self.calls[:]
        self.key("<Primary>Insert")
        self.assertTrue(self.calls and self.calls[0][1] is term and self.calls[0][0].startswith("copy"))

        registry = self.win.shortcut_registry
        registry.set_accels("paste-primary", ["<Primary><Alt>v"])
        del self.calls[:]
        self.key("<Shift>Insert")  # the old binding is gone
        self.assertEqual(self.calls, [])
        self.key("<Primary><Alt>v")
        self.assertEqual(self.calls, [("paste_primary", term)])
        self.assertIn("V", accel_label("paste-primary"))
        self.assertEqual(self.win._settings.shortcuts, {"paste-primary": [registry.effective_accels("paste-primary")[0]]})
        registry.set_accels("paste-primary", [])  # disabled
        del self.calls[:]
        self.key("<Primary><Alt>v")
        self.assertEqual(self.calls, [])
        registry.reset_all()
        del self.calls[:]
        self.key("<Shift>Insert")
        self.assertEqual(self.calls, [("paste_primary", term)])

    def test_hints_follow_the_bindings(self) -> None:
        from ophion.gtkui.controllers.shortcuts import refresh_shortcuts_window

        sidebar_button = next(w for w in _walk(self.win) if w.get_tooltip_text() and
                              w.get_tooltip_text().startswith("Hide sidebar"))
        self.assertIn("Ctrl+B", sidebar_button.get_tooltip_text())
        self.win.app_dialogs.show_shortcuts()
        window = self.win.app_dialogs._shortcuts_window
        self.addCleanup(window.hide)
        self.win.shortcut_registry.set_accels("toggle-sidebar", ["<Primary><Alt>b"])
        self.assertIn("Alt", sidebar_button.get_tooltip_text())
        row = window._sshm_rows["toggle-sidebar"]
        self.assertEqual(row.get_property("accelerator"), "<Primary><Alt>b")
        self.win.shortcut_registry.set_accels("toggle-sidebar", [])
        refresh_shortcuts_window(window)
        self.assertFalse(row.get_visible())
        self.assertEqual(sidebar_button.get_tooltip_text(), "Hide sidebar")


def _walk(widget):
    from gi.repository import Gtk  # type: ignore

    yield widget
    if isinstance(widget, Gtk.Container):
        for child in widget.get_children():
            yield from _walk(child)


if __name__ == "__main__":
    unittest.main()
