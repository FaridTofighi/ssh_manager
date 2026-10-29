"""The RDP toolbar: auto-hide, pin, gating with reasons, menus and the info popover (skipped without a display)."""

import os
import unittest
from unittest import mock


def _display() -> bool:
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return False
    try:
        import gi

        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk  # type: ignore

        return Gtk.init_check(None)[0]
    except Exception:
        return False


@unittest.skipUnless(_display(), "needs a display")
class ToolbarTests(unittest.TestCase):
    def setUp(self) -> None:
        from ssh_manager.gtkui.rdp.toolbar import RdpToolbar

        self.bar = RdpToolbar()
        self.bar.show_all()
        self.addCleanup(self.bar.destroy)

    def test_hide_pin_and_reveal(self) -> None:
        self.assertTrue(self.bar.visible_bar)
        self.bar.conceal()
        self.assertFalse(self.bar.visible_bar)
        self.assertTrue(self.bar.handle.get_visible())
        self.bar.reveal()
        self.assertTrue(self.bar.visible_bar)
        self.bar.pin.set_active(True)
        self.bar.conceal()
        self.assertTrue(self.bar.visible_bar)  # pinned: stays
        self.bar._hide_due()
        self.assertTrue(self.bar.visible_bar)
        self.bar.pin.set_active(False)
        self.bar._hide_due()
        self.assertTrue(self.bar.visible_bar)  # a toolbar button has the keyboard: it stays
        self.bar._bar.set_focus_child(None)
        self.bar._hide_due()
        self.assertFalse(self.bar.visible_bar)

    def test_gating_reasons_and_callbacks(self) -> None:
        self.bar.set_capabilities({"screenshot": "Screenshots are not available with External window."})
        self.assertFalse(self.bar.buttons["screenshot"].get_sensitive())
        self.assertIn("not available", self.bar.buttons["screenshot"].get_tooltip_text())
        self.assertTrue(self.bar.buttons["reconnect"].get_sensitive())
        calls = []
        self.bar.on_reconnect = lambda: calls.append("reconnect")
        self.bar.on_fullscreen = calls.append
        self.bar.buttons["reconnect"].clicked()
        self.bar.fullscreen.set_active(True)
        self.bar.set_fullscreen(False)  # reflects the window: no callback
        self.assertEqual(calls, ["reconnect", True])
        self.assertFalse(self.bar.fullscreen.get_active())

    def test_menus_call_back_with_choices(self) -> None:
        chosen = []
        self.bar.on_resolution = chosen.append
        self.bar.on_clipboard = chosen.append
        self.bar.on_scale = chosen.append
        with mock.patch("ssh_manager.gtkui.rdp.toolbar.popup_menu"), \
                mock.patch("ssh_manager.gtkui.rdp.toolbar.build_menu") as build:
            for which in ("resolution", "clipboard", "scale"):
                self.bar._menu(which)
        for spec in (call.args[0] for call in build.call_args_list):
            for section in spec:
                for item in section.items:
                    item.callback()
        self.assertIn((1920, 1080), chosen)
        self.assertIn(None, chosen)  # fit to the window
        self.assertIn("to_remote", chosen)
        self.assertIn("smart", chosen)

    def test_info_popover_is_plain_text(self) -> None:
        self.bar.info_lines = lambda: [("Server", "<b>evil</b>:3389"), ("Devices", "")]
        self.bar._show_info()
        labels = self.bar.info_popover.get_child().get_children()
        texts = [label.get_text() for label in labels]
        self.assertIn("<b>evil</b>:3389", texts)  # shown literally, never as markup
        self.assertIn("—", texts)

    def test_keyboard_state(self) -> None:
        self.bar.set_keyboard_state(True, "Right Ctrl gives the keyboard back")
        tip = self.bar.buttons["keyboard"].get_tooltip_text()
        self.assertIn("sent to the remote computer", tip)
        self.assertIn("Right Ctrl", tip)
        self.bar.set_keyboard_state(False, "x")
        self.assertIn("not grabbed", self.bar.buttons["keyboard"].get_tooltip_text())


if __name__ == "__main__":
    unittest.main()
