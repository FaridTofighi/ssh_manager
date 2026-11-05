"""New Host… uses the group the user selected or right-clicked; the group chooser only when there is none.

Measured before the change: with a top-level group selected (or right-clicked), prompt_new_host() opened
"Choose a Group for the New Host" listing its sub-groups, although the app store can hold hosts directly in
a top-level group (manager.add_host(parent, "", host); the tree shows them under the group).
"""

import unittest
from unittest import mock

from ophion.config import HostEntry
from tests.gtk_window_fixture import display_available


@unittest.skipUnless(display_available(), "needs a display")
class NewHostTargetTests(unittest.TestCase):
    def setUp(self) -> None:
        from tests.gtk_window_fixture import WindowSandbox

        self.sandbox = WindowSandbox(store=True)
        self.addCleanup(self.sandbox.close)
        self.win = self.sandbox.window
        self.win.tree.rebuild()

    def new_host(self, alias, *, picked=None, **kwargs):
        """Run New Host…; the picker answers ``picked``, the host dialog ``alias``. Returns what was asked."""

        from ophion.gtkui.controllers.host_actions import HostActions

        asked = {"picker": [], "dialog": []}

        def pick(_self, title, **_kw):
            asked["picker"].append(title)
            return picked

        def run(_self, dialog, parent, child):
            asked["dialog"].append((parent, child))
            dialog.destroy()
            return HostEntry(alias, {"hostname": "10.1.1.1"}), None, None

        with mock.patch.object(HostActions, "pick_group", pick), mock.patch.object(HostActions, "run_host_dialog", run):
            self.win.hosts.prompt_new_host(**kwargs)
        return asked

    def stored(self, parent, child, alias) -> bool:
        return self.win.manager.host_id(parent, child, alias) is not None

    def parent_path(self, parent):
        from ophion.gtkui.constants import RowKind, TreeColumn

        return self.win.tree.find_tree_path(
            lambda model, it: model.get_value(it, TreeColumn.KIND) == RowKind.PARENT.value
            and model.get_value(it, TreeColumn.PARENT) == parent)

    # ------------------------------------------------------------ the fix
    def test_a_selected_top_level_group_is_the_target(self) -> None:
        self.win.tree.select_parent_group("prod")
        asked = self.new_host("api-01")
        self.assertEqual(asked, {"picker": [], "dialog": [("prod", "")]})
        self.assertTrue(self.stored("prod", "", "api-01"))
        selected = self.win.tree.selected_host()
        self.assertEqual((selected.alias, selected.parent_group, selected.child_group), ("api-01", "prod", ""))
        self.assertTrue(self.win.tree_view.row_expanded(self.parent_path("prod")))
        self.assertIn("api-01", [h.alias for h in self.win.manager.tree["prod"][""].hosts])

    def test_right_click_on_an_unselected_top_level_group(self) -> None:
        from gi.repository import Gdk

        self.win.tree.select_host("prod", "web.conf", "web-01")
        view = self.win.tree_view
        path = self.parent_path("staging")
        event = mock.Mock(button=3, type=Gdk.EventType.BUTTON_PRESS, x=1, y=1)
        menus = []
        with mock.patch.object(view, "get_path_at_pos", return_value=(path, view.get_column(0), 0, 0)), \
                mock.patch.object(type(self.win.tree), "show_context_menu",
                                  lambda tree, _event=None: menus.append(tree.context_menu_spec())):
            self.win.tree.on_button_press(view, event)
        item = next(item for section in menus[0] for item in section.items if "New _Host" in item.label)
        from ophion.gtkui.controllers.host_actions import HostActions

        asked = []

        def run(_self, dialog, parent, child):
            asked.append((parent, child))
            dialog.destroy()
            return HostEntry("cache-01", {"hostname": "10.1.1.2"}), None, None

        no_chooser = AssertionError("no group chooser")
        with mock.patch.object(HostActions, "pick_group", side_effect=no_chooser), \
                mock.patch.object(type(self.win.groups), "prompt_child_destination", side_effect=no_chooser), \
                mock.patch.object(HostActions, "run_host_dialog", run):
            item.callback()
        self.assertEqual(asked, [("staging", "")])
        self.assertTrue(self.stored("staging", "", "cache-01"))

    def test_template_and_save_to_inventory_use_the_same_target(self) -> None:
        self.win.tree.select_parent_group("staging")
        self.assertEqual(self.new_host("tpl-01", template=mock.Mock(defaults={"user": "ops"}))["dialog"],
                         [("staging", "")])
        quick = HostEntry("quick", {"hostname": "10.2.2.2"})
        self.assertEqual(self.new_host("saved-01", prefill=quick), {"picker": [], "dialog": [("staging", "")]})
        self.assertTrue(self.stored("staging", "", "saved-01"))

    def test_a_shared_read_only_group_is_never_the_target(self) -> None:
        with mock.patch.object(type(self.win.tree), "infer_target_group",
                               lambda _tree: ("[Shared] corp", "web.conf")):
            asked = self.new_host("x-01", picked=None)
        self.assertEqual((len(asked["picker"]), asked["dialog"]), (1, []))  # the picker, not a doomed dialog

    # ------------------------------------------------------- unchanged
    def test_a_child_group_or_host_row_is_the_target_as_before(self) -> None:
        self.win.tree.select_child_group("prod", "web.conf")
        self.assertEqual(self.new_host("web-03"), {"picker": [], "dialog": [("prod", "web.conf")]})
        self.win.tree.select_host("staging", "hosts.conf", "stage-app")
        self.assertEqual(self.new_host("stage-2"), {"picker": [], "dialog": [("staging", "hosts.conf")]})
        self.assertTrue(self.stored("prod", "web.conf", "web-03") and self.stored("staging", "hosts.conf", "stage-2"))

    def test_nothing_selected_asks_for_a_group_and_can_create_it(self) -> None:
        self.win.tree_view.get_selection().unselect_all()
        asked = self.new_host("new-01", picked=("prod", "batch", True))
        self.assertEqual((asked["picker"], asked["dialog"]), (["Choose a Group for the New Host"], [("prod", "batch")]))
        self.assertTrue(self.stored("prod", "batch", "new-01"))
        self.win.tree_view.get_selection().unselect_all()  # the new host was selected
        self.assertEqual(self.new_host("never", picked=None)["dialog"], [])  # Cancel in the picker: nothing

    def test_a_duplicate_alias_in_the_same_group_is_refused(self) -> None:
        self.win.tree.select_child_group("prod", "web.conf")
        events = []
        with mock.patch.object(self.win, "_log_event", lambda message, ok: events.append((message, ok))):
            self.new_host("web-01")
        self.assertFalse(events[-1][1])
        self.assertIn("already exists", events[-1][0])
        self.assertEqual([h.alias for h in self.win.manager.tree["prod"]["web.conf"].hosts], ["web-01", "web-02"])

    def test_hosts_directly_in_a_top_level_group_behave_like_others(self) -> None:
        manager = self.win.manager
        ok, message = manager.add_host("prod", "", HostEntry("edge-01", {"hostname": "10.3.3.3"}))
        self.assertTrue(ok, message)
        self.win.tree.rebuild(refresh=True)
        self.win.tree.select_host("prod", "", "edge-01")
        self.assertEqual(self.win.tree.selected_host().alias, "edge-01")
        self.assertIn("prod::::edge-01", self.win.tree.host_lookup)
        self.win._sidebar.search_entry.set_text("edge")
        self.win.tree.rebuild()
        self.assertEqual([key for key in self.win.tree.host_lookup], ["prod::::edge-01"])
        self.win._sidebar.search_entry.set_text("")
        ok, message = manager.move_hosts([("prod", "", "edge-01")], "staging", "hosts.conf")
        self.assertTrue(ok, message)
        self.assertTrue(self.stored("staging", "hosts.conf", "edge-01"))
        ok, message = manager.delete_host("staging", "hosts.conf", "edge-01")
        self.assertTrue(ok, message)


@unittest.skipUnless(display_available(), "needs a display")
class FolderInventoryTests(unittest.TestCase):
    """A folder inventory keeps hosts in <group>/<child>.conf files: a top-level group still asks for one."""

    def test_top_level_group_keeps_the_choosers(self) -> None:
        from ophion.gtkui.controllers.host_actions import HostActions
        from tests.gtk_window_fixture import WindowSandbox

        sandbox = WindowSandbox()
        self.addCleanup(sandbox.close)
        win = sandbox.window
        win.tree.rebuild()
        parent = next(iter(win.manager.tree))
        win.tree.select_parent_group(parent)
        self.assertFalse(win.hosts.holds_hosts_directly(parent))
        with mock.patch.object(HostActions, "pick_group", return_value=None) as pick:
            win.hosts.prompt_new_host()
        self.assertEqual(pick.call_args.kwargs["initial_parent"], parent)
        with mock.patch.object(type(win.groups), "prompt_child_destination", return_value=None) as choose:
            win.hosts.create_host_in_current_group()
        choose.assert_called_once()


if __name__ == "__main__":
    unittest.main()
