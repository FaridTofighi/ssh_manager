"""The session strip when the tabs start to overflow: the actions at its end keep their width (no GTK
"Negative content width" warning) and the scroll chevrons appear once the layout is done."""

import unittest

from tests.gtk_window_fixture import display_available
from tests.test_local_terminal import pump


@unittest.skipUnless(display_available(), "needs a display")
class OverflowTests(unittest.TestCase):
    def test_actions_are_never_squeezed_and_chevrons_follow(self) -> None:
        from gi.repository import Gtk

        from ophion.gtkui.views.workspace import SessionStrip, SessionTab

        window = Gtk.OffscreenWindow()
        self.addCleanup(window.destroy)
        window.set_size_request(700, -1)
        strip = SessionStrip(on_new=lambda: None)
        filler = Gtk.Box()
        filler.set_size_request(200, 20)  # stands for the session toolbar
        strip.actions.pack_start(filler, False, False, 0)
        window.add(strip)
        window.show_all()
        widths = []
        strip.actions.connect("size-allocate", lambda _box, alloc: widths.append(alloc.width))
        tabs = []
        for index in range(12):
            tabs.append(SessionTab(f"session-{index:02d}-with-a-long-name", on_close=lambda: None,
                                   on_context_menu=lambda _event: None))
            strip.set_tabs(tabs)
            pump(0.05)
        minimum = strip.actions.get_preferred_width()[0]
        self.assertGreaterEqual(min(widths), minimum, widths)  # was 1 px when the first tab overflowed
        self.assertTrue(strip.scroll_left.get_visible() and strip.scroll_right.get_visible())


if __name__ == "__main__":
    unittest.main()
