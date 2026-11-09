"""Characterization (§2.4.3, R7): the ordered signal connections of the window, the session notebook,
a session tab and its VTE terminals.

``GObject.Object.connect`` / ``connect_after`` are wrapped while the real window is built, a session tab
opens and splits. Each connection on one of those objects is recorded as
``[receiver, signal, handler, "after" or ""]``; a handler is named by ``__name__`` (``<lambda>`` for a
lambda), not by its class, so a method moved to a helper keeps its name while a reordered or dropped
connection shows.
"""

import functools
import unittest
from unittest import mock

from tests.gtk_window_fixture import display_available
from tests.refactor import golden


def handler_name(handler) -> str:
    if isinstance(handler, functools.partial):
        return f"partial({handler_name(handler.func)})"
    function = getattr(handler, "__func__", handler)
    return getattr(function, "__name__", type(handler).__name__)


@unittest.skipUnless(display_available(), "needs a display")
class ConnectOrderTests(unittest.TestCase):
    def test_connection_order(self) -> None:
        from gi.repository import GObject, Vte

        from ophion.gtk_app import SSHManagerWindow
        from ophion.gtk_terminal import TerminalNotebook, TerminalTab
        from tests.gtk_window_fixture import WindowSandbox

        kinds = ((SSHManagerWindow, "window"), (TerminalNotebook, "notebook"), (TerminalTab, "tab"),
                 (Vte.Terminal, "vte"))
        recorded = []
        original_connect, original_after = GObject.Object.connect, GObject.Object.connect_after

        def receiver(obj):
            return next((name for cls, name in kinds if isinstance(obj, cls)), None)

        def connect(self, signal, handler, *args):
            kind = receiver(self)
            if kind:
                recorded.append([kind, signal, handler_name(handler), ""])
            return original_connect(self, signal, handler, *args)

        def connect_after(self, signal, handler, *args):
            kind = receiver(self)
            if kind:
                recorded.append([kind, signal, handler_name(handler), "after"])
            return original_after(self, signal, handler, *args)

        with mock.patch.object(GObject.Object, "connect", connect), \
                mock.patch.object(GObject.Object, "connect_after", connect_after), \
                mock.patch("ophion.gtk_terminal.TerminalTab._spawn"), \
                mock.patch("ophion.gtk_terminal.TerminalTab._spawn_secondary"):
            sandbox = WindowSandbox(store=True)
            self.addCleanup(sandbox.close)
            built = len(recorded)
            host = sandbox.window.tree.find_host_by_alias("web-01")
            tab = sandbox.window.terminal_tabs.new_terminal(host, autostart=False)
            opened = len(recorded)
            tab.toggle_split()
        result = {"window_built": recorded[:built], "tab_opened": recorded[built:opened],
                  "tab_split": recorded[opened:]}
        self.assertTrue(result["window_built"] and result["tab_opened"] and result["tab_split"])
        golden.check(self, "connect_order", result)


if __name__ == "__main__":
    unittest.main()
