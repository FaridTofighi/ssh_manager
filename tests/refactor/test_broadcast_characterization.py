"""Characterization (§2.4.4): the broadcast-area methods the suite did not run (docs/refactor/terminal-inventory.md).

Broadcast ``q`` / ``r``, Close and the include chip are covered by ``tests/test_broadcast_ended.py`` and
others. What no test ran: typed keys reaching the other sessions (``TerminalTab.feed_input``, from the
terminal's key-press handler through ``TerminalNotebook._on_tab_input``), ``close_current`` (Ctrl+W) and
``_on_paramiko_remote_closed``. ``_on_reconnect_clicked`` and ``_on_close_button_clicked`` are connected
nowhere at the baseline (recorded in the inventory, not tested).
"""

import unittest
from unittest import mock

from ophion.config import HostEntry
from tests.gtk_window_fixture import display_available
from tests.refactor import golden


@unittest.skipUnless(display_available(), "needs a display")
class BroadcastTypingTests(unittest.TestCase):
    def setUp(self) -> None:
        from ophion.gtk_terminal import TerminalNotebook

        patch = mock.patch("ophion.gtk_terminal.TerminalTab._spawn")
        patch.start()
        self.addCleanup(patch.stop)
        self.logs = []
        self.notebook = TerminalNotebook(lambda message, level=None: self.logs.append(message))
        self.addCleanup(self.notebook.destroy)
        self.tabs = []
        for name in ("a", "b", "c"):
            tab = self.notebook.new_terminal(HostEntry(alias=name, parent_group="p", child_group="c"), autostart=False)
            tab._set_status("Connected", True)
            self.tabs.append(tab)

    def typed(self, source, keys: str) -> dict:
        """What each session's terminal is sent when ``keys`` (letters, ``\r`` = Return) are typed into
        ``source``. The source's own terminal gets the keys from VTE itself, not through ``feed_child``."""

        from gi.repository import Gdk

        sent = {}
        patches = [mock.patch.object(tab._terminal, "feed_child",
                                     side_effect=lambda data, name=tab.title: sent.setdefault(name, []).append(
                                         data.decode("utf-8"))) for tab in self.tabs]
        for patch in patches:
            patch.start()
        try:
            for char in keys:
                event = Gdk.Event.new(Gdk.EventType.KEY_PRESS)
                event.keyval = Gdk.KEY_Return if char == "\r" else Gdk.unicode_to_keyval(ord(char))
                event.state = 0
                event.string = char
                source._on_terminal_key_press(source._terminal, event)
        finally:
            for patch in patches:
                patch.stop()
        return sent

    def test_typing_reaches_the_included_connected_sessions(self) -> None:
        a, b, c = self.tabs
        result = {"broadcast_off": self.typed(a, "ls\r")}
        self.notebook.set_broadcast_mode(True)
        result["broadcast_on"] = self.typed(a, "uptime\r")
        self.notebook._on_broadcast_include_toggled(c, False)
        result["c_excluded"] = self.typed(a, "whoami\r")
        result["typed_into_excluded_c"] = self.typed(c, "id\r")
        c._set_status("Disconnected", False)
        self.notebook._on_broadcast_include_toggled(c, True)
        result["c_disconnected"] = self.typed(b, "date\r")
        golden.check(self, "broadcast_typing", result)

    def test_close_current_closes_the_current_tab(self) -> None:
        current = self.notebook.current_tab()
        with mock.patch.object(current, "confirm_close", wraps=current.confirm_close) as asked:
            self.notebook.close_current()
        self.assertNotIn(current, self.notebook.tabs())
        golden.check(self, "broadcast_close_current", {
            "closed": current.title, "left": [tab.title for tab in self.notebook.tabs()],
            "asked_the_tab": asked.call_count})


@unittest.skipUnless(display_available(), "needs a display")
class ParamikoRemoteClosedTests(unittest.TestCase):
    def test_remote_close_ends_the_session(self) -> None:
        from ophion.gtk_terminal import TerminalTab

        logs, closed = [], []
        tab = TerminalTab(HostEntry(alias="web", parent_group="p", child_group="c"),
                          lambda message, level=None: logs.append([message, getattr(level, "value", level)]),
                          autostart=False)
        self.addCleanup(tab.destroy)
        tab.connect("session-closed", lambda _tab, alias, ok: closed.append([alias, ok]))
        with mock.patch.object(tab, "_stop_paramiko_bridge") as stopped:
            tab._on_paramiko_remote_closed()
        stopped.assert_called_once_with()
        golden.check(self, "broadcast_paramiko_remote_closed", {
            "status": tab._statusbar.status_label.get_text(), "logs": logs, "session_closed": closed})


if __name__ == "__main__":
    unittest.main()
