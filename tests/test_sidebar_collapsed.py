"""The sidebar starts with every group collapsed; expansion is session-only state."""

import json
import os
import time
import unittest
from pathlib import Path
from unittest import mock

from ssh_manager.config import GroupConfig, HostEntry


def _display() -> bool:
    try:
        import gi

        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk  # type: ignore

        return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")) and Gtk.init_check(None)[0]
    except Exception:
        return False


def _pump() -> None:
    from gi.repository import Gtk

    for _ in range(200):
        if not Gtk.events_pending():
            break
        Gtk.main_iteration_do(False)


def nested_tree(depth: int = 12):
    """Acme: hosts directly, a 12-level subgroup chain, a group of only subgroups; Beta: one host."""

    def group(parent, path, hosts=()):
        return GroupConfig(parent=parent, name=path, path=Path(), file_backed=False,
                           hosts=[HostEntry(alias, {"hostname": f"10.0.0.{i + 1}"}, parent_group=parent,
                                            child_group=path) for i, alias in enumerate(hosts)])

    chain = "/".join(f"L{n}" for n in range(1, depth + 1))
    acme = {
        "": group("Acme", "", ["acme-root"]),
        "Team": group("Team", "Team"),  # only subgroups below
        "Team/Web": group("Acme", "Team/Web", ["web-01", "web-02"]),
        "Team/Same": group("Acme", "Team/Same", ["same-a"]),
        chain: group("Acme", chain, ["deep-host"]),
    }
    acme["Team"].parent = "Acme"
    beta = {"Same": group("Beta", "Same", ["same-b"])}  # same name as Acme's Team/Same, other branch
    return {"Acme": acme, "Beta": beta}


def synthetic(parents=20, groups=800, hosts=1500):
    tree = {}
    for p in range(parents):
        name = f"Acme {p:02d}"
        tree[name] = {f"team{g % 5}/site{g % 4}/role{g}": GroupConfig(parent=name, name=f"role{g}", path=Path(),
                                                                       hosts=[], file_backed=False)
                      for g in range(groups // parents)}
    slots = [(p, c) for p in tree for c in tree[p]]
    for i in range(hosts):
        p, c = slots[i % len(slots)]
        tree[p][c].hosts.append(HostEntry(f"host-{i:04d}", {"hostname": f"10.1.{i // 250}.{i % 250}"},
                                          parent_group=p, child_group=c))
    return tree


@unittest.skipUnless(_display(), "needs a display")
class TreeViewTests(unittest.TestCase):
    def setUp(self) -> None:
        from ssh_manager.gtkui.controllers.tree_controller import build_tree_rows
        from ssh_manager.gtkui.views.session_tree import SessionTreeView

        self.build = lambda tree=None: build_tree_rows([(tree or nested_tree(), False)])
        self.view = SessionTreeView()
        self.addCleanup(self.view.destroy)
        self.view.populate(self.build())

    def expanded_paths(self):
        found = []
        self.view.store.foreach(lambda model, path, it: found.append(str(path)) if self.view.row_expanded(path)
                                else False)
        return found

    def visible_texts(self):
        texts = []
        self.view.store.foreach(lambda model, path, it: texts.append(model.get_value(it, 1))
                                if self.view.is_row_visible(path) else False)
        return texts

    def test_a_fresh_view_has_nothing_expanded(self) -> None:
        self.assertEqual(self.expanded_paths(), [])
        self.assertEqual(self.view.expanded_keys, set())
        self.assertEqual(self.view.expand_calls, 0)
        self.assertEqual(self.visible_texts(), ["Acme", "Beta"])

    def test_user_expansion_survives_populate_and_new_groups_start_closed(self) -> None:
        self.view.set_row_expanded("parent", "Acme")
        self.view.set_row_expanded("child", "Acme", "Team")
        tree = nested_tree()
        tree["Gamma"] = {"New": GroupConfig(parent="Gamma", name="New", path=Path(), hosts=[], file_backed=False)}
        self.view.populate(self.build(tree))
        self.assertEqual(self.view.expanded_keys, {("parent", "Acme", ""), ("child", "Acme", "Team")})
        visible = self.visible_texts()
        self.assertIn("Web", visible)
        self.assertNotIn("web-01", visible)  # Team/Web itself was not opened
        self.assertIn("Gamma", visible)
        self.assertNotIn("New", visible)

    def test_expand_to_row_opens_ancestors_only(self) -> None:
        chain = "/".join(f"L{n}" for n in range(1, 13))
        path = self.view.expand_to_row("host", "Acme", chain, "deep-host")
        self.assertIsNotNone(path)
        self.assertTrue(self.view.is_row_visible(path))
        self.assertEqual(path.get_depth(), 14)  # Acme + 12 levels + the host
        self.assertIn(("child", "Acme", chain), self.view.expanded_keys)
        self.assertIn(("child-folder", "Acme", "L1/L2"), self.view.expanded_keys)  # intermediate folders
        self.view.set_row_collapsed("child-folder", "Acme", "L1/L2")
        self.assertNotIn(("child-folder", "Acme", "L1/L2"), self.view.expanded_keys)
        self.assertFalse(self.view.is_row_visible(path))

    def test_same_names_in_different_branches_stay_distinct(self) -> None:
        self.view.set_row_expanded("parent", "Beta")
        self.view.set_row_expanded("child", "Beta", "Same")
        visible = self.visible_texts()
        self.assertIn("same-b", visible)
        self.assertNotIn("same-a", visible)

    def test_expand_all_and_collapse_all(self) -> None:
        self.view.expand_all_rows()
        self.assertIn("deep-host", self.visible_texts())
        self.assertGreater(len(self.view.expanded_keys), 12)
        self.view.collapse_all_rows()
        self.assertEqual((self.view.expanded_keys, self.expanded_paths()), (set(), []))

    def test_reopening_a_parent_restores_its_open_subgroups(self) -> None:
        self.view.set_row_expanded("parent", "Acme")
        self.view.set_row_expanded("child", "Acme", "Team")
        self.view.set_row_collapsed("parent", "Acme")
        self.assertIn(("child", "Acme", "Team"), self.view.expanded_keys)
        self.view.set_row_expanded("parent", "Acme")
        self.assertIn("Web", self.visible_texts())

    def test_temporary_expansion_never_touches_the_user_state(self) -> None:
        self.view.set_row_expanded("parent", "Beta")
        before = set(self.view.expanded_keys)
        self.view.populate(self.build(), temporary={("parent", "Acme", ""), ("child", "Acme", "Team")})
        self.assertIn("Web", self.visible_texts())
        self.assertNotIn("same-b", self.visible_texts())
        self.view.set_row_expanded("child", "Acme", "Team/Web")  # toggled while searching
        self.assertEqual(self.view.expanded_keys, before)
        self.view.populate(self.build())
        self.assertEqual(self.view.expanded_keys, before)
        self.assertEqual(self.visible_texts(), ["Acme", "Beta", "Same"])

    def test_stale_keys_are_dropped_and_renames_carry_over(self) -> None:
        self.view.set_row_expanded("parent", "Acme")
        self.view.set_row_expanded("child", "Acme", "Team")
        self.view.set_row_expanded("child", "Acme", "Team/Web")
        tree = nested_tree()
        tree["Acme"]["Crew"] = tree["Acme"].pop("Team")
        tree["Acme"]["Crew/Web"] = tree["Acme"].pop("Team/Web")
        tree["Acme"]["Crew/Same"] = tree["Acme"].pop("Team/Same")
        self.view.remap_keys("Acme", "Acme", "Team", "Crew")
        self.view.populate(self.build(tree))
        self.assertEqual(self.view.expanded_keys,
                         {("parent", "Acme", ""), ("child", "Acme", "Crew"), ("child", "Acme", "Crew/Web")})
        del tree["Acme"]
        self.view.populate(self.build(tree))
        self.assertEqual(self.view.expanded_keys, set())  # deleted group: forgotten

    def test_drag_hover_expansion_closes_again(self) -> None:
        self.view._on_drag_motion()
        self.view.set_row_expanded("parent", "Acme")  # what GTK's hover auto-expand does
        self.assertNotIn(("parent", "Acme", ""), self.view.expanded_keys)
        self.view._on_drag_leave()
        _pump()
        self.assertEqual(self.expanded_paths(), [])
        self.view.set_row_expanded("parent", "Acme")  # a real click afterwards is remembered
        self.assertIn(("parent", "Acme", ""), self.view.expanded_keys)

    def test_accessible_name_is_unchanged(self) -> None:
        self.assertEqual(self.view.get_accessible().get_name(), "Hosts and groups")

    def test_large_inventory_first_populate_expands_nothing(self) -> None:
        from ssh_manager.gtkui.controllers.tree_controller import build_tree_rows
        from ssh_manager.gtkui.views.session_tree import SessionTreeView

        rows = build_tree_rows([(synthetic(), False)])
        self.assertEqual(sum(1 for top in rows for row in top.walk() if row.kind.value == "host"), 1500)
        view = SessionTreeView()
        self.addCleanup(view.destroy)
        with mock.patch.object(SessionTreeView, "expand_row", autospec=True,
                               side_effect=Gtk_expand_row()) as spy:
            start = time.perf_counter()
            view.populate(rows)
            elapsed = time.perf_counter() - start
        self.assertEqual(spy.call_count, 0)
        self.assertLess(elapsed, 5.0)  # generous; ~0.15 s here (was ~0.5 s with expand_all)
        self.assertEqual(view.store.iter_n_children(None), 20)


def Gtk_expand_row():
    from gi.repository import Gtk

    return lambda view, path, all_: Gtk.TreeView.expand_row(view, path, all_)


@unittest.skipUnless(_display(), "needs a display")
class WindowTests(unittest.TestCase):
    """The real window with the app-owned inventory (prod / web.conf, prod / db / primary.conf, staging)."""

    def setUp(self) -> None:
        from tests.gtk_window_fixture import WindowSandbox

        self.sandbox = WindowSandbox(store=True)
        self.addCleanup(self.sandbox.close)
        self.win = self.sandbox.window
        self.view = self.win.tree_view

    def visible(self, kind=None):
        rows = []
        self.view.store.foreach(lambda model, path, it: rows.append((model.get_value(it, 2), model.get_value(it, 1)))
                                if self.view.is_row_visible(path) else False)
        return [text for k, text in rows if kind is None or k == kind]

    def selected_text(self):
        model, paths = self.view.get_selection().get_selected_rows()
        return [model.get_value(model.get_iter(p), 1) for p in paths]

    def test_startup_shows_only_top_level_groups(self) -> None:
        self.assertEqual(self.visible(), ["prod", "staging"])
        self.assertEqual(self.view.expanded_keys, set())
        self.assertEqual(self.selected_text(), [])

    def new_window(self):
        from ssh_manager.gtk_app import SSHManagerWindow

        window = SSHManagerWindow(self.sandbox.app, self.win.manager.base_dir, self.win._settings,
                                  manager=self.win.manager)
        self.addCleanup(window.destroy)
        return window

    def test_every_launch_starts_collapsed_and_nothing_is_saved(self) -> None:
        from ssh_manager.gtkui.controllers.ui_state import ui_state_path_for_base

        self.view.expand_all_rows()
        self.win.tree.select_host("prod", "web.conf", "web-01")
        self.win._save_ui_state()
        state_path = ui_state_path_for_base(self.win.manager.base_dir)
        saved = state_path.read_text() if state_path.exists() else ""
        settings_files = [p for p in (self.sandbox.root / "config").rglob("*") if p.is_file()]
        everything = saved + "".join(p.read_text(errors="ignore") for p in settings_files)
        for needle in ("expanded_keys", "collapsed_keys", "expanded_groups", "web.conf", "db/primary"):
            self.assertNotIn(needle, everything)
        # An old file that did carry such a key is ignored.
        state = json.loads(saved) if saved else {}
        state["expanded_keys"] = [["parent", "prod", ""]]
        state["collapsed_keys"] = []
        state_path.write_text(json.dumps(state))
        window = self.new_window()
        self.assertEqual(window.tree_view.expanded_keys, set())
        texts = []
        window.tree_store.foreach(lambda model, path, it: texts.append(model.get_value(it, 1))
                                  if window.tree_view.is_row_visible(path) else False)
        self.assertEqual(texts, ["prod", "staging"])

    def test_restore_selection_never_expands(self) -> None:
        self.win.tree.select_host("prod", "web.conf", "web-01")  # a user action: revealed
        self.assertEqual(self.selected_text(), ["web-01"])
        self.view.set_row_collapsed("parent", "prod")
        self.view.get_selection().unselect_all()
        self.win.tree.restore_selection("host", {"parent": "prod", "child": "web.conf", "alias": "web-01"})
        self.win.tree.rebuild(refresh=True)
        self.assertNotIn(("parent", "prod", ""), self.view.expanded_keys)
        self.assertEqual(self.visible(), ["prod", "staging"])
        self.assertEqual(self.selected_text(), [])  # the hidden host is not selected …
        self.assertNotEqual(self.win.host_page.view.content.kind, "host")  # … and not shown

    def test_collapsing_the_selected_hosts_group_keeps_a_visible_selection(self) -> None:
        self.win.tree.select_host("prod", "web.conf", "web-01")
        self.view.set_row_collapsed("parent", "prod")  # GTK moves the cursor to the collapsed row
        self.win.tree.rebuild(refresh=True)
        self.assertEqual(self.visible(), ["prod", "staging"])
        self.assertEqual(self.selected_text(), ["prod"])

    def test_refresh_and_status_updates_keep_the_state(self) -> None:
        self.view.set_row_expanded("parent", "staging")
        keys = set(self.view.expanded_keys)
        self.win.tree.rebuild(refresh=True)
        self.win.tree.set_host_status_for_key("staging::hosts.conf::stage-app", "connected")
        self.assertEqual(self.view.expanded_keys, keys)
        self.assertIn("hosts.conf", self.visible())

    def test_creating_items_reveals_them(self) -> None:
        from gi.repository import Gtk

        with mock.patch.object(type(self.win), "_prompt_text", return_value="ops"):
            self.win.groups.create_parent_group()
        self.assertEqual(self.selected_text(), ["ops"])
        new_host = HostEntry("db-replica", {"hostname": "10.0.4.13"})
        with mock.patch.object(type(self.win.hosts), "run_host_dialog", return_value=(new_host, None, None)):
            self.win.hosts.create_host_in_group("prod", "db/primary.conf")
        self.assertEqual(self.selected_text(), ["db-replica"])
        self.assertIn("db-replica", self.visible())
        self.assertNotIn("web-01", self.visible())  # siblings stay closed
        self.assertIsInstance(self.view, Gtk.TreeView)

    def test_search_reveals_matches_and_clearing_restores(self) -> None:
        self.view.set_row_expanded("parent", "staging")
        before = set(self.view.expanded_keys)
        self.win._sidebar.search_entry.set_text("db-primary")
        _pump()
        self.win.tree.rebuild()
        self.assertIn("db-primary", self.visible("host"))
        self.assertEqual(self.view.expanded_keys, before)
        self.win._sidebar.search_entry.set_text("")
        _pump()
        self.win.tree.rebuild()
        self.assertEqual(self.view.expanded_keys, before)
        self.assertEqual(self.visible("host"), ["stage-app"] if "stage-app" in self.visible("host") else [])
        self.assertNotIn("db-primary", self.visible())
        self.assertIn("hosts.conf", self.visible())

    def test_filter_reveals_matches(self) -> None:
        self.win.tree.on_filter_text_changed("hostname:10.0.4.12")
        self.assertEqual(self.visible("host"), ["db-primary"])
        self.assertEqual(self.view.expanded_keys, set())
        self.win.tree.on_filter_text_changed("")
        self.assertEqual(self.visible(), ["prod", "staging"])

    def test_import_opens_only_the_first_level(self) -> None:
        self.win.tree.reveal_imported(["prod"])
        self.assertEqual(self.view.expanded_keys, {("parent", "prod", "")})
        self.assertEqual(self.selected_text(), ["prod"])
        visible = self.visible()
        self.assertIn("web.conf", visible)
        self.assertNotIn("web-01", visible)

    def test_rename_keeps_and_delete_drops_the_open_state(self) -> None:
        self.view.set_row_expanded("parent", "prod")
        self.win.tree.select_parent_group("prod")
        with mock.patch.object(type(self.win), "_prompt_text", return_value="production"):
            self.win.groups.rename_parent_group()
        self.assertIn(("parent", "production", ""), self.view.expanded_keys)
        self.assertNotIn(("parent", "prod", ""), self.view.expanded_keys)
        self.win.tree.select_parent_group("production")
        with mock.patch.object(type(self.win), "_confirm", return_value=True):
            self.win.groups.delete_parent_group()
        self.assertEqual(self.view.expanded_keys, set())

    def test_move_host_reveals_the_target(self) -> None:
        self.win.tree.select_host("prod", "web.conf", "web-01")
        with mock.patch.object(type(self.win.hosts), "pick_group", return_value=("staging", "hosts.conf", False)):
            self.win.hosts.move_selected()
        self.assertEqual(self.selected_text(), ["web-01"])
        self.assertIn(("child", "staging", "hosts.conf"), self.view.expanded_keys)
        self.assertNotIn("db", [k[2] for k in self.view.expanded_keys])  # nothing else was opened

    def test_expand_and_collapse_all_actions(self) -> None:
        from ssh_manager.gtkui.controllers.context_menus import blank_menu
        from ssh_manager.gtkui.menu_spec import MenuSection

        self.win.lookup_action("expand-all-groups").activate(None)
        self.assertIn("db-primary", self.visible())
        self.win.lookup_action("collapse-all-groups").activate(None)
        self.assertEqual((self.view.expanded_keys, self.visible()), (set(), ["prod", "staging"]))
        spec = blank_menu(self.win.actions, accels=self.win.tree.menu_accels())
        view_items = [section for section in spec if isinstance(section, MenuSection) and section.name == "view"][0]
        self.assertEqual([item.label for item in view_items.items], ["_Expand All Groups", "C_ollapse All Groups"])
        self.assertEqual([item.accel for item in view_items.items], ["Shift+Ctrl+O", "Shift+Ctrl+L"])
        self.assertEqual(self.win._sidebar.expand_all_button.get_action_name(), "win.expand-all-groups")


class ShortcutTests(unittest.TestCase):
    def test_no_accelerator_is_bound_twice(self) -> None:
        try:
            from ssh_manager.gtkui.controllers.shortcuts import ACTIONS
        except Exception as exc:  # pragma: no cover - no GTK
            raise unittest.SkipTest(str(exc))
        seen = {}
        for spec in ACTIONS:
            for accel in spec.accels:
                self.assertNotIn(accel.lower(), seen, f"{accel}: {spec.name} and {seen.get(accel.lower())}")
                seen[accel.lower()] = spec.name
        self.assertEqual(seen["<primary><shift>o"], "expand-all-groups")
        self.assertEqual(seen["<primary><shift>l"], "collapse-all-groups")


if __name__ == "__main__":
    unittest.main()
