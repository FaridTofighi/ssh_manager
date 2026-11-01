"""Which sessions a macro types into: ``TerminalNotebook.macro_targets`` (the broadcast rule) and its setting."""

import itertools
import json
import random
import tempfile
import unittest
from pathlib import Path


class FakeTab:
    def __init__(self, name, terminal=True):
        self.name = name
        self.is_terminal = terminal

    def __repr__(self):
        return self.name


def fake_notebook(tabs, order=None, included=None, connected=None, broadcast=True):
    from ophion.gtk_terminal import TerminalNotebook

    class Notebook:
        macro_targets = TerminalNotebook.macro_targets
        macro_unreachable = TerminalNotebook.macro_unreachable
        _in_tab_order = TerminalNotebook._in_tab_order
        _terminal_tabs = TerminalNotebook._terminal_tabs

        def _ordered_tabs(self):
            return list(order or tabs)

    notebook = Notebook()
    notebook._tabs = list(tabs)
    notebook._broadcast_mode = broadcast
    notebook._broadcast_included = dict(included or {})
    notebook._tab_status = dict(connected if connected is not None else {t: True for t in tabs})
    return notebook


class TargetResolutionTests(unittest.TestCase):
    """``TerminalNotebook.macro_targets``: the broadcast rule of typed input, the source first."""

    def setUp(self) -> None:
        try:
            import ophion.gtk_terminal  # noqa: F401
        except (ImportError, ValueError) as exc:
            self.skipTest(str(exc))
        self.a, self.b, self.c, self.d = (FakeTab(n) for n in "ABCD")
        self.files = FakeTab("files", terminal=False)

    def test_truth_table(self) -> None:
        a, b, c, d = self.a, self.b, self.c, self.d
        tabs = [a, b, c, d, self.files]
        connected = {a: True, b: True, c: True, d: False, self.files: True}
        cases = [
            # broadcast, included, source → targets
            (False, {}, a, [a]),
            (True, {}, a, [a, b, c]),  # d is not connected; the file browser never takes part
            (True, {c: False}, a, [a, b]),
            (True, {a: False}, a, [a]),  # the source is excluded: only it
            (True, {c: False}, c, [c]),
            (True, {b: False, c: False}, a, [a]),
            (True, {}, d, []),  # the source is not connected: nothing runs
            (True, {}, self.files, []),
        ]
        for broadcast, included, source, expected in cases:
            with self.subTest(broadcast=broadcast, included=included, source=source):
                notebook = fake_notebook(tabs, included=included, connected=connected, broadcast=broadcast)
                self.assertEqual(notebook.macro_targets(source), expected)
        notebook = fake_notebook(tabs, connected=connected)
        self.assertEqual(notebook.macro_unreachable(a), [d])  # included, but not connected: reported
        self.assertEqual(fake_notebook(tabs, connected=connected, broadcast=False).macro_unreachable(a), [])
        self.assertEqual(fake_notebook(tabs, connected=connected, included={a: False}).macro_unreachable(a), [])

    def test_source_first_then_tab_order_without_duplicates(self) -> None:
        a, b, c, d = self.a, self.b, self.c, self.d
        notebook = fake_notebook([a, b, c, d], order=[d, c, b, a])
        self.assertEqual(notebook.macro_targets(b), [b, d, c, a])
        self.assertEqual(len(set(notebook.macro_targets(b))), 4)
        self.assertEqual(fake_notebook([a, b], order=[]).macro_targets(b), [b, a])  # unknown order: kept last

    def test_same_sessions_as_typed_input(self) -> None:
        from ophion.gtk_terminal import broadcast_target_tabs

        rng = random.Random(7)
        tabs = [FakeTab(f"t{i}") for i in range(5)]
        for _round in range(300):
            included = {t: rng.random() < 0.6 for t in tabs if rng.random() < 0.8}
            connected = {t: rng.random() < 0.7 for t in tabs}
            source = rng.choice(tabs)
            notebook = fake_notebook(tabs, included=included, connected=connected)
            targets = notebook.macro_targets(source)
            typed = broadcast_target_tabs(tabs, source, included, connected)
            if not connected[source]:
                self.assertEqual(targets, [])
            else:
                self.assertEqual(targets[0], source)
                self.assertEqual(set(targets[1:]), set(typed))
                self.assertEqual(len(targets), len(set(targets)))
        for combo in itertools.product([True, False], repeat=2):
            notebook = fake_notebook(tabs, connected={t: True for t in tabs}, broadcast=combo[0],
                                     included={tabs[0]: combo[1]})
            self.assertEqual(notebook.macro_targets(tabs[0])[0], tabs[0])


class MacroSettingsTests(unittest.TestCase):
    def test_default_validation_and_round_trip(self) -> None:
        from ophion.settings import AppSettings, MacroSettings

        self.assertTrue(MacroSettings().confirm_multi_session)
        with self.assertLogs("ophion.settings", "WARNING"):
            self.assertTrue(MacroSettings.from_dict({"confirm_multi_session": "no"}).confirm_multi_session)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text(json.dumps({"host_key_policy": "reject"}))
            settings = AppSettings.load(path)
            self.assertTrue(settings.macros.confirm_multi_session)
            settings.macros.confirm_multi_session = False
            settings.save()
            self.assertFalse(AppSettings.load(path).macros.confirm_multi_session)
            self.assertEqual(json.loads(path.read_text())["macros"], {"confirm_multi_session": False})


if __name__ == "__main__":
    unittest.main()
