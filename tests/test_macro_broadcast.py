"""Macros and broadcast: a macro types into the sessions typed input reaches, and only those."""

import logging
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.gtk_window_fixture import display_available

PASSWORD = "master-pw-for-tests"
SECRET = "Br0adcast-SENTINEL-secret"


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
class MacroBroadcastTests(unittest.TestCase):
    def setUp(self) -> None:
        from tests.gtk_window_fixture import WindowSandbox

        self.sandbox = WindowSandbox(store=True)
        self.addCleanup(self.sandbox.close)
        self.win = self.sandbox.window
        self.win.realize()
        self.tabs = self.win.terminal_tabs
        self.macros = self.win.macros
        self.logs = self.enterContext(self.assertLogs(level=logging.DEBUG))
        logging.getLogger("ophion").debug("broadcast macro test")
        # Every session runs cat without echo: what reaches its pty appears once on its screen.
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: [p.unlink() for p in folder.iterdir()] and folder.rmdir())
        script = folder / "quiet-cat"
        script.write_text("#!/bin/sh\nstty -echo\nexec cat\n")
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        self.win._settings.local_shell.command = str(script)
        self.win._settings.local_shell.on_exit = "hold"
        self.box = self.sandbox.use_keybox(PASSWORD)
        self.macros.start()
        self.assertTrue(pump(10, until=lambda: self.macros.state == "ready"))
        self.asked = []
        self.answer = (True, False)
        self.macros.ask_run_on_sessions = lambda _parent, name, sessions: (
            self.asked.append((name, list(sessions))) or self.answer)
        self.a, self.b, self.c = self.session(True), self.session(True), self.session(False)

    def tearDown(self) -> None:
        self.tabs.set_broadcast_mode(False)
        for tab in list(self.tabs.tabs()):
            with mock.patch("ophion.gtkui.local_terminal.confirm", return_value=True):
                self.tabs.close_tab(tab)
        pump(0.2)

    # ---------------------------------------------------------------- helpers
    def session(self, included: bool):
        tab = self.tabs.new_local_terminal()
        self.assertTrue(pump(5, until=lambda: tab.is_session_active()))
        self.tabs._on_broadcast_include_toggled(tab, included)  # what the tile's checkbox does
        pump(0.5)  # stty -echo has run
        return tab

    def text(self, tab) -> str:
        from ophion.gtk_terminal import terminal_text

        return terminal_text(tab._terminal)

    def macro(self, name="M", steps=None, **kw):
        from ophion.macros import Macro

        macro = Macro(name=name, steps=steps or [{"type": "text", "text": "MARK-ONE\r"},
                                                 {"type": "key", "key": "Enter"},
                                                 {"type": "text", "text": "MARK-TWO\r"}], **kw)
        self.macros.store.add(macro)
        self.macros.store.save()
        self.macros.sync()
        return macro

    def run_and_wait(self, macro, source):
        self.assertTrue(self.macros.run_macro(macro.id, tab=source, term=source._terminal))
        self.assertTrue(pump(5, until=lambda: not self.macros.playbacks))
        pump(0.4)

    def counts(self, marker="MARK-ONE"):
        return [self.text(t).count(marker) for t in (self.a, self.b, self.c)]

    def toasts(self):
        return [m.text for m in self.win._toasts.history.items()]

    # ------------------------------------------------------------------ tests
    def test_included_sessions_receive_every_step_exactly_once(self) -> None:
        self.tabs.set_broadcast_mode(True)
        macro = self.macro()
        self.run_and_wait(macro, self.a)
        self.assertEqual(self.counts(), [1, 1, 0])  # A and B once each (never mirrored again), C nothing
        self.assertEqual(self.counts("MARK-TWO"), [1, 1, 0])
        self.assertEqual(self.asked, [("M", [self.tabs.label_for(self.a), self.tabs.label_for(self.b)])])
        self.assertIn("Macro “M” finished on 2 sessions.", self.toasts())
        self.run_and_wait(macro, self.b)  # from the other included session: the same pair
        self.assertEqual(self.counts(), [2, 2, 0])
        self.run_and_wait(macro, self.c)  # from the excluded session: only it, no question
        self.assertEqual(self.counts(), [2, 2, 1])
        self.assertEqual(len(self.asked), 2)

    def test_broadcast_off_runs_on_the_current_session_only(self) -> None:
        macro = self.macro()
        self.run_and_wait(macro, self.b)
        self.assertEqual(self.counts(), [0, 1, 0])
        self.assertEqual(self.asked, [])
        self.assertNotIn("finished on", " ".join(self.toasts()))

    def test_unreachable_busy_and_closed_sessions(self) -> None:
        from gi.repository import GLib  # type: ignore

        self.tabs.set_broadcast_mode(True)
        self.b._terminal.feed_child(b"\x04")  # cat ends: B is included but disconnected
        self.assertTrue(pump(5, until=lambda: not self.b.is_session_active()))
        pump(0.3)
        d = self.session(True)
        slow = self.macro("Slow", steps=[{"type": "text", "text": "MARK-ONE\r"}, {"type": "delay", "ms": 1500},
                                         {"type": "text", "text": "MARK-TWO\r"}])
        other = self.macro("Other", steps=[{"type": "delay", "ms": 5000}, {"type": "text", "text": "x"}])
        self.tabs._on_broadcast_include_toggled(self.c, False)
        # A busy source is refused.
        self.assertTrue(self.macros.run_macro(other.id, tab=d, term=d._terminal))
        self.assertFalse(self.macros.run_macro(slow.id, tab=d, term=d._terminal))  # a busy source is refused
        self.assertIn("A macro is already running in this session.", self.toasts())
        self.macros.stop_group(self.macros.playbacks[d].group)
        pump(0.2)
        e = self.session(True)
        self.assertTrue(self.macros.run_macro(other.id, tab=e, term=e._terminal) is True)
        busy_group = self.macros.playbacks[e].group
        self.assertIn(self.a, [p.tab for p in busy_group.playbacks])  # broadcast from E reached A and D
        self.macros._stop(self.macros.playbacks[self.a])  # A is free again, D and E still run
        pump(0.2)
        self.assertTrue(self.macros.run_macro(slow.id, tab=self.a, term=self.a._terminal))
        group = self.macros.playbacks[self.a].group
        self.assertEqual([p.tab for p in group.playbacks], [self.a])  # D and E are busy: skipped
        self.assertEqual(group.results.get(self.b), "disconnected")
        self.assertEqual(group.results.get(d), "busy")
        self.macros.stop_group(busy_group)
        self.assertTrue(pump(5, until=lambda: not self.macros.playbacks))
        warning = next(t for t in self.toasts() if t.startswith("Macro “Slow” finished on"))
        self.assertIn("1 of 4 sessions", warning)
        for tab, reason in ((self.b, "disconnected"), (d, "busy"), (e, "busy")):
            self.assertIn(f"{self.tabs.label_for(tab)} ({reason})", warning)
        # Closed mid-run: only that player stops; the others finish; no timer is left behind.
        f = self.session(True)
        self.assertTrue(self.macros.run_macro(slow.id, tab=self.a, term=self.a._terminal))
        group = self.macros.playbacks[self.a].group
        players = {p.tab: p.player for p in group.playbacks}
        self.assertEqual(set(players), {self.a, d, e, f})
        pump(0.3)
        source = players[f]._source
        with mock.patch("ophion.gtkui.local_terminal.confirm", return_value=True):
            self.tabs.close_tab(f)
        pump(0.2)
        self.assertFalse(players[f].running)
        self.assertIsNone(GLib.main_context_default().find_source_by_id(source))
        self.assertTrue(all(players[t].running for t in (self.a, d, e)))
        self.assertTrue(pump(5, until=lambda: not self.macros.playbacks))
        self.assertEqual(group.results[f], "closed")
        self.assertEqual([group.results[t] for t in (self.a, d, e)], ["done"] * 3)
        pump(0.4)  # cat's output reaches the screen
        self.assertEqual(self.text(d).count("MARK-TWO"), 1)

    def test_confirmation(self) -> None:
        self.tabs.set_broadcast_mode(True)
        macro = self.macro(confirm_before_run=True)
        self.answer = (False, False)
        with mock.patch.object(self.macros, "confirm") as single:
            self.assertFalse(self.macros.run_macro(macro.id, tab=self.a, term=self.a._terminal))
        single.assert_not_called()  # one dialog only, even with "Ask before running it"
        pump(0.5)
        self.assertEqual(self.counts(), [0, 0, 0])  # Cancel: nothing anywhere
        self.answer = (True, True)  # Run + Don't ask again
        plain = self.macro("Plain")
        self.run_and_wait(plain, self.a)
        self.assertFalse(self.win._settings.macros.confirm_multi_session)
        from ophion.settings import AppSettings

        self.assertFalse(AppSettings.load(self.win._settings.config_path).macros.confirm_multi_session)
        asked = len(self.asked)
        self.run_and_wait(plain, self.a)
        self.assertEqual(len(self.asked), asked)  # not asked again
        with mock.patch.object(self.macros, "confirm", return_value=True) as single:  # its own question still
            self.run_and_wait(macro, self.a)
        self.assertEqual(len(self.asked), asked + 1)
        single.assert_not_called()
        self.tabs.set_broadcast_mode(False)
        self.win._settings.macros.confirm_multi_session = True
        self.run_and_wait(plain, self.a)  # one session: never the multi-session question
        self.assertEqual(len(self.asked), asked + 1)

    def test_stop_and_stop_all(self) -> None:
        from gi.repository import Gtk  # type: ignore

        self.tabs.set_broadcast_mode(True)
        slow = self.macro("Slow", steps=[{"type": "delay", "ms": 3000}, {"type": "text", "text": "MARK-ONE\r"}])
        self.assertTrue(self.macros.run_macro(slow.id, tab=self.a, term=self.a._terminal))
        group = self.macros.playbacks[self.a].group
        a_bar, b_bar = self.macros.playbacks[self.a].bar, self.macros.playbacks[self.b].bar
        labels = lambda bar: [b.get_label() for b in bar.get_action_area().get_children()]  # noqa: E731
        self.assertIn("Stop _All", labels(a_bar))
        self.assertNotIn("Stop _All", labels(b_bar))
        b_bar.response(Gtk.ResponseType.CLOSE)  # Stop on B: only B
        self.assertNotIn(self.b, self.macros.playbacks)
        self.assertIn(self.a, self.macros.playbacks)
        a_bar.response(1)  # Stop All
        self.assertFalse(self.macros.playbacks)
        self.assertEqual(group.results, {self.a: "stopped", self.b: "stopped"})
        self.macros.stop_group(group)  # after the end: nothing happens
        self.macros.stop_playback()
        pump(3.5)
        self.assertEqual(self.counts(), [0, 0, 0])

    def test_the_targets_are_frozen_when_the_run_starts(self) -> None:
        self.tabs.set_broadcast_mode(True)
        macro = self.macro("Frozen", steps=[{"type": "text", "text": "MARK-ONE\r"}, {"type": "delay", "ms": 600},
                                            {"type": "text", "text": "MARK-TWO\r"}])
        self.assertTrue(self.macros.run_macro(macro.id, tab=self.a, term=self.a._terminal))
        pump(0.2)
        self.tabs._on_broadcast_include_toggled(self.b, False)
        self.tabs._on_broadcast_include_toggled(self.c, True)
        self.tabs.set_broadcast_mode(False)
        self.assertTrue(pump(5, until=lambda: not self.macros.playbacks))
        pump(0.3)
        self.assertEqual(self.counts("MARK-TWO"), [1, 1, 0])

    def test_recording_banner_mentions_broadcast(self) -> None:
        self.tabs.set_broadcast_mode(True)
        self.tabs._focus_tab(self.a)
        self.assertTrue(self.macros.start_recording(tab=self.a, term=self.a._terminal))
        text = self.macros.recording.bar.get_content_area().get_children()[0].get_text()
        self.assertIn("Broadcast is on: what you type is also sent to 1 other session.", text)
        self.macros._end_recording()

    def test_secret_steps_on_three_sessions_never_leak(self) -> None:
        self.tabs._on_broadcast_include_toggled(self.c, True)
        self.tabs.set_broadcast_mode(True)
        macro = self.macro("Login", steps=[{"type": "text", "text": SECRET, "secret": True},
                                           {"type": "key", "key": "Enter"}])
        self.run_and_wait(macro, self.a)
        self.assertEqual(self.counts(SECRET), [1, 1, 1])  # delivered (cat prints it back)
        self.assertNotIn(SECRET, "\n".join(self.logs.output))
        self.assertNotIn(SECRET, "\n".join(self.toasts()))
        self.assertNotIn(SECRET, repr(self.asked))
        config = Path(os.environ["XDG_CONFIG_HOME"])
        for path in config.rglob("*"):
            if path.is_file():
                self.assertNotIn(SECRET.encode(), path.read_bytes(), path)


if __name__ == "__main__":
    unittest.main()
