"""Interface density: presets, accessibility floor, stylesheet and real layout.

The layout checks render the main window offscreen at 1920 × 1041 in a child
process (``python3 -m tests.test_gtkui_density --child DENSITY``), because the
snapshot helpers sandbox HOME/XDG on import. The child prints the chrome
budget measured from real widget allocations as JSON.
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

from ssh_manager.gtkui.controllers.ui_state import UiState
from ssh_manager.gtkui.theme import css_dark, css_light, density
from ssh_manager.gtkui.theme.density import COMFORTABLE, COMPACT, MIN_FONT_PX, Density
from ssh_manager.gtkui.theme.stylesheet import unresolved_color_refs

ROOT = Path(__file__).resolve().parent.parent
SIZE = (1920, 1041)


def _display_available() -> bool:
    from tests.gtk_window_fixture import display_available

    return display_available()


# ===================================================================== presets
class PresetTests(unittest.TestCase):
    def test_presets_have_identical_keys(self) -> None:
        self.assertEqual(set(COMPACT.flat()), set(COMFORTABLE.flat()))
        self.assertEqual(
            [name for name, _ in COMPACT.groups()], [name for name, _ in COMFORTABLE.groups()]
        )

    def test_no_value_below_the_accessibility_floor(self) -> None:
        for preset in (COMPACT, COMFORTABLE):
            with self.subTest(density=preset.density.value):
                self.assertEqual(density.floor_violations(preset), [])

    def test_compact_is_the_default_and_comfortable_is_the_reference(self) -> None:
        self.assertIs(density.DEFAULT_DENSITY, Density.COMPACT)
        size, sidebar = COMFORTABLE.size, COMFORTABLE.sidebar
        self.assertEqual(
            (size.TOPBAR, size.SIDEBAR, size.PAGE_BAR, size.HOST_HEADER, size.INFO_BAR,
             size.SIDEBAR_FOOTER, size.TREE_ROW, size.SEARCH),
            (76, 408, 73, 123, 69, 66, 41, 46),
        )
        self.assertEqual((sidebar.HEADER_TOP, sidebar.SEARCH_TOP, sidebar.TREE_TOP_GAP), (29, 22, 20))
        self.assertEqual(COMFORTABLE.space.values(), (4, 8, 12, 16, 20, 24, 32, 36))
        self.assertEqual(COMFORTABLE.base_font_scale, 1.0)

    def test_compact_targets(self) -> None:
        size = COMPACT.size
        # Band tokens are content heights: + 1px border = 44 / 44 / 28; the session strip is 33 in total.
        self.assertEqual((size.TOPBAR + 1, size.HOST_HEADER + 1, size.INFO_BAR + 1), (44, 44, 28))
        self.assertEqual(size.SESSION_STRIP, 33)
        self.assertEqual((size.SIDEBAR, size.SIDEBAR_MIN, size.SIDEBAR_MAX), (284, 220, 480))
        self.assertEqual((size.TREE_ROW, size.TREE_GROUP_ROW, size.SEARCH), (30, 30, 32))
        self.assertTrue(COMPACT.variant.MERGED_PAGE_HEADER and COMPACT.variant.SINGLE_LINE_STATUS)
        self.assertFalse(COMPACT.variant.TERMINAL_CARD)
        self.assertEqual(COMPACT.space.values()[:5], (2, 4, 8, 12, 16))

    def test_coerce_and_live_views(self) -> None:
        from ssh_manager.gtkui.theme.tokens import FontScale, Size

        self.assertIs(Density.coerce("Comfortable"), Density.COMFORTABLE)
        self.assertIs(Density.coerce("huge"), Density.COMPACT)
        self.assertIs(Density.coerce(None), Density.COMPACT)
        with density.using("comfortable"):
            self.assertEqual(Size.TOPBAR, 76)
            comfortable_body = FontScale.BODY
        with density.using("compact"):
            self.assertEqual(Size.TOPBAR, 43)
            # Body is 1em of the density's base font in Compact.
            self.assertAlmostEqual(FontScale.BODY, 1.0, places=2)
        self.assertGreater(comfortable_body, 1.0)

    def test_listeners_run_only_on_change(self) -> None:
        calls = []
        density.add_listener(calls.append)
        try:
            with density.using("compact"):
                density.set_active("compact")
                density.set_active("comfortable")
                density.set_active("compact")
        finally:
            density.remove_listener(calls.append)
        self.assertEqual([tokens.density for tokens in calls], [Density.COMFORTABLE, Density.COMPACT])


class DensitySettingTests(unittest.TestCase):
    def test_ui_state_round_trip_and_fallback(self) -> None:
        self.assertEqual(UiState().density, "compact")
        self.assertEqual(UiState.from_dict({}).density, "compact")
        self.assertEqual(UiState.from_dict({"density": "comfortable"}).density, "comfortable")
        for bad in ("huge", 3, None, ["compact"]):
            with self.subTest(value=bad):
                self.assertEqual(UiState.from_dict({"density": bad}).density, "compact")
        state = UiState(density="comfortable")
        self.assertEqual(UiState.from_dict(state.to_dict()).density, "comfortable")

    def test_sidebar_width_follows_the_density_default(self) -> None:
        from ssh_manager.gtkui.controllers.layout import resolve_sidebar_width
        from ssh_manager.gtkui.controllers.ui_state import DEFAULT_SIDEBAR_WIDTH

        with density.using("compact"):
            self.assertEqual(resolve_sidebar_width(DEFAULT_SIDEBAR_WIDTH), 283)
            self.assertEqual(resolve_sidebar_width(9999), 480)
            self.assertEqual(resolve_sidebar_width(100), 220)
            self.assertEqual(resolve_sidebar_width(350), 350)
        with density.using("comfortable"):
            self.assertEqual(resolve_sidebar_width(DEFAULT_SIDEBAR_WIDTH), 407)


# ================================================================== stylesheet
class DensityStylesheetTests(unittest.TestCase):
    FONT_SIZE = re.compile(r"font-size:\s*([0-9.]+)em")

    def test_every_theme_and_density_resolves_its_tokens(self) -> None:
        for module in (css_dark, css_light):
            for preset in (COMPACT, COMFORTABLE):
                with self.subTest(theme=module.__name__, density=preset.density.value):
                    css = module.css(tokens=preset)
                    self.assertEqual(unresolved_color_refs(css), set())
                    self.assertNotIn("$", css)
                    self.assertNotRegex(css, r"font-size:\s*\d+px")

    def test_rendered_type_never_below_11px(self) -> None:
        for preset in (COMPACT, COMFORTABLE):
            for points in (10, 11, 14, 24):
                css = css_dark.css(tokens=preset, font_pt=points)
                base_px = points * 96 / 72 * preset.base_font_scale
                smallest = min(float(value) for value in self.FONT_SIZE.findall(css)) * base_px
                with self.subTest(density=preset.density.value, points=points):
                    self.assertGreaterEqual(smallest, MIN_FONT_PX - 0.05)

    def test_compact_body_is_13px_at_the_default_font(self) -> None:
        self.assertAlmostEqual(11 * 96 / 72 * COMPACT.base_font_scale, 13.0, places=1)

    def test_comfortable_stylesheet_has_no_compact_rules(self) -> None:
        self.assertNotIn("compact density", css_dark.css(tokens=COMFORTABLE))
        self.assertIn("compact density", css_dark.css(tokens=COMPACT))

    @unittest.skipUnless(_display_available(), "needs a display")
    def test_gtk_parses_every_combination(self) -> None:
        from gi.repository import GLib, Gtk  # type: ignore

        for module in (css_dark, css_light):
            for preset in (COMPACT, COMFORTABLE):
                provider = Gtk.CssProvider()
                try:
                    provider.load_from_data(module.css(tokens=preset).encode("utf-8"))
                except GLib.Error as exc:  # pragma: no cover - failure path
                    self.fail(f"{module.__name__}/{preset.density.value}: {exc}")


# ================================================================ runtime switch
@unittest.skipUnless(_display_available(), "needs a display")
class RuntimeSwitchTests(unittest.TestCase):
    def setUp(self) -> None:
        from tests.gtk_window_fixture import WindowSandbox

        self.sandbox = WindowSandbox()
        self.win = self.sandbox.window

    def tearDown(self) -> None:
        density.set_active("compact")
        self.sandbox.close()

    def test_switch_keeps_sessions_and_relayouts(self) -> None:
        from ssh_manager.gtkui.theme.loader import ThemeMode, get_theme_manager
        from ssh_manager.gtkui.theme.tokens import Size

        win = self.win
        self.assertIs(win.density, Density.COMPACT)
        win.tree.select_host("prod", "web.conf", "web-01")
        with mock.patch("ssh_manager.gtk_terminal.TerminalTab._spawn"):
            win.sessions.connect_selected()
        tab = win.terminal_tabs.current_tab()
        terminal = tab._terminal
        terminal.feed(b"line one\r\nline two\r\n")
        cursor = terminal.get_cursor_position()

        win.set_density("comfortable")
        self.assertIs(win.terminal_tabs.current_tab(), tab)
        self.assertIs(tab._terminal, terminal)
        self.assertEqual(terminal.get_cursor_position(), cursor)
        self.assertIn("comfortable density", get_theme_manager().stylesheet())
        self.assertEqual(win._header.get_size_request()[1], Size.TOPBAR)
        self.assertEqual(Size.TOPBAR, 76)
        self.assertTrue(win.host_page.view.page_bar.get_visible())

        win._apply_theme_mode(ThemeMode.LIGHT)  # theme switch with a session running
        win.set_density("compact")
        self.assertIs(tab._terminal, terminal)
        self.assertEqual(terminal.get_cursor_position(), cursor)
        self.assertEqual(win._header.get_size_request()[1], 43)
        self.assertFalse(win.host_page.view.page_bar.get_visible())
        self.assertEqual(win._ui.density, "compact")
        win._save_ui_state()
        self.assertEqual(win._load_ui_state()["density"], "compact")

    def test_font_size_change_keeps_floor(self) -> None:
        from ssh_manager.gtkui.theme.loader import get_theme_manager

        self.win._apply_ui_font(10)
        css = get_theme_manager().stylesheet()
        smallest = min(float(v) for v in re.findall(r"font-size:\s*([0-9.]+)em", css))
        self.assertGreaterEqual(smallest * 10 * 96 / 72 * COMPACT.base_font_scale, MIN_FONT_PX - 0.05)
        self.win._apply_ui_font(11)

    def test_focus_mode_keeps_broadcast_strip_and_esc_leaves(self) -> None:
        from gi.repository import Gdk  # type: ignore

        win = self.win
        win.layout.set_maximized(True)
        self.assertFalse(win._header.get_visible())
        self.assertFalse(win.layout.sidebar_visible)
        event = Gdk.Event.new(Gdk.EventType.KEY_PRESS)
        event.keyval = Gdk.KEY_Escape
        event.state = 0
        self.assertTrue(win._on_window_key_press(win, event))
        self.assertFalse(win.layout.maximized)
        self.assertTrue(win._header.get_visible())


# ================================================================ real layout
def _child(density_name: str) -> None:
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
    base = snap._SANDBOX / "many"
    inventory = {
        f"group-{g}/hosts.conf": "".join(f"Host host-{g}-{h:02d}\n    hostname 10.0.{g}.{h}\n\n" for h in range(14))
        for g in range(3)
    }
    snap._write_inventory(base, True, inventory)
    window = snap._make_window(app, base, True, density_name)
    host = window.tree.find_host_by_alias("host-0-00")
    window.tree.select_host(host.parent_group, host.child_group, host.alias)
    tab = window.terminal_tabs.new_terminal(host, autostart=False)
    tab._set_status("Connected", True)
    snap._pump()
    result = {}

    def hook(holder) -> None:
        result.update(snap.chrome_budget(window, holder))
        tree = window.tree_view
        model = tree.get_model()
        rows = {}
        for kind, path in (("group", "0"), ("child", "0:0"), ("host", "0:0:0")):
            rect = tree.get_background_area(Gtk.TreePath.new_from_string(path), None)
            rows[kind] = rect.height
        result["row_heights"] = rows
        result["hosts"] = sum(1 for _ in range(model.iter_n_children(None)))

    snap.capture_window(window, snap._SANDBOX / "density.png", *SIZE, on_rendered=hook)
    print("RESULT " + json.dumps(result))


def _child_workspace() -> None:
    """Terminal allocation with the host panel closed/open, overflow chevrons with many tabs."""

    os.environ["GSETTINGS_BACKEND"] = "memory"
    sys.path.insert(0, str(ROOT / "tools"))
    import ui_snapshots as snap

    from gi.repository import Gio, Gtk  # type: ignore

    if not Gtk.init_check(None)[0]:
        print(json.dumps({"skip": "no display"}))
        return
    snap._pin_resolution()
    snap._patch_toplevel_mapping()
    snap._stub_terminal_io()
    from ssh_manager import gtk_app
    from ssh_manager.gtkui.views.host_page import PanelPage

    app = gtk_app.SSHManagerApplication(snap._SANDBOX / "inventory")
    app.set_flags(Gio.ApplicationFlags.NON_UNIQUE)
    app.register(None)
    base = snap._SANDBOX / "ws"
    snap._write_inventory(base, True)
    window = snap._make_window(app, base, True, "compact")
    host = snap._first_host(window)
    window.tree.select_host(host.parent_group, host.child_group, host.alias)
    for _ in range(16):
        window.terminal_tabs.new_terminal(host, autostart=False)
    snap._pump()
    result = {}

    def measure(key):
        def hook(holder):
            tab = window.terminal_tabs.current_tab()
            alloc = tab._terminal.get_allocation()
            strip = window.terminal_tabs._strip
            result[key] = {
                "terminal": [alloc.width, alloc.height],
                "chevrons": strip.scroll_left.get_visible() and strip.scroll_right.get_visible(),
                "panel": window.host_page.view.panel.is_open,
            }
        return hook

    snap.capture_window(window, snap._SANDBOX / "a.png", 1280, 800, on_rendered=measure("closed"))
    window.host_page.view.panel.set_transition_duration(0)
    window.host_page.view.open_panel(PanelPage.DETAILS)
    snap._pump()
    snap.capture_window(window, snap._SANDBOX / "b.png", 1280, 800, on_rendered=measure("open"))
    print("RESULT " + json.dumps(result))


def _run_child(density_name: str) -> dict:
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    env.pop("GDK_SCALE", None)
    proc = subprocess.run(
        [sys.executable, "-m", "tests.test_gtkui_density", "--child", density_name],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=600,
    )
    match = re.search(r"^RESULT (.*)$", proc.stdout, re.MULTILINE)
    if match is None:
        if '"skip"' in proc.stdout:
            raise unittest.SkipTest("no display in child process")
        raise AssertionError(f"child failed ({proc.returncode}):\n{proc.stdout[-2000:]}\n{proc.stderr[-3000:]}")
    return json.loads(match.group(1))


@unittest.skipUnless(_display_available(), "needs a display")
class RealLayoutTests(unittest.TestCase):
    """Allocations of the real main window at 1920 × 1041 with a live session."""

    def test_compact_gives_the_terminal_the_window(self) -> None:
        budget = _run_child("compact")
        self.assertEqual(tuple(budget["window"]), SIZE)
        self.assertGreaterEqual(budget["height_share"], 0.78, budget)
        self.assertGreaterEqual(budget["width_share"], 0.80, budget)
        self.assertLessEqual(budget["rows"]["top bar"], 44)
        self.assertLessEqual(budget["rows"]["session strip"], 34)
        self.assertLessEqual(budget["rows"]["status bar"], 28)
        self.assertGreaterEqual(budget["sidebar"], 220)
        self.assertLessEqual(budget["sidebar"], 480)
        self.assertLessEqual(max(budget["row_heights"].values()), 30 + COMPACT.tree.GROUP_GAP)
        self.assertLessEqual(budget["row_heights"]["host"], 30)
        self.assertGreaterEqual(budget["row_heights"]["host"], 28)
        self.assertGreaterEqual(budget["tree_rows"], 28)

    def test_host_panel_never_resizes_the_terminal_and_tabs_overflow(self) -> None:
        result = _run_child("workspace")
        self.assertFalse(result["closed"]["panel"])
        self.assertTrue(result["open"]["panel"])
        self.assertEqual(result["closed"]["terminal"], result["open"]["terminal"])
        self.assertTrue(result["closed"]["chevrons"])  # 16 tabs do not fit in 1280 px

    def test_comfortable_keeps_the_reference_geometry(self) -> None:
        budget = _run_child("comfortable")
        rows = budget["rows"]
        # Content heights from the reference + their 1px borders (the reference's
        # Terminal/Details/Activity band is gone since Phase 6).
        self.assertEqual((rows["top bar"], rows["page bar"], rows["host header / host bar"]), (77, 74, 124))
        self.assertEqual(rows["session strip"], COMFORTABLE.size.SESSION_STRIP)
        self.assertEqual(rows["status bar"], 70)
        self.assertEqual(budget["sidebar"], 407)
        self.assertEqual(budget["row_heights"]["host"], 41)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--child" and sys.argv[2] == "workspace":
        _child_workspace()
    elif len(sys.argv) == 3 and sys.argv[1] == "--child":
        _child(sys.argv[2])
    else:
        unittest.main()
