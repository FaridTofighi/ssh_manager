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


if __name__ == "__main__":
    unittest.main()
