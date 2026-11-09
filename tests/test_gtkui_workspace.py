import unittest
from unittest import mock

from ophion.config import HostEntry
from ophion.gtkui.theme.tokens import ConnectionStatus
from ophion.gtkui.views.statusbar import stat_level


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
        from ophion.gtk_terminal import _status_for_message

        self.assertIs(_status_for_message("Connected", True), ConnectionStatus.CONNECTED)
        self.assertIs(_status_for_message("Connecting…", False), ConnectionStatus.CONNECTING)
        self.assertIs(_status_for_message("Authentication failed", False), ConnectionStatus.FAILED)
        self.assertIs(_status_for_message("Connection failed", False), ConnectionStatus.FAILED)
        self.assertIs(_status_for_message("Disconnected", False), ConnectionStatus.UNKNOWN)


@unittest.skipUnless(HAS_DISPLAY, "GTK display not available")
class NotebookTests(unittest.TestCase):
    def setUp(self) -> None:
        from ophion.gtk_terminal import TerminalNotebook

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
        self.notebook._sync_toolbar()
        self.assertTrue(self.notebook._toolbar.plain_copy.get_sensitive())


class FakeClipboard:
    """Stands in for the clipboard: a test never replaces what the user copied."""

    def __init__(self) -> None:
        self.text = None

    def set_text(self, text, _length) -> None:
        self.text = text


@unittest.skipUnless(HAS_DISPLAY, "GTK display not available")
class PlainTextCopyTests(unittest.TestCase):
    """Copy as plain text: the selection, else everything the session shows (scrollback and screen)."""

    def setUp(self) -> None:
        from ophion.gtk_terminal import TerminalNotebook

        self.logs = []
        self.notebook = TerminalNotebook(lambda message, level: self.logs.append((message, level)))
        self.addCleanup(self.notebook.destroy)
        # Not marked connected: that starts the remote stats, which run ssh in a thread.
        self.tab = self.notebook.new_terminal(HostEntry(alias="web", parent_group="p", child_group="c"),
                                              autostart=False)
        self.clipboard = FakeClipboard()
        patch = mock.patch("ophion.gtk_terminal.Gtk.Clipboard.get", return_value=self.clipboard)
        patch.start()
        self.addCleanup(patch.stop)

    def feed(self, text: str) -> None:
        import time

        from gi.repository import Gtk

        self.tab._terminal.feed(text.encode("utf-8"))
        deadline = time.monotonic() + 0.3  # VTE takes in fed text on a timer, not only on pending events
        while time.monotonic() < deadline:
            while Gtk.events_pending():
                Gtk.main_iteration_do(False)
            time.sleep(0.01)

    def test_the_toolbar_button_copies_the_whole_session_without_colors(self) -> None:
        from gi.repository import Gtk

        self.feed("deploy@web:~$ uptime\r\n \x1b[32m10:42:01\x1b[0m up 3 days\r\ndeploy@web:~$ ")
        button = self.notebook._toolbar.plain_copy
        self.assertNotIsInstance(button, Gtk.ToggleButton)  # an action, not a mode
        button.clicked()
        self.assertEqual(self.clipboard.text, "deploy@web:~$ uptime\n 10:42:01 up 3 days\ndeploy@web:~$")
        self.assertIn(("Copied 3 lines as plain text.", "success"), [(m, getattr(lv, "value", lv)) for m, lv in self.logs])

    def test_a_selection_is_copied_instead(self) -> None:
        self.feed("first line\r\nsecond line\r\n")
        term = self.tab._terminal
        with mock.patch.object(term, "get_has_selection", return_value=True), \
                mock.patch.object(term, "get_text_selected", return_value="second line\n"):
            self.assertEqual(self.tab.copy_as_plain_text(), 1)
        self.assertEqual(self.clipboard.text, "second line")

    def test_an_empty_session_copies_nothing(self) -> None:
        self.assertEqual(self.tab.copy_as_plain_text(), 0)
        self.assertIsNone(self.clipboard.text)  # the clipboard keeps what it had
        self.assertIn("Nothing to copy", self.logs[-1][0])

    def test_the_menu_item_works_without_a_selection_and_the_action_copies_too(self) -> None:
        self.feed("hello\r\n")
        spec = self.tab.terminal_menu_spec(self.tab._terminal)
        item = next(i for section in spec for i in section.items if i.label == "Copy as Plain _Text")
        self.assertTrue(item.sensitive)
        item.callback()
        self.assertEqual(self.clipboard.text, "hello")
        self.clipboard.text = None
        self.notebook.copy_current_as_plain_text()  # what the "toggle-strip-ansi" action (and its shortcut) runs
        self.assertEqual(self.clipboard.text, "hello")

    def test_ctrl_shift_c_still_copies_only_the_selection(self) -> None:
        self.feed("hello\r\n")
        term = self.tab._terminal
        with mock.patch.object(term, "copy_clipboard_format") as copy_selection:
            self.tab.copy_clipboard(term)
        copy_selection.assert_called_once()
        self.assertIsNone(self.clipboard.text)  # never the whole terminal


@unittest.skipUnless(HAS_DISPLAY, "GTK display not available")
class SpawnPathTests(unittest.TestCase):
    """Exercise the real spawn path with VTE's spawn mocked (no ssh process)."""

    def setUp(self) -> None:
        # The host-key pre-flight (tests/test_hostkey_unknown_password_hosts.py) runs in a thread; here the key is known.
        mock.patch("ophion.gtk_terminal.TerminalTab._preflight_then_start",
                   lambda tab, argv, envv: tab._start_child(argv, envv)).start()
        self.addCleanup(mock.patch.stopall)

    def _tab(self):
        from ophion.gtk_terminal import TerminalTab

        tab = TerminalTab(HostEntry(alias="web", options={"hostname": "web.internal"}), lambda *_: None, autostart=False)
        tab._terminal.spawn_async = mock.Mock()
        return tab

    def test_spawn_without_sshpass_runs_plain_ssh(self) -> None:
        tab = self._tab()
        with mock.patch("ophion.gtk_terminal.shutil.which", return_value=None):
            tab._spawn()
        args = tab._terminal.spawn_async.call_args[0]
        argv, envv = args[2], args[3]
        self.assertEqual(argv[0], "ssh")
        self.assertFalse(any(item.startswith("SSHPASS=") for item in envv))
        self.assertIs(tab.status, ConnectionStatus.CONNECTING)
        self.assertFalse(tab._banner.get_revealed())
        tab.destroy()

    def test_spawn_with_override_password_uses_sshpass(self) -> None:
        tab = self._tab()
        with mock.patch("ophion.gtk_terminal.shutil.which", return_value="/usr/bin/sshpass"):
            tab._spawn("s3cret", password_origin="retry")
        argv, envv = tab._terminal.spawn_async.call_args[0][2:4]
        self.assertEqual(argv[:2], ["sshpass", "-e"])
        self.assertIn("SSHPASS=s3cret", envv)
        self.assertNotIn("s3cret", " ".join(argv))  # only in the environment
        self.assertEqual(tab._password_source, "retry")
        # Changed deliberately (Phase 1): a retry password is kept for this attempt only, never staged for saving.
        self.assertEqual(tab._attempt_password, "s3cret")
        self.assertFalse(hasattr(tab, "_password_to_save"))
        tab.destroy()

    def test_override_without_sshpass_does_not_spawn(self) -> None:
        tab = self._tab()
        with mock.patch("ophion.gtk_terminal.shutil.which", return_value=None):
            tab._spawn("s3cret")
        tab._terminal.spawn_async.assert_not_called()
        self.assertEqual(tab._password_source, "manual")
        tab.destroy()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
