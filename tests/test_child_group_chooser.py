"""New Host / drop on a big top-level group: a searchable child-group chooser that never outgrows the screen."""

import time
import unittest
from unittest import mock

from tests.gtk_window_fixture import display_available

SCREEN = (1366, 768)  # a small laptop screen: the dialog must fit in 70 % of it


def pump(seconds=0.0):
    from gi.repository import Gtk  # type: ignore

    deadline = time.monotonic() + seconds
    while True:
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        if time.monotonic() >= deadline:
            return
        time.sleep(0.01)


def child_names(count: int):
    """Long, path-like names as in the user's inventory."""

    return [f"{i:04d} - pod/1 - Devops-Basic/AIFA - AI/3- Production/PENC - persian_english_name_convertor-{i}"
            for i in range(count)]


def labels_of(widget):
    from gi.repository import Gtk  # type: ignore

    found = []
    if isinstance(widget, Gtk.Label):
        found.append(widget.get_text())
    if isinstance(widget, Gtk.Container):
        for child in widget.get_children():
            found.extend(labels_of(child))
    return found


class FakeKey:
    def __init__(self, keyval):
        self.keyval = keyval


@unittest.skipUnless(display_available(), "needs a display")
class ChooserTests(unittest.TestCase):
    def chooser(self, children, **kw):
        from ophion.gtkui.dialogs.child_group_chooser import ChildGroupChooserDialog

        kw.setdefault("normalize", lambda name: name.strip() and (name.strip() if name.strip().endswith(".conf")
                                                                     else f"{name.strip()}.conf"))
        dialog = ChildGroupChooserDialog(None, "New Host — choose a group", "Choose a group.", "Top", children, **kw)
        self.addCleanup(dialog.destroy)
        dialog.show_all()
        pump(0.1)
        self.closed = []
        dialog.connect_after("response", lambda _d, response: self.closed.append(response))
        return dialog

    def accept(self, dialog) -> bool:
        from gi.repository import Gtk  # type: ignore

        before = len(self.closed)
        dialog.response(Gtk.ResponseType.OK)
        return len(self.closed) > before  # the validator stops the emission when the input is invalid

    def test_search_select_create_and_validate(self) -> None:
        from gi.repository import Gdk  # type: ignore

        names = ["alpha.conf", "beta.conf", "Gamma.conf", "gamma-two.conf"]
        dialog = self.chooser(names, validator=lambda v: "Invalid name." if "/" in v else None)
        chooser = dialog.list
        self.assertIs(dialog.get_focus(), chooser.entry)
        self.assertIsNone(chooser.selected())  # nothing pre-selected
        self.assertEqual(chooser.count.get_text(), "4 groups")
        self.assertFalse(self.accept(dialog))  # nothing chosen: stays open
        self.assertTrue(dialog.error.get_visible())
        chooser.entry.set_text("gam")
        self.assertEqual(chooser.visible_values(), ["Gamma.conf", "gamma-two.conf"])
        self.assertEqual(chooser.count.get_text(), "2 of 4 groups")
        self.assertEqual(chooser.selected(), ("gam", True))  # "Create new group “gam”" first
        chooser._on_entry_key(chooser.entry, FakeKey(Gdk.KEY_Down))
        self.assertEqual(dialog.selected(), ("Gamma.conf", False))
        chooser.entry.set_text("GAMMA.CONF")  # an exact match (any case): no "Create" row
        self.assertEqual(chooser.filter.iter_n_children(None), 1)
        self.assertEqual(dialog.selected(), ("Gamma.conf", False))
        self.assertTrue(self.accept(dialog))
        dialog2 = self.chooser(names, validator=lambda v: "Invalid name." if "/" in v else None)
        dialog2.list.entry.set_text("new-group")
        self.assertEqual(dialog2.selected(), ("new-group.conf", True))
        dialog2.list.entry.set_text("bad/name")
        self.assertFalse(self.accept(dialog2))
        self.assertEqual(dialog2.error.get_text(), "Invalid name.")
        dialog2.list.entry.set_text("beta")  # normalises to an existing group: not created again
        self.assertEqual(dialog2.selected(), ("beta.conf", False))

    def test_initial_selection_escape_and_no_children(self) -> None:
        from gi.repository import Gtk  # type: ignore

        dialog = self.chooser(["a.conf", "b.conf"], initial="b.conf")
        self.assertEqual(dialog.selected(), ("b.conf", False))
        with mock.patch.object(Gtk.Dialog, "run", return_value=Gtk.ResponseType.CANCEL):
            self.assertIsNone(dialog.ask())  # Esc / Cancel
        empty = self.chooser([])
        self.assertEqual(empty.list.count.get_text(), "0 groups")
        empty.list.entry.set_text("first")
        self.assertEqual(empty.selected(), ("first.conf", True))
        self.assertTrue(self.accept(empty))

    def test_twenty_thousand_entries(self) -> None:
        names = child_names(20000)
        started = time.monotonic()
        dialog = self.chooser(names)
        self.assertLess(time.monotonic() - started, 1.5)
        started = time.monotonic()
        dialog.list.entry.set_text("19999")
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(dialog.list.visible_values(), [names[19999]])
        _minimum, natural = dialog.get_preferred_height()
        self.assertLess(natural, SCREEN[1] * 0.7)


@unittest.skipUnless(display_available(), "needs a display")
class LargeGroupRegressionTests(unittest.TestCase):
    def make_groups(self, children, *, store_names=True):
        from gi.repository import Gtk  # type: ignore

        from ophion.gtkui.controllers.group_actions import GroupActions
        from ophion.gtkui.dialogs.prompt import prompt_text

        win = Gtk.Window()
        self.addCleanup(win.destroy)
        win.manager = mock.Mock(tree={"Fanap-infra2026": {name: object() for name in children}},
                                allows_display_names=store_names)
        win._prompt_text = lambda title, message, default="", **kw: prompt_text(win, title, message, default, **kw)
        return GroupActions(win)

    def test_new_host_on_a_group_with_1500_children_fits_the_screen(self) -> None:
        from gi.repository import Gtk  # type: ignore

        names = child_names(1500)
        groups = self.make_groups(names)
        seen = {}

        def measure(dialog):
            dialog.show_all()
            pump(0.1)
            _minimum, natural = dialog.get_preferred_height()
            seen["height"] = natural
            seen["labels"] = labels_of(dialog)
            seen["buttons"] = [b for b in dialog.get_action_area().get_children() if isinstance(b, Gtk.Button)]
            return Gtk.ResponseType.CANCEL

        with mock.patch.object(Gtk.Dialog, "run", measure):
            self.assertIsNone(groups.prompt_child_destination("Fanap-infra2026"))
        self.assertLess(seen["height"], SCREEN[1] * 0.7)
        self.assertGreaterEqual(len(seen["buttons"]), 2)  # accept and cancel are there (and on screen: see above)
        text = "\n".join(seen["labels"])
        for name in names[:50] + names[-50:]:
            self.assertNotIn(name, text)


    def test_the_title_and_message_never_list_the_groups(self) -> None:
        from ophion.gtkui.dialogs import child_group_chooser

        names = child_names(40)
        groups = self.make_groups(names)
        groups._last_child["Fanap-infra2026"] = names[7]
        seen = {}

        def capture(dialog_self):
            seen["title"], seen["selected"] = dialog_self.get_title(), dialog_self.selected()
            seen["labels"] = labels_of(dialog_self)
            return names[3], False

        with mock.patch.object(child_group_chooser.ChildGroupChooserDialog, "ask", capture):
            self.assertEqual(groups.prompt_child_destination("Fanap-infra2026", title="T"), (names[3], False))
        self.assertEqual(seen["title"], "T")
        self.assertEqual(seen["selected"], (names[7], False))  # the last choice, not the first of 40
        self.assertFalse(any(name in "\n".join(seen["labels"]) for name in names))
        self.assertEqual(groups._last_child["Fanap-infra2026"], names[3])


class CallSiteTests(unittest.TestCase):
    """New Host on a top-level group and dropping hosts on one both use the chooser."""

    def window(self):
        from ophion.gtkui.constants import RowKind

        win = mock.Mock()
        win.ensure_editable_inventory.return_value = True
        win.manager.tree = {"Top": {"a.conf": object(), "b.conf": object()}}
        win.manager.allows_display_names = False
        win.manager.create_child_group.return_value = (True, "created")
        win.manager.move_hosts.return_value = (True, "moved")
        win.tree.current_item.return_value = (RowKind.PARENT.value, {"parent": "Top"})
        return win

    def answer(self, choice):
        from ophion.gtkui.dialogs import child_group_chooser

        self.asked = []

        def ask(dialog_self):
            self.asked.append((dialog_self.get_title(), dialog_self.parent_group))
            return choice

        return mock.patch.object(child_group_chooser.ChildGroupChooserDialog, "ask", ask)

    def setUp(self) -> None:
        if not display_available():
            self.skipTest("needs a display")

    def test_new_host_in_an_existing_and_in_a_new_child_group(self) -> None:
        from ophion.gtkui.controllers.group_actions import GroupActions
        from ophion.gtkui.controllers.host_actions import HostActions

        win = self.window()
        win.groups = GroupActions(win)
        hosts = HostActions(win)
        with self.answer(("b.conf", False)), mock.patch.object(hosts, "create_host_in_group") as create:
            hosts.create_host_in_current_group()
        create.assert_called_once_with("Top", "b.conf")
        win.manager.create_child_group.assert_not_called()
        self.assertEqual(self.asked, [("New Host — choose a group", "Top")])
        with self.answer(("new.conf", True)), mock.patch.object(hosts, "create_host_in_group") as create:
            hosts.create_host_in_current_group()
        win.manager.create_child_group.assert_called_once_with("Top", "new.conf")
        create.assert_called_once_with("Top", "new.conf")
        with self.answer(None), mock.patch.object(hosts, "create_host_in_group") as create:
            hosts.create_host_in_current_group()  # cancelled
        create.assert_not_called()

    def test_dropping_hosts_on_a_top_level_group(self) -> None:
        from ophion.gtkui.controllers.drag_drop import DragDropController
        from ophion.gtkui.controllers.group_actions import GroupActions

        win = self.window()
        win.groups = GroupActions(win)
        drops = DragDropController(win)
        items = [{"parent": "Other", "child": "x.conf", "alias": "web-01"}]
        with self.answer(("new.conf", True)):
            drops._drop_hosts(items, {"type": "parent", "parent": "Top"})
        self.assertEqual(self.asked, [("Move to child group", "Top")])
        win.manager.move_hosts.assert_called_once_with([("Other", "x.conf", "web-01")], "Top", "new.conf",
                                                       create_child=True)


@unittest.skipUnless(display_available(), "needs a display")
class GroupPickerTests(unittest.TestCase):
    def picker(self, tree, **kw):
        from ophion.gtkui.dialogs.group_picker import GroupPickerDialog

        manager = mock.Mock(tree=tree, allows_display_names=False)
        dialog = GroupPickerDialog(None, manager, "Move", **kw)
        self.addCleanup(dialog.destroy)
        return dialog

    def test_small_inventories_keep_the_combo_boxes(self) -> None:
        dialog = self.picker({"P1": {"a.conf": 1, "b.conf": 1}, "P2": {"c.conf": 1}})
        self.assertIsNone(dialog._parent_list)
        self.assertIsNone(dialog._child_list)
        self.assertEqual(dialog.selected(), ("P1", "a.conf", False))
        dialog._parent_combo.set_active(1)
        self.assertEqual(dialog.selected(), ("P2", "c.conf", False))

    def test_large_ones_use_the_searchable_list_with_the_same_answers(self) -> None:
        children = {f"c{i:03d}.conf": 1 for i in range(200)}
        parents = {f"P{i:02d}": dict(children) for i in range(40)}
        dialog = self.picker(parents, initial_parent="P05", initial_child="c150.conf", allow_new_child=True)
        self.assertIsNotNone(dialog._parent_list)
        self.assertIsNotNone(dialog._child_list)
        self.assertEqual(dialog.selected(), ("P05", "c150.conf", False))
        dialog._child_list.select_value("c007.conf")
        self.assertEqual(dialog.selected(), ("P05", "c007.conf", False))
        dialog._parent_list.select_value("P39")
        self.assertEqual(dialog.selected(), ("P39", "c000.conf", False))  # like the combo: the first child
        dialog._new_child_entry.set_text("fresh.conf")
        self.assertEqual(dialog.selected(), ("P39", "fresh.conf", True))
        _minimum, natural = dialog.get_preferred_height()
        self.assertLess(natural, SCREEN[1] * 0.7)
        one_big = self.picker({"P1": dict(children), "P2": {"x.conf": 1}})  # 2 parents: combo; children: list
        self.assertIsNone(one_big._parent_list)
        self.assertEqual(one_big.selected(), ("P1", "c000.conf", False))


@unittest.skipUnless(display_available(), "needs a display")
class ScreenFitTests(unittest.TestCase):
    """No dialog may outgrow the screen: its buttons always stay inside the monitor."""

    def work_area(self, dialog):
        return dialog._work_area_height()

    def assert_fits(self, dialog):
        from gi.repository import Gtk  # type: ignore

        dialog.show_all()
        pump(0.2)
        limit = self.work_area(dialog)
        width, height = dialog.get_size()
        self.assertLessEqual(height, limit * 0.85 + 1)
        buttons = [b for b in dialog.get_action_area().get_children() if isinstance(b, Gtk.Button)]
        self.assertTrue(buttons)
        for button in buttons:
            _x, y = button.translate_coordinates(dialog, 0, 0)
            self.assertGreaterEqual(y, 0)
            self.assertLessEqual(y + button.get_allocated_height(), height)

    def test_a_dialog_with_a_huge_body_keeps_its_buttons_on_screen(self) -> None:
        from gi.repository import Gtk  # type: ignore

        from ophion.gtkui.dialogs.base import BaseDialog

        dialog = BaseDialog(None, "Huge")
        self.addCleanup(dialog.destroy)
        label = Gtk.Label(label=" ".join(["word"] * 1000))  # 5 000 characters
        label.set_line_wrap(True)
        label.set_max_width_chars(40)
        dialog.add_body(label)
        dialog.add_cancel()
        dialog.add_primary("_OK")
        self.assert_fits(dialog)
        self.assertTrue(dialog._fitted)

    def test_prompt_messages_scroll_and_are_bounded(self) -> None:
        from ophion.gtkui.dialogs.prompt import PromptDialog

        dialog = PromptDialog(None, "Long", "x" * 10000)
        self.addCleanup(dialog.destroy)
        self.assert_fits(dialog)
        text = dialog.message_scroller.get_child().get_child().get_text()
        self.assertTrue(text.endswith("(+8000 more)"))
        self.assertLessEqual(dialog.message_scroller.get_allocated_height(), 160 + 4)

    def test_short_prompt_messages_look_as_before(self) -> None:
        from ophion.gtkui.dialogs.prompt import PromptDialog

        dialog = PromptDialog(None, "Short", "Name of the new group:")
        self.addCleanup(dialog.destroy)
        dialog.show_all()
        pump(0.2)
        scroller = dialog.message_scroller
        label = scroller.get_child().get_child()
        adjustment = scroller.get_vadjustment()
        self.assertLessEqual(adjustment.get_upper(), adjustment.get_page_size() + 1)  # nothing to scroll
        self.assertLessEqual(abs(scroller.get_allocated_height() - label.get_allocated_height()), 1)
        self.assertFalse(getattr(dialog, "_fitted", False))

if __name__ == "__main__":
    unittest.main()
