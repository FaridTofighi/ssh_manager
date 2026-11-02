"""Session tile / pane header geometry: every control fits inside the header, clear of the pane border.

Measured before the change: the "BROADCAST" badge and the "✓ Broadcast" chip were stretched to the whole
header height (y 0, 27 px in a 28 px header with a 1 px bottom border), so their pills touched the pane's
border; the header's right padding (2 px) was half its left padding (4 px).
"""

import unittest

from tests.gtk_window_fixture import display_available
from tests.test_local_terminal import pump

WIDTHS = (260, 340, 450, 680)


@unittest.skipUnless(display_available(), "needs a display")
class HeaderGeometryTests(unittest.TestCase):
    def setUp(self) -> None:
        from ophion.gtkui.theme.loader import get_theme_manager

        self.theme = get_theme_manager()
        self.addCleanup(self.theme.apply, self.theme.mode)

    def frame(self, width: int, state: str):
        from gi.repository import Gtk

        from ophion.gtkui.theme.tokens import ConnectionStatus
        from ophion.gtkui.views.workspace import TileFrame

        top = Gtk.OffscreenWindow()
        top.get_style_context().add_class("sm-window")  # the app's rules for the chip are scoped to it
        self.addCleanup(top.destroy)
        frame = TileFrame("web-01 (2) — a long session title", "connected", included=state != "excluded",
                          on_include_toggled=lambda _on: None, on_close=lambda: None, on_focus=lambda: None,
                          address="deploy@web-01.example.com:2222")
        frame.set_body(Gtk.Label(label="terminal"))
        if state in ("included", "excluded"):
            frame.set_broadcast(state == "included")
            frame.style().add_class("broadcasting")
        if state == "focused":
            frame.set_focused(True)
        if state == "zoomed":
            frame.set_zoomed(True)
        if state in ("failed", "disconnected"):
            frame.set_status(ConnectionStatus.FAILED if state == "failed" else ConnectionStatus.UNKNOWN)
        frame.set_size_request(width, 160)
        top.add(frame)
        top.show_all()
        pump(0.1)
        return frame

    def check(self, frame, label: str) -> None:
        from gi.repository import Gtk

        from ophion.gtkui.theme.tokens import Size, Space

        box = frame.header.box
        header = box.get_allocation()
        ctx = box.get_style_context()
        pad, border = ctx.get_padding(ctx.get_state()), ctx.get_border(ctx.get_state())
        self.assertEqual(header.height, Size.PANE_HEADER, f"{label}: the header grew")
        self.assertEqual(pad.left, pad.right, f"{label}: right padding {pad.right} ≠ left {pad.left}")
        top, bottom = pad.top + border.top, header.height - pad.bottom - border.bottom
        left, right = pad.left + border.left, header.width - pad.right - border.right
        spans = []
        for child in box.get_children():
            if not child.get_visible():
                continue
            a = child.get_allocation()
            x, y = a.x - header.x, a.y - header.y
            name = ",".join(child.get_style_context().list_classes()) or type(child).__name__
            self.assertGreaterEqual(y, top + 1, f"{label}: {name} touches the top (y={y})")
            self.assertLessEqual(y + a.height, bottom - 1, f"{label}: {name} touches the bottom ({y}+{a.height})")
            self.assertGreaterEqual(x, left, f"{label}: {name} starts in the padding (x={x})")
            self.assertLessEqual(x + a.width, right, f"{label}: {name} ends in the padding ({x}+{a.width})")
            if isinstance(child, Gtk.Button):
                self.assertLessEqual(a.height, Size.PANE_HEADER - 2 * Space.XS, f"{label}: {name} too tall")
                self.assertGreaterEqual(a.width, child.get_preferred_width()[0], f"{label}: {name} clipped")
            spans.append((x, x + a.width, name))
        spans.sort()
        for (_x1, end, first), (start, _x2, second) in zip(spans, spans[1:]):
            self.assertLessEqual(end, start, f"{label}: {first} overlaps {second}")

    # Measured (expected failure until the change): badge and chip at y 0, 27 px tall in the 28 px header
    # (they touch the pane border); right padding 2 px, left 4 px.
    @unittest.expectedFailure
    def test_every_control_fits_in_every_state_width_and_theme(self) -> None:
        from ophion.gtkui.theme.loader import ThemeMode

        for theme in (ThemeMode.LIGHT, ThemeMode.DARK):
            self.theme.apply(theme)
            for width in WIDTHS:
                for state in ("off", "included", "excluded", "focused", "zoomed", "failed", "disconnected"):
                    label = f"{theme.value} {width}px {state}"
                    with self.subTest(label):
                        self.check(self.frame(width, state), label)

    # Measured (expected failure until the change): badge and chip at y 0, 27 px tall in the 28 px header
    # (they touch the pane border); right padding 2 px, left 4 px.
    @unittest.expectedFailure
    def test_right_to_left(self) -> None:
        from gi.repository import Gtk

        Gtk.Widget.set_default_direction(Gtk.TextDirection.RTL)
        self.addCleanup(Gtk.Widget.set_default_direction, Gtk.TextDirection.LTR)
        frame = self.frame(340, "included")
        self.check(frame, "rtl")
        header = frame.header
        self.assertLess(header.close_button.get_allocation().x, header.title.get_allocation().x)  # mirrored


if __name__ == "__main__":
    unittest.main()
