"""The sidebar tree panel is painted with the surface token in every theme.

Regression test for docs/bugs/sidebar-background.png: GTK themes paint
``treeview.view`` (and the ``.cell`` nodes TreeView draws rows and the empty
area with) in ``theme_base_color``, which showed as a gray box inside the
sidebar.

GTK reads ``GTK_THEME`` once, so each GTK theme runs in a child process
(``python3 -m tests.test_gtkui_surfaces --child THEME``) that renders the main
window offscreen and prints sampled tree-area pixels as JSON. The child uses
a throw-away HOME and an in-memory GSettings backend.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GTK_THEMES = ("Adwaita", "Adwaita:dark")
MODES = ("system-light", "system-dark", "light", "dark")
TOLERANCE = 1


def _display_available() -> bool:
    from tests.gtk_window_fixture import display_available

    return display_available()


# ====================================================================== child
def _child(theme: str) -> None:
    os.environ["GTK_THEME"] = theme
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
    from ssh_manager.gtkui.theme.loader import ThemeMode, get_theme_manager

    app = gtk_app.SSHManagerApplication(snap._SANDBOX / "inventory")
    app.set_flags(Gio.ApplicationFlags.NON_UNIQUE)
    app.register(None)
    populated = snap._SANDBOX / "populated"
    snap._write_inventory(populated, populated=True)
    empty = snap._SANDBOX / "empty"
    snap._write_inventory(empty, populated=False)

    results = []

    def record(mode: str, scene: str, window) -> None:
        manager = get_theme_manager()
        for backdrop in (False, True):
            _pixbuf, _box, samples = snap.sidebar_samples(window, backdrop=backdrop)
            results.append({
                "mode": mode, "scene": scene, "backdrop": backdrop,
                "expected": manager.palette.bg_surface, "dark": manager.is_dark,
                "page": window._sidebar.stack.get_visible_child_name(),
                "samples": [[label, list(point), list(rgb)] for label, point, rgb in samples],
            })

    for mode in MODES:
        theme_mode, _, desktop = mode.partition("-")
        snap.set_desktop_color_scheme(desktop == "dark")

        window = snap.make_window_with_mode(app, populated, theme_mode)
        snap._select_first_host(window)
        snap._pump()
        record(mode, "hosts", window)
        window._sidebar.search_entry.set_text("zz-no-such-host")
        window.tree.rebuild(refresh=False)  # search-changed is debounced
        snap._pump()
        record(mode, "no-results", window)
        window.destroy()

        window = snap.make_window_with_mode(app, empty, theme_mode)
        record(mode, "first-run", window)
        window.destroy()

        # Switch at runtime from the opposite variant: no restart, no stale colors.
        if theme_mode == "system":
            snap.set_desktop_color_scheme(desktop != "dark")
            window = snap.make_window_with_mode(app, populated, "system")
            snap.set_desktop_color_scheme(desktop == "dark")
        else:
            window = snap.make_window_with_mode(app, populated, "light" if theme_mode == "dark" else "dark")
            window._apply_theme_mode(ThemeMode(theme_mode))
        snap._pump()
        record(mode, "switched-at-runtime", window)
        window.destroy()
        snap._pump()

    print("RESULTS " + json.dumps(results))


# ===================================================================== parent
def _run_child(theme: str):
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    env.pop("GDK_SCALE", None)
    proc = subprocess.run(
        [sys.executable, "-m", "tests.test_gtkui_surfaces", "--child", theme],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=600,
    )
    match = re.search(r"^RESULTS (.*)$", proc.stdout, re.MULTILINE)
    if match is None:
        if '"skip"' in proc.stdout:
            raise unittest.SkipTest("no display in child process")
        raise AssertionError(f"child for {theme} failed ({proc.returncode}):\n{proc.stdout[-2000:]}\n{proc.stderr[-4000:]}")
    return json.loads(match.group(1))


def _rgb(value: str):
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


@unittest.skipUnless(_display_available(), "needs a display")
class SidebarSurfaceTests(unittest.TestCase):
    """Pixels behind the tree rows equal ``palette.bg_surface`` (±1 per channel)."""

    def _check_theme(self, theme: str) -> None:
        results = _run_child(theme)
        expected_scenes = {"hosts", "no-results", "first-run", "switched-at-runtime"}
        self.assertEqual({r["mode"] for r in results}, set(MODES))
        self.assertEqual({r["scene"] for r in results}, expected_scenes)
        for result in results:
            expected = _rgb(result["expected"])
            self.assertEqual(result["dark"], result["mode"].endswith("dark"), result["mode"])
            page = {"no-results": "no-results", "first-run": "empty"}.get(result["scene"], "tree")
            self.assertEqual(result["page"], page, result["scene"])
            if page == "tree":
                self.assertTrue(any(s[0].startswith("empty space") for s in result["samples"]),
                                "no empty space below the rows was sampled")
            self.assertGreaterEqual(len(result["samples"]), 6)
            for label, point, rgb in result["samples"]:
                with self.subTest(theme=theme, mode=result["mode"], scene=result["scene"],
                                  backdrop=result["backdrop"], point=label):
                    off = max(abs(a - b) for a, b in zip(rgb, expected))
                    self.assertLessEqual(
                        off, TOLERANCE,
                        f"{label} at {point} is #{''.join(f'{c:02X}' for c in rgb)}, expected {result['expected']}",
                    )

    def test_adwaita(self) -> None:
        self._check_theme("Adwaita")

    def test_adwaita_dark(self) -> None:
        self._check_theme("Adwaita:dark")


class SidebarCssLintTests(unittest.TestCase):
    """Every sidebar rule that sets a background uses a token, never a literal or transparent.

    The scrolled window's undershoot/overshoot indicators are exempt: they are
    painted over the rows and must stay transparent.
    """

    RULE = re.compile(r"([^{}]+)\{([^{}]*)\}")
    BACKGROUND = re.compile(r"background(?:-color)?\s*:\s*([^;]+);")

    def _sidebar_backgrounds(self, css: str):
        css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
        for selectors, body in self.RULE.findall(css):
            # undershoot/overshoot are edge indicators drawn over the rows, not containers.
            sidebar = [sel.strip() for sel in selectors.split(",")
                       if ".sidebar-pane" in sel and not re.search(r"\b(under|over)shoot\b", sel)]
            if not sidebar:
                continue
            for value in self.BACKGROUND.findall(body):
                yield sidebar, value.strip()

    def test_sidebar_backgrounds_use_tokens(self) -> None:
        from ssh_manager.gtkui.theme import css_dark, css_light

        for module in (css_dark, css_light):
            css = module.css(animations=True)
            found = list(self._sidebar_backgrounds(css))
            self.assertTrue(found, "no .sidebar-pane background rules found")
            for selectors, value in found:
                with self.subTest(theme=module.__name__, selectors=selectors[0]):
                    self.assertRegex(value, r"^@sm_[a-z_]+$", f"{selectors}: background {value!r}")

    def test_sidebar_chain_is_explicit(self) -> None:
        """The nodes between the pane and the rows each get the surface token."""

        from ssh_manager.gtkui.theme import css_light

        css = re.sub(r"/\*.*?\*/", "", css_light.css(animations=True), flags=re.S)
        covered = set()
        for selectors, body in self.RULE.findall(css):
            if re.search(r"background-color\s*:\s*@sm_bg_surface\s*;", body) and "background-image: none" in body:
                covered.update(sel.strip() for sel in selectors.split(","))
        for node in (
            ".sidebar-pane", ".sidebar-pane stack", ".sidebar-pane scrolledwindow",
            ".sidebar-pane scrolledwindow viewport", ".sidebar-pane treeview.view",
            ".sidebar-pane treeview.view.cell", ".sidebar-pane treeview.view:backdrop",
            ".sidebar-pane treeview.view:selected", ".sidebar-pane treeview.view:hover",
            ".sidebar-pane .empty-state",
        ):
            self.assertIn(node, covered)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--child":
        _child(sys.argv[2])
    else:
        unittest.main()
