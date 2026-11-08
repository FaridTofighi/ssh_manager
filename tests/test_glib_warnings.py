"""No GLib warnings from closing sessions: nothing touches signal handlers that are already gone."""

import unittest
import warnings

from ophion.config import HostEntry
from tests.gtk_window_fixture import display_available


def pump(rounds: int = 30) -> None:
    from gi.repository import Gtk

    for _ in range(rounds):
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)


def glib_warnings(record) -> list:
    return [str(w.message) for w in record if "handler" in str(w.message) or "emission" in str(w.message)]


@unittest.skipUnless(display_available(), "needs a display")
class ClosingTests(unittest.TestCase):
    def test_a_closed_notebook_never_syncs_its_toolbar_later(self) -> None:
        from ophion.gtk_terminal import TerminalNotebook

        notebook = TerminalNotebook(lambda _message, _ok: None)
        tab = notebook.new_terminal(HostEntry(alias="a", parent_group="p", child_group="c"), autostart=False)
        tab._set_status("Connected", True)  # queues the toolbar sync for the next idle
        with warnings.catch_warnings(record=True) as record:
            warnings.simplefilter("always")
            notebook.destroy()  # before that idle runs: its toggles lose their handlers
            pump()
        self.assertEqual(glib_warnings(record), [])

    def test_a_tab_whose_terminal_is_gone_closes_quietly(self) -> None:
        from ophion.gtk_terminal import TerminalNotebook

        notebook = TerminalNotebook(lambda _message, _ok: None)
        self.addCleanup(notebook.destroy)
        tab = notebook.new_terminal(HostEntry(alias="a", parent_group="p", child_group="c"), autostart=False)
        self.assertTrue(tab._input_commit_handlers)
        with warnings.catch_warnings(record=True) as record:
            warnings.simplefilter("always")
            tab._terminal.destroy()  # e.g. its window was closed first: GTK dropped the handlers
            tab.destroy()
            pump()
        self.assertEqual(glib_warnings(record), [])
        self.assertEqual(tab._input_commit_handlers, {})

    def test_the_session_page_is_not_shown_while_the_notebook_is_elsewhere(self) -> None:
        from unittest import mock

        from gi.repository import Gtk

        from ophion.gtkui.controllers.host_page import TerminalPage

        notebook = Gtk.Box()
        page = TerminalPage(notebook)
        self.addCleanup(page.destroy)
        page.show_sessions(True)
        self.assertEqual(page.get_visible_child_name(), "sessions")
        page.show_sessions(False)
        page.remove(notebook)  # laid out somewhere else for a while (a test's offscreen window)
        with mock.patch.object(page, "set_visible_child_name") as switch:
            page.show_sessions(True)  # GTK warns for a name the stack does not have
        switch.assert_not_called()
        self.assertEqual(page.get_visible_child_name(), "ready")


    def test_window_tests_never_send_desktop_notifications(self) -> None:
        from tests.gtk_window_fixture import WindowSandbox

        sandbox = WindowSandbox(store=True)
        self.addCleanup(sandbox.close)
        host = next(h for groups in sandbox.window.manager.tree.values() for g in groups.values() for h in g.hosts)
        app = sandbox.window.get_application()
        before = len(app.sent_notifications)
        sandbox.window.sessions.notify_connection_success(host)  # a real one on the desktop
        self.assertEqual(len(app.sent_notifications), before + 1)
        self.assertEqual(app.sent_notifications[-1][1].__class__.__name__, "Notification")


if __name__ == "__main__":
    unittest.main()
