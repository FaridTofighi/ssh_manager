"""Characterization (§2.4.5): tile mode with 1, 2, 4 and 7 sessions in both densities, against goldens.

A real ``TerminalNotebook`` in a 1280 x 800 offscreen window (spawns patched). Per tile: the frame's
allocation in the notebook, its header buttons' allocations in the frame, and the window a press reaches at
the zoom button's centre and 3 px inside each edge (resolved like GDK picks the window,
``tests/test_gtkui_pane_header_hitarea.window_at``): its rectangle, and whether it is the button's own
input window (the same rectangle as the button). Then the first tile is zoomed and the allocations
recorded again. Where tile mode is not entered (one session) only that is recorded.
"""

import unittest
from unittest import mock

from ophion.config import HostEntry
from tests.gtk_window_fixture import display_available
from tests.refactor import golden

COUNTS = (1, 2, 4, 7)


def box(widget, relative):
    if not widget.get_mapped():
        return None
    x, y = widget.translate_coordinates(relative, 0, 0)
    return [x, y, widget.get_allocated_width(), widget.get_allocated_height()]


@unittest.skipUnless(display_available(), "needs a display")
class TileGeometryTests(unittest.TestCase):
    def settle(self, window) -> None:
        from tests.test_local_terminal import pump

        for _ in range(4):
            pump(0.05)
            window.check_resize()
        pump(0.05)

    def hits(self, button, window) -> list:
        from tests.test_gtkui_pane_header_hitarea import window_at

        x0, y0 = button.translate_coordinates(window, 0, 0)
        w, h = button.get_allocated_width(), button.get_allocated_height()
        points = {"centre": (w // 2, h // 2), "left": (3, h // 2), "right": (w - 4, h // 2),
                  "top": (w // 2, 3), "bottom": (w // 2, h - 4)}
        result = []
        for name, (px, py) in points.items():
            x, y = x0 + px, y0 + py
            target, local_x, local_y = window_at(window.get_window(), x, y)
            rect = [x - local_x, y - local_y, target.get_width(), target.get_height()]
            # A button takes presses through its input-only window, which covers exactly its allocation.
            result.append([name, rect, rect == [x0, y0, w, h]])
        return result

    def layout(self, count: int) -> dict:
        from gi.repository import Gtk

        from ophion.gtk_terminal import TerminalNotebook

        window = Gtk.OffscreenWindow()
        window.set_size_request(1280, 800)
        self.addCleanup(window.destroy)
        notebook = TerminalNotebook(lambda *_a: None)
        window.add(notebook)
        tabs = [notebook.new_terminal(HostEntry(alias=f"host-{n}", parent_group="p", child_group="c",
                                                options={"hostname": f"10.0.0.{n}"}), autostart=False)
                for n in range(count)]
        window.show_all()
        notebook.set_tile_mode(True)
        self.settle(window)

        def tiles():
            frames = []
            for tab in tabs:
                frame = notebook._tile_wrappers[tab]
                header = frame.header
                frames.append({
                    "frame": box(frame, notebook),
                    "chip": box(header.broadcast_chip, frame) if header.broadcast_chip is not None else None,
                    "zoom": box(header.zoom_button, frame),
                    "close": box(getattr(header, "close_button", None), frame) if getattr(header, "close_button", None) else None,
                    "zoom_hits": self.hits(header.zoom_button, window) if header.zoom_button.get_mapped() else None,
                })
            return frames

        result = {"tile_mode": notebook.tile_mode}
        if not notebook.tile_mode:  # e.g. one session: tile mode is not entered
            return result
        result["tiles"] = tiles()
        notebook.toggle_tile_zoom(tabs[0])
        self.settle(window)
        result["zoomed_first"] = tiles()
        return result

    def test_tile_layouts(self) -> None:
        from ophion.gtkui.theme import density
        from ophion.gtkui.theme.loader import ThemeMode, get_theme_manager

        # The app's stylesheet at a fixed font size, as at start-up: without it (a test run on its own) GTK's
        # default button padding gives other sizes than after a test that loaded it.
        theme = get_theme_manager()
        self.addCleanup(theme.set_ui_font_size, theme.font_pt)
        self.addCleanup(theme.apply, theme.mode)
        theme.set_ui_font_size(11)
        theme.apply(ThemeMode.DARK)
        self.addCleanup(density.set_active, density.active())
        with mock.patch("ophion.gtk_terminal.TerminalTab._spawn"):
            for preset in (density.Density.COMFORTABLE, density.Density.COMPACT):
                density.set_active(preset)
                for count in COUNTS:
                    with self.subTest(density=preset.value, sessions=count):
                        golden.check(self, f"tiles_{preset.value}_{count}", self.layout(count))


if __name__ == "__main__":
    unittest.main()
