"""Tree colours per inventory: each workspace keeps its own, and old (shared) colours are split up once."""

import json
import unittest

from tests.gtk_window_fixture import display_available


class UiStateTests(unittest.TestCase):
    def test_round_trip_and_garbage(self) -> None:
        from ophion.gtkui.controllers.ui_state import UiState

        ui = UiState.from_dict({"inventory_colors": {"/a": {"groups": {"prod": "#112233"}, "hosts": {}}},
                                "unscoped_colors": {"groups": {}, "hosts": {"p::c::h": "#445566"}}})
        self.assertEqual(ui.inventory_colors, {"/a": {"groups": {"prod": "#112233"}, "hosts": {}}})
        self.assertEqual(ui.unscoped_colors, {"groups": {}, "hosts": {"p::c::h": "#445566"}})
        again = UiState.from_dict(json.loads(json.dumps(ui.to_dict())))
        self.assertEqual((again.inventory_colors, again.unscoped_colors), (ui.inventory_colors, ui.unscoped_colors))
        for value in (None, 3, [], {"/a": "x"}, {"/a": {"groups": 5}}, {7: {}}):
            with self.subTest(value=value):
                parsed = UiState.from_dict({"inventory_colors": value}).inventory_colors
                self.assertTrue(all(set(entry) == {"groups", "hosts"} for entry in parsed.values()), parsed)
                self.assertTrue(all(isinstance(scope, str) for scope in parsed))

    def test_an_old_file_keeps_its_colours_to_split_up(self) -> None:
        from ophion.gtkui.controllers.ui_state import UiState

        ui = UiState.from_dict({"group_colors": {"prod": "#112233"}, "host_colors": {"p::c::h": "#445566"}})
        self.assertEqual(ui.inventory_colors, {})
        self.assertIsNone(ui.unscoped_colors)  # not split up yet: the window takes the flat colours at its start
        self.assertEqual(ui.to_dict()["unscoped_colors"], {"groups": {"prod": "#112233"},
                                                           "hosts": {"p::c::h": "#445566"}})


@unittest.skipUnless(display_available(), "needs a display")
class WindowTests(unittest.TestCase):
    def sandbox(self, state=None):
        from tests.gtk_window_fixture import WindowSandbox

        if state is not None:  # an existing UI state file next to the inventory, before the window opens
            from unittest import mock

            from ophion.gtkui.controllers import ui_state

            original = ui_state.load_ui_state
            patch = mock.patch("ophion.gtk_app.load_ui_state",
                               lambda path: dict(state) if path.name == ui_state.UI_STATE_FILE else original(path))
            patch.start()
            self.addCleanup(patch.stop)
        box = WindowSandbox(store=True)
        self.addCleanup(box.close)
        return box

    def test_each_inventory_keeps_its_own_colours(self) -> None:
        box = self.sandbox()
        win = box.window
        colors = win.colors
        store_path = win.manager.base_dir
        colors.set_host_color("prod", "web.conf", "web-01", "#aa3344")
        colors.set_group_color("prod", "", "#44aa33")
        win.inventory.load_workspace(box.base)  # a folder inventory with the same group and host names
        self.assertEqual(colors.item_color_for("prod", "web.conf", "web-01"), "")
        self.assertEqual(colors.group_color_for("prod", ""), "")
        colors.set_group_color("prod", "", "#3344aa")
        win.inventory.load_workspace(store_path)
        self.assertEqual(colors.host_color_for("prod", "web.conf", "web-01"), "#aa3344")
        self.assertEqual(colors.group_color_for("prod", ""), "#44aa33")
        saved = json.loads(win._ui_state_path.read_text())
        self.assertEqual(sorted(entry["groups"]["prod"] for entry in saved["inventory_colors"].values()),
                         ["#3344aa", "#44aa33"])
        # Older versions read the flat keys: they hold the active inventory's colours.
        self.assertEqual(saved["group_colors"], {"prod": "#44aa33"})
        self.assertEqual(saved["host_colors"], {"prod::web.conf::web-01": "#aa3344"})

    def test_old_colours_go_to_the_inventories_that_have_those_items(self) -> None:
        legacy = {"group_colors": {"prod": "#111111", "prod/db": "#222222", "gone": "#999999"},
                  "host_colors": {"prod::web.conf::web-01": "#333333", "gone::x::y": "#999999"}}
        box = self.sandbox(legacy)
        win = box.window
        colors = win.colors
        self.assertEqual(colors.group_colors, {"prod": "#111111", "prod/db": "#222222"})
        self.assertEqual(colors.host_colors, {"prod::web.conf::web-01": "#333333"})
        self.assertEqual(colors.item_color_for("prod", "web.conf", "web-01"), "#333333")  # unchanged on screen
        (box.base / "other").mkdir()
        (box.base / "other" / "x.conf").write_text("Host y\n    hostname y.internal\n")
        win.inventory.load_workspace(box.base)  # first time: it takes the old colours of its own items
        self.assertEqual(colors.group_colors, {"prod": "#111111", "prod/db": "#222222"})
        self.assertEqual(colors.host_colors, {"prod::web.conf::web-01": "#333333"})
        colors.set_group_color("prod", "", "#555555")  # from now on separate
        win.inventory.load_workspace(box.store.path)
        self.assertEqual(colors.group_color_for("prod", ""), "#111111")
        saved = json.loads(win._ui_state_path.read_text())
        self.assertNotIn("gone", json.dumps(saved["inventory_colors"]))

    def test_export_uses_the_active_inventorys_colours(self) -> None:
        from ophion.exporters.native import build_export

        box = self.sandbox()
        win = box.window
        win.colors.set_group_color("prod", "", "#44aa33")
        win.inventory.load_workspace(box.base)
        win.colors.set_group_color("staging", "", "#3344aa")
        win.inventory.load_workspace(box.store.path)
        doc, _report = build_export(box.store, group_colors=win.colors.group_colors,
                                    host_colors=win.colors.host_colors)
        colors = {g["name"]: g["color"].lower() for g in doc["groups"]}  # the export writes #RRGGBB
        self.assertEqual((colors["prod"], colors["staging"]), ("#44aa33", ""))


if __name__ == "__main__":
    unittest.main()
