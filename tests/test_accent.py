"""The accent colour derivation (gtkui/theme/accent.py): contrast guarantees, semantic colours untouched."""

import colorsys
import dataclasses
import random
import unittest

from ophion.gtkui.theme import accent as A
from ophion.gtkui.theme.tokens import DARK, LIGHT, contrast_report, parse_hex


def hls(color):
    return colorsys.rgb_to_hls(*(channel / 255.0 for channel in parse_hex(color)))


def hue_distance(a, b):
    distance = abs(a - b) % 360.0
    return min(distance, 360.0 - distance)


class DerivationTests(unittest.TestCase):
    def colors(self):
        rng = random.Random(20261105)  # deterministic
        presets = [color for _key, _name, color in A.PRESETS if color]
        return presets + ["#{:06X}".format(rng.randrange(0x1000000)) for _ in range(200)] + [
            "#FFFFE0", "#0A1030", "#FFFF00", "#000000", "#FFFFFF", "#808080", "#00FF00", "#0000FF"]

    def test_every_contrast_pair_passes_for_presets_and_random_colours(self) -> None:
        for base in (LIGHT, DARK):
            for color in self.colors():
                with self.subTest(theme=base.name, color=color):
                    palette = A.accent_palette(base, color)
                    failing = [(fg, bg, round(ratio, 2), minimum) for fg, bg, ratio, minimum in contrast_report(palette)
                               if ratio < minimum]
                    self.assertEqual(failing, [])
                    self.assertIn(palette.on_accent, (A.WHITE, A.NEAR_BLACK))
                    self.assertEqual(palette.pane_border_focus, palette.accent)

    def test_hue_is_kept_and_presets_change_lightness_only(self) -> None:
        for dark in (False, True):
            for color in self.colors():
                derived = A.derive_accent(color, dark)
                h0, _l0, s0 = hls(color)
                h1, l1, s1 = hls(derived.roles["accent"])
                if s0 >= 0.2 and s1 >= 0.15 and 0.1 < l1 < 0.9:
                    self.assertLessEqual(hue_distance(h0 * 360, h1 * 360), 3.0, (color, dark, derived.roles["accent"]))
            for key, _name, color in A.PRESETS[1:]:
                derived = A.derive_accent(color, dark)
                self.assertFalse(derived.adjusted, key)
                self.assertAlmostEqual(hls(derived.roles["accent"])[2], hls(color)[2], delta=0.05, msg=key)

    def test_deterministic(self) -> None:
        for color in ("#2F6FEB", "#123456", "#FFFFE0"):
            self.assertEqual(A.derive_accent(color, True), A.derive_accent(color, True))

    def test_unreadable_colours_are_adjusted(self) -> None:
        self.assertTrue(A.derive_accent("#FFFFE0", False).adjusted)  # near-white on a white surface
        self.assertFalse(A.derive_accent("#2F6FEB", False).adjusted)


class PaletteTests(unittest.TestCase):
    def test_default_is_the_palette_itself(self) -> None:
        for base in (LIGHT, DARK):
            for value in ("default", None, "", "nonsense", "#12", 42):
                self.assertIs(A.accent_palette(base, value), base)
        golden = {  # today's accent roles: default users see no change
            "light": ("#176B4D", "#12593F", "#0E4A34", "#176B4D", "#EAF5EF", "#CFE6DB", "#FFFFFF", "#EAF5EF",
                      "#176B4D", "#176B4D", "#EAF5EF", "#233B32"),
            "dark": ("#1F7A57", "#1A6E4E", "#196A4B", "#5CCB9C", "#202A25", "#2F4A3E", "#FFFFFF", "#202A25",
                     "#5CCB9C", "#1F7A57", "#202A25", "#3F4A45"),
        }
        for base in (LIGHT, DARK):
            self.assertEqual(tuple(getattr(A.accent_palette(base, "default"), role) for role in A.ACCENT_ROLES),
                             golden[base.name])

    def test_semantic_and_neutral_roles_never_change(self) -> None:
        for base in (LIGHT, DARK):
            for key, _name, color in A.PRESETS + [("custom", "", "#3355AA")]:
                palette = A.accent_palette(base, key if color is None or key != "custom" else color)
                changed = {field.name for field in dataclasses.fields(base)
                           if getattr(palette, field.name) != getattr(base, field.name)}
                self.assertLessEqual(changed, set(A.ACCENT_ROLES), (base.name, key))
                for role in ("success", "badge_connected", "warning", "danger", "danger_strong", "info", "broadcast",
                             "broadcast_bg", "broadcast_fg", "pane_border_broadcast", "text_primary", "bg_surface"):
                    self.assertEqual(getattr(palette, role), getattr(base, role), role)


class ValueTests(unittest.TestCase):
    def test_normalize(self) -> None:
        self.assertEqual(A.normalize_accent("Blue"), "blue")
        self.assertEqual(A.normalize_accent("#abc"), "#AABBCC")
        self.assertEqual(A.normalize_accent("12ab9F"), "#12AB9F")
        for bad in ("", None, "#12", "#GGGGGG", "chartreuse", 7, ["blue"]):
            self.assertEqual(A.normalize_accent(bad), "default", bad)
        self.assertIsNone(A.base_color("default"))
        self.assertEqual(A.base_color("blue"), "#2F6FEB")
        self.assertEqual(A.display_color("default"), "#176B4D")

    def test_broadcast_hint(self) -> None:
        self.assertTrue(A.near_broadcast("purple"))
        self.assertTrue(A.near_broadcast("indigo"))
        self.assertTrue(A.near_broadcast("#6A55C9"))
        for value in ("default", "blue", "teal", "red", "graphite", "#808080"):
            self.assertFalse(A.near_broadcast(value), value)

    def test_presets(self) -> None:
        self.assertEqual([key for key, _n, _c in A.PRESETS],
                         ["default", "teal", "blue", "indigo", "purple", "pink", "red", "orange", "amber", "graphite"])


if __name__ == "__main__":
    unittest.main()
