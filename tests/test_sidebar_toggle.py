"""One sidebar toggle, in the top bar, in the same place whether the sidebar is open or closed.

Measured before the change: the sidebar was hidden with a "‹" button at the bottom of the sidebar footer
(``Sidebar.collapse_button``) and shown again with another button in the header brand block
(``sidebar_expand_button``), which existed only while the sidebar was hidden (x=107 in a 1,100 px window)
and was packed after a wordmark that could ellipsize (52 px allocated, 60 px natural with its margin).
"""

import unittest
from unittest import mock

from tests.gtk_window_fixture import display_available
from tests.test_gtkui_pane_header_hitarea import window_at
from tests.test_local_terminal import pump

# Measured: two buttons, the header one hidden while the sidebar is open (expected failure until the fix).
NEW_BEHAVIOUR = unittest.expectedFailure


@unittest.skipUnless(display_available(), "needs a display")
class HeaderToggleGeometryTests(unittest.TestCase):
    """A HeaderView alone in an offscreen window: open = brand as wide as the sidebar, closed = brand shrunk."""

    def header(self, width: int):
        from gi.repository import Gtk

        from ophion.gtkui.controllers.notifications import MessageHistory
        from ophion.gtkui.views.header import HeaderView

        top = Gtk.OffscreenWindow()
        self.addCleanup(top.destroy)
        header = HeaderView(MessageHistory())
        header.set_size_request(width, -1)
        top.add(header)
        top.show_all()
        pump(0.1)
        return header

    @staticmethod
    def box(widget, header):
        x, y = widget.translate_coordinates(header, 0, 0)
        alloc = widget.get_allocation()
        return x, y, alloc.width, alloc.height

    def set_open(self, header, open_: bool) -> None:
        if hasattr(header, "set_sidebar_shown"):
            header.set_sidebar_shown(open_)
        header.set_brand_width(260 if open_ else None)
        pump(0.1)

    @NEW_BEHAVIOUR
    def test_same_place_open_and_closed_at_every_width_and_density(self) -> None:
        from ophion.gtkui.theme import density

        self.addCleanup(density.set_active, density.active())
        for preset in (density.COMFORTABLE, density.COMPACT):
            density.set_active(preset)
            for width in (700, 900, 1366):
                with self.subTest(density=preset.name, width=width):
                    header = self.header(width)
                    header.apply_density()
                    toggle = header.sidebar_toggle_button
                    places = {}
                    for open_ in (True, False, True):
                        self.set_open(header, open_)
                        self.assertTrue(toggle.get_mapped(), f"open={open_}")
                        places.setdefault(open_, self.box(toggle, header))
                        self.assertEqual(self.box(toggle, header), places[open_])
                    self.assertEqual(places[True], places[False])

    @NEW_BEHAVIOUR
    def test_wordmark_toggle_and_divider_never_overlap(self) -> None:
        header = self.header(900)
        wordmark, toggle, divider = header.wordmark, header.sidebar_toggle_button, header.divider
        for open_ in (True, False):
            self.set_open(header, open_)
            wx, _wy, ww, _wh = self.box(wordmark, header)
            tx, _ty, tw, _th = self.box(toggle, header)
            dx, _dy, _dw, _dh = self.box(divider, header)
            self.assertLessEqual(wx + ww, tx, f"open={open_}: the wordmark runs under the toggle")
            self.assertGreaterEqual(ww, wordmark.get_layout().get_pixel_size()[0], "the wordmark is clipped")
            self.assertFalse(wordmark.get_layout().is_ellipsized())
            self.assertLess(tx + tw, dx, f"open={open_}: the divider is not after the toggle")
            if open_:
                bx, _by, bw, _bh = self.box(header.brand, header)
                self.assertEqual(bx + bw, 260 + bx, "the brand is as wide as the sidebar")

    @NEW_BEHAVIOUR
    def test_the_whole_button_clicks_once(self) -> None:
        from gi.repository import Gdk, Gtk

        header = self.header(900)
        toggled = []
        header.on_sidebar_toggle = lambda: toggled.append(1)
        toggle = header.sidebar_toggle_button
        top = header.get_toplevel()
        alloc = toggle.get_allocation()
        points = [(alloc.width // 2, alloc.height // 2), (3, alloc.height // 2), (alloc.width - 4, alloc.height // 2),
                  (alloc.width // 2, 3), (alloc.width // 2, alloc.height - 4)]
        for px, py in points:
            before = len(toggled)
            wx, wy = toggle.translate_coordinates(top, px, py)
            target, x, y = window_at(top.get_window(), wx, wy)
            for kind in (Gdk.EventType.BUTTON_PRESS, Gdk.EventType.BUTTON_RELEASE):
                event = Gdk.Event.new(kind)
                event.button.window = target
                event.button.x, event.button.y = float(x), float(y)
                event.button.button = 1
                event.button.time = Gtk.get_current_event_time()
                event.button.set_device(Gdk.Display.get_default().get_default_seat().get_pointer())
                Gtk.main_do_event(event)
            pump(0.05)
            self.assertEqual(len(toggled) - before, 1, f"at ({px},{py})")


@unittest.skipUnless(display_available(), "needs a display")
class WindowToggleTests(unittest.TestCase):
    def setUp(self) -> None:
        from tests.gtk_window_fixture import WindowSandbox

        self.sandbox = WindowSandbox(store=True)
        self.addCleanup(self.sandbox.close)
        self.win = self.sandbox.window

    def assert_state(self, visible: bool) -> None:
        from ophion.gtkui.constants import Icons

        toggle = self.win._header.sidebar_toggle_button
        text = "Hide sidebar (Ctrl+B)" if visible else "Show sidebar (Ctrl+B)"
        self.assertEqual(self.win.layout.sidebar_visible, visible)
        self.assertEqual(toggle.get_active(), visible)
        self.assertEqual(toggle.icon_name, Icons.SIDEBAR_SHOWN if visible else Icons.SIDEBAR)
        self.assertEqual(toggle.get_tooltip_text(), text)
        self.assertEqual(toggle.get_accessible().get_name(), text)
        self.assertTrue(toggle.get_visible())

    @NEW_BEHAVIOUR
    def test_one_toggle_and_no_footer_button(self) -> None:
        from gi.repository import Gtk

        sidebar = self.win._sidebar
        self.assertFalse(hasattr(sidebar, "collapse_button"))
        self.assertEqual(sidebar._footer.get_children(), [sidebar.settings_link, sidebar.keys_link])
        self.assertIs(self.win._header.sidebar_expand_button, self.win._header.sidebar_toggle_button)
        found = []

        def walk(widget):
            if isinstance(widget, Gtk.Actionable) and widget.get_action_name() == "win.toggle-sidebar":
                found.append(widget)
            if isinstance(widget, Gtk.Container):
                widget.forall(walk)
        walk(self.win)
        self.assertEqual(found, [])  # the toggle activates the action itself (no second bound button)

    @NEW_BEHAVIOUR
    def test_state_follows_every_way_of_changing_it(self) -> None:
        layout = self.win.layout
        self.assert_state(True)
        layout.toggle_sidebar()
        self.assert_state(False)
        layout.toggle_sidebar()
        self.assert_state(True)
        layout.set_sidebar_visible(False, initial=True)
        self.assert_state(False)
        layout.set_sidebar_visible(True, initial=True)
        layout.set_maximized(True)
        layout.set_maximized(False)
        self.assert_state(True)

    @NEW_BEHAVIOUR
    def test_click_and_ctrl_b_toggle_and_persist(self) -> None:
        from gi.repository import Gdk

        saved = []
        with mock.patch.object(self.win, "_save_ui_state", lambda: saved.append(self.win.layout.sidebar_visible)):
            self.win._header.sidebar_toggle_button.clicked()
            self.assert_state(False)
            event = Gdk.Event.new(Gdk.EventType.KEY_PRESS)
            event.key.keyval, event.key.state = Gdk.KEY_b, Gdk.ModifierType.CONTROL_MASK
            event.key.window = self.win.get_window() or Gdk.get_default_root_window()
            self.assertTrue(self.win.activate_key(event.key))  # the window's accelerator, before any focus widget
            self.assert_state(True)
        self.assertEqual(saved, [False, True])

    def test_restored_hidden_state_at_startup(self) -> None:
        from ophion.gtk_app import SSHManagerWindow
        from ophion.settings import AppSettings

        self.win.layout.set_sidebar_visible(False)
        self.win._save_ui_state()
        window = SSHManagerWindow(self.sandbox.app, self.win.manager.base_dir, AppSettings.load(),
                                  manager=self.win.manager)
        self.addCleanup(window.destroy)
        self.assertFalse(window.layout.sidebar_visible)
        header = window._header
        toggle = getattr(header, "sidebar_toggle_button", header.sidebar_expand_button)
        self.assertEqual(toggle.get_tooltip_text(), "Show sidebar (Ctrl+B)")


if __name__ == "__main__":
    unittest.main()
