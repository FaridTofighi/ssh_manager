"""RDP display logic: keyboard grab, clipboard policy, reconnect plan and backoff, embed watch, gtk-frdp checks."""

import unittest

from ophion.rdp.clipboard import ClipboardPolicy
from ophion.rdp.engines import FRDP_CAPABILITIES, Platform, build_engines, frdp_missing
from ophion.rdp.keyboard import (
    DEFAULT_RELEASE, Effect, GrabState, KeyboardGrab, accelerator_label, parse_accelerator, release_hint,
)
from ophion.rdp.outcome import Kind, RdpOutcome, classify_rdp_exit
from ophion.rdp.reconnect import HARD_CAP, Plan, ReconnectScheduler, plan_for
from ophion.rdp.x11 import EmbedWatch


class KeyboardTests(unittest.TestCase):
    def test_grab_follows_focus_and_releases_modifiers(self) -> None:
        kb = KeyboardGrab()
        self.assertEqual(kb.focus_in(), [Effect.GRAB])
        self.assertTrue(kb.grabbed)
        self.assertEqual(kb.key("Alt_L", frozenset()), [Effect.FORWARD])
        self.assertEqual(kb.key("Tab", frozenset({"Alt"})), [Effect.FORWARD])  # Alt+Tab reaches the remote
        self.assertEqual(kb.focus_out(), [Effect.RELEASE_MODIFIERS, Effect.UNGRAB])  # no stuck Alt
        self.assertEqual(kb.state, GrabState.RELEASED)
        self.assertEqual(kb.pressed, [])

    def test_release_shortcut_always_works(self) -> None:
        kb = KeyboardGrab()
        kb.focus_in()
        kb.key("Control_L", frozenset())
        effects = kb.key("Control_R", frozenset({"Control", "Alt"}))
        self.assertEqual(effects, [Effect.HANDLE_LOCALLY, Effect.RELEASE_MODIFIERS, Effect.UNGRAB])
        self.assertFalse(kb.grabbed)

    def test_tab_switch_and_toggle(self) -> None:
        kb = KeyboardGrab()
        kb.focus_in()
        self.assertEqual(kb.tab_switch(), [Effect.UNGRAB])
        kb.focus_in()
        self.assertEqual(kb.toggle(), [Effect.UNGRAB])  # off
        self.assertEqual(kb.focus_in(), [])  # stays released while off
        self.assertEqual(kb.toggle(), [Effect.GRAB])  # on again, still focused

    def test_reserved_app_shortcuts_only_when_released(self) -> None:
        kb = KeyboardGrab()
        self.assertEqual(kb.key("Tab", frozenset({"Control"})), [Effect.HANDLE_LOCALLY])
        kb.focus_in()
        self.assertEqual(kb.key("Tab", frozenset({"Control"})), [Effect.FORWARD])

    def test_accelerators(self) -> None:
        self.assertEqual(parse_accelerator(DEFAULT_RELEASE), (frozenset({"Control", "Alt"}), "Control_R"))
        self.assertEqual(parse_accelerator("<Primary><Shift>F12"), (frozenset({"Control", "Shift"}), "F12"))
        self.assertEqual(accelerator_label(DEFAULT_RELEASE), "Ctrl+Alt+Right Ctrl")
        self.assertIn("Right Ctrl", release_hint("xfreerdp-embed"))
        self.assertIn("Ctrl+Shift+F12", release_hint("frdp", "<Primary><Shift>F12"))
        self.assertIn("own window", release_hint("external"))


class ClipboardTests(unittest.TestCase):
    def test_directions(self) -> None:
        both, to_remote, off = ClipboardPolicy("both"), ClipboardPolicy("to_remote"), ClipboardPolicy("off")
        self.assertTrue(both.allows("to_remote")[0] and both.allows("from_remote")[0])
        self.assertTrue(to_remote.allows("to_remote")[0])
        self.assertFalse(to_remote.allows("from_remote")[0])
        self.assertFalse(off.allows("to_remote")[0])
        self.assertFalse(off.enabled)

    def test_kinds_and_sizes(self) -> None:
        policy = ClipboardPolicy("both")
        self.assertFalse(policy.allows("to_remote", "files")[0])  # needs engine support
        self.assertTrue(ClipboardPolicy("both", files_supported=True).allows("to_remote", "files", 1024)[0])
        self.assertFalse(policy.allows("to_remote", "text", 10 * 1024 * 1024)[0])
        self.assertFalse(policy.allows("to_remote", "binary")[0])
        self.assertEqual(ClipboardPolicy.describe("text", 12), "clipboard text, 12 bytes")
        self.assertIn("only", ClipboardPolicy("from_remote").indicator())


class ReconnectTests(unittest.TestCase):
    def outcome(self, code, *, connected=True, lines=(), user=False):
        return classify_rdp_exit(code, list(lines), connected=connected, stopped_by_user=user)

    def test_plans(self) -> None:
        dropped = self.outcome(147)
        self.assertEqual(plan_for(dropped), Plan.AUTO)
        self.assertEqual(plan_for(dropped, enabled=False), Plan.ASK)
        self.assertEqual(plan_for(self.outcome(0, user=True)), Plan.NEVER)  # the user's own disconnect
        self.assertEqual(plan_for(self.outcome(12)), Plan.NEVER)  # signed out
        self.assertEqual(plan_for(self.outcome(3)), Plan.ASK)  # idle timeout: ask first
        self.assertEqual(plan_for(self.outcome(5)), Plan.ASK)  # someone else took the session
        self.assertEqual(plan_for(self.outcome(134, connected=False)), Plan.ASK)  # never automatic for sign-in
        self.assertEqual(plan_for(self.outcome(147, connected=False)), Plan.ASK)  # never connected: no loop
        self.assertEqual(plan_for(RdpOutcome(Kind.CERT_REJECTED, "certificate", "", connected=True)), Plan.ASK)

    def test_backoff_is_capped_and_finite(self) -> None:
        scheduler = ReconnectScheduler(5)
        delays = [scheduler.next_delay() for _ in range(6)]
        self.assertEqual(delays, [2.0, 4.0, 8.0, 16.0, 32.0, None])
        self.assertEqual(scheduler.progress(), "attempt 5 of 5")
        unlimited = ReconnectScheduler(0)
        values = [unlimited.next_delay() for _ in range(HARD_CAP + 1)]
        self.assertIsNone(values[-1])  # "unlimited" still ends
        self.assertEqual(max(v for v in values if v), 60.0)

    def test_cancel_and_success_reset(self) -> None:
        scheduler = ReconnectScheduler(3)
        scheduler.next_delay()
        scheduler.cancel()
        self.assertIsNone(scheduler.next_delay())
        scheduler.succeeded()
        self.assertEqual(scheduler.next_delay(), 2.0)


class EmbedWatchTests(unittest.TestCase):
    def test_found_waiting_timeout_gone(self) -> None:
        watch = EmbedWatch(timeout_s=2)
        self.assertEqual(watch.poll([], 0.5), "waiting")
        self.assertEqual(watch.poll([77], 1.0), "found")
        self.assertEqual(watch.child, 77)
        self.assertEqual(watch.poll([77], 5.0), "found")
        self.assertEqual(watch.poll([], 6.0), "gone")
        self.assertEqual(EmbedWatch(timeout_s=1).poll([], 1.5), "timeout")


class FrdpCheckTests(unittest.TestCase):
    def test_member_check_with_a_fake_display_class(self) -> None:
        import gi

        gi.require_version("Gtk", "3.0")
        from gi.repository import GObject, Gtk  # type: ignore

        class FakeDisplay(Gtk.DrawingArea):
            __gtype_name__ = "SshmTestFrdpDisplay"
            __gsignals__ = {name: (GObject.SignalFlags.RUN_LAST, None, ())
                            for name in ("rdp-connected", "rdp-disconnected")}
            username = GObject.Property(type=str, default="")
            password = GObject.Property(type=str, default="")
            domain = GObject.Property(type=str, default="")
            scaling = GObject.Property(type=bool, default=False)

            def open_host(self, host, port):
                pass

            def close(self):
                pass

            def is_open(self):
                return False

        missing = frdp_missing(FakeDisplay)
        self.assertIn("method set_scaling", missing)
        self.assertIn("signal rdp-error", missing)
        self.assertNotIn("property password", missing)

    def test_unusable_gtk_frdp_is_reported(self) -> None:
        platform = Platform("wayland", False, "0.2", "gtk-frdp 0.2 lacks signal rdp-error.", False, "")
        frdp = {e.id: e for e in build_engines([], platform)}["frdp"]
        self.assertFalse(frdp.available)
        self.assertIn("lacks", frdp.reason)
        self.assertTrue(FRDP_CAPABILITIES["screenshot"] and FRDP_CAPABILITIES["embedded"])
        self.assertFalse(FRDP_CAPABILITIES["drives"])


if __name__ == "__main__":
    unittest.main()
