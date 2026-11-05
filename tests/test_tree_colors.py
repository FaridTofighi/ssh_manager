"""Tree icon colours: own colour → nearest coloured group → the default for the kind → the theme's colour.

Measured before: a nested folder (``a/b/c``) skipped its intermediate groups (``a/b``): only the full path
and the top-level group were consulted. There were no defaults.
"""

import colorsys
import random
import unittest
from unittest import mock

from ophion.gtkui.theme.tokens import DARK, LIGHT, contrast_ratio, parse_hex
from tests.gtk_window_fixture import display_available


def controller(group_colors=None, host_colors=None, default_group="", default_host=""):
    from ophion.gtkui.controllers.colors import ColorController
    from ophion.gtkui.controllers.ui_state import UiState

    win = mock.Mock()
    win._ui = UiState(group_colors=dict(group_colors or {}), host_colors=dict(host_colors or {}),
                      default_group_color=default_group, default_host_color=default_host)
    win.tree.make_host_key = lambda parent, child, alias: f"{parent}::{child}::{alias}"
    return ColorController(win)


def old_item_color_for(group_colors, host_colors, parent, child, alias=""):
    """The resolver before this change (no ancestor walk, no defaults)."""

    def group(p, c):
        return group_colors.get(p if not c else f"{p}/{c}", "")

    resolved = (group(parent, child) if child else "") or group(parent, "")
    if alias:
        return host_colors.get(f"{parent}::{child}::{alias}", "") or resolved
    return resolved


class ResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch("ophion.gtkui.theme.loader.current_palette", return_value=LIGHT)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_nested_folders_inherit_from_every_ancestor(self) -> None:
        colors = controller({"top/a/b": "#112233"})
        self.assertEqual(colors.item_color_for("top", "a/b/c"), "#112233")  # depth 1 above
        self.assertEqual(colors.item_color_for("top", "a/b/c/d/e"), "#112233")  # depth 3 above
        self.assertEqual(colors.item_color_for("top", "a/b/c", "web-01"), "#112233")  # a host inside
        self.assertEqual(colors.item_color_for("top", "a"), "")  # nothing above a
        self.assertEqual(old_item_color_for({"top/a/b": "#112233"}, {}, "top", "a/b/c"), "")  # the old gap

    def test_matrix(self) -> None:
        colors = controller({"top": "#AA0000", "top/x/y": "#00AA00"}, {"top::x/y/z::web": "#0000AA"},
                            default_group="#E5B100", default_host="#8A94A6")
        drawn_group = colors.drawn_default("#E5B100")[0]
        drawn_host = colors.drawn_default("#8A94A6")[0]
        cases = [
            (("top", "x/y/z", "web"), "#0000AA"),  # the host's own colour beats everything
            (("top", "x/y/z", "db"), "#00AA00"),  # nearest coloured group
            (("top", "x/q", "db"), "#AA0000"),  # the top-level group
            (("top", "x/q"), "#AA0000"),
            (("other", "x"), drawn_group),  # no colour anywhere: the group default
            (("other", ""), drawn_group),
            (("other", "x", "db"), drawn_host),  # a host in uncoloured groups: the host default
        ]
        for args, expected in cases:
            with self.subTest(args=args):
                self.assertEqual(colors.item_color_for(*args), expected)
        self.assertEqual(controller().item_color_for("other", "x", "db"), "")  # both unset: the theme's

    def test_automatic_defaults_match_the_old_resolver(self) -> None:
        groups = {"prod": "#AA0000", "prod/web.conf": "#00AA00", "lab/x": "#123456"}
        hosts = {"prod::web.conf::web-01": "#0000AA"}
        colors = controller(groups, hosts)
        for args in [("prod", ""), ("prod", "web.conf"), ("prod", "db.conf"), ("lab", ""), ("lab", "x"),
                     ("staging", "hosts.conf"), ("prod", "web.conf", "web-01"), ("prod", "web.conf", "web-02"),
                     ("prod", "db.conf", "db-01"), ("staging", "hosts.conf", "app")]:
            with self.subTest(args=args):
                self.assertEqual(colors.item_color_for(*args), old_item_color_for(groups, hosts, *args))


class ReadabilityTests(unittest.TestCase):
    def test_drawn_colours_reach_3_to_1_and_keep_their_hue(self) -> None:
        from ophion.gtkui.theme.accent import readable_color

        rng = random.Random(20261105)
        colours = ["#{:06X}".format(rng.randrange(0x1000000)) for _ in range(200)] + ["#FFFFE0", "#0A1030",
                                                                                         "#FFFFFF", "#000000"]
        for palette in (LIGHT, DARK):
            backgrounds = (palette.bg_surface, palette.bg_selected)
            for colour in colours:
                drawn, adjusted = readable_color(colour, backgrounds)
                for bg in backgrounds:
                    self.assertGreaterEqual(contrast_ratio(drawn, bg), 3.0, (palette.name, colour, drawn, bg))
                h0, _l0, s0 = colorsys.rgb_to_hls(*(c / 255 for c in parse_hex(colour)))
                h1, l1, s1 = colorsys.rgb_to_hls(*(c / 255 for c in parse_hex(drawn)))
                if s0 >= 0.2 and s1 >= 0.15 and 0.1 < l1 < 0.9:
                    distance = abs(h0 - h1) * 360 % 360
                    self.assertLessEqual(min(distance, 360 - distance), 3.0, (colour, drawn))
                if not adjusted:
                    self.assertEqual(drawn, colour.upper())
        self.assertEqual(readable_color("#FFFFE0", (LIGHT.bg_surface, LIGHT.bg_selected))[1], True)

    def test_the_stored_default_never_changes(self) -> None:
        with mock.patch("ophion.gtkui.theme.loader.current_palette", return_value=LIGHT):
            colors = controller(default_group="#FFFFE0")
            self.assertNotEqual(colors.item_color_for("a", ""), "#FFFFE0")  # drawn darker on white
        self.assertEqual(colors.default_group_color, "#FFFFE0")
        self.assertEqual(colors.win._ui.default_group_color, "#FFFFE0")


class StateTests(unittest.TestCase):
    def test_round_trip_and_bad_values(self) -> None:
        from ophion.gtkui.controllers.ui_state import UiState

        old = UiState.from_dict({"group_colors": {"a": "#112233"}})
        self.assertEqual((old.default_group_color, old.default_host_color), ("", ""))
        for stored, loaded in (("#e5b100", "#E5B100"), ("#abc", "#AABBCC"), ("orange", ""), ("#12", ""), (7, ""),
                               (None, ""), ("", "")):
            with self.subTest(stored=stored):
                state = UiState.from_dict({"default_group_color": stored, "default_host_color": stored})
                self.assertEqual((state.default_group_color, state.default_host_color), (loaded, loaded))
                again = UiState.from_dict(state.to_dict())
                self.assertEqual((again.default_group_color, again.default_host_color), (loaded, loaded))


@unittest.skipUnless(display_available(), "needs a display")
class ExportTests(unittest.TestCase):
    def test_defaults_are_never_exported(self) -> None:
        import json

        from ophion.exporters.native import ExportOptions
        from tests.gtk_window_fixture import WindowSandbox

        sandbox = WindowSandbox(store=True)
        self.addCleanup(sandbox.close)
        win = sandbox.window
        win.colors.set_group_color("prod", "web.conf", "#112233")
        win.colors.set_default_colors(group="#E5B100", host="#8A94A6")
        target = sandbox.root / "out.ophion"
        win.native.choose_path = lambda _name: str(target)
        win.native.ask_export = lambda **_kw: ("all", ExportOptions(), None)
        win.native.export("all")
        win.native.wait()
        document = json.loads(target.read_bytes())
        colours = []

        def walk(groups, path=""):
            for group in groups:
                name = f"{path}/{group['name']}" if path else group["name"]
                colours.append((name, group.get("color", "")))
                colours.extend((f"{name}:{host['alias']}", host.get("color", "")) for host in group.get("hosts", []))
                walk(group.get("children", []), name)
        walk(document["groups"])
        self.assertIn(("prod/web.conf", "#112233"), colours)  # an explicit colour is exported
        self.assertEqual({colour for name, colour in colours if name != "prod/web.conf"}, {""})
        self.assertNotIn("default_group_color", json.dumps(document))


@unittest.skipUnless(display_available(), "needs a display")
class WindowColourTests(unittest.TestCase):
    """The tree, the details header and the group picker draw what item_color_for resolves."""

    def setUp(self) -> None:
        from ophion.gtkui.theme.loader import ThemeMode, get_theme_manager
        from tests.gtk_window_fixture import WindowSandbox

        theme = get_theme_manager()
        self.addCleanup(theme.apply, theme.mode)
        theme.apply(ThemeMode.LIGHT)
        self.theme = theme
        self.sandbox = WindowSandbox(store=True)
        self.addCleanup(self.sandbox.close)
        self.win = self.sandbox.window
        self.win.tree.rebuild()

    def colours(self):
        from ophion.gtkui.constants import TreeColumn

        model, found = self.win.tree_store, {}

        def walk(it):
            while it is not None:
                key = (model.get_value(it, TreeColumn.KIND), model.get_value(it, TreeColumn.PARENT),
                       model.get_value(it, TreeColumn.CHILD), model.get_value(it, TreeColumn.ALIAS))
                found[key] = model.get_value(it, TreeColumn.COLOR) or ""
                walk(model.iter_children(it))
                it = model.iter_next(it)
        walk(model.get_iter_first())
        return found

    def test_defaults_recolour_the_open_tree_in_place(self) -> None:
        from ophion.gtkui.theme.loader import ThemeMode

        tree, colors = self.win.tree, self.win.colors
        colors.set_group_color("staging", "", "#AA0000")
        tree.rebuild()
        tree.select_host("prod", "web.conf", "web-01")
        expanded = set(self.win.tree_view.expanded_keys)
        store = self.win.tree_view.get_model()
        before = self.colours()
        self.assertEqual(before[("parent", "prod", "", "")], "")  # automatic: the theme's colour
        colors.set_default_colors(group="#E5B100", host="#8A94A6")
        after = self.colours()
        group, host = colors.drawn_default("#E5B100")[0], colors.drawn_default("#8A94A6")[0]
        self.assertEqual(after[("parent", "prod", "", "")], group)
        self.assertEqual(after[("host", "prod", "web.conf", "web-01")], host)
        self.assertEqual(after[("parent", "staging", "", "")], "#AA0000")  # an explicit colour still wins
        self.assertEqual(after[("host", "staging", "hosts.conf", "stage-app")], "#AA0000")
        self.assertIs(self.win.tree_view.get_model(), store)  # recoloured, not rebuilt
        self.assertEqual(tree.selected_host().alias, "web-01")
        self.assertEqual(set(self.win.tree_view.expanded_keys), expanded)
        self.assertEqual((self.win._ui.default_group_color, self.win._ui.default_host_color), ("#E5B100", "#8A94A6"))
        self.theme.apply(ThemeMode.DARK)  # drawn for the dark surfaces now (the stored value stays)
        self.assertEqual(self.colours()[("parent", "prod", "", "")], colors.drawn_default("#E5B100")[0])
        colors.set_default_colors(group="", host="")
        self.assertEqual(self.colours(), before)

    def test_details_header_and_picker_use_the_same_colour(self) -> None:
        from ophion.gtkui.controllers import host_actions

        colors = self.win.colors
        colors.set_default_colors(group="#E5B100", host="#8A94A6")
        colors.set_group_color("prod", "web.conf", "#112233")
        self.win.tree.rebuild()
        for select, args in ((lambda: self.win.tree.select_host("prod", "web.conf", "web-01"), ("prod", "web.conf", "web-01")),
                             (lambda: self.win.tree.select_parent_group("staging"), ("staging", "")),
                             (lambda: self.win.tree.select_host("staging", "hosts.conf", "stage-app"),
                              ("staging", "hosts.conf", "stage-app"))):
            select()
            self.win.host_page.refresh()
            expected = colors.item_color_for(*args)
            self.assertEqual(self.win.host_page.build_content().icon_color, expected, args)
            self.assertEqual(self.win.host_page.view.header._tile_icon._color, expected, args)
        self.assertEqual(colors.item_color_for("prod", "web.conf", "web-01"), "#112233")
        self.assertEqual(colors.item_color_for("staging", ""), colors.drawn_default("#E5B100")[0])
        self.assertEqual(colors.item_color_for("staging", "hosts.conf", "stage-app"), colors.drawn_default("#8A94A6")[0])

        made = []
        with mock.patch.object(host_actions, "GroupPickerDialog",
                               side_effect=lambda *a, **kw: made.append(kw) or mock.Mock(run=lambda: 0)):
            self.win.hosts.pick_group("Pick")
        self.assertEqual(made[0]["color_for"], colors.item_color_for)  # the picker's folders: the same resolver


if __name__ == "__main__":
    unittest.main()
