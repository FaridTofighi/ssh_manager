"""Panes shown together (split pane, tile mode): borders, gutter, focus, broadcast.

The pixel checks render the main window offscreen in a child process
(``python3 -m tests.test_gtkui_panes --child THEME DENSITY``), which prints
the sampled border/gutter colors of every pane as JSON.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from ssh_manager.gtkui.theme.density import COMFORTABLE, COMPACT
from ssh_manager.gtkui.theme.tokens import DARK, LIGHT, contrast_ratio

ROOT = Path(__file__).resolve().parent.parent
PANE_ROLES = ("pane_border", "pane_border_focus", "pane_border_broadcast", "pane_header_bg",
              "pane_header_bg_focus", "pane_gutter", "pane_dim")


def _display_available() -> bool:
    from tests.gtk_window_fixture import display_available

    return display_available()


class PaneTokenTests(unittest.TestCase):
    def test_tokens_in_both_palettes_and_densities(self) -> None:
        for palette in (DARK, LIGHT):
            for role in PANE_ROLES:
                with self.subTest(palette=palette.name, role=role):
                    self.assertTrue(getattr(palette, role))
        for preset in (COMPACT, COMFORTABLE):
            size = preset.size
            self.assertGreaterEqual(size.PANE_GUTTER, 6)
            self.assertGreaterEqual(size.PANE_MIN, 120)
        self.assertEqual((COMPACT.size.PANE_GUTTER, COMPACT.size.PANE_RADIUS, COMPACT.size.PANE_HEADER), (6, 6, 28))
        self.assertEqual((COMFORTABLE.size.PANE_GUTTER, COMFORTABLE.size.PANE_RADIUS, COMFORTABLE.size.PANE_HEADER), (8, 8, 34))

    def test_contrast(self) -> None:
        from ssh_manager.gtkui.theme.terminal_schemes import DARK_COLORS

        for palette in (DARK, LIGHT):
            gutter = palette.pane_gutter
            with self.subTest(palette=palette.name):
                self.assertGreaterEqual(contrast_ratio(palette.pane_border, gutter), 3.0)
                self.assertGreaterEqual(contrast_ratio(palette.pane_border, DARK_COLORS[0]), 1.8)
                self.assertGreaterEqual(contrast_ratio(palette.pane_border_focus, gutter), 3.0)
                self.assertGreaterEqual(contrast_ratio(palette.pane_border_broadcast, gutter), 3.0)
                self.assertEqual(palette.pane_gutter, palette.bg_canvas)

    def test_pane_css_uses_tokens(self) -> None:
        from ssh_manager.gtkui.theme import css_dark, css_light

        for module in (css_dark, css_light):
            for preset in (COMPACT, COMFORTABLE):
                css = module.css(tokens=preset)
                for selector in (".pane-frame", ".pane-frame.focused", ".pane-frame.broadcast", ".pane-header",
                                 ".pane-gutter", "paned.pane-split > separator"):
                    self.assertIn(selector, css)
                block = re.search(r"\.pane-frame \{([^}]*)\}", css).group(1)
                self.assertIn("@sm_pane_border", block)
                self.assertIn(f"border-radius: {preset.size.PANE_RADIUS}px", block)


# ===================================================================== child
def _child(theme: str, density_name: str) -> None:
    os.environ["GSETTINGS_BACKEND"] = "memory"
    sys.path.insert(0, str(ROOT / "tools"))
    import ui_snapshots as snap  # sandboxes HOME/XDG on import

    from gi.repository import Gio, Gtk  # type: ignore

    if not Gtk.init_check(None)[0]:
        print(json.dumps({"skip": "no display"}))
        return
    snap._pin_resolution()
    snap._patch_toplevel_mapping()
    snap._stub_terminal_io()
    from ssh_manager import gtk_app

    app = gtk_app.SSHManagerApplication(snap._SANDBOX / "inventory")
    app.set_flags(Gio.ApplicationFlags.NON_UNIQUE)
    app.register(None)
    base = snap._SANDBOX / "panes"
    snap._write_inventory(base, True, snap.DENSITY_INVENTORY)
    results = {}

    def pixel(pixbuf, x, y):
        return "#%02X%02X%02X" % snap.pixel(pixbuf, int(x), int(y))

    def sample(window, frames, holder, pixbuf, *, horizontal_neighbors=True):
        boxes = []
        for frame in frames:
            x, y = frame.translate_coordinates(holder, 0, 0)
            a = frame.get_allocation()
            boxes.append((x, y, a.width, a.height))
        panes = []
        for frame, (x, y, w, h) in zip(frames, boxes):
            panes.append({
                "focused": frame.focused,
                "broadcast": frame.style().has_class("broadcast"),
                "left": pixel(pixbuf, x, y + h // 2),
                "right": pixel(pixbuf, x + w - 1, y + h // 2),
                "top": pixel(pixbuf, x + w // 2, y),
                "bottom": pixel(pixbuf, x + w // 2, y + h - 1),
                "box": [x, y, w, h],
            })
        gaps = []
        for x1, y1, w1, h1 in boxes:
            right = [b for b in boxes if abs(b[1] - y1) <= 2 and b[0] >= x1 + w1]
            if not right:
                continue
            x2 = min(b[0] for b in right)  # the nearest pane to the right in the same row
            gap = x2 - (x1 + w1)
            colors = sorted({pixel(pixbuf, gx, y1 + h1 // 4) for gx in range(x1 + w1, x2)})
            gaps.append({"width": gap, "colors": colors})
        return {"panes": panes, "gaps": gaps}

    from ssh_manager.gtkui.theme.loader import get_theme_manager

    for kind in ("split", "tile", "broadcast"):
        window = snap._make_window(app, base, theme == "dark", density_name)
        host = window.tree.find_host_by_alias("apzkrfpspst0103.fns")
        notebook = window.terminal_tabs
        tabs = [notebook.new_terminal(host, autostart=False) for _ in range(1 if kind == "split" else 3)]
        for tab in tabs:
            tab._set_status("Connected", True)
        if kind == "split":
            tabs[0].toggle_split()
        else:
            notebook.set_tile_mode(True)
            if kind == "broadcast":
                notebook.set_broadcast_mode(True)
                notebook._tile_wrappers[tabs[2]].include.set_active(False)
        notebook.focus_tab(tabs[0])
        snap._pump()

        def hook(holder, kind=kind, window=window, tabs=tabs, notebook=notebook):
            pixbuf = holder._pixbuf
            frames = tabs[0].pane_frames if kind == "split" else [notebook._tile_wrappers[t] for t in tabs]
            entry = sample(window, frames, holder, pixbuf)
            entry["palette"] = get_theme_manager().palette.name
            if kind == "split":
                paned = tabs[0]._paned
                paned.set_position(5)  # cannot squeeze a pane below its minimum
                snap._pump()
                entry["min_position"] = paned.get_position()
                alloc = paned.get_allocation()
                entry["paned_width"] = alloc.width
                from gi.repository import Gdk  # type: ignore

                event = Gdk.Event.new(Gdk.EventType._2BUTTON_PRESS)
                event.window = paned.get_handle_window()
                entry["reset_handled"] = tabs[0]._on_paned_press(paned, event)
                entry["reset_position"] = paned.get_position()
            results[kind] = entry

        pixbuf, holder = snap.render_window_pixbuf(window, 1280, 800)
        holder._pixbuf = pixbuf
        hook(holder)
        holder.restore()
        window.destroy()
        snap._pump()
    print("RESULT " + json.dumps(results))


def _run_child(theme: str, density_name: str) -> dict:
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    env.pop("GDK_SCALE", None)
    proc = subprocess.run([sys.executable, "-m", "tests.test_gtkui_panes", "--child", theme, density_name],
                          cwd=ROOT, env=env, capture_output=True, text=True, timeout=600)
    match = re.search(r"^RESULT (.*)$", proc.stdout, re.MULTILINE)
    if match is None:
        if '"skip"' in proc.stdout:
            raise unittest.SkipTest("no display in child process")
        raise AssertionError(f"child failed ({proc.returncode}):\n{proc.stdout[-2000:]}\n{proc.stderr[-3000:]}")
    return json.loads(match.group(1))


@unittest.skipUnless(_display_available(), "needs a display")
class PanePixelTests(unittest.TestCase):
    """Each pane is enclosed by its border token; panes are separated by the gutter."""

    def _check(self, theme: str, density_name: str) -> None:
        palette = DARK if theme == "dark" else LIGHT
        preset = COMPACT if density_name == "compact" else COMFORTABLE
        results = _run_child(theme, density_name)
        for kind, entry in results.items():
            self.assertEqual(entry["palette"], theme)
            self.assertGreaterEqual(len(entry["panes"]), 2, kind)
            borders = set()
            for pane in entry["panes"]:
                if pane["broadcast"]:
                    expected = palette.pane_border_broadcast
                elif pane["focused"]:
                    expected = palette.pane_border_focus
                else:
                    expected = palette.pane_border
                for side in ("left", "right", "top", "bottom"):
                    with self.subTest(theme=theme, density=density_name, kind=kind, side=side, pane=pane["box"]):
                        self.assertEqual(pane[side], expected.upper())
                borders.add(pane["left"])
            self.assertGreaterEqual(len(borders), 2, f"{kind}: focused/broadcast pane looks like the others")
            self.assertTrue(entry["gaps"], f"{kind}: no gutter measured")
            for gap in entry["gaps"]:
                with self.subTest(theme=theme, density=density_name, kind=kind, gap=gap):
                    self.assertGreaterEqual(gap["width"], max(6, preset.size.PANE_GUTTER))
                    self.assertEqual(gap["colors"], [palette.pane_gutter.upper()])
        split = results["split"]
        self.assertGreaterEqual(split["min_position"], preset.size.PANE_MIN)
        self.assertTrue(split["reset_handled"])  # double-click on the divider
        self.assertEqual(split["reset_position"], split["paned_width"] // 2)
        focused = [p for p in results["split"]["panes"] if p["focused"]]
        self.assertEqual(len(focused), 1)

    def test_dark_compact(self) -> None:
        self._check("dark", "compact")

    def test_light_comfortable(self) -> None:
        self._check("light", "comfortable")

    def test_dark_comfortable(self) -> None:
        self._check("dark", "comfortable")

    def test_light_compact(self) -> None:
        self._check("light", "compact")


@unittest.skipUnless(_display_available(), "needs a display")
class PaneBehaviourTests(unittest.TestCase):
    def setUp(self) -> None:
        from tests.gtk_window_fixture import WindowSandbox

        self.sandbox = WindowSandbox()
        self.win = self.sandbox.window
        self.win.tree.select_host("prod", "web.conf", "web-01")
        with mock.patch("ssh_manager.gtk_terminal.TerminalTab._spawn"), \
                mock.patch("ssh_manager.gtk_terminal.TerminalTab._spawn_secondary"):
            self.win.sessions.connect_selected()
            self.tab = self.win.terminal_tabs.current_tab()
            self.tab.toggle_split()

    def tearDown(self) -> None:
        self.sandbox.close()

    def test_single_pane_has_no_frame(self) -> None:
        with mock.patch("ssh_manager.gtk_terminal.TerminalTab._spawn"):
            self.win.sessions.connect_selected()
        single = self.win.terminal_tabs.current_tab()
        self.assertEqual(single.pane_frames, [])
        self.assertFalse(single._pane_holder.get_style_context().has_class("split"))

    def test_split_frames_labels_focus_and_zoom(self) -> None:
        tab = self.tab
        frames = tab.pane_frames
        self.assertEqual(len(frames), 2)
        self.assertEqual([f.header.title.get_text() for f in frames], ["web-01 · pane 1", "web-01 · pane 2"])
        self.assertRegex(frames[0].header.address.get_text(), r"\S+@web-01\.internal:22")
        self.assertIsNone(frames[0].header.close_button)  # the first pane is the session itself
        self.assertIsNotNone(frames[1].header.close_button)
        first, second = tab._terminals()
        tab.focus_pane(first)
        self.assertEqual([f.focused for f in frames], [True, False])
        self.assertTrue(tab.focus_neighbor("right"))  # Alt+→
        self.assertEqual([f.focused for f in frames], [False, True])
        self.assertFalse(tab.focus_neighbor("right"))  # nothing further right (single tab, no tiles)
        self.assertTrue(tab.focus_neighbor("left"))
        self.assertTrue(frames[0].focused)
        tab.toggle_pane_zoom(first)
        self.assertFalse(frames[1].get_visible())
        self.assertIs(tab.zoomed_pane, first)
        tab.toggle_pane_zoom(first)
        self.assertTrue(frames[1].get_visible())
        self.assertIsNone(tab.zoomed_pane)

    def test_alt_arrow_key_moves_focus(self) -> None:
        from gi.repository import Gdk

        first, second = self.tab._terminals()
        self.tab.focus_pane(first)
        event = Gdk.Event.new(Gdk.EventType.KEY_PRESS)
        event.keyval = Gdk.KEY_Right
        event.state = Gdk.ModifierType.MOD1_MASK
        self.assertTrue(self.tab._on_terminal_key_press(first, event))
        self.assertIs(self.tab._focused_terminal, second)

    def test_close_pane_gives_space_back_and_keeps_contents(self) -> None:
        tab = self.tab
        first, _second = tab._terminals()
        from gi.repository import Gtk

        first.feed(b"line 1\r\nline 2\r\nline 3\r\n")
        for _ in range(200):  # VTE processes fed data from the main loop
            if not Gtk.events_pending():
                break
            Gtk.main_iteration_do(False)
        # An unrealized VTE does not parse output in tests; the same terminal object
        # with an unchanged cursor shows nothing was reset or recreated.
        cursor = first.get_cursor_position()
        tab.pane_frames[1].header.close_button.clicked()
        self.assertEqual(tab.pane_frames, [])
        self.assertIsNone(tab._paned)
        self.assertIs(tab._pane_container.get_parent(), tab._pane_holder)
        self.assertIs(tab._focused_terminal, first)
        self.assertEqual(first.get_cursor_position(), cursor)
        # Tile mode and back keeps the terminal and its contents.
        notebook = self.win.terminal_tabs
        with mock.patch("ssh_manager.gtk_terminal.TerminalTab._spawn"):
            self.win.sessions.connect_selected()
        notebook.set_tile_mode(True)
        notebook.set_tile_mode(False)
        self.assertIs(tab._terminal, first)
        self.assertEqual(first.get_cursor_position(), cursor)

    def test_tile_frames_focus_broadcast_chip_and_zoom(self) -> None:
        notebook = self.win.terminal_tabs
        with mock.patch("ssh_manager.gtk_terminal.TerminalTab._spawn"):
            self.win.sessions.connect_selected()
        other = notebook.current_tab()
        notebook.set_tile_mode(True)
        frames = notebook._tile_wrappers
        notebook.focus_tab(self.tab)
        self.assertTrue(frames[self.tab].focused)
        self.assertFalse(frames[other].focused)
        self.assertTrue(notebook._focus_neighbor_tile(self.tab, "right"))
        self.assertTrue(frames[other].focused)
        notebook.set_broadcast_mode(True)
        self.assertTrue(frames[other].style().has_class("broadcast"))
        frames[other].include.set_active(False)  # the chip, not a stock check box
        self.assertFalse(frames[other].style().has_class("broadcast"))
        self.assertTrue(frames[other].include.get_style_context().has_class("broadcast-chip"))
        notebook.toggle_tile_zoom(other)
        self.assertFalse(frames[self.tab].get_visible())
        notebook.toggle_tile_zoom(other)
        self.assertTrue(frames[self.tab].get_visible())


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--child":
        _child(sys.argv[2], sys.argv[3])
    else:
        unittest.main()
