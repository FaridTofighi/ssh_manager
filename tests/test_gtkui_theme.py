import unittest

from ssh_manager.gtkui.theme import css_dark, css_light
from ssh_manager.gtkui.theme.stylesheet import build_css, defined_colors, unresolved_color_refs
from ssh_manager.gtkui.theme.terminal_schemes import (
    DARK_COLORS,
    LIGHT_COLORS,
    SOLARIZED_PALETTE,
    resolve_terminal_colors,
)
from ssh_manager.gtkui.theme.tokens import (
    PALETTES,
    STATUS_GLYPHS,
    ConnectionStatus,
    Level,
    Radius,
    Space,
    contrast_ratio,
    contrast_report,
)
from ssh_manager.settings import TerminalAppearanceSettings


class TokenTests(unittest.TestCase):
    def test_palettes_define_the_same_roles(self) -> None:
        dark_roles = {role for role, _ in PALETTES["dark"].items()}
        light_roles = {role for role, _ in PALETTES["light"].items()}
        self.assertEqual(dark_roles, light_roles)

    def test_contrast_ratio_matches_known_values(self) -> None:
        self.assertAlmostEqual(contrast_ratio("#000000", "#ffffff"), 21.0, places=2)
        self.assertAlmostEqual(contrast_ratio("#777777", "#ffffff"), 4.48, places=2)

    def test_token_pairs_meet_wcag_aa_in_both_themes(self) -> None:
        for palette in PALETTES.values():
            for fg, bg, ratio, minimum in contrast_report(palette):
                with self.subTest(theme=palette.name, fg=fg, bg=bg):
                    self.assertGreaterEqual(ratio, minimum, f"{palette.name}: {fg} on {bg} is {ratio:.2f}")

    def test_status_and_level_colors_come_from_palette(self) -> None:
        for palette in PALETTES.values():
            self.assertEqual(palette.status_color("connected"), palette.success)
            self.assertEqual(palette.status_color("failed"), palette.danger)
            self.assertEqual(palette.status_color("bogus"), palette.text_disabled)
            self.assertEqual(palette.level_color(Level.WARNING), palette.warning)
            self.assertEqual(palette.level_color("nope"), palette.info)

    def test_status_shapes_differ_so_color_is_not_the_only_cue(self) -> None:
        shapes = [STATUS_GLYPHS[s] for s in (ConnectionStatus.CONNECTED, ConnectionStatus.CONNECTING, ConnectionStatus.FAILED)]
        self.assertEqual(len(set(shapes)), 3)
        self.assertIs(ConnectionStatus.coerce("weird"), ConnectionStatus.UNKNOWN)

    def test_spacing_scale_is_fixed(self) -> None:
        self.assertEqual(Space.values(), (4, 8, 12, 16, 20, 24, 32, 36))


class StylesheetTests(unittest.TestCase):
    def test_both_themes_have_no_unresolved_color_names(self) -> None:
        for module in (css_dark, css_light):
            for animations in (True, False):
                css = module.css(animations=animations)
                with self.subTest(module=module.__name__, animations=animations):
                    self.assertEqual(unresolved_color_refs(css), set())

    def test_every_palette_role_is_defined_as_a_css_color(self) -> None:
        for palette in PALETTES.values():
            defined = defined_colors(build_css(palette))
            for role, _value in palette.items():
                self.assertIn(f"sm_{role}", defined)

    def test_animations_flag_controls_transitions(self) -> None:
        self.assertIn("transition:", css_dark.css(animations=True))
        self.assertNotIn("transition:", css_dark.css(animations=False))

    def test_font_sizes_are_relative(self) -> None:
        css = css_light.css()
        self.assertNotRegex(css, r"font-size:\s*\d+px")
        self.assertIn("em;", css)

    def test_radii_stay_on_scale(self) -> None:
        import re

        for value in re.findall(r"border-radius:\s*([^;]+);", css_dark.css()):
            for part in value.split():
                self.assertIn(part, {"0"} | {f"{r}px" for r in Radius.values()}, value)

    def test_inputs_and_buttons_have_no_drop_shadow(self) -> None:
        import re

        # Only primary buttons and floating surfaces are elevated.
        ELEVATED = (".toast", "popover", ".primary", ".suggested-action", ".logo-tile", ".empty-card")

        css = css_dark.css()
        for block in re.findall(r"([^{}]+)\{([^}]*)\}", css):
            selector, body = block
            for shadow in re.findall(r"box-shadow:\s*([^;]+);", body):
                if shadow.strip() == "none" or "inset" in shadow or shadow.strip().startswith("0 0 0 "):
                    continue  # no shadow, inner border or focus ring
                self.assertTrue(
                    any(name in selector for name in ELEVATED),
                    f"unexpected shadow on {selector.strip()}",
                )


@unittest.skipUnless(__import__("importlib").util.find_spec("gi"), "PyGObject not installed")
class StylesheetParseTests(unittest.TestCase):
    def test_gtk_parses_both_stylesheets_without_errors(self) -> None:
        import gi

        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk  # type: ignore

        for module in (css_dark, css_light):
            errors = []
            provider = Gtk.CssProvider()
            provider.connect("parsing-error", lambda _p, section, err: errors.append(err.message))
            provider.load_from_data(module.css().encode("utf-8"))
            self.assertEqual(errors, [], module.__name__)


class TerminalSchemeTests(unittest.TestCase):
    def test_defaults_without_appearance(self) -> None:
        colors = resolve_terminal_colors(None)
        self.assertEqual((colors.background, colors.foreground), DARK_COLORS)

    def test_profile_theme_applies_only_to_default_scheme(self) -> None:
        appearance = TerminalAppearanceSettings()
        self.assertEqual(resolve_terminal_colors(appearance, "light").background, LIGHT_COLORS[0])
        appearance.color_scheme = "dark"
        self.assertEqual(resolve_terminal_colors(appearance, "light").background, DARK_COLORS[0])

    def test_custom_colors_and_palette_override(self) -> None:
        appearance = TerminalAppearanceSettings(
            use_custom_colors=True, fg_color="#010203", bg_color="#040506", palette=["#111111"]
        )
        colors = resolve_terminal_colors(appearance)
        self.assertEqual((colors.foreground, colors.background, colors.palette), ("#010203", "#040506", ["#111111"]))

    def test_solarized_uses_solarized_palette(self) -> None:
        colors = resolve_terminal_colors(TerminalAppearanceSettings(color_scheme="solarized-dark"))
        self.assertEqual(colors.palette, SOLARIZED_PALETTE)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
