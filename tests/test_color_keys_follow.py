"""Tree colours follow their item: renames and moves carry the keys, deletes remove them (no orphans)."""

import unittest
from unittest import mock

from tests.gtk_window_fixture import display_available


class KeyRemapTests(unittest.TestCase):
    def remap(self, groups, hosts, *args):
        from ophion.gtkui.controllers.colors import remap_color_keys

        groups, hosts = dict(groups), dict(hosts)
        remap_color_keys(groups, hosts, *args)
        return groups, hosts

    def test_a_top_level_rename_moves_the_group_its_subgroups_and_hosts(self) -> None:
        groups, hosts = self.remap({"prod": "#111111", "prod/db": "#222222", "prodx": "#333333"},
                                   {"prod::db::a": "#444444", "prod::::b": "#555555", "prodx::y::z": "#666666"},
                                   "prod", "", "live", "")
        self.assertEqual(groups, {"live": "#111111", "live/db": "#222222", "prodx": "#333333"})
        self.assertEqual(hosts, {"live::db::a": "#444444", "live::::b": "#555555", "prodx::y::z": "#666666"})

    def test_a_subgroup_rename_or_move_carries_what_is_below_it(self) -> None:
        groups, hosts = self.remap({"prod/db": "#111111", "prod/db/replicas": "#222222", "prod/dbx": "#333333"},
                                   {"prod::db::a": "#444444", "prod::db/replicas::b": "#555555",
                                    "prod::dbx::c": "#666666"},
                                   "prod", "db", "staging", "data/db")
        self.assertEqual(groups, {"staging/data/db": "#111111", "staging/data/db/replicas": "#222222",
                                  "prod/dbx": "#333333"})
        self.assertEqual(hosts, {"staging::data/db::a": "#444444", "staging::data/db/replicas::b": "#555555",
                                 "prod::dbx::c": "#666666"})

    def test_stale_keys_at_the_new_place_are_dropped(self) -> None:
        # Left behind by the old bug: a group renamed to "live" must not inherit them.
        groups, hosts = self.remap({"prod": "#111111", "live/old": "#999999"}, {"live::old::x": "#999999"},
                                   "prod", "", "live", "")
        self.assertEqual((groups, hosts), ({"live": "#111111"}, {}))

    def test_forgetting_a_group_removes_everything_below_it(self) -> None:
        from ophion.gtkui.controllers.colors import forget_color_keys

        groups = {"prod": "#1", "prod/db": "#2", "prodx": "#3"}
        hosts = {"prod::db::a": "#4", "prodx::y::z": "#5"}
        forget_color_keys(groups, hosts, "prod", "db")
        self.assertEqual((groups, hosts), ({"prod": "#1", "prodx": "#3"}, {"prodx::y::z": "#5"}))
        forget_color_keys(groups, hosts, "prod", "")
        self.assertEqual((groups, hosts), ({"prodx": "#3"}, {"prodx::y::z": "#5"}))


@unittest.skipUnless(display_available(), "needs a display")
class WindowTests(unittest.TestCase):
    store = True

    def setUp(self) -> None:
        from tests.test_favorites_ui import ConsistencyTests

        class Helper(ConsistencyTests):
            store = self.store

            def runTest(self) -> None:  # pragma: no cover - only the helpers are used
                pass

        self.h = Helper()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.win = self.h.win
        self.colors = self.win.colors
        self.colors.set_host_color("prod", "web.conf", "web-01", "#aa3344")
        self.colors.set_group_color("prod", "web.conf", "#3344aa")
        self.colors.set_group_color("prod", "", "#44aa33")

    def color(self, parent, child, alias=""):
        own = self.colors.host_color_for(parent, child, alias) if alias else self.colors.group_color_for(parent, child)
        return own

    def saved(self):
        import json

        state = json.loads(self.win._ui_state_path.read_text())
        return state["group_colors"], state["host_colors"]

    def test_a_renamed_host_keeps_its_colour(self) -> None:
        self.h.edit("web-01", "www-01")
        self.assertEqual(self.color("prod", "web.conf", "www-01"), "#aa3344")
        self.assertEqual(self.saved()[1], {"prod::web.conf::www-01": "#aa3344"})

    def test_moved_hosts_keep_their_colour(self) -> None:
        self.h.select("web-01")
        with mock.patch.object(self.win.hosts, "pick_group", return_value=("staging", "hosts.conf", False)):
            self.win.hosts.move_selected()
        self.assertEqual(self.color("staging", "hosts.conf", "web-01"), "#aa3344")
        self.h.select("web-01", "web-02")
        with mock.patch.object(self.win.hosts, "pick_group", return_value=("prod", "db/primary.conf", False)):
            self.win.hosts.bulk_move_selected()
        self.assertEqual(self.color("prod", "db/primary.conf", "web-01"), "#aa3344")
        self.win.dnd._drop_hosts([{"parent": "prod", "child": "db/primary.conf", "alias": "web-01"}],
                                 {"type": "child", "parent": "staging", "child": "hosts.conf"})
        self.assertEqual(self.colors.host_colors, {"staging::hosts.conf::web-01": "#aa3344"})

    def test_renamed_and_moved_groups_keep_their_colours(self) -> None:
        self.win.tree.select_parent_group("prod")
        with self.h.prompt("production"):
            self.win.groups.rename_parent_group()
        self.assertEqual(self.color("production", ""), "#44aa33")
        self.assertEqual(self.color("production", "web.conf"), "#3344aa")
        self.assertEqual(self.color("production", "web.conf", "web-01"), "#aa3344")
        self.win.tree.select_child_group("production", "web.conf")
        with self.h.prompt("www.conf"):
            self.win.groups.rename_child_group()
        self.assertEqual(self.color("production", "www.conf", "web-01"), "#aa3344")
        self.win.dnd._drop_child_group([{"parent": "production", "child": "www.conf"}],
                                       {"type": "parent", "parent": "staging"})
        self.assertEqual(self.colors.group_colors, {"production": "#44aa33", "staging/www.conf": "#3344aa"})
        self.assertEqual(self.colors.host_colors, {"staging::www.conf::web-01": "#aa3344"})
        self.assertEqual(self.saved(), (self.colors.group_colors, self.colors.host_colors))

    def test_deleted_items_leave_no_colour_behind(self) -> None:
        self.h.select("web-01")
        with mock.patch.object(self.win, "_confirm", return_value=True):
            self.win.hosts.delete_selected()
            self.assertEqual(self.colors.host_colors, {})
            self.win.tree.select_child_group("prod", "web.conf")
            self.win.groups.delete_child_group()
            self.assertEqual(self.colors.group_colors, {"prod": "#44aa33"})
            self.win.tree.select_parent_group("prod")
            self.win.groups.delete_parent_group()
        self.assertEqual((self.colors.group_colors, self.colors.host_colors), ({}, {}))
        self.assertEqual(self.saved(), ({}, {}))


class FolderWindowTests(WindowTests):
    store = False


if __name__ == "__main__":
    unittest.main()
