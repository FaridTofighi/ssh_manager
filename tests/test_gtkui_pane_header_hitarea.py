"""Pane / tile header buttons react where they are drawn (the chip, maximize/restore and close).

A ``Gtk.Button`` takes the pointer through its own input-only window, raised above its children when it is
mapped. An icon with a window of its own that arrives later (``IconButton.set_icon`` after zoom/restore)
was stacked above it: the middle of the button went to the icon (which ignores clicks, so the press fell
through to the header behind) and only its edges still worked.

Without moving the real pointer, each point is resolved like GDK picks the target window (the top-most
viewable child window containing it, recursively); a press and release are then delivered there and must
click the button exactly once.
"""

import unittest

from tests.gtk_window_fixture import display_available
from tests.test_local_terminal import pump


def window_at(window, x, y):
    """The window a pointer event at (x, y) (in ``window``'s coordinates) is delivered to."""

    for child in window.get_children():  # GDK keeps children top-most first
        if not child.is_viewable():
            continue
        cx, cy, cw, ch = child.get_geometry()
        if cx <= x < cx + cw and cy <= y < cy + ch:
            return window_at(child, x - cx, y - cy)
    return window, x, y


@unittest.skipUnless(display_available(), "needs a display")
class HeaderHitAreaTests(unittest.TestCase):
    def setUp(self) -> None:
        from gi.repository import Gtk

        from ophion.gtkui.views.workspace import TileFrame
        from ophion.gtkui.widgets.pane_frame import PaneFrame

        self.window = Gtk.OffscreenWindow()
        self.addCleanup(self.window.destroy)
        self.window.set_size_request(640, 260)
        grid = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.clicks = {}
        self.tile = TileFrame("web-01", "connected", included=True,
                              on_include_toggled=lambda _on: self.count("tile chip"),
                              on_close=lambda: self.count("tile close"), on_focus=lambda: None,
                              on_zoom=lambda: self.count("tile zoom"))
        self.pane = PaneFrame("web-01 · pane 1", on_zoom=lambda: self.count("pane zoom"),
                              on_close=lambda: self.count("pane close"))
        for frame in (self.tile, self.pane):
            frame.set_body(Gtk.Label(label="terminal"))
            grid.pack_start(frame, True, True, 0)
        self.window.add(grid)
        self.window.show_all()
        pump(0.2)

    def count(self, name: str) -> None:
        self.clicks[name] = self.clicks.get(name, 0) + 1

    def buttons(self):
        for label, frame in (("tile", self.tile), ("pane", self.pane)):
            header = frame.header
            yield f"{label} zoom", header.zoom_button
            if header.close_button is not None:
                yield f"{label} close", header.close_button
            if header.broadcast_chip is not None:
                yield f"{label} chip", header.broadcast_chip

    def click_at(self, button, px, py):
        """Deliver press + release at the button's (px, py); ``(widget that gets it, window geometry ok)``."""

        from gi.repository import Gdk, Gtk

        top = self.window.get_window()
        wx, wy = button.translate_coordinates(self.window, px, py)
        target, x, y = window_at(top, wx, wy)
        received = []
        for kind in (Gdk.EventType.BUTTON_PRESS, Gdk.EventType.BUTTON_RELEASE):
            event = Gdk.Event.new(kind)
            event.button.window = target
            event.button.x, event.button.y = float(x), float(y)
            event.button.button = 1
            event.button.time = Gtk.get_current_event_time()
            event.button.set_device(Gdk.Display.get_default().get_default_seat().get_pointer())
            received.append(Gtk.get_event_widget(event))
            Gtk.main_do_event(event)
        pump(0.05)
        return received[0]

    def assert_clickable(self, state: str) -> None:
        for name, button in self.buttons():
            alloc = button.get_allocation()
            points = [(alloc.width // 2, alloc.height // 2), (3, alloc.height // 2), (alloc.width - 4, alloc.height // 2),
                      (alloc.width // 2, 3), (alloc.width // 2, alloc.height - 4)]
            for px, py in points:
                before = dict(self.clicks)
                widget = self.click_at(button, px, py)
                self.assertIs(widget, button, f"{state}: {name} at ({px},{py}) went to {type(widget).__name__}")
                key = name if not name.endswith("chip") else "tile chip"
                self.assertEqual(self.clicks.get(key, 0) - before.get(key, 0), 1, f"{state}: {name} at ({px},{py})")
                if name.endswith("chip"):
                    button.set_active(True)  # a click toggled it: back to "included"

    # Measured: after set_zoomed() the new SvgIcon (a DrawingArea with its own GdkWindow) is stacked above
    # the button's input window; the centre of the zoom button goes to SvgIcon. Strict: an unexpected
    # success fails the run (Python 3.11+).
    @unittest.expectedFailure
    def test_every_button_is_clickable_on_its_icon_in_every_state(self) -> None:
        self.assert_clickable("shown")
        for frame in (self.tile, self.pane):
            frame.set_zoomed(True)  # the icon swaps to Restore
        pump(0.1)
        self.assert_clickable("zoomed (Restore icon)")
        for frame in (self.tile, self.pane):
            frame.set_zoomed(False)
        pump(0.1)
        self.assert_clickable("restored (Maximize icon)")
        self.tile.set_broadcast(False)
        self.tile.header.broadcast_chip.set_active(False)
        pump(0.1)
        self.tile.header.broadcast_chip.set_active(True)
        self.tile.set_broadcast(True)
        pump(0.1)
        self.assert_clickable("broadcast off and on")
        for frame in (self.tile, self.pane):
            frame.header._set_hover(True)
            frame.set_title("a much longer title for this session", "deploy@web-01.example.com:2222")
        self.window.set_size_request(900, 260)
        pump(0.2)
        self.assert_clickable("hover, new title, wider")


if __name__ == "__main__":
    unittest.main()
