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
        from ssh_manager.gtkui.theme import density

        with density.using("comfortable"):
            self.assertEqual(Space.values(), (4, 8, 12, 16, 20, 24, 32, 36))
        with density.using("compact"):
            self.assertEqual(Space.values(), (2, 4, 8, 12, 16, 16, 24, 12))


class RelayContrastTests(unittest.TestCase):
    def test_environment_badges_meet_aa(self) -> None:
        from ssh_manager.gtkui.theme.tokens import ENV_BADGES

        for theme, badges in ENV_BADGES.items():
            for kind, (background, foreground) in badges.items():
                with self.subTest(theme=theme, kind=kind):
                    self.assertGreaterEqual(contrast_ratio(foreground, background), 4.5)

    def test_selected_tree_row_and_tabs_meet_aa(self) -> None:
        for palette in PALETTES.values():
            with self.subTest(palette=palette.name):
                self.assertGreaterEqual(contrast_ratio(palette.accent_text, palette.bg_selected), 4.5)
                self.assertGreaterEqual(contrast_ratio(palette.accent_text, palette.bg_surface), 4.5)
                self.assertGreaterEqual(contrast_ratio(palette.on_accent, palette.accent), 4.5)


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
        ELEVATED = (".toast", "popover", ".primary", ".suggested-action", ".logo-tile", ".empty-card", ".host-panel")

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


from ssh_manager.gtkui.theme.tokens import DARK  # noqa: E402


class NeutralDarkPaletteTests(unittest.TestCase):
    """Phase 5: the dark theme is neutral gray; green is only the accent."""

    _DARK = DARK

    EXPECTED = {
        "bg_canvas": "#121315", "bg_window": "#151619", "bg_surface": "#1A1B1E",
        "bg_surface_raised": "#212226", "bg_input": "#16171A", "bg_page_bar": "#1A1B1E",
        "bg_hover": "#25272B", "bg_selected": "#202A25", "accent_tint": "#202A25",
        "border_subtle": "#2A2C31", "border_input": "#35383E", "border_strong": "#454951",
        "accent_soft_border": "#2F4A3E", "text_primary": "#E6E8EB", "text_secondary": "#A3A8B0",
        "text_muted": "#8A9098", "text_faint": "#6B7079", "text_disabled": "#6B7079",
        "tab_inactive": "#8F959E", "accent": "#1F7A57", "accent_hover": "#1A6E4E",
        "accent_pressed": "#196A4B", "accent_text": "#5CCB9C", "focus_ring": "#5CCB9C",
        "success": "#48C48D", "warning": "#E3A548", "danger": "#F07A6E", "info": "#7DB1E8",
        "chip_bg": "#25272B", "avatar_bg": "#3F4A45", "icon_group": "#D9A84E",
        "icon_subgroup": "#A3A8B0", "icon_host": "#A3A8B0", "scrollbar": "#454951",
        "shadow": "rgba(0, 0, 0, 0.35)", "shadow_popover": "rgba(0, 0, 0, 0.45)",
    }
    # Chromatic on purpose: the accent family, semantic colors, folder amber, broadcast.
    CHROMATIC = {
        "accent", "accent_hover", "accent_pressed", "accent_text", "accent_soft_border", "focus_ring",
        "on_accent", "success", "warning", "danger", "danger_strong", "on_danger", "info",
        "icon_group", "broadcast_bg", "broadcast_fg", "shadow", "shadow_popover",
        "pane_border_focus", "pane_border_broadcast", "pane_dim",
    }
    OLD_GREEN = ("#17201C", "#0F1613", "#131B18", "#1C2722", "#1B2521", "#202C27", "#18382B", "#27332E", "#33423B")
    # text_faint is decorative: counts, separators, keyboard hints, timestamps, captions.
    FAINT_ALLOWED = {
        ".caption", "menuitem .accel-hint", "modelbutton .accel-hint", ".breadcrumb-sep",
        ".empty-state .empty-caption", ".activity-time", ".status-sep",
    }

    @staticmethod
    def _rgb(value):
        value = value.lstrip("#")
        return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))

    def ratio(self, fg, bg):
        from ssh_manager.gtkui.theme.tokens import contrast_ratio

        return contrast_ratio(getattr(self._DARK, fg), getattr(self._DARK, bg))

    def test_palette_matches_the_approved_table(self) -> None:
        for role, value in self.EXPECTED.items():
            with self.subTest(role=role):
                self.assertEqual(getattr(self._DARK, role).upper(), value.upper())

    # The approved text grays are cool (hue ≈ 220°) with a 13–15 channel spread.
    TEXT_GRAYS = {"text_primary", "text_secondary", "text_muted", "text_faint", "text_disabled",
                  "tab_inactive", "icon_subgroup", "icon_host"}
    # Deliberate faint green hints from the approved table.
    GREEN_HINTS = {"bg_selected", "accent_tint", "avatar_bg", "pane_header_bg_focus"}

    def test_neutral_tokens_are_gray(self) -> None:
        for role, value in self._DARK.items():
            if role in self.CHROMATIC:
                continue
            r, g, b = self._rgb(value)
            with self.subTest(role=role, value=value):
                self.assertLessEqual(max(r, g, b) - min(r, g, b), 16 if role in self.TEXT_GRAYS else 12)
                if role not in self.GREEN_HINTS:
                    self.assertLessEqual(g, b, "green cast on a neutral token")

    def test_elevation_steps_by_lightness(self) -> None:
        from ssh_manager.gtkui.theme.tokens import relative_luminance

        layers = ["bg_canvas", "bg_window", "bg_surface", "bg_surface_raised", "bg_hover"]
        lum = [relative_luminance(getattr(self._DARK, role)) for role in layers]
        self.assertEqual(lum, sorted(lum))
        self.assertEqual(len(set(lum)), len(lum))
        # Visible steps (≥ 1.05:1) between the layers the eye compares directly; the
        # window base sits between canvas and surface and is almost never on screen.
        for upper, lower in (("bg_surface", "bg_window"), ("bg_surface_raised", "bg_surface"),
                             ("bg_hover", "bg_surface_raised"), ("bg_surface", "bg_canvas")):
            with self.subTest(pair=(upper, lower)):
                self.assertGreaterEqual(self.ratio(upper, lower), 1.05)

    def test_text_and_ui_contrast(self) -> None:
        text = [(fg, bg) for fg in ("text_primary", "text_secondary", "text_muted")
                for bg in ("bg_surface", "bg_surface_raised", "bg_canvas", "bg_input")]
        text += [("tab_inactive", "bg_surface"), ("accent_text", "bg_surface"), ("accent_text", "bg_selected")]
        text += [("on_accent", bg) for bg in ("accent", "accent_hover", "accent_pressed")]
        text += [(fg, bg) for fg in ("success", "warning", "danger", "info")
                 for bg in ("bg_surface", "bg_surface_raised")]
        for fg, bg in text:
            with self.subTest(fg=fg, bg=bg):
                self.assertGreaterEqual(self.ratio(fg, bg), 4.5)
        for fg in ("focus_ring", "success", "warning", "danger", "info"):
            for bg in ("bg_surface", "bg_surface_raised", "bg_hover", "bg_selected"):
                with self.subTest(ui=fg, bg=bg):
                    self.assertGreaterEqual(self.ratio(fg, bg), 3.0)
        # Decorative only (counts): below 4.5:1 by design, still readable.
        self.assertGreaterEqual(self.ratio("text_faint", "bg_surface"), 3.0)

    def test_environment_badges(self) -> None:
        from ssh_manager.gtkui.theme.tokens import ENV_BADGES, contrast_ratio

        for kind, (background, foreground) in ENV_BADGES["dark"].items():
            with self.subTest(kind=kind):
                self.assertGreaterEqual(contrast_ratio(foreground, background), 4.5)
        r, g, b = self._rgb(ENV_BADGES["dark"]["development"][0])
        self.assertLessEqual(max(r, g, b) - min(r, g, b), 12)

    def test_terminal_default_scheme(self) -> None:
        from ssh_manager.gtkui.theme.terminal_schemes import resolve_terminal_colors
        from ssh_manager.gtkui.theme.tokens import contrast_ratio
        from ssh_manager.settings import TerminalAppearanceSettings

        colors = resolve_terminal_colors(TerminalAppearanceSettings())
        self.assertEqual((colors.background, colors.foreground), ("#16171a", "#d6d9de"))
        self.assertGreaterEqual(contrast_ratio(colors.foreground, colors.background), 7.0)
        self.assertGreaterEqual(contrast_ratio(colors.foreground, colors.highlight), 4.5)
        r, g, b = self._rgb(colors.background)
        self.assertLessEqual(max(r, g, b) - min(r, g, b), 12)
        self.assertLess(contrast_ratio("#000000", colors.background),
                        contrast_ratio("#000000", self._DARK.bg_surface))  # darker than the surface
        # User colors and other schemes are untouched.
        custom = TerminalAppearanceSettings(use_custom_colors=True, fg_color="#00ff00", bg_color="#101010")
        user = resolve_terminal_colors(custom)
        self.assertEqual((user.background, user.foreground, user.cursor, user.highlight),
                         ("#101010", "#00ff00", "#b9c4cf", "#2f333a"))
        light = resolve_terminal_colors(TerminalAppearanceSettings(color_scheme="light"))
        self.assertEqual((light.background, light.highlight), ("#f8fafc", "#2f333a"))

    def test_dark_css_has_no_green_neutrals_or_unresolved_names(self) -> None:
        from ssh_manager.gtkui.theme import density

        for preset in (density.COMPACT, density.COMFORTABLE):
            css = css_dark.css(tokens=preset)
            with self.subTest(density=preset.density.value):
                self.assertEqual(unresolved_color_refs(css), set())
                for literal in self.OLD_GREEN:
                    self.assertNotIn(literal.lower(), css.lower())

    def test_text_faint_is_decorative_only(self) -> None:
        import re

        from ssh_manager.gtkui.theme import density

        for preset in (density.COMPACT, density.COMFORTABLE):
            css = re.sub(r"/\*.*?\*/", "", css_dark.css(tokens=preset), flags=re.S)
            for selectors, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
                if "@sm_text_faint" not in body:
                    continue
                for selector in selectors.split(","):
                    with self.subTest(density=preset.density.value, selector=selector.strip()):
                        self.assertIn(" ".join(selector.split()), self.FAINT_ALLOWED)

    def test_no_literal_colors_outside_the_token_modules(self) -> None:
        import re
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent / "ssh_manager" / "gtkui"
        allowed = {root / "theme" / "tokens.py", root / "theme" / "terminal_schemes.py"}
        offenders = [
            f"{path.relative_to(root)}:{number}"
            for path in root.rglob("*.py") if path not in allowed
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
            if re.search(r"#[0-9A-Fa-f]{6}\b", line)
        ]
        self.assertEqual(offenders, [])


class NeutralDarkStateTests(unittest.TestCase):
    """Hover, selected and focus stay distinguishable on every gray layer."""

    def test_states_on_gray_layers(self) -> None:
        from ssh_manager.gtkui.theme.tokens import DARK, contrast_ratio

        for state in ("bg_hover", "bg_selected"):
            for layer in ("bg_surface", "bg_canvas"):
                with self.subTest(state=state, layer=layer):
                    self.assertGreaterEqual(contrast_ratio(getattr(DARK, state), getattr(DARK, layer)), 1.05)
        for layer in ("bg_canvas", "bg_window", "bg_surface", "bg_surface_raised", "bg_input", "bg_hover", "bg_selected"):
            with self.subTest(focus_on=layer):
                self.assertGreaterEqual(contrast_ratio(DARK.focus_ring, getattr(DARK, layer)), 3.0)
        # Disabled text stays visible (decorative contrast) on surfaces.
        self.assertGreaterEqual(contrast_ratio(DARK.text_disabled, DARK.bg_surface), 3.0)
