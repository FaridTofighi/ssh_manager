"""Broadcast: ``q`` / ``r`` on an ended session act on every ended broadcast session (real local shells)."""

import os
import unittest
from unittest import mock

from tests.gtk_window_fixture import display_available
from tests.test_local_terminal import pump


def key(keyval: int):
    from gi.repository import Gdk  # type: ignore

    event = Gdk.Event.new(Gdk.EventType.KEY_PRESS)
    event.keyval = keyval
    event.state = 0
    return event


@unittest.skipUnless(display_available(), "needs a display")
class EndedBroadcastCase(unittest.TestCase):
    def setUp(self) -> None:
        from tests.gtk_window_fixture import WindowSandbox

        self.sandbox = WindowSandbox(store=True)
        self.addCleanup(self.sandbox.close)
        self.win = self.sandbox.window
        self.win.realize()
        self.tabs = self.win.terminal_tabs
        os.makedirs(os.environ["HOME"], exist_ok=True)
        self.win._settings.local_shell.on_exit = "hold"
        self.win._settings.local_shell.command = "/bin/sh"

    def tearDown(self) -> None:
        for tab in list(self.tabs.tabs()):
            with mock.patch("ophion.gtkui.local_terminal.confirm", return_value=True):
                self.tabs.close_tab(tab)
        pump(0.3)

    def open(self, count: int) -> list:
        opened = []
        for _ in range(count):
            tab = self.tabs.new_local_terminal()
            self.assertTrue(pump(5, until=lambda t=tab: t._shell_pid is not None), "the shell did not start")
            self.tabs._on_broadcast_include_toggled(tab, True)  # local shells start excluded from broadcast
            opened.append(tab)
        return opened

    def end(self, *tabs) -> None:
        for tab in tabs:
            tab._terminal.feed_child(b"exit\n")
        self.assertTrue(pump(5, until=lambda: all(not t.is_session_active() for t in tabs)), "the shells did not end")
        pump(0.2)

    def broadcast(self, on: bool = True) -> None:
        self.tabs.set_broadcast_mode(on)
        self.assertEqual(self.tabs.broadcast_active, on)

    def exclude(self, tab) -> None:
        self.tabs._on_broadcast_include_toggled(tab, False)

    def press(self, tab, name: str) -> bool:
        from gi.repository import Gdk  # type: ignore

        return tab._on_terminal_key_press(tab._terminal, key(getattr(Gdk, f"KEY_{name}")))

    def remaining(self) -> list:
        pump(0.5)
        return list(self.tabs.tabs())


class CloseTests(EndedBroadcastCase):
    def test_1_q_closes_every_ended_broadcast_session_in_tile_mode(self) -> None:
        tabs = self.open(4)
        self.broadcast()
        self.end(*tabs)
        self.assertTrue(self.press(tabs[1], "q"))
        self.assertEqual(self.remaining(), [])
        self.assertTrue(self.tabs.tile_mode or not self.tabs.broadcast_active)
        self.assertEqual(self.tabs._stack.get_visible_child_name(), "placeholder")  # the empty state
        self.assertEqual(self.tabs._tile_wrappers, {})

    def test_2_an_excluded_ended_session_stays(self) -> None:
        tabs = self.open(4)
        self.broadcast()
        self.exclude(tabs[3])
        self.end(*tabs)
        self.press(tabs[0], "q")
        self.assertEqual(self.remaining(), [tabs[3]])

    def test_3_an_included_session_still_running_stays(self) -> None:
        tabs = self.open(4)
        self.broadcast()
        self.end(tabs[0], tabs[1], tabs[2])
        self.press(tabs[0], "q")
        self.assertEqual(self.remaining(), [tabs[3]])
        self.assertTrue(tabs[3].is_session_active())

    def test_4_broadcast_off_closes_only_that_tab(self) -> None:
        tabs = self.open(3)
        self.end(*tabs)
        self.assertEqual(self.tabs.ended_broadcast_peers(tabs[0]), [])
        self.press(tabs[0], "q")
        self.assertEqual(self.remaining(), tabs[1:])

    def test_5_an_excluded_tab_closes_only_itself(self) -> None:
        tabs = self.open(3)
        self.broadcast()
        self.exclude(tabs[0])
        self.end(*tabs)
        self.assertEqual(self.tabs.ended_broadcast_peers(tabs[0]), [])
        self.press(tabs[0], "q")
        self.assertEqual(self.remaining(), tabs[1:])

    def test_banner_close_and_the_tab_menu(self) -> None:
        from ophion.gtk_terminal import _RESPONSE_CLOSE

        tabs = self.open(4)
        self.broadcast()
        self.end(*tabs)
        self.tabs.close_tab(tabs[3])  # the tab menu's Close: that tab only
        self.assertEqual(self.remaining(), tabs[:3])
        tabs[0]._on_banner_response(tabs[0]._banner, _RESPONSE_CLOSE)
        self.assertEqual(self.remaining(), [])

    def test_a_refusing_tab_does_not_stop_the_others(self) -> None:
        tabs = self.open(3)
        self.broadcast()
        self.end(*tabs)
        with mock.patch.object(tabs[1], "confirm_close", return_value=False):
            self.press(tabs[0], "q")
            self.assertEqual(self.remaining(), [tabs[1]])

    def test_a_shell_closing_itself_closes_only_itself(self) -> None:
        tabs = self.open(3)
        self.broadcast()
        self.end(tabs[1], tabs[2])
        self.win._settings.local_shell.on_exit = "close"  # this tab's shell exits and the tab closes itself
        self.end(tabs[0])
        self.assertEqual(self.remaining(), tabs[1:])

    def test_10_q_is_not_typed_into_the_peers(self) -> None:
        from gi.repository import Vte  # type: ignore

        tabs = self.open(3)
        self.broadcast()
        self.end(*tabs)
        with mock.patch.object(Vte.Terminal, "feed_child") as fed:
            self.press(tabs[0], "q")
            pump(0.3)
        fed.assert_not_called()


class ReconnectTests(EndedBroadcastCase):
    def watch(self, tabs) -> list:
        """Record ``reconnect()`` calls (tab index, time) without restarting the shells."""

        import time

        calls = []
        for index, tab in enumerate(tabs):
            patcher = mock.patch.object(tab, "reconnect", side_effect=lambda i=index: calls.append((i, time.monotonic())))
            patcher.start()
            self.addCleanup(patcher.stop)
        return calls

    def test_7_r_reconnects_every_ended_broadcast_session_t_first_then_staggered(self) -> None:
        tabs = self.open(4)
        self.broadcast()
        self.end(*tabs)
        calls = self.watch(tabs)
        self.assertTrue(self.press(tabs[2], "r"))
        self.assertEqual([i for i, _t in calls], [2])  # T at once
        self.assertTrue(pump(3, until=lambda: len(calls) == 4))
        pump(0.3)
        self.assertEqual([i for i, _t in calls], [2, 0, 1, 3])  # then the others in tab order
        gaps = [later - earlier for (_a, earlier), (_b, later) in zip(calls, calls[1:])]
        self.assertTrue(all(gap >= 0.1 for gap in gaps), gaps)  # staggered (150 ms apart)

    def test_8_excluded_running_off_and_focused_excluded_are_not_reconnected(self) -> None:
        tabs = self.open(4)
        self.broadcast()
        self.exclude(tabs[3])
        self.end(tabs[0], tabs[1], tabs[3])  # tabs[2] still runs
        calls = self.watch(tabs)
        self.press(tabs[0], "r")
        pump(1.0)
        self.assertEqual(sorted(i for i, _t in calls), [0, 1])
        calls.clear()
        self.press(tabs[3], "r")  # the focused tab is excluded: only itself
        pump(1.0)
        self.assertEqual([i for i, _t in calls], [3])
        calls.clear()
        self.broadcast(False)
        self.press(tabs[1], "r")
        pump(1.0)
        self.assertEqual([i for i, _t in calls], [1])

    def test_9_banner_toolbar_and_tab_menu(self) -> None:
        from ophion.gtk_terminal import _RESPONSE_RECONNECT

        tabs = self.open(3)
        self.broadcast()
        self.end(*tabs)
        calls = self.watch(tabs)
        tabs[1]._on_banner_response(tabs[1]._banner, _RESPONSE_RECONNECT)
        self.assertTrue(pump(2, until=lambda: len(calls) == 3))
        self.assertEqual([i for i, _t in calls], [1, 0, 2])
        calls.clear()
        self.tabs.focus_tab(tabs[0])
        pump(0.2)
        self.tabs.reconnect_current()  # the toolbar button and its shortcut
        self.assertTrue(pump(2, until=lambda: len(calls) == 3))
        self.assertEqual([i for i, _t in calls], [0, 1, 2])
        calls.clear()
        with mock.patch("ophion.gtk_terminal.tab_menu_spec") as spec, \
                mock.patch("ophion.gtk_terminal.popup_menu"), \
                mock.patch("ophion.gtk_terminal.build_context_menu"):
            self.tabs._show_tab_menu(tabs[2], None)
        spec.call_args.kwargs["reconnect"]()  # the tab menu: that tab only
        pump(1.0)
        self.assertEqual([i for i, _t in calls], [2])

    def test_a_peer_that_fails_does_not_stop_the_others(self) -> None:
        tabs = self.open(3)
        self.broadcast()
        self.end(*tabs)
        calls = self.watch(tabs)
        tabs[1].reconnect.side_effect = RuntimeError("boom")
        with self.assertLogs("ophion", level="WARNING"):
            self.press(tabs[0], "r")
            self.assertTrue(pump(2, until=lambda: len(calls) == 2))
            pump(0.3)
        self.assertEqual([i for i, _t in calls], [0, 2])

    def test_a_closed_peer_is_skipped(self) -> None:
        tabs = self.open(3)
        self.broadcast()
        self.end(*tabs)
        calls = self.watch(tabs)
        self.press(tabs[0], "r")
        self.tabs.close_tab(tabs[2])  # closed before its turn
        pump(1.0)
        self.assertEqual([i for i, _t in calls], [0, 1])

    def test_10_r_is_not_typed_into_the_peers(self) -> None:
        from gi.repository import Vte  # type: ignore

        tabs = self.open(3)
        self.broadcast()
        self.end(*tabs)
        self.watch(tabs)
        with mock.patch.object(Vte.Terminal, "feed_child") as fed:
            self.press(tabs[0], "r")
            pump(0.6)
        fed.assert_not_called()


if __name__ == "__main__":
    unittest.main()
