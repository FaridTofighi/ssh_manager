"""The active-tunnels chip and the drop / recovery toasts (rate-limited)."""

import unittest
from unittest import mock

from tests.gtk_window_fixture import display_available
from tests.test_local_terminal import pump
from tests.test_tunnels_ui import TunnelsUiCase


@unittest.skipUnless(display_available(), "needs a display")
class StatusChipTests(TunnelsUiCase):
    def setUp(self) -> None:
        super().setUp()
        self.controller.listeners.append(self.win._update_tunnel_chip)
        self.chip = self.win._header.tunnels_button

    def test_chip_counts_active_tunnels_and_opens_the_manager(self) -> None:
        self.win._update_tunnel_chip()
        self.assertFalse(self.chip.get_visible())
        first, second = self.forward(), self.forward()
        self.controller.save(self.host, [first, second])
        self.controller.start(self.host, first)
        self.controller.start(self.host, second)
        self.assertTrue(pump(10, until=lambda: self.chip.get_label() == "⇄ 2"))
        self.assertTrue(self.chip.get_visible())
        self.assertEqual(self.chip.get_accessible().get_name(), "2 active tunnels")
        with mock.patch.object(self.win.app_dialogs, "open_tunnels") as opened:
            self.chip.clicked()
        opened.assert_called_once()
        self.controller.stop(self.host, first)
        self.assertTrue(pump(5, until=lambda: self.chip.get_label() == "⇄ 1"))
        self.assertEqual(self.chip.get_accessible().get_name(), "1 active tunnel")
        self.controller.stop_all()
        self.assertTrue(pump(5, until=lambda: not self.chip.get_visible()))

    def test_drop_and_recovery_toasts_are_rate_limited(self) -> None:
        toasts = []
        self.win._notify = lambda text, level=None, **kw: toasts.append(text)
        forward = self.forward()
        self.controller.save(self.host, [forward])
        key = self.controller.host_key(self.host)
        for _ in range(3):
            self.controller._on_event(key, "dropped")
        self.controller._on_event(key, "recovered")
        self.controller._on_event(key, "recovered")
        self.assertEqual(toasts, ["The tunnels of web-01 lost their connection; reconnecting…",
                                  "The tunnels of web-01 are connected again."])
        self.controller._last_toast.clear()  # a minute later
        self.controller._on_event(key, "dropped")
        self.assertEqual(len(toasts), 3)

    def test_a_real_drop_shows_reconnecting_then_recovers(self) -> None:
        toasts = []
        self.win._notify = lambda text, level=None, **kw: toasts.append(text)
        self.controller.manager.backoff_min = 0.2
        forward = self.forward()
        self.controller.save(self.host, [forward])
        self.controller.start(self.host, forward)
        self.assertTrue(pump(10, until=lambda: self.controller.status(self.host, forward).state == "active"))
        for transport in list(self.server._transports):
            transport.close()
        self.assertTrue(pump(15, until=lambda: any("are connected again" in t for t in toasts)))
        self.assertTrue(any("reconnecting" in t for t in toasts))
        self.assertEqual(self.controller.status(self.host, forward).state, "active")


if __name__ == "__main__":
    unittest.main()
