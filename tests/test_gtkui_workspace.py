import unittest

from ssh_manager.config import HostEntry
from ssh_manager.gtkui.theme.tokens import ConnectionStatus
from ssh_manager.gtkui.views.statusbar import stat_level


def _display_available() -> bool:
    try:
        import gi

        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk  # type: ignore

        return bool(Gtk.init_check(None)[0])
    except Exception:
        return False


HAS_DISPLAY = _display_available()


class StatLevelTests(unittest.TestCase):
    def test_thresholds(self) -> None:
        self.assertEqual(stat_level(None), "")
        self.assertEqual(stat_level(79.9), "")
        self.assertEqual(stat_level(80.0), "stat-warn")
        self.assertEqual(stat_level(89.9), "stat-warn")
        self.assertEqual(stat_level(90.0), "stat-critical")


class StatusMessageTests(unittest.TestCase):
    def test_messages_map_to_states(self) -> None:
        from ssh_manager.gtk_terminal import _status_for_message

        self.assertIs(_status_for_message("Connected", True), ConnectionStatus.CONNECTED)
        self.assertIs(_status_for_message("Connecting…", False), ConnectionStatus.CONNECTING)
        self.assertIs(_status_for_message("Authentication failed", False), ConnectionStatus.FAILED)
        self.assertIs(_status_for_message("Connection failed", False), ConnectionStatus.FAILED)
        self.assertIs(_status_for_message("Disconnected", False), ConnectionStatus.UNKNOWN)


@unittest.skipUnless(HAS_DISPLAY, "GTK display not available")
class NotebookTests(unittest.TestCase):
    def setUp(self) -> None:
        from ssh_manager.gtk_terminal import TerminalNotebook

        self.logs = []
        self.notebook = TerminalNotebook(lambda message, ok: self.logs.append((message, ok)))

    def tearDown(self) -> None:
        self.notebook.destroy()

    def _open(self, alias: str):
        tab = self.notebook.new_terminal(HostEntry(alias=alias, parent_group="p", child_group="c"), autostart=False)
        tab._set_status("Connected", True)
        return tab

    def test_broadcast_requires_two_sessions_and_enables_tiles(self) -> None:
        self._open("a")
        self.notebook.set_broadcast_mode(True)
        self.assertFalse(self.notebook.broadcast_active)
        self._open("b")
        self.notebook.set_broadcast_mode(True)
        self.assertTrue(self.notebook.broadcast_active)
        self.assertTrue(self.notebook.tile_mode)
        self.assertTrue(self.notebook._broadcast_strip.get_reveal_child())
        # Leaving tile mode also turns broadcast off.
        self.notebook.set_tile_mode(False)
        self.assertFalse(self.notebook.broadcast_active)
        self.assertFalse(self.notebook._broadcast_strip.get_reveal_child())

    def test_broadcast_highlights_only_included_sessions(self) -> None:
        a = self._open("a")
        b = self._open("b")
        self.notebook._on_broadcast_include_toggled(b, False)
        self.notebook.set_broadcast_mode(True)
        self.assertTrue(a._pane_holder.get_style_context().has_class("broadcast-active"))
        self.assertFalse(b._pane_holder.get_style_context().has_class("broadcast-active"))

    def test_navigation_and_close_helpers(self) -> None:
        a, b, c = self._open("a"), self._open("b"), self._open("c")
        self.notebook.goto_tab(1)
        self.assertIs(self.notebook.current_tab(), a)
        self.notebook.next_tab()
        self.assertIs(self.notebook.current_tab(), b)
        self.notebook.prev_tab()
        self.notebook.prev_tab()
        self.assertIs(self.notebook.current_tab(), c)
        self.notebook.close_to_right(a)
        self.assertEqual(self.notebook.tabs(), [a])
        d = self._open("d")
        self.notebook.close_others(d)
        self.assertEqual(self.notebook.tabs(), [d])

    def test_custom_title_and_find_tab(self) -> None:
        tab = self._open("web")
        tab.title = "prod web"
        self.assertEqual(tab.title, "prod web")
        tab.title = "web"
        self.assertEqual(tab.title, "web")
        self.assertIs(self.notebook.find_tab(HostEntry(alias="web", parent_group="p", child_group="c")), tab)
        self.assertIsNone(self.notebook.find_tab(HostEntry(alias="web", parent_group="x", child_group="c")))

    def test_toolbar_follows_active_session(self) -> None:
        tab = self._open("a")
        self.assertFalse(self.notebook._toolbar.tile.get_sensitive())
        self._open("b")
        self.assertTrue(self.notebook._toolbar.broadcast.get_sensitive())
        self.notebook.focus_tab(tab)
        tab._set_strip_ansi(True)
        self.notebook._sync_toolbar()
        self.assertTrue(self.notebook._toolbar.plain_copy.get_active())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
