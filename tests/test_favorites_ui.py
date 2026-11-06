"""Favorites in the main window: scoping, starring, the sidebar section, the filter and inventory changes."""

import json
import unittest
from pathlib import Path
from unittest import mock

from tests.gtk_window_fixture import display_available


def pump(rounds: int = 50) -> None:
    from gi.repository import Gtk

    for _ in range(rounds):
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)


@unittest.skipUnless(display_available(), "needs a display")
class FavoritesCase(unittest.TestCase):
    store = True

    def setUp(self) -> None:
        from tests.gtk_window_fixture import WindowSandbox

        self.sandbox = WindowSandbox(store=self.store)
        self.addCleanup(self.sandbox.close)
        self.win = self.sandbox.window
        self.fav = self.win.favorites

    def host(self, alias: str):
        return next(h for groups in self.win.manager.tree.values() for g in groups.values() for h in g.hosts
                    if h.alias == alias)

    def select(self, *aliases: str) -> None:
        from ophion.gtkui.constants import RowKind, TreeColumn

        view = self.win.tree_view
        view.expand_all_rows()
        selection = view.get_selection()
        selection.unselect_all()
        model = self.win.tree_store

        def visit(_model, path, tree_iter) -> bool:
            if (model.get_value(tree_iter, TreeColumn.KIND) == RowKind.HOST.value
                    and model.get_value(tree_iter, TreeColumn.ALIAS) in aliases):
                selection.select_path(path)
            return False

        model.foreach(visit)

    def star(self, *aliases: str) -> None:
        self.select(*aliases)
        self.fav.toggle_selected()

    def starred_aliases(self):
        return [target.alias or target.child or target.parent for _key, target in self.fav.targets()]


class DataTests(FavoritesCase):
    def test_keys_are_the_store_ids(self) -> None:
        self.star("web-01")
        self.assertEqual(self.fav.keys(), [f"host:{self.host('web-01').id}"])
        self.assertEqual(self.win._ui.favorites, {self.fav.scope: self.fav.keys()})

    def test_each_inventory_keeps_its_own_list(self) -> None:
        store_path = self.win.manager.base_dir
        self.star("web-01")
        self.win.inventory.load_workspace(self.sandbox.base)  # a folder inventory with the same hosts
        self.assertEqual(self.fav.keys(), [])
        self.assertFalse(self.fav.is_favorite_host(self.host("web-01")))
        self.star("stage-app")
        self.assertEqual(self.fav.keys(), ["host:staging::hosts.conf::stage-app"])
        self.win.inventory.load_workspace(store_path)
        self.assertEqual(self.starred_aliases(), ["web-01"])
        saved = json.loads((self.win._ui_state_path).read_text())["favorites"]
        self.assertEqual(sorted(len(keys) for keys in saved.values()), [1, 1])

    def test_never_exported_and_nothing_but_the_ui_state_is_written(self) -> None:
        from ophion.exporters.native import build_export

        root = self.sandbox.root

        def snapshot():
            return {str(p): (p.stat().st_size, p.stat().st_mtime_ns) for p in root.rglob("*")
                    if p.is_file() and p.name != ".ui_state.json"}

        before = snapshot()
        with mock.patch.object(self.win._credential_store, "bind_host") as bind:
            self.star("web-01", "db-primary")
        bind.assert_not_called()
        self.assertEqual(snapshot(), before)
        doc, _report = build_export(self.sandbox.store, group_colors=self.win.colors.group_colors,
                                    host_colors=self.win.colors.host_colors)
        self.assertNotIn("favorit", json.dumps(doc).lower())

    def test_the_cap_tells_the_user(self) -> None:
        from ophion.gtkui.favorites import FAVORITES_LIMIT

        self.fav.lists.add(self.fav.scope, [f"host:other-{n}" for n in range(FAVORITES_LIMIT)])
        with mock.patch.object(self.win, "_notify") as notify:
            self.star("web-01")
        self.assertFalse(self.fav.is_favorite_host(self.host("web-01")))
        self.assertIn("100", notify.call_args[0][0])

    def test_a_deleted_host_is_dropped_and_unknown_keys_never_fail(self) -> None:
        self.star("web-01", "web-02")
        self.fav.lists.add(self.fav.scope, ["host:no-such-id", "group:nope", "garbage"])
        self.assertEqual(self.starred_aliases(), ["web-01", "web-02"])  # unknown keys are skipped
        self.sandbox.store.delete_hosts([self.host("web-02").id])
        self.win.tree.rebuild(refresh=True)
        self.assertEqual(self.fav.keys(), [f"host:{self.host('web-01').id}"])  # pruned with the rebuild


class FolderDataTests(FavoritesCase):
    store = False

    def test_folder_inventories_key_by_path(self) -> None:
        self.star("db-primary")
        self.assertEqual(self.fav.keys(), ["host:prod::db/primary.conf::db-primary"])
        self.assertEqual(self.fav.scope, str(Path(self.sandbox.base).resolve()))


if __name__ == "__main__":
    unittest.main()
