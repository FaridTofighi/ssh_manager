import unittest

from ssh_manager.gtkui.pane_activation import ActivePane


class ActivePaneTests(unittest.TestCase):
    def assertOneActive(self, state: ActivePane) -> None:
        if state.panes:
            self.assertIn(state.active, state.panes)
            self.assertEqual(sum(state.is_active(p) for p in state.panes), 1)
        else:
            self.assertIsNone(state.active)

    def test_first_pane_is_active(self) -> None:
        state = ActivePane(["a", "b"])
        self.assertEqual(state.active, "a")
        self.assertOneActive(state)
        self.assertIsNone(ActivePane().active)

    def test_activate_a_b_a(self) -> None:
        state = ActivePane(["a", "b"])
        self.assertTrue(state.activate("b"))
        self.assertEqual(state.active, "b")
        self.assertTrue(state.activate("a"))
        self.assertEqual(state.active, "a")
        self.assertOneActive(state)

    def test_reactivating_the_active_pane_is_a_no_op(self) -> None:
        state = ActivePane(["a", "b"])
        self.assertFalse(state.activate("a"))
        state.activate("b")
        self.assertFalse(state.activate("b"))
        self.assertEqual(state.active, "b")

    def test_unknown_pane_is_ignored(self) -> None:
        state = ActivePane(["a"])
        self.assertFalse(state.activate("zzz"))
        self.assertEqual(state.active, "a")

    def test_added_pane_becomes_active(self) -> None:
        state = ActivePane(["a"])
        self.assertTrue(state.add("b"))
        self.assertEqual(state.active, "b")
        self.assertFalse(state.add("c", activate=False))
        self.assertEqual(state.active, "b")
        self.assertEqual(state.panes, ["a", "b", "c"])
        self.assertOneActive(state)

    def test_first_added_pane_is_active_even_without_activate(self) -> None:
        state = ActivePane()
        self.assertTrue(state.add("a", activate=False))
        self.assertEqual(state.active, "a")

    def test_closing_the_active_pane_activates_its_neighbor(self) -> None:
        state = ActivePane(["a", "b", "c"])
        state.activate("b")
        self.assertTrue(state.remove("b"))
        self.assertEqual(state.active, "c")  # the next one
        self.assertTrue(state.remove("c"))
        self.assertEqual(state.active, "a")  # last pane closed: the previous one
        self.assertTrue(state.remove("a"))
        self.assertIsNone(state.active)
        self.assertOneActive(state)

    def test_closing_another_pane_keeps_the_active_one(self) -> None:
        state = ActivePane(["a", "b", "c"])
        state.activate("c")
        self.assertFalse(state.remove("a"))
        self.assertEqual(state.active, "c")
        self.assertFalse(state.remove("zzz"))

    def test_relayout_keeps_the_active_pane(self) -> None:
        # Tile ⇄ tabs and split ⇄ tile re-create the pane list in another order.
        state = ActivePane(["a", "b", "c"])
        state.activate("b")
        self.assertFalse(state.replace(["c", "b", "a"]))
        self.assertEqual(state.active, "b")
        self.assertTrue(state.replace(["x", "y"]))  # the active pane is gone
        self.assertEqual(state.active, "x")
        self.assertTrue(state.replace([]))
        self.assertIsNone(state.active)

    def test_exactly_one_active_through_a_sequence(self) -> None:
        state = ActivePane(["a"])
        for step in (lambda: state.add("b"), lambda: state.activate("a"), lambda: state.add("c", activate=False),
                     lambda: state.remove("a"), lambda: state.activate("c"), lambda: state.replace(["c", "b", "d"]),
                     lambda: state.remove("c"), lambda: state.add("b")):
            step()
            self.assertOneActive(state)


if __name__ == "__main__":
    unittest.main()
