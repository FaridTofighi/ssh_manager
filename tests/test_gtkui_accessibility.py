import unittest

from tests.gtk_window_fixture import WindowSandbox


def _display_available() -> bool:
    try:
        import gi

        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk  # type: ignore

        return bool(Gtk.init_check(None)[0])
    except Exception:
        return False


HAS_DISPLAY = _display_available()


def _walk(widget):
    yield widget
    children = []
    if hasattr(widget, "get_children"):
        try:
            children = widget.get_children()
        except Exception:
            children = []
    for child in children:
        yield from _walk(child)
    if hasattr(widget, "get_popover"):
        popover = widget.get_popover()
        if popover is not None and popover.get_child() is not None:
            yield from _walk(popover.get_child())


def _is_icon_only(button) -> bool:
    from gi.repository import Gtk  # type: ignore

    if not isinstance(button, Gtk.Button):
        return False
    if button.get_label():
        return False
    child = button.get_child()
    if isinstance(child, Gtk.Label):
        return False
    if isinstance(child, Gtk.Box):
        return not any(isinstance(c, Gtk.Label) and c.get_text() for c in _walk(child))
    return True


@unittest.skipUnless(HAS_DISPLAY, "GTK display not available")
class MainWindowAccessibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sandbox = WindowSandbox()
        cls.window = cls.sandbox.window
        cls.app = cls.sandbox.app

    @classmethod
    def tearDownClass(cls) -> None:
        cls.sandbox.close()

    def _widgets(self):
        yield from _walk(self.window.get_titlebar())
        yield from _walk(self.window.get_child())

    def test_icon_only_buttons_have_tooltip_and_accessible_name(self) -> None:
        missing = []
        for widget in self._widgets():
            if _is_icon_only(widget):
                tooltip = widget.get_tooltip_text() or ""
                accessible = widget.get_accessible()
                name = accessible.get_name() if accessible is not None else ""
                if not tooltip or not name:
                    missing.append((type(widget).__name__, tooltip, name))
        self.assertEqual(missing, [])

    def test_relay_top_bar_and_page_controls_are_named(self) -> None:
        header = self.window._header
        for widget, expected in (
            (header.avatar_button, "Application menu"),
            (header.workspace_button, "Switch workspace"),
            (header.help_button, "Help"),
        ):
            self.assertEqual(widget.get_accessible().get_name(), expected)
        self.assertTrue(header.health_button.get_accessible().get_name())
        page = self.window.host_page.view
        self.assertEqual(page.page_bar.more.get_accessible().get_name(), "More actions")
        self.assertEqual([b.get_label() for b in page.tabs.buttons.values()], ["Terminal", "Details", "Activity"])
        self.assertTrue(all(b.get_tooltip_text() for b in page.tabs.buttons.values()))

    def test_search_and_tree_are_named(self) -> None:
        self.assertEqual(self.window._sidebar.search_entry.get_accessible().get_name(), "Search hosts")
        self.assertTrue(self.window.tree_view.get_accessible().get_name())

    def test_every_accelerator_is_registered(self) -> None:
        from ssh_manager.gtkui.controllers.shortcuts import ACTIONS

        for spec in ACTIONS:
            if spec.accels:
                with self.subTest(action=spec.name):
                    self.assertIsNotNone(self.window.lookup_action(spec.name))
                    self.assertEqual(list(self.app.get_accels_for_action(spec.detailed_name)), list(spec.accels))
        self.assertEqual(list(self.app.get_accels_for_action("win.goto-tab(3)")), ["<Alt>3"])

    def test_shortcuts_window_lists_actions(self) -> None:
        from gi.repository import Gtk  # type: ignore

        from ssh_manager.gtkui.controllers.shortcuts import build_shortcuts_window

        shortcuts = build_shortcuts_window(self.window)
        # Never destroyed (GTK 3.24 bug, see build_shortcuts_window): kept for the process.
        _KEEP_ALIVE.append(shortcuts)
        titles = [w.props.title for w in _walk(shortcuts) if isinstance(w, Gtk.ShortcutsShortcut)]
        self.assertIn("Toggle broadcast input", titles)
        self.assertIn("Keyboard shortcuts", titles)
        # Closing only hides it (a destroyed ShortcutsWindow crashes GTK on the next restyle).
        self.assertTrue(shortcuts.emit("delete-event", None))
        self.assertGreater(len(titles), 25)


_KEEP_ALIVE: list = []


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
