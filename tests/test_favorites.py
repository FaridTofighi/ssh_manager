"""Favorites data layer (GTK-free): keys, order, the cap, per-inventory scopes, tolerant loading, sync."""

import json
import tempfile
import unittest
from pathlib import Path

from ophion.gtkui.controllers.ui_state import UiState, load_ui_state, save_ui_state
from ophion.gtkui.favorites import (
    FAVORITES_LIMIT, Favorites, group_key, group_path, host_key, scope_for,
)


class KeyTests(unittest.TestCase):
    def test_hosts_and_groups_never_share_a_key(self) -> None:
        self.assertEqual(host_key("3f2a"), "host:3f2a")
        self.assertEqual(group_key("3f2a"), "group:3f2a")
        self.assertEqual(group_path("prod", ""), "prod::")
        self.assertEqual(group_path("prod", "db/primary"), "prod::db/primary::")
        self.assertEqual(host_key("prod::db::web-01"), "host:prod::db::web-01")

    def test_scope_is_the_resolved_inventory_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            self.assertEqual(scope_for(base / "a" / ".." / "inv"), str((base / "inv").resolve()))


class ListTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = {}
        self.fav = Favorites(self.state)

    def test_add_remove_toggle_keep_the_starring_order(self) -> None:
        self.assertEqual(self.fav.add("s", ["host:b", "host:a", "host:b"]), (["host:b", "host:a"], 0))
        self.assertEqual(self.fav.add("s", ["host:a", "group:x"]), (["group:x"], 0))
        self.assertEqual(self.fav.keys("s"), ["host:b", "host:a", "group:x"])
        self.assertTrue(self.fav.contains("s", "host:a"))
        self.fav.remove("s", ["host:a", "host:missing"])
        self.assertEqual(self.fav.keys("s"), ["host:b", "group:x"])
        self.assertTrue(self.fav.toggle("s", ["host:c"]))  # none in: all added
        self.assertTrue(self.fav.toggle("s", ["host:b", "host:d"]))  # some in: the rest added
        self.assertEqual(self.fav.keys("s"), ["host:b", "group:x", "host:c", "host:d"])
        self.assertFalse(self.fav.toggle("s", ["host:b", "host:d"]))  # all in: all removed
        self.assertEqual(self.fav.keys("s"), ["group:x", "host:c"])
        self.assertEqual(self.state, {"s": ["group:x", "host:c"]})  # the UI state's own dict is edited

    def test_an_empty_scope_leaves_no_entry(self) -> None:
        self.fav.add("s", ["host:a"])
        self.fav.remove("s", ["host:a"])
        self.assertEqual(self.state, {})

    def test_the_cap_refuses_extras(self) -> None:
        keys = [f"host:{n}" for n in range(FAVORITES_LIMIT + 5)]
        added, refused = self.fav.add("s", keys)
        self.assertEqual((len(added), refused), (FAVORITES_LIMIT, 5))
        self.assertEqual(self.fav.add("s", ["host:new"]), ([], 1))
        self.assertEqual(len(self.fav.keys("s")), FAVORITES_LIMIT)

    def test_scopes_are_separate(self) -> None:
        self.fav.add("inv-a", ["host:1"])
        self.fav.add("inv-b", ["host:2"])
        self.assertEqual(self.fav.keys("inv-a"), ["host:1"])
        self.assertEqual(self.fav.keys("inv-b"), ["host:2"])
        self.assertFalse(self.fav.contains("inv-b", "host:1"))

    def test_rename_and_prefix_remap_follow_path_keys(self) -> None:
        self.fav.add("s", ["host:p::db::web", "host:p::db::api", "group:p::db::", "group:p::", "host:q::x::y",
                           "group:p::dbx::"])
        self.assertTrue(self.fav.rename("s", "host:p::db::web", "host:p::db::www"))
        self.assertFalse(self.fav.rename("s", "host:none", "host:other"))
        self.fav.remap_prefix("s", "p::db::", "p::data::")
        self.assertEqual(self.fav.keys("s"), ["host:p::data::www", "host:p::data::api", "group:p::data::",
                                              "group:p::", "host:q::x::y", "group:p::dbx::"])
        self.fav.remap_prefix("s", "p::", "prod::")  # a top-level rename moves the group itself too
        self.assertEqual(self.fav.keys("s")[:4], ["host:prod::data::www", "host:prod::data::api",
                                                  "group:prod::data::", "group:prod::"])

    def test_a_rename_onto_an_existing_favorite_keeps_one(self) -> None:
        self.fav.add("s", ["host:a", "host:b"])
        self.fav.rename("s", "host:a", "host:b")
        self.assertEqual(self.fav.keys("s"), ["host:b"])

    def test_prune_drops_unknown_keys(self) -> None:
        self.fav.add("s", ["host:1", "host:2", "group:9"])
        self.assertTrue(self.fav.prune("s", {"host:2", "group:9", "host:other"}))
        self.assertEqual(self.fav.keys("s"), ["host:2", "group:9"])
        self.assertFalse(self.fav.prune("s", {"host:2", "group:9"}))


class UiStateTests(unittest.TestCase):
    def test_old_files_load_with_no_favorites_and_the_section_open(self) -> None:
        ui = UiState.from_dict({"sidebar_width": 300, "group_colors": {"a": "#112233"}})
        self.assertEqual(ui.favorites, {})
        self.assertTrue(ui.favorites_expanded)

    def test_garbage_values_load_as_empty(self) -> None:
        for value in (None, 3, "x", ["host:a"], {"s": "host:a"}, {"s": [1, None, ""]}, {5: ["host:a"]}):
            with self.subTest(value=value):
                self.assertEqual(UiState.from_dict({"favorites": value}).favorites, {})
        self.assertTrue(UiState.from_dict({"favorites_expanded": "no"}).favorites_expanded)

    def test_loading_drops_duplicates_and_keeps_the_cap(self) -> None:
        keys = ["host:a", "host:a", 7, "host:b"] + [f"host:{n}" for n in range(200)]
        ui = UiState.from_dict({"favorites": {"s": keys, "t": []}})
        self.assertEqual(ui.favorites["s"][:2], ["host:a", "host:b"])
        self.assertEqual(len(ui.favorites["s"]), FAVORITES_LIMIT)
        self.assertNotIn("t", ui.favorites)

    def test_round_trip_through_the_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".ui_state.json"
            ui = UiState()
            ui.favorites = {"/inv": ["host:1", "group:2"]}
            ui.favorites_expanded = False
            self.assertTrue(save_ui_state(path, ui.to_dict()))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            again = UiState.from_dict(load_ui_state(path))
            self.assertEqual((again.favorites, again.favorites_expanded), ({"/inv": ["host:1", "group:2"]}, False))
            self.assertEqual(json.loads(path.read_text())["favorites"], {"/inv": ["host:1", "group:2"]})


if __name__ == "__main__":
    unittest.main()
