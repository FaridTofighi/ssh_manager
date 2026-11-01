"""The remote screen inside the tab (X11): embedding, resizing, fallback, toolbar, reconnect, gtk-frdp adapter.

The fake client (tests/fake_freerdp.py, ``embed`` mode) does what xfreerdp does with ``/parent-window``: it
creates an X window, reparents it into the tab's socket and reports the sizes it is given. Skipped without an
X11 display (GTK on Wayland cannot embed X windows).
"""

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from ophion.auth_flow import PasswordResolution
from ophion.config import HostEntry
from ophion.rdp import engines
from ophion.rdp.engines import ClientBinary, EngineReport, Platform, build_engines, parse_help_options
from ophion.trust import TrustStore, pin_key
from tests import fake_freerdp
from tests.test_rdp_session import CONNECTED, FakePasswordDialog, FakeSaveDialog, probe_result

FIXTURES = Path(__file__).parent / "fixtures" / "freerdp"
A = ":".join(["AA"] * 32)


def _x11() -> bool:
    if not os.environ.get("DISPLAY"):
        return False
    try:
        import gi

        gi.require_version("Gtk", "3.0")
        gi.require_version("GdkX11", "3.0")
        from gi.repository import Gdk, GdkX11, Gtk  # type: ignore

        from ophion.rdp.x11 import open_x11

        x11 = open_x11()
        if x11 is None:
            return False
        x11.close()
        return Gtk.init_check(None)[0] and isinstance(Gdk.Display.get_default(), GdkX11.X11Display)
    except Exception:
        return False


def pump_until(predicate, timeout=10.0) -> bool:
    from gi.repository import GLib  # type: ignore

    deadline = time.monotonic() + timeout
    context = GLib.MainContext.default()
    while time.monotonic() < deadline:
        while context.pending():
            context.iteration(False)
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


@unittest.skipUnless(_x11(), "needs an X11 display")
class EmbedTests(unittest.TestCase):
    def setUp(self) -> None:
        from gi.repository import Gtk  # type: ignore

        from ophion.gtkui.rdp.runtime import RdpRuntime
        from ophion.rdp.process import ChildRegistry

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.record = self.dir / "record.jsonl"
        path = fake_freerdp.install(self.dir, "xfreerdp3")
        client = ClientBinary("xfreerdp3", str(path), (3, 15, 0), "3.15.0",
                              parse_help_options((FIXTURES / "help-3.15.txt").read_text()))
        platform = Platform("x11", True, "", "", False, "debian")
        built = build_engines([client], platform)
        self.report = EngineReport(platform, [client], built, engines.choose_engine(built)[0])
        self.assertEqual(self.report.chosen.id, "xfreerdp-embed")
        self.trust = TrustStore(self.dir / "pins.json")
        self.trust.set(pin_key("h-win", "10.0.0.50", 3389, scope="rdp"), A, "CN=win-01")
        self.runtime = RdpRuntime(registry=ChildRegistry(self.dir / "children.json"), trust_store=self.trust,
                                  reap=False, state_path=self.dir / "embed.json")
        self.window = Gtk.Window()
        self.window.set_default_size(900, 600)
        self.addCleanup(self.window.destroy)
        self.logs = []
        FakePasswordDialog.answers, FakePasswordDialog.requests, FakeSaveDialog.offers = [], [], []
        for target, value in (("PasswordRequestDialog", FakePasswordDialog), ("SavePasswordDialog", FakeSaveDialog),
                              ("RETRY_DELAY_S", 0)):
            patcher = mock.patch(f"ophion.gtkui.rdp.tab.{target}", value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def tab(self, **script):
        from ophion.gtkui.rdp.tab import RdpSessionTab

        env = mock.patch.dict(os.environ, fake_freerdp.env_for(self.record, **script))
        env.start()
        self.addCleanup(env.stop)
        host = HostEntry("win-01", {"hostname": "10.0.0.50", "user": "alice"}, protocol="rdp", id="h-win",
                         metadata={"rdp": {"v": 1}})
        tab = RdpSessionTab(host, lambda message, level: self.logs.append(message), runtime=self.runtime,
                            password_provider=lambda: PasswordResolution("pw", "credential"),
                            report_provider=lambda: self.report, probe_fn=lambda *a, **k: probe_result(),
                            autostart=False)
        self.window.add(tab)
        self.window.show_all()
        self.assertTrue(pump_until(lambda: tab.get_realized()))
        tab._begin_connect()
        return tab

    def relayout(self) -> None:
        """Force GTK's layout: the test window's frame clock may never tick (no compositor frame events)."""

        pump_until(lambda: False, 0.3)
        self.window.check_resize()
        pump_until(lambda: False, 0.6)  # past the 250 ms resize debounce

    def args(self, index=0):
        return fake_freerdp.records(self.record)[index]["stdin"].split("\n")

    def test_embeds_resizes_and_screenshots(self) -> None:
        tab = self.tab(lines=[CONNECTED], embed=True, sleep=30)
        self.assertTrue(pump_until(lambda: tab._embedded_child, 15), tab.connection_log.text() + tab.status_label.get_text())
        self.assertEqual(tab.stack.get_visible_child_name(), "display")
        args = self.args()
        self.assertTrue(any(a.startswith("/parent-window:") for a in args))
        self.assertIn("-toggle-fullscreen", args)
        self.assertIn("/dynamic-resolution", args)
        size = next(a for a in args if a.startswith("/size:"))
        self.assertNotEqual(size, "/size:1920x1080")  # the tab's size, not the host's default
        self.relayout()
        first = list(tab.surface.resizes)
        self.assertTrue(first and min(first[0]) > 200, first)  # never a transient tiny size
        from gi.repository import Gtk  # type: ignore

        extra = Gtk.Label(label="taller toolbar")  # the socket's space changes inside the tab
        extra.set_size_request(-1, 60)
        tab.display_box.pack_start(extra, False, False, 0)
        extra.show()
        self.relayout()
        self.assertTrue(pump_until(lambda: "configure" in tab.connection_log.text(), 10), tab.connection_log.text())
        self.assertTrue(len(tab.surface.resizes) > len(first), (first, tab.surface.resizes))
        self.assertLess(tab.surface.resizes[-1][1], first[-1][1])
        width, height = tab.surface.resizes[-1]
        self.assertEqual((width % 2, height % 2), (0, 0))
        target = self.dir / "shot.png"
        self.assertEqual(tab.save_screenshot(str(target)), str(target))
        self.assertGreater(target.stat().st_size, 100)
        self.assertFalse(tab.focus_button.get_visible())  # nothing to focus: it is in the tab
        tab.disconnect()
        self.assertTrue(pump_until(lambda: tab.process is None, 10))
        self.assertIsNone(tab.surface)

    def test_no_window_in_time_falls_back_to_external_and_is_remembered(self) -> None:
        with mock.patch("ophion.gtkui.rdp.display.EMBED_TIMEOUT_S", 1.0), \
                mock.patch("ophion.gtkui.rdp.display.SocketSurface.start_watch",
                           lambda surface, timeout_s=1.0, original=__import__(
                               "ophion.gtkui.rdp.display", fromlist=["x"]).SocketSurface.start_watch:
                           original(surface, 1.0)):
            tab = self.tab(lines=[CONNECTED], embed=True, no_reparent=True, sleep=30)
            self.assertTrue(pump_until(lambda: len(fake_freerdp.records(self.record)) == 2, 20),
                            tab.connection_log.text())
        self.assertTrue(self.runtime.embed_failed("xfreerdp-embed", "x11"))
        self.assertFalse(any(a.startswith("/parent-window:") for a in self.args(1)))
        self.assertIn("own window", " ".join(self.logs))
        self.assertTrue(pump_until(lambda: tab.connected_method == "log", 10))
        self.assertEqual(tab.engine.id, "external")
        tab.disconnect()

    def test_toolbar_changes_reconnect_with_new_options(self) -> None:
        tab = self.tab(lines=[CONNECTED], embed=True, sleep=30)
        self.assertTrue(pump_until(lambda: tab._embedded_child, 15), tab.connection_log.text() + tab.status_label.get_text())
        tab.set_resolution((1280, 720))
        self.assertTrue(pump_until(lambda: len(fake_freerdp.records(self.record)) == 2, 15))
        args = self.args(1)
        self.assertIn("/size:1280x720", args)
        self.assertNotIn("/dynamic-resolution", args)
        self.assertTrue(pump_until(lambda: tab._embedded_child, 15), tab.connection_log.text() + tab.status_label.get_text())
        tab.set_clipboard("off")
        self.assertTrue(pump_until(lambda: len(fake_freerdp.records(self.record)) == 3, 15))
        self.assertIn("-clipboard", self.args(2))
        self.assertIn("Changed for this session: clipboard off", tab.connection_log.text())
        tab.disconnect()

    def test_dropped_connection_reconnects_with_backoff(self) -> None:
        with mock.patch("ophion.rdp.reconnect.BASE_S", 0.2), \
                mock.patch("ophion.gtkui.rdp.tab.ReconnectScheduler.next_delay",
                           lambda self: None if self.cancelled or self.attempts >= self.limit
                           else (setattr(self, "attempts", self.attempts + 1) or 0.2)):
            tab = self.tab(lines=[CONNECTED, "ERRCONNECT_CONNECT_TRANSPORT_FAILED [0x0002000D]"], gap=1.5,
                           exit=147)
            self.assertTrue(pump_until(lambda: len(fake_freerdp.records(self.record)) >= 2, 20),
                            tab.connection_log.text())
        self.assertIn("Reconnecting in", tab.connection_log.text())
        tab.cancel_reconnect()
        self.assertFalse(tab._reconnecting)

    def test_idle_timeout_asks_instead_of_reconnecting(self) -> None:
        tab = self.tab(lines=[CONNECTED, "[INFO][com.freerdp.core] - ERRINFO_IDLE_TIMEOUT (0x00000003):idle"],
                       gap=1.0, exit=3)
        self.assertTrue(pump_until(lambda: tab.last_outcome is not None and tab.process is None, 15))
        time.sleep(0.5)
        pump_until(lambda: False, 0.5)
        self.assertEqual(len(fake_freerdp.records(self.record)), 1)
        self.assertIn("Use Reconnect", tab.status_label.get_text())
        self.assertTrue(tab._banner.get_revealed())

    def test_info_and_toolbar_gating(self) -> None:
        tab = self.tab(lines=[CONNECTED], embed=True, sleep=30)
        self.assertTrue(pump_until(lambda: tab._embedded_child, 15), tab.connection_log.text() + tab.status_label.get_text())
        info = dict(tab.info_lines())
        self.assertEqual(info["Engine"], "Embedded (FreeRDP in the tab, X11)")
        self.assertIn("clipboard", info["Devices"])
        self.assertFalse(tab.toolbar.buttons["send_keys"].get_sensitive())
        self.assertIn("Ctrl+Alt+End", tab.toolbar.buttons["send_keys"].get_tooltip_text())
        self.assertFalse(tab.toolbar.buttons["drives"].get_sensitive())
        self.assertIn("Right Ctrl", tab.toolbar.buttons["keyboard"].get_tooltip_text())
        fullscreen = []
        tab.on_fullscreen = fullscreen.append
        tab.toolbar.fullscreen.set_active(True)
        self.assertEqual(fullscreen, [True])
        tab.set_fullscreen(False)
        self.assertFalse(tab.toolbar.fullscreen.get_active())
        tab.disconnect()


@unittest.skipUnless(_x11(), "needs an X11 display")
class FrdpAdapterTests(unittest.TestCase):
    """FrdpSurface against a stand-in with gtk-frdp 0.2's members (the real typelib is not installed here)."""

    def fake_class(self):
        from gi.repository import GObject, Gtk  # type: ignore

        class FakeFrdpDisplay(Gtk.DrawingArea):
            __gtype_name__ = "SshmFakeFrdpDisplay"
            __gsignals__ = {
                "rdp-connected": (GObject.SignalFlags.RUN_LAST, None, ()),
                "rdp-disconnected": (GObject.SignalFlags.RUN_LAST, None, ()),
                "rdp-error": (GObject.SignalFlags.RUN_LAST, None, (str,)),
                "rdp-auth-failure": (GObject.SignalFlags.RUN_LAST, None, (str,)),
                "rdp-needs-certificate-verification": (GObject.SignalFlags.RUN_LAST, None,
                                                       (str, int, str, str, str, str, int)),
            }
            username = GObject.Property(type=str, default="")
            password = GObject.Property(type=str, default="")
            domain = GObject.Property(type=str, default="")
            scaling = GObject.Property(type=bool, default=False)
            opened = []
            verdicts = []

            def open_host(self, host, port):
                FakeFrdpDisplay.opened.append((host, port, self.get_property("username"),
                                               self.get_property("password"), self.get_property("domain")))

            def close(self):
                self.emit("rdp-disconnected")

            def is_open(self):
                return True

            def set_scaling(self, scaling):
                self.set_property("scaling", scaling)

            def certificate_verify_ex_finish(self, value):
                FakeFrdpDisplay.verdicts.append(value)

            def is_keyboard_grabbed(self):
                return True

            def get_pixbuf(self):
                return None

        return FakeFrdpDisplay

    def test_adapter_connects_and_answers_certificates_from_the_app(self) -> None:
        from ophion.gtkui.rdp.display import FrdpSurface
        from ophion.rdp.engines import frdp_missing

        cls = self.fake_class()
        self.assertEqual(frdp_missing(cls), [])
        surface = FrdpSurface(cls)
        self.addCleanup(surface.destroy)
        events = []
        surface.on_connected = lambda: events.append("connected")
        surface.on_certificate_refused = lambda fp: events.append(("refused", fp))
        surface.accepted = [A]
        surface.open("10.0.0.50", 3389, "alice", "secret", "CORP", True)
        self.assertEqual(cls.opened[-1], ("10.0.0.50", 3389, "alice", "secret", "CORP"))
        self.assertEqual(surface.display.get_property("password"), "")  # not kept on the widget
        surface.display.emit("rdp-needs-certificate-verification", "10.0.0.50", 3389, "win", "CN=win", "CN=win",
                             A.lower(), 0)
        surface.display.emit("rdp-needs-certificate-verification", "10.0.0.50", 3389, "win", "CN=x", "CN=x",
                             ":".join(["BB"] * 32), 0)
        self.assertEqual(cls.verdicts, [2, 0])  # session-only accept for the approved one, refuse the other
        self.assertEqual(events[-1][0], "refused")
        surface.display.emit("rdp-connected")
        self.assertIn("connected", events)
        self.assertTrue(surface.keyboard_grabbed())


if __name__ == "__main__":
    unittest.main()
