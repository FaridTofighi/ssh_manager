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
        from ssh_manager.gtkui.dialogs.child_group_chooser import ChildGroupChooserDialog

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


if __name__ == "__main__":
    unittest.main()
