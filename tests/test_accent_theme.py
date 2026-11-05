"""The accent colour in the running app: saved with the UI state, applied by the ThemeManager, used by widgets."""

import unittest
from unittest import mock

from tests.gtk_window_fixture import display_available


class UiStateTests(unittest.TestCase):
    def test_round_trip_and_bad_values(self) -> None:
        from ophion.gtkui.controllers.ui_state import UiState

        self.assertEqual(UiState.from_dict({}).accent_color, "default")  # old files: today's green
        for stored, loaded in (("blue", "blue"), ("#abc", "#AABBCC"), ("#12AB9F", "#12AB9F"), ("BLUE", "blue"),
                               ("chartreuse", "default"), ("#12", "default"), (7, "default"), (None, "default")):
            with self.subTest(stored=stored):
                state = UiState.from_dict({"accent_color": stored})
                self.assertEqual(state.accent_color, loaded)
                self.assertEqual(UiState.from_dict(state.to_dict()).accent_color, loaded)


class HardCodedAccentTests(unittest.TestCase):
    def test_the_green_accent_is_only_in_the_palette(self) -> None:
        """A literal accent green elsewhere would stay green after the user picks another accent."""

        import dataclasses
        import re
        from pathlib import Path

        from ophion.gtkui.theme.accent import ACCENT_ROLES
        from ophion.gtkui.theme.tokens import DARK, LIGHT

        greens = {getattr(palette, role).upper() for palette in (LIGHT, DARK) for role in ACCENT_ROLES}
        semantic = {getattr(palette, field.name).upper() for palette in (LIGHT, DARK)
                    for field in dataclasses.fields(palette) if field.name not in ACCENT_ROLES}
        greens -= semantic | {"#FFFFFF"}  # a value a semantic role shares is not the accent's
        root = Path(__file__).resolve().parent.parent / "ophion"
        allowed = {root / "gtkui" / "theme" / "tokens.py"}
        found = [f"{path.relative_to(root)}:{number}"
                 for path in root.rglob("*.py") if path not in allowed
                 for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
                 for color in re.findall(r"#[0-9A-Fa-f]{6}\b", line) if color.upper() in greens]
        self.assertEqual(found, [])
        # The terminal's own selection colour is derived from the dark accent text but is a terminal colour
        # (never changed by the accent): it is written as #28443a, not as an accent value.


@unittest.skipUnless(display_available(), "needs a display")
class ThemeManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        from ophion.gtkui.theme.loader import get_theme_manager

        self.theme = get_theme_manager()
        self.addCleanup(self.theme.apply, self.theme.mode)
        self.addCleanup(self.theme.set_accent, self.theme.accent)

    def test_set_accent_restyles_once_and_keeps_one_provider(self) -> None:
        from gi.repository import Gtk

        from ophion.gtkui.theme.accent import derive_accent
        from ophion.gtkui.theme.loader import ThemeMode, current_palette
        from ophion.gtkui.theme.tokens import DARK, LIGHT

        self.theme.apply(ThemeMode.LIGHT)
        self.theme.set_accent("default")
        calls, added, removed = [], [], []
        self.theme.add_listener(calls.append)
        self.addCleanup(self.theme.remove_listener, calls.append)
        real_add, real_remove = Gtk.StyleContext.add_provider_for_screen, Gtk.StyleContext.remove_provider_for_screen
        with mock.patch.object(Gtk.StyleContext, "add_provider_for_screen",
                               side_effect=lambda *a: (added.append(a[1]), real_add(*a))), \
                mock.patch.object(Gtk.StyleContext, "remove_provider_for_screen",
                                  side_effect=lambda *a: (removed.append(a[1]), real_remove(*a))):
            before = self.theme._provider
            self.assertEqual(self.theme.set_accent("Blue"), "blue")
        self.assertEqual(calls, [False])  # the listeners once (cairo-drawn widgets repaint)
        self.assertEqual((removed, len(added)), ([before], 1))  # swapped: one provider installed at any time
        blue = derive_accent("#2F6FEB", False).roles["accent"]
        self.assertEqual(current_palette().accent, blue)
        css = self.theme.stylesheet()
        self.assertIn(f"@define-color sm_accent {blue};", css)
        self.assertNotIn(f"@define-color sm_accent {LIGHT.accent};", css)
        self.assertEqual(self.theme.set_accent("blue"), "blue")  # unchanged: nothing reloads
        self.assertEqual(calls, [False])
        self.theme.apply(ThemeMode.DARK)  # the accent follows the variant
        self.assertEqual(current_palette().accent, derive_accent("#2F6FEB", True).roles["accent"])
        self.assertEqual(current_palette().success, DARK.success)
        self.assertEqual(self.theme.set_accent("not a colour"), "default")
        self.assertIs(current_palette(), DARK)

    def test_widgets_follow_the_accent(self) -> None:
        from gi.repository import Gdk, Gtk

        from ophion.gtkui.theme.loader import ThemeMode, current_palette
        from tests.test_local_terminal import pump

        self.theme.apply(ThemeMode.LIGHT)
        window = Gtk.OffscreenWindow()
        self.addCleanup(window.destroy)
        window.get_style_context().add_class("sm-window")
        button = Gtk.Button(label="Connect")
        button.get_style_context().add_class("primary")
        window.add(button)
        window.show_all()
        pump(0.05)

        def background():
            ctx = button.get_style_context()
            return ctx.get_property("background-color", ctx.get_state()).to_string()

        for value in ("orange", "default"):
            self.theme.set_accent(value)
            pump(0.6)  # buttons fade their background (CSS transition)
            rgba = Gdk.RGBA()  # the palette's accent, as GTK prints it
            rgba.parse(current_palette().accent)
            self.assertEqual(background(), rgba.to_string(), value)


@unittest.skipUnless(display_available(), "needs a display")
class StartupTests(unittest.TestCase):
    def test_a_saved_accent_is_applied_before_the_window_is_built(self) -> None:
        from ophion.gtk_app import SSHManagerWindow
        from ophion.gtkui.theme.accent import derive_accent
        from ophion.gtkui.theme.loader import current_palette, get_theme_manager
        from ophion.settings import AppSettings
        from tests.gtk_window_fixture import WindowSandbox

        theme = get_theme_manager()
        self.addCleanup(theme.apply, theme.mode)
        self.addCleanup(theme.set_accent, theme.accent)
        sandbox = WindowSandbox(store=True)
        self.addCleanup(sandbox.close)
        sandbox.window._ui.accent_color = "purple"
        sandbox.window._save_ui_state()
        theme.set_accent("default")
        window = SSHManagerWindow(sandbox.app, sandbox.window.manager.base_dir, AppSettings.load(),
                                  manager=sandbox.window.manager)
        self.addCleanup(window.destroy)
        dark = theme.is_dark
        self.assertEqual(window._ui.accent_color, "purple")
        self.assertEqual(current_palette().accent, derive_accent("#8E4EC6", dark).roles["accent"])


if __name__ == "__main__":
    unittest.main()
