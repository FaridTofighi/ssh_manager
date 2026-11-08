"""Favorites in the main window: scoping, starring, the sidebar section, the filter and inventory changes."""

import json
import os
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
        self.addCleanup(lambda: self.sandbox.close())  # the sandbox a test ended with
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



def favorite_item(spec):
    """The Favorites entry of a context-menu spec (and the name of its section)."""

    for section in spec:
        for item in section.items:
            if "Fa_vorites" in item.label:
                return section.name, item
    return None, None


def key_event(widget, keyval, state=0):
    from gi.repository import Gdk

    event = Gdk.Event.new(Gdk.EventType.KEY_PRESS)
    event.key.keyval = keyval
    event.key.state = state
    event.key.window = widget.get_window()
    return event  # not event.key: the struct lives in the event


class StarActionTests(FavoritesCase):
    def test_host_menu_item_toggles_and_relabels(self) -> None:
        from ophion.gtkui.constants import Icons

        self.select("web-01")
        section, item = favorite_item(self.win.tree.context_menu_spec())
        self.assertEqual((section, item.label, item.icon, item.accel), ("organize", "Add to Fa_vorites", Icons.STAR,
                                                                        "Ctrl+D"))
        organize = next(s for s in self.win.tree.context_menu_spec() if s.name == "organize")
        self.assertIs(organize.items[0], next(i for i in organize.items if "Fa_vorites" in i.label))  # first there
        item.callback()
        self.assertTrue(self.fav.is_favorite_host(self.host("web-01")))
        _section, item = favorite_item(self.win.tree.context_menu_spec())
        self.assertEqual((item.label, item.icon), ("Remove from Fa_vorites", Icons.STAR_FILLED))
        item.callback()
        self.assertEqual(self.fav.keys(), [])

    def test_group_menus_have_it_too(self) -> None:
        for select in (lambda: self.win.tree.select_parent_group("prod"),
                       lambda: self.win.tree.select_child_group("prod", "web.conf")):
            select()
            section, item = favorite_item(self.win.tree.context_menu_spec())
            self.assertEqual((section, item.sensitive), ("organize", True))
            item.callback()
        self.assertEqual(self.starred_aliases(), ["prod", "web.conf"])

    def test_multi_selection_stars_all_then_removes_all(self) -> None:
        self.star("web-02")
        self.select("web-01", "web-02")
        _section, item = favorite_item(self.win.tree.context_menu_spec())
        self.assertEqual(item.label, "Add to Fa_vorites")  # not all of them are favorites yet
        item.callback()
        self.assertEqual(sorted(self.starred_aliases()), ["web-01", "web-02"])
        self.select("web-01", "web-02")
        _section, item = favorite_item(self.win.tree.context_menu_spec())
        self.assertEqual(item.label, "Remove from Fa_vorites")
        item.callback()
        self.assertEqual(self.fav.keys(), [])

    def test_shared_inventory_rows_cannot_be_favorites(self) -> None:
        from ophion.config import ConfigManager

        self.win.inventory.shared_manager = ConfigManager(self.sandbox.base)
        self.win.tree.rebuild()
        self.win.tree.select_parent_group("[Shared] prod")
        _section, item = favorite_item(self.win.tree.context_menu_spec())
        self.assertFalse(item.sensitive)
        self.assertTrue(item.tooltip)
        self.win.actions.toggle_favorite()
        self.assertEqual(self.fav.keys(), [])

    def test_ctrl_d_only_in_the_tree(self) -> None:
        from gi.repository import Gdk

        from ophion.gtkui.controllers.shortcuts import ACTION_BY_NAME

        spec = ACTION_BY_NAME["toggle-favorite"]
        self.assertEqual((spec.accels, spec.display_accels), ((), ("<Primary>d",)))
        self.assertEqual(self.win.get_application().get_actions_for_accel("<Primary>d"), [])  # never a global key:
        # in a terminal Ctrl+D is end-of-input and must reach the shell
        self.select("web-01")
        view = self.win.tree_view
        self.assertTrue(self.win.tree.on_key_press(view, key_event(view, Gdk.KEY_d, Gdk.ModifierType.CONTROL_MASK)))
        self.assertTrue(self.fav.is_favorite_host(self.host("web-01")))
        self.assertFalse(self.win.tree.on_key_press(view, key_event(view, Gdk.KEY_d)))  # plain d: type-ahead
        self.win.tree.on_key_press(view, key_event(view, Gdk.KEY_D, Gdk.ModifierType.CONTROL_MASK))
        self.assertEqual(self.fav.keys(), [])

    def test_header_star_follows_the_selection(self) -> None:
        from gi.repository import Atk

        header = self.win.host_page.view.header
        star = header.favorite_button
        self.select("web-01")
        self.win.host_page.refresh()
        self.assertTrue(star.get_visible())
        self.assertFalse(star.get_active())
        self.assertIn("Add to Favorites", star.get_tooltip_text())
        self.assertIn("Ctrl+D", star.get_tooltip_text())
        star.clicked()
        self.assertTrue(self.fav.is_favorite_host(self.host("web-01")))
        self.assertTrue(star.get_active())
        self.assertIn("Remove from Favorites", star.get_tooltip_text())
        states = star.get_accessible().ref_state_set()
        self.assertTrue(states.contains_state(Atk.StateType.PRESSED) or states.contains_state(Atk.StateType.CHECKED))
        self.win.tree.select_parent_group("staging")
        self.win.host_page.refresh()
        self.assertTrue(star.get_visible())  # groups have it too
        self.assertFalse(star.get_active())
        self.select("web-01", "web-02")
        self.win.host_page.refresh()
        self.assertFalse(star.get_visible())  # not for a multi-selection



# Allocations of the sidebar's parts in a 1200x800 window before the Favorites section existed (7a80c3a),
# taken with tools/run_gtk_tests.py (private X server, 96 dpi): (x, y, width, height) relative to the
# sidebar. With no favorites the sidebar must look exactly like this. Other displays (DPI, fonts) give
# other numbers, so it is only compared under the runner, which says so in OPHION_TEST_DISPLAY.
GOLDEN = {
    "comfortable": {"header": (24, 29, 235, 52), "search": (24, 103, 234, 46), "stack": (0, 149, 283, 584),
                    "tree": (0, 169, 283, 564), "footer": (0, 733, 283, 67)},
    "compact": {"header": (12, 0, 259, 40), "search": (12, 40, 259, 32), "stack": (0, 72, 283, 696),
                "tree": (0, 78, 283, 690), "footer": (0, 768, 283, 32)},
}
GOLDEN_DISPLAY = "private-96dpi"


class SectionCase(FavoritesCase):
    def show(self, width: int = 1200, height: int = 800, density=None) -> None:
        from gi.repository import Gtk

        if density is not None:
            self.win.set_density(density)
        self.win.resize(width, height)
        self.win.show_all()
        Gtk.Window.show(self.win)
        self.settle()

    def settle(self) -> None:
        for _ in range(3):  # a layout pass now: nothing draws frames for windows nobody sees
            pump()
            self.win.check_resize()
        pump()

    @property
    def section(self):
        return self.win._sidebar.favorites

    def rows(self):
        from ophion.gtkui.constants import TreeColumn

        model = self.section.list.get_model()
        return [(model[i][TreeColumn.KIND], model[i][TreeColumn.TEXT]) for i in range(len(model))]

    def parts(self):
        sidebar = self.win._sidebar
        found = {}
        for name, widget in (("header", sidebar._header), ("search", sidebar._search_row), ("stack", sidebar.stack),
                             ("tree", sidebar._scroller), ("footer", sidebar._footer)):
            x, y = widget.translate_coordinates(sidebar, 0, 0)
            found[name] = (x, y, widget.get_allocated_width(), widget.get_allocated_height())
        return found


class SectionVisibilityTests(SectionCase):
    def test_absent_without_favorites_and_the_layout_is_unchanged(self) -> None:
        from tests.gtk_window_fixture import WindowSandbox

        from ophion.gtkui.theme import density

        for preset in (density.Density.COMFORTABLE, density.Density.COMPACT):
            with self.subTest(density=preset.value):
                # A fresh window per density, as the golden was taken (a switch at runtime settles later).
                self.sandbox.close()
                self.sandbox = WindowSandbox(store=True)
                self.win = self.sandbox.window
                self.show(density=preset)
                self.assertFalse(self.section.get_visible())
                self.assertFalse(self.section.get_mapped())
                self.assertEqual(self.section.get_allocated_height(), 1)  # never allocated: no height
                with_section = self.parts()
                sidebar = self.win._sidebar
                position = sidebar.get_children().index(self.section)
                sidebar.remove(self.section)  # the sidebar as it was before the section existed
                self.settle()
                self.assertEqual(self.parts(), with_section)
                sidebar.pack_start(self.section, False, False, 0)
                sidebar.reorder_child(self.section, position)
                if os.environ.get("OPHION_TEST_DISPLAY") == GOLDEN_DISPLAY:
                    self.assertEqual(with_section, GOLDEN[preset.value])

    def test_appears_with_one_and_goes_with_the_last(self) -> None:
        self.show()
        self.star("web-01")
        self.settle()
        self.assertTrue(self.section.get_mapped())
        self.assertEqual(self.rows(), [("host", "web-01")])
        self.assertEqual(self.section.count_label.get_text(), "1")
        self.assertEqual(self.section.header.get_accessible().get_name(), "Favorites, 1 item, expanded")
        self.star("web-01")
        self.settle()
        self.assertFalse(self.section.get_visible())

    def test_hidden_while_searching_or_filtering(self) -> None:
        self.show()
        self.star("web-01")
        sidebar = self.win._sidebar
        sidebar.search_entry.set_text("web")
        self.win.tree.rebuild()  # the search entry's own signal rebuilds after a delay
        self.assertFalse(self.section.get_visible())
        sidebar.search_entry.set_text("")
        self.win.tree.rebuild()
        self.assertTrue(self.section.get_visible())
        sidebar.filter_entry.set_text("tag:prod")
        self.assertFalse(self.section.get_visible())
        sidebar.filter_entry.set_text("")
        self.assertTrue(self.section.get_visible())

    def test_collapsed_state_survives_a_restart(self) -> None:
        from ophion.gtk_app import SSHManagerWindow
        from ophion.settings import AppSettings

        self.show()
        self.star("web-01")
        self.section.header.clicked()
        self.assertFalse(self.section.expanded)
        self.assertFalse(self.section.scroller.get_visible())
        self.assertEqual(self.section.header.get_accessible().get_name(), "Favorites, 1 item, collapsed")
        again = SSHManagerWindow(self.sandbox.app, self.win.manager.base_dir, AppSettings.load(),
                                 manager=self.win.manager)
        try:
            self.assertFalse(again._sidebar.favorites.expanded)
            self.assertEqual(again.favorites.keys(), self.fav.keys())
        finally:
            again.destroy()


class SectionBehaviourTests(SectionCase):
    def test_rows_show_name_colour_and_status(self) -> None:
        from ophion.gtkui.constants import TreeColumn

        self.show()
        self.star("web-01")
        self.win.tree.select_parent_group("staging")
        self.fav.toggle_selected()
        self.win.colors.set_host_color("prod", "web.conf", "web-01", "#aa3344")
        self.win.tree.rebuild()
        model = self.section.list.get_model()
        self.assertEqual(self.rows(), [("host", "web-01"), ("parent", "staging")])
        self.assertEqual(model[0][TreeColumn.COLOR], "#aa3344")
        self.assertEqual(model[1][TreeColumn.COUNT], 1)
        self.assertIn("prod › web.conf", model[0][TreeColumn.TOOLTIP])
        self.win.tree.set_host_status(self.host("web-01"), "connected")  # the tree's own event
        self.assertEqual(model[0][TreeColumn.STATUS], "connected")
        self.assertIn("Connected", model[0][TreeColumn.TOOLTIP])

    def test_a_click_selects_and_reveals_in_the_tree(self) -> None:
        from gi.repository import Gtk

        self.show()
        self.star("db-primary")
        self.win.tree_view.collapse_all_rows()
        self.win.tree_view.get_selection().unselect_all()
        self.section.list.set_cursor(Gtk.TreePath.new_from_indices([0]), None, False)
        self.assertEqual(self.win.tree.selected_host().alias, "db-primary")
        path = self.win.tree.host_iters and self.fav.tree_path(self.fav.targets()[0][1])
        self.assertTrue(self.win.tree_view.is_row_visible(path))  # its collapsed groups were opened
        self.assertEqual(self.section.list.selected_index(), 0)
        self.win.tree.select_parent_group("prod")  # the tree stays the one selection
        self.assertIsNone(self.section.list.selected_index())

    def test_enter_connects_and_a_group_opens(self) -> None:
        from gi.repository import Gtk

        self.show()
        self.star("web-01")
        self.win.tree.select_child_group("prod", "web.conf")
        self.fav.toggle_selected()
        self.win.tree_view.collapse_all_rows()
        column = self.section.list.get_column(0)
        with mock.patch.object(self.win.sessions, "open_hosts") as open_hosts:
            self.section.list.row_activated(Gtk.TreePath.new_from_indices([0]), column)
        open_hosts.assert_called_once()
        self.assertEqual([h.alias for h in open_hosts.call_args[0][0]], ["web-01"])
        self.win.tree_view.collapse_all_rows()
        self.section.list.row_activated(Gtk.TreePath.new_from_indices([1]), column)
        group_path = self.fav.tree_path(self.fav.targets()[1][1])
        self.assertTrue(self.win.tree_view.row_expanded(group_path))
        self.assertEqual(self.win.tree.current_item()[1]["child"], "web.conf")

    def test_the_context_menu_is_the_tree_rows_menu(self) -> None:
        from gi.repository import Gtk

        from ophion.gtkui.controllers import favorites as F

        self.show()
        self.star("web-01")
        tree_labels = [[i.label for i in s.items] for s in self.win.tree.context_menu_spec()]
        self.win.tree_view.get_selection().unselect_all()
        self.section.list.set_cursor(Gtk.TreePath.new_from_indices([0]), None, False)
        shown = []
        # A stand-in menu: emitted here, the popup has no key press behind it ("no trigger event" warning).
        with mock.patch("ophion.gtkui.widgets.menus.build_context_menu",
                        side_effect=lambda spec, **kw: shown.append(spec) or mock.Mock()):
            self.section.list.emit("popup-menu")
        self.assertEqual([[i.label for i in s.items] for s in shown[0]], tree_labels)
        self.assertTrue(F)

    def test_row_heights_match_the_tree(self) -> None:
        from gi.repository import Gtk

        from ophion.gtkui.theme import density

        for preset in (density.Density.COMFORTABLE, density.Density.COMPACT):
            with self.subTest(density=preset.value):
                self.show(density=preset)
                if not self.fav.keys():
                    self.star("web-01")
                    self.win.tree.select_parent_group("staging")
                    self.fav.toggle_selected()
                self.settle()
                self.win.tree.select_host("prod", "web.conf", "web-01")
                tree_path = self.fav.tree_path(self.fav.targets()[0][1])
                tree_row = self.win.tree_view.get_background_area(tree_path, None).height
                for index in range(2):  # a host and a group
                    area = self.section.list.get_background_area(Gtk.TreePath.new_from_indices([index]), None)
                    self.assertLessEqual(area.height, tree_row)
                self.assertEqual(self.section.header.get_allocated_height(), self.section.row_height())

    def test_thirty_favorites_scroll_inside_the_section(self) -> None:
        from ophion.gtkui.theme import density
        from ophion.gtkui.views.favorites import MAX_ROWS
        from ophion.inventory.store import NewHost

        group = self.sandbox.store.create_group(None, "many")
        for n in range(30):
            self.sandbox.store.add_host(NewHost(group.id, f"many-{n:02d}", f"10.9.0.{n}", 22, "u"))
        self.win.tree.rebuild(refresh=True)
        self.star(*[f"many-{n:02d}" for n in range(30)])
        for preset in (density.Density.COMFORTABLE, density.Density.COMPACT):
            with self.subTest(density=preset.value):
                self.show(density=preset)
                rows = MAX_ROWS[preset is density.Density.COMPACT]
                self.assertEqual(self.section.count, 30)
                self.assertEqual(self.section.scroller.get_allocated_height(), rows * self.section.row_height())
                adjustment = self.section.scroller.get_vadjustment()
                self.assertGreater(adjustment.get_upper(), adjustment.get_page_size())  # it scrolls

    def test_a_short_window_keeps_the_tree_usable(self) -> None:
        from ophion.gtkui.views.favorites import MIN_TREE_ROWS
        from ophion.gtkui.views.session_tree import row_height_for
        from ophion.inventory.store import NewHost

        group = self.sandbox.store.create_group(None, "many")
        for n in range(30):
            self.sandbox.store.add_host(NewHost(group.id, f"many-{n:02d}", f"10.9.0.{n}", 22, "u"))
        self.win.tree.rebuild(refresh=True)
        self.star(*[f"many-{n:02d}" for n in range(30)])
        for height in (500, 420):
            with self.subTest(height=height):
                self.show(700, height)
                minimum = MIN_TREE_ROWS * row_height_for(self.win.tree_view)
                self.assertGreaterEqual(self.win._sidebar.stack.get_allocated_height(), minimum)
                self.assertTrue(self.section.get_mapped())  # the header stays
        self.show(1200, 800)
        self.assertGreater(self.section.scroller.get_allocated_height(), 0)  # room again: the rows come back


class FilterTests(SectionCase):
    def tree_hosts(self):
        from ophion.gtkui.constants import RowKind, TreeColumn

        found = []
        self.win.tree_store.foreach(lambda m, _p, it: found.append(m[it][TreeColumn.ALIAS])
                                    if m[it][TreeColumn.KIND] == RowKind.HOST.value else None)
        return sorted(found)

    def test_is_favorite_and_with_another_term(self) -> None:
        self.star("web-01", "db-primary")
        self.win._sidebar.filter_entry.set_text("is:favorite")
        self.assertEqual(self.tree_hosts(), ["db-primary", "web-01"])
        self.win._sidebar.filter_entry.set_text("is:favorite AND tag:prod")
        self.assertEqual(self.tree_hosts(), ["web-01"])
        self.win._sidebar.filter_entry.set_text("is:fav")
        self.assertEqual(self.tree_hosts(), ["db-primary", "web-01"])
        self.win._sidebar.filter_entry.set_text("")
        self.assertEqual(len(self.tree_hosts()), 4)

    def test_documented_in_the_filter_help(self) -> None:
        from ophion.gtkui.views.sidebar import FILTER_HELP

        self.assertIn("is:favorite", FILTER_HELP)


class SectionPerformanceTests(SectionCase):
    def test_starring_never_rebuilds_and_stays_fast(self) -> None:
        import time

        from ophion.inventory.store import NewHost

        store = self.sandbox.store
        groups = [store.create_group(None, f"g{n}") for n in range(12)]
        store.add_hosts_bulk(NewHost(groups[n % 12].id, f"bulk-{n:04d}", f"10.{n // 256}.{n % 256}.1", 22, "u")
                             for n in range(1200))
        self.win.tree.rebuild(refresh=True)
        ids = [h.id for g in self.win.manager.tree.values() for c in g.values() for h in c.hosts][:50]
        self.fav.lists.add(self.fav.scope, [f"host:{i}" for i in ids])
        startup = []
        for _attempt in range(3):  # what a rebuild (and so the start) adds: the lookup, prune and section
            started = time.perf_counter()
            self.fav.inventory_changed()
            startup.append(time.perf_counter() - started)
        self.select("web-01")
        refresh = []
        with mock.patch.object(self.win.tree, "rebuild", wraps=self.win.tree.rebuild) as rebuild:
            for _attempt in range(3):
                started = time.perf_counter()
                self.fav.toggle_selected()
                refresh.append(time.perf_counter() - started)
            self.assertEqual(rebuild.call_count, 0)
        print(f"\n[favorites] 1200 hosts, 50 favorites: rebuild adds {min(startup) * 1000:.1f} ms, "
              f"star/unstar {min(refresh) * 1000:.1f} ms")
        self.assertLess(min(startup), 0.005, startup)
        self.assertLess(min(refresh), 0.05, refresh)


class SmokeTests(SectionCase):
    def test_themes_rtl_and_keyboard(self) -> None:
        from gi.repository import Gtk

        from ophion.gtkui.theme.loader import ThemeMode, get_theme_manager

        self.show()
        self.star("web-01", "web-02")
        for mode in (ThemeMode.LIGHT, ThemeMode.DARK):
            get_theme_manager().apply(mode)
            self.settle()
            self.assertTrue(self.section.get_mapped())
        from gi.repository import Gdk

        focus_in = Gdk.Event.new(Gdk.EventType.FOCUS_CHANGE)  # an active window, as for a user at the keyboard
        focus_in.focus_change.in_, focus_in.focus_change.send_event = 1, 1
        focus_in.focus_change.window = self.win.get_window()
        self.win.event(focus_in)
        order = []
        self.win._sidebar.search_entry.grab_focus()
        for _ in range(4):
            self.win.emit("move-focus", Gtk.DirectionType.TAB_FORWARD)
            order.append(self.win.get_focus())
        self.assertIn(self.section.header, order)
        self.assertLess(order.index(self.section.header), order.index(self.section.list))
        self.assertLess(order.index(self.section.list), order.index(self.win.tree_view)
                        if self.win.tree_view in order else len(order))
        self.assertEqual(self.section.list.get_accessible().get_name(), "Favorites")

    def test_right_to_left(self) -> None:
        from gi.repository import Gtk

        from tests.gtk_window_fixture import WindowSandbox

        self.sandbox.close()
        Gtk.Widget.set_default_direction(Gtk.TextDirection.RTL)
        self.addCleanup(Gtk.Widget.set_default_direction, Gtk.TextDirection.LTR)
        self.sandbox = WindowSandbox(store=True)
        self.win, self.fav = self.sandbox.window, self.sandbox.window.favorites
        self.show()
        self.star("web-01")
        self.settle()
        header = self.section.header
        chevron_x = self.section._chevron.translate_coordinates(header, 0, 0)[0]
        count_x = self.section.count_label.translate_coordinates(header, 0, 0)[0]
        self.assertGreater(chevron_x, header.get_allocated_width() // 2)  # mirrored: the chevron at the right
        self.assertLess(count_x, chevron_x)



class ConsistencyTests(FavoritesCase):
    """Favorites follow renames and moves and lose deleted items (app store: ids; folder inventory: paths)."""

    def edit(self, alias: str, new_alias: str) -> None:
        import dataclasses

        self.select(alias)
        host = self.host(alias)
        updated = dataclasses.replace(host, alias=new_alias, options=dict(host.options), metadata=dict(host.metadata))
        hosts = self.win.hosts
        with mock.patch.object(hosts, "host_dialog"), \
                mock.patch.object(hosts, "run_host_dialog", return_value=(updated, None, None)), \
                mock.patch.object(self.win.creds, "check_secrets_policy", return_value=True), \
                mock.patch.object(self.win.creds, "apply_host_credentials"):
            hosts.edit_selected()

    def prompt(self, answer: str):
        return mock.patch.object(self.win, "_prompt_text", return_value=answer)

    def test_a_renamed_host_stays_a_favorite(self) -> None:
        self.star("web-01")
        self.edit("web-01", "www-01")
        self.assertEqual(self.starred_aliases(), ["www-01"])

    def test_moved_hosts_stay_favorites(self) -> None:
        self.star("web-01", "web-02")
        self.select("web-01")
        with mock.patch.object(self.win.hosts, "pick_group", return_value=("staging", "hosts.conf", False)):
            self.win.hosts.move_selected()  # the Move dialog
        self.select("web-01", "web-02")
        with mock.patch.object(self.win.hosts, "pick_group", return_value=("prod", "db/primary.conf", False)):
            self.win.hosts.bulk_move_selected()  # several at once
        self.win.dnd._drop_hosts([{"parent": "prod", "child": "db/primary.conf", "alias": "web-02"}],
                                 {"type": "child", "parent": "staging", "child": "hosts.conf"})  # a drag
        self.assertEqual(sorted(self.starred_aliases()), ["web-01", "web-02"])
        self.assertEqual({(t.alias, t.parent, t.child) for _k, t in self.fav.targets()},
                         {("web-01", "prod", "db/primary.conf"), ("web-02", "staging", "hosts.conf")})

    def test_renamed_and_moved_groups_stay_favorites_with_their_hosts(self) -> None:
        self.star("web-01")
        self.win.tree.select_parent_group("prod")
        self.fav.toggle_selected()
        self.win.tree.select_child_group("prod", "web.conf")
        self.fav.toggle_selected()
        self.win.tree.select_parent_group("prod")
        with self.prompt("production"):
            self.win.groups.rename_parent_group()
        self.win.tree.select_child_group("production", "web.conf")
        with self.prompt("www.conf"):
            self.win.groups.rename_child_group()
        self.assertEqual(self.starred_aliases(), ["web-01", "production", "www.conf"])
        self.win.dnd._drop_child_group([{"parent": "production", "child": "www.conf"}],
                                       {"type": "parent", "parent": "staging"})
        self.assertEqual([(t.kind.value, t.parent, t.child) for _k, t in self.fav.targets()],
                         [("host", "staging", "www.conf"), ("parent", "production", ""), ("child", "staging", "www.conf")])

    def test_deleted_items_go_and_a_duplicate_is_not_a_favorite(self) -> None:
        self.star("web-01", "db-primary")
        self.win.tree.select_parent_group("staging")
        self.fav.toggle_selected()
        self.select("web-01")
        dialog = mock.Mock()
        clone = None

        def run(dialog_, parent, child):
            import dataclasses

            host = self.host("web-01")
            return dataclasses.replace(host, alias="web-01-copy", id="", options=dict(host.options),
                                       metadata=dict(host.metadata)), None, None

        with mock.patch.object(self.win.hosts, "host_dialog", return_value=dialog), \
                mock.patch.object(self.win.hosts, "run_host_dialog", side_effect=run), \
                mock.patch.object(self.win.creds, "check_secrets_policy", return_value=True), \
                mock.patch.object(self.win.creds, "apply_host_credentials"):
            self.win.hosts.duplicate_selected()
        self.assertIsNone(clone)
        self.assertFalse(self.fav.is_favorite_host(self.host("web-01-copy")))
        self.select("db-primary")
        with mock.patch.object(self.win, "_confirm", return_value=True):
            self.win.hosts.delete_selected()
            self.win.tree.select_parent_group("staging")
            self.win.groups.delete_parent_group()
        self.assertEqual(self.starred_aliases(), ["web-01"])
        self.assertEqual(len(self.fav.keys()), 1)  # pruned, not only hidden

    def test_replacing_the_inventory_drops_what_is_gone(self) -> None:
        self.star("web-01", "stage-app")
        manager = self.win.manager
        if self.store:
            self.sandbox.store.delete_hosts([h.id for g in manager.tree.values() for c in g.values() for h in c.hosts
                                            if h.alias == "stage-app"])
        else:
            (self.sandbox.base / "staging" / "hosts.conf").write_text("")
        self.win.inventory.refresh()  # what an import (replace) or a reload ends with
        self.assertEqual(self.starred_aliases(), ["web-01"])
        self.assertEqual(len(self.fav.keys()), 1)


class FolderConsistencyTests(ConsistencyTests):
    store = False


if __name__ == "__main__":
    unittest.main()
