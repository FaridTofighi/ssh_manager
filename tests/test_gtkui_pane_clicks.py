"""A press anywhere on a pane activates it — split panes, tiles, duplicated sessions.

The window's content is moved into an offscreen window so every widget is
realized and mapped, then real ``GdkEventButton`` / ``GdkEventScroll`` events go
through ``Gtk.main_do_event`` at points inside each pane's terminal, header,
footer and border (and on the divider / gutter between panes). The point is
hit-tested against the GdkWindow tree exactly like the display server would.
"""

import os
import unittest
from unittest import mock

from tests.gtk_window_fixture import WindowSandbox


def _display_available() -> bool:
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return False
    try:
        import gi

        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk  # type: ignore

        return Gtk.init_check(None)[0]
    except Exception:
        return False


def _pump(rounds: int = 300) -> None:
    from gi.repository import Gtk  # type: ignore

    for _ in range(rounds):
        if not Gtk.events_pending():
            break
        Gtk.main_iteration_do(False)


def _wait(condition, timeout: float = 3.0) -> bool:
    """Run the main loop until ``condition()`` is true (VTE parses output on a timer)."""

    import time

    from gi.repository import Gtk  # type: ignore

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        Gtk.main_iteration_do(False)
        time.sleep(0.005)
    return condition()


class Offscreen:
    """The main window's content, realized and mapped in an offscreen toplevel.

    An offscreen toplevel never becomes the active window (and a synthetic
    focus-in makes X fail with BadWindow), so every press here is a press into a
    window that does not have keyboard focus; keyboard focus is checked as the
    toplevel's focus widget (``get_focus()``).
    """

    def __init__(self, window, width: int = 1280, height: int = 800) -> None:
        from gi.repository import Gtk  # type: ignore

        self.window = window
        self.child = window.get_child()
        window.remove(self.child)
        self.top = Gtk.OffscreenWindow()
        self.top.set_default_size(width, height)
        for css_class in window.get_style_context().list_classes():
            self.top.get_style_context().add_class(css_class)
        self.child.set_size_request(width, height)
        self.top.add(self.child)
        self.child.show_all()
        self.top.show()
        _pump()

    def restore(self) -> None:
        self.top.remove(self.child)
        self.child.set_size_request(-1, -1)
        self.window.add(self.child)
        self.top.destroy()
        _pump()

    @staticmethod
    def _pointer():
        from gi.repository import Gdk  # type: ignore

        return Gdk.Display.get_default().get_default_seat().get_pointer()

    def _window_at(self, x: float, y: float):
        """The GdkWindow that GDK would deliver a press at toplevel point (x, y) to.

        The topmost visible child window under the point that is not pass-through,
        with the point in its coordinates.
        """

        hit = (self.top.get_window(), x, y)

        def walk(gdk_window, lx, ly):
            nonlocal hit
            for child in gdk_window.get_children():  # topmost first
                if not child.is_visible() or child.get_pass_through():  # e.g. the unfocused pane's veil
                    continue
                cx, cy = child.get_position()
                if 0 <= lx - cx < child.get_width() and 0 <= ly - cy < child.get_height():
                    hit = (child, lx - cx, ly - cy)
                    walk(child, lx - cx, ly - cy)
                    return

        walk(self.top.get_window(), x, y)
        return hit

    def point(self, widget, x: float, y: float):
        return widget.translate_coordinates(self.top, int(x), int(y))

    def _event(self, kind, widget, x, y):
        from gi.repository import Gdk, Gtk  # type: ignore

        tx, ty = self.point(widget, x, y)
        gdk_window, lx, ly = self._window_at(tx, ty)
        event = Gdk.Event.new(kind)
        target = event.scroll if kind == Gdk.EventType.SCROLL else event.button
        target.window = gdk_window
        target.x, target.y = lx, ly
        target.x_root, target.y_root = tx, ty
        target.time = Gtk.get_current_event_time()
        event.set_device(self._pointer())
        return event

    def press(self, widget, x: float, y: float, button: int = 1, *, release: bool = True) -> None:
        from gi.repository import Gdk, Gtk  # type: ignore

        event = self._event(Gdk.EventType.BUTTON_PRESS, widget, x, y)
        event.button.button = button
        Gtk.main_do_event(event)
        if release:
            up = event.copy()
            up.type = Gdk.EventType.BUTTON_RELEASE
            Gtk.main_do_event(up)
        _pump()

    def scroll(self, widget, x: float, y: float) -> None:
        from gi.repository import Gdk, Gtk  # type: ignore

        event = self._event(Gdk.EventType.SCROLL, widget, x, y)
        event.scroll.direction = Gdk.ScrollDirection.UP
        Gtk.main_do_event(event)
        _pump()

    def regions(self, frame):
        """(name, widget, x, y) points inside ``frame``: terminal, header, footer, border."""

        alloc = frame.get_allocation()
        regions = []
        term = _terminal_in(frame)
        if term is not None:
            t = term.get_allocation()
            regions.append(("terminal", term, t.width // 2, t.height // 2))
        header = frame.header.title
        regions.append(("header", header, 2, header.get_allocated_height() // 2))
        if frame.footer is not None and frame.footer.get_visible():
            footer = frame.footer
            regions.append(("footer", footer, footer.get_allocated_width() - 4, footer.get_allocated_height() // 2))
        regions.append(("top border", frame, alloc.width // 2, 0))
        regions.append(("bottom border", frame, alloc.width // 2, alloc.height - 1))
        regions.append(("right border", frame, alloc.width - 1, alloc.height // 2))
        return regions


def _terminal_in(widget):
    from gi.repository import Gtk, Vte  # type: ignore

    if isinstance(widget, Vte.Terminal):
        return widget
    if isinstance(widget, Gtk.Container):
        for child in widget.get_children():
            found = _terminal_in(child)
            if found is not None:
                return found
    return None


@unittest.skipUnless(_display_available(), "needs a display")
class PaneClickTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sandbox = WindowSandbox()
        self.win = self.sandbox.window
        self.notebook = self.win.terminal_tabs
        self.spawn = mock.patch("ssh_manager.gtk_terminal.TerminalTab._spawn")
        self.spawn_secondary = mock.patch("ssh_manager.gtk_terminal.TerminalTab._spawn_secondary")
        self.spawn.start()
        self.spawn_secondary.start()
        # A right-click opens the terminal menu (gnome-terminal behaviour); a real Gtk.Menu would keep its
        # pointer grab after the test and swallow the next tests' clicks.
        self.menus = []
        self.popup = mock.patch("ssh_manager.gtk_terminal.popup_menu", side_effect=lambda menu, *a, **k: self.menus.append(
            menu.get_attach_widget()))
        self.popup.start()
        self.screen = None
        self.presses = []  # (terminal, button) presses that reached VTE

    def tearDown(self) -> None:
        if self.screen is not None:
            self.screen.restore()
        self.spawn.stop()
        self.spawn_secondary.stop()
        self.popup.stop()
        self.sandbox.close()

    # ------------------------------------------------------------ helpers
    def open(self, *aliases: str):
        tabs = []
        for alias in aliases:
            host = self.win.tree.find_host_by_alias(alias)
            tab = self.notebook.new_terminal(host, autostart=False)
            tab._set_status("Connected", True)
            tabs.append(tab)
        return tabs

    def show(self) -> Offscreen:
        self.screen = Offscreen(self.win)
        return self.screen

    def spy(self, *terms) -> None:
        for term in terms:
            term.connect("button-press-event", lambda t, e: self.presses.append((t, e.button)) or False)

    def assertActive(self, tab, term, frames, frame) -> None:
        for other in frames:
            self.assertEqual(other.focused, other is frame, "exactly one focused frame")
            self.assertEqual(other.style().has_class("focused"), other is frame)
        self.assertIs(tab.active_terminal, term)
        self.assertIs(self.notebook.current_tab(), tab)
        self.assertIs(self.screen.top.get_focus(), term)  # keyboard input goes to this pane
        self.assertIs(self.win.host_page.selection()[1], tab.host)

    # -------------------------------------------------------------- split
    def test_split_any_region_activates_and_the_press_still_reaches_vte(self) -> None:
        (tab,) = self.open("web-01")
        tab.toggle_split()
        screen = self.show()
        first, second = tab._terminals()
        frames = tab.pane_frames
        self.spy(first, second)
        for _round in range(2):  # A → B → A → B
            for frame, term, other_frame, other_term in ((frames[0], first, frames[1], second),
                                                         (frames[1], second, frames[0], first)):
                for name, widget, x, y in screen.regions(frame):
                    with self.subTest(pane=frames.index(frame), region=name):
                        tab.activate_pane(other_term)  # start from the other pane
                        self.assertTrue(other_frame.focused)
                        del self.presses[:]
                        screen.press(widget, x, y)
                        self.assertActive(tab, term, frames, frame)
                        if name == "terminal":
                            self.assertEqual(self.presses, [(term, 1)], "the press was consumed")

    def test_split_middle_and_right_click_activate_and_reach_vte(self) -> None:
        (tab,) = self.open("web-01")
        tab.toggle_split()
        screen = self.show()
        first, second = tab._terminals()
        frames = tab.pane_frames
        self.spy(first, second)
        for button in (2, 3):
            with self.subTest(button=button):
                tab.activate_pane(first)
                del self.presses[:]
                del self.menus[:]
                screen.press(second, 40, 40, button=button)
                self.assertActive(tab, second, frames, frames[1])
                self.assertEqual(self.presses, [(second, button)])
                self.assertEqual(self.menus, [second] if button == 3 else [])  # the menu of the clicked pane

    def test_split_drag_select_still_selects(self) -> None:
        from gi.repository import Gdk, Gtk, Vte  # type: ignore

        from ssh_manager.gtk_terminal import terminal_text

        (tab,) = self.open("web-01")
        tab.toggle_split()
        screen = self.show()
        first, second = tab._terminals()
        second.feed(b"hello selection world\r\nsecond line\r\n")
        self.assertTrue(_wait(lambda: "hello" in terminal_text(second)), "VTE did not show the text")
        tab.activate_pane(first)
        cell_w, cell_h = second.get_char_width(), second.get_char_height()
        pad = 8
        screen.press(second, pad + 1, pad + cell_h // 2, release=False)
        self.assertIs(tab.active_terminal, second)
        motion = screen._event(Gdk.EventType.MOTION_NOTIFY, second, pad + cell_w * 12, pad + cell_h // 2)
        motion.motion.state = Gdk.ModifierType.BUTTON1_MASK
        Gtk.main_do_event(motion)
        up = screen._event(Gdk.EventType.BUTTON_RELEASE, second, pad + cell_w * 12, pad + cell_h // 2)
        up.button.button = 1
        Gtk.main_do_event(up)
        self.assertTrue(_wait(second.get_has_selection), "activation broke drag selection")
        self.assertIn("llo sel", second.get_text_selected(Vte.Format.TEXT) or "")  # dragged across the first line

    def test_split_wheel_and_divider_do_not_change_the_active_pane(self) -> None:
        (tab,) = self.open("web-01")
        tab.toggle_split()
        screen = self.show()
        first, second = tab._terminals()
        frames = tab.pane_frames
        tab.activate_pane(first)
        screen.scroll(second, 40, 40)
        self.assertActive(tab, first, frames, frames[0])
        # The divider: the paned's handle window, in toplevel coordinates.
        handle = tab._paned.get_handle_window()
        x, y = handle.get_width() // 2, handle.get_height() // 2
        window = handle
        while window is not None and window != screen.top.get_window():
            dx, dy = window.get_position()
            x, y = x + dx, y + dy
            window = window.get_parent()
        self.assertEqual(screen._window_at(x, y)[0], handle, "the press would not land on the divider")
        screen.press(screen.top, x, y)
        self.assertActive(tab, first, frames, frames[0])

    def test_clicking_the_active_pane_changes_nothing(self) -> None:
        (tab,) = self.open("web-01")
        tab.toggle_split()
        screen = self.show()
        first, second = tab._terminals()
        screen.press(second, 30, 30)
        results = []
        activate = tab._panes.activate
        transitions = []
        for frame in tab.pane_frames:
            original = frame.set_focused

            def set_focused(focused, *, dim_others=True, frame=frame, original=original):
                if focused != frame.focused:
                    transitions.append((frame, focused))
                original(focused, dim_others=dim_others)

            frame.set_focused = set_focused
        with mock.patch.object(tab._panes, "activate", side_effect=lambda p: results.append(activate(p)) or results[-1]), \
                mock.patch.object(self.notebook, "_sync_toolbar") as toolbar, \
                mock.patch.object(self.notebook, "_emit_sessions_changed") as sessions:
            screen.press(second, 30, 30)
            screen.press(tab.pane_frames[1].header.title, 2, 4)
            screen.press(second, 30, 30, button=3)
        self.assertTrue(results, "the presses did not reach the activation path")
        self.assertEqual(set(results), {False}, "re-activating the active pane changed it")
        self.assertEqual(transitions, [], "a frame flipped its focused state")
        toolbar.assert_not_called()
        sessions.assert_not_called()
        self.assertIs(tab.active_terminal, second)

    def test_header_buttons_activate_first_then_act(self) -> None:
        (tab,) = self.open("web-01")
        tab.toggle_split()
        screen = self.show()
        first, second = tab._terminals()
        frames = tab.pane_frames
        tab.activate_pane(first)
        zoom = frames[1].header.zoom_button
        screen.press(zoom, zoom.get_allocated_width() // 2, zoom.get_allocated_height() // 2)
        self.assertIs(tab.active_terminal, second)
        self.assertIs(tab.zoomed_pane, second)
        screen.press(zoom, zoom.get_allocated_width() // 2, zoom.get_allocated_height() // 2)
        self.assertIsNone(tab.zoomed_pane)
        close = frames[1].header.close_button
        tab.activate_pane(first)
        screen.press(close, close.get_allocated_width() // 2, close.get_allocated_height() // 2)
        self.assertEqual(tab.pane_frames, [])
        self.assertIs(tab.active_terminal, first)  # the neighbor is active
        self.assertIs(screen.top.get_focus(), first)

    def test_new_split_activates_the_new_pane(self) -> None:
        (tab,) = self.open("web-01")
        self.show()
        tab.toggle_split()
        _pump()
        first, second = tab._terminals()
        self.assertIs(tab.active_terminal, second)
        self.assertEqual([f.focused for f in tab.pane_frames], [False, True])
        self.assertIs(self.screen.top.get_focus(), second)

    def test_alt_arrow_uses_the_same_path(self) -> None:
        (tab,) = self.open("web-01")
        tab.toggle_split()
        self.show()
        first, second = tab._terminals()
        tab.activate_pane(first)
        self.assertTrue(tab.focus_neighbor("right"))
        self.assertActive(tab, second, tab.pane_frames, tab.pane_frames[1])

    def test_click_in_an_unfocused_window_activates(self) -> None:
        (tab,) = self.open("web-01")
        tab.toggle_split()
        screen = self.show()
        self.assertFalse(screen.top.is_active())
        first, second = tab._terminals()
        tab.activate_pane(first)
        screen.press(tab.pane_frames[1].header.title, 2, 4)
        self.assertActive(tab, second, tab.pane_frames, tab.pane_frames[1])

    # --------------------------------------------------------------- tile
    def _tiles(self, count: int):
        aliases = ["web-01", "web-02", "db-primary", "stage-app"]
        tabs = self.open(*[aliases[i % len(aliases)] for i in range(count)])  # 6 → duplicated sessions
        self.notebook.set_tile_mode(True)
        screen = self.show()
        frames = [self.notebook._tile_wrappers[t] for t in tabs]
        return tabs, frames, screen

    def test_tiles_any_region_activates(self) -> None:
        for count in (2, 4, 6):
            with self.subTest(tiles=count):
                if self.screen is not None:
                    self.screen.restore()
                    self.screen = None
                    for tab in list(self.notebook.tabs()):
                        self.notebook.close_tab(tab)
                    self.notebook.set_tile_mode(False)
                    _pump()
                tabs, frames, screen = self._tiles(count)
                self.spy(*[t.active_terminal for t in tabs])
                for index, (tab, frame) in enumerate(zip(tabs, frames)):
                    for name, widget, x, y in screen.regions(frame):
                        with self.subTest(tiles=count, tile=index, region=name):
                            other = tabs[(index + 1) % count]
                            self.notebook.activate_pane(other)
                            self.assertIs(self.notebook.current_tab(), other)
                            del self.presses[:]
                            screen.press(widget, x, y)
                            self.assertActive(tab, tab.active_terminal, frames, frame)
                            if name == "terminal":
                                self.assertEqual(self.presses, [(tab.active_terminal, 1)])

    def test_tile_right_click_and_gutter(self) -> None:
        tabs, frames, screen = self._tiles(2)
        self.spy(*[t.active_terminal for t in tabs])
        self.notebook.activate_pane(tabs[0])
        screen.press(tabs[1].active_terminal, 30, 30, button=3)
        self.assertActive(tabs[1], tabs[1].active_terminal, frames, frames[1])
        self.assertEqual(self.presses, [(tabs[1].active_terminal, 3)])
        # The gutter between the two tiles belongs to neither.
        left = frames[0].get_allocation()
        right = frames[1].get_allocation()
        self.assertGreater(right.x, left.x + left.width)
        grid = self.notebook._tile_grid
        gx, gy = frames[0].translate_coordinates(grid, left.width, left.height // 2)
        gap = (right.x - (left.x + left.width)) // 2
        screen.press(grid, gx + gap, gy)
        self.assertActive(tabs[1], tabs[1].active_terminal, frames, frames[1])
        screen.scroll(tabs[0].active_terminal, 30, 30)
        self.assertActive(tabs[1], tabs[1].active_terminal, frames, frames[1])

    def test_tile_click_sets_the_broadcast_source(self) -> None:
        tabs, frames, screen = self._tiles(3)
        self.notebook.set_broadcast_mode(True)
        screen.press(frames[2].header.title, 2, 4)
        self.assertIs(self.notebook.current_tab(), tabs[2])
        self.assertIs(screen.top.get_focus(), tabs[2].active_terminal)  # typed text lands here first
        fed = {tab: [] for tab in tabs}
        for tab in tabs:
            tab.feed_input = lambda data, t=tab: fed[t].append(data)
        self.notebook._on_tab_input(tabs[2], "uptime\r")
        self.assertEqual(fed, {tabs[0]: ["uptime\r"], tabs[1]: ["uptime\r"], tabs[2]: []})

    def test_split_pane_inside_a_tile(self) -> None:
        tabs = self.open("web-01", "web-02")
        tabs[1].toggle_split()
        self.notebook.set_tile_mode(True)
        screen = self.show()
        first, second = tabs[1]._terminals()
        self.notebook.activate_pane(tabs[0])
        calls = []
        original = type(tabs[1]).activate_pane

        def record(tab, term=None, **kwargs):
            calls.append(term)
            return original(tab, term, **kwargs)

        with mock.patch.object(type(tabs[1]), "activate_pane", autospec=True, side_effect=record):
            screen.press(tabs[1].pane_frames[0].header.title, 2, 4)
        self.assertIs(self.notebook.current_tab(), tabs[1])
        self.assertIs(tabs[1].active_terminal, first)
        self.assertTrue(self.notebook._tile_wrappers[tabs[1]].focused)
        self.assertTrue(tabs[1].pane_frames[0].focused)
        self.assertEqual(calls, [first], "the tile and its pane both activated")

    def test_tile_toggle_keeps_the_active_pane(self) -> None:
        tabs = self.open("web-01", "web-02")
        tabs[1].toggle_split()
        first, second = tabs[1]._terminals()
        self.notebook.activate_pane(tabs[1], first)
        self.notebook.set_tile_mode(True)
        self.assertIs(self.notebook.current_tab(), tabs[1])
        self.assertIs(tabs[1].active_terminal, first)
        self.notebook.set_tile_mode(False)
        self.assertIs(self.notebook.current_tab(), tabs[1])
        self.assertIs(tabs[1].active_terminal, first)


if __name__ == "__main__":
    unittest.main()
