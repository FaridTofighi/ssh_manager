"""Every dialog, with long messages: wrapping text never makes a dialog wider than its standard width.

``tests/dialog_sweep.py`` builds each dialog class and puts a 400-character text into every wrapping label.
After ``BaseDialog.fit_wrapping_labels`` (what happens before every show) a dialog may only be wider than
its declared DialogSize (+ 8 %) when its fixed content needs it (natural width == minimum width: tables,
button rows, monospace fingerprints); the table is printed when a check fails.
"""

import unittest

from tests.gtk_window_fixture import display_available


@unittest.skipUnless(display_available(), "needs a display")
class DialogSweepTests(unittest.TestCase):
    def test_wrapping_text_never_widens_a_dialog(self) -> None:
        from tests import dialog_sweep as S
        from tests.gtk_window_fixture import WindowSandbox

        from ophion.gtkui.theme import density

        self.addCleanup(density.set_active, density.active())
        sandbox = WindowSandbox(store=True)
        self.addCleanup(sandbox.close)
        for preset in (density.Density.COMFORTABLE, density.Density.COMPACT):
            density.set_active(preset)
            for long_text in (False, True):
                rows = S.sweep(sandbox.window, long_text)
                report = S.table(rows)
                with self.subTest(density=preset.value, long_text=long_text):
                    self.assertEqual([row[0] for row in rows if row[4].startswith("not built")], [], report)
                    self.assertGreaterEqual(len(rows), 34, report)
                    offenders = [name for name, nominal, opens, resizable, problem, minimum in self.with_minimum(
                        sandbox.window, rows, long_text) if problem and opens > minimum]
                    self.assertEqual(offenders, [], f"wrapping text widens these dialogs:\n{report}")

    @staticmethod
    def with_minimum(win, rows, long_text):
        """The rows with each dialog's minimum width (what its fixed content needs)."""

        import tempfile
        from pathlib import Path

        from tests import dialog_sweep as S

        result = []
        with tempfile.TemporaryDirectory() as tmp:
            makers = S.factories(win, Path(tmp))
            for name, nominal, opens, resizable, problem in rows:
                minimum = opens
                if problem:
                    dialog = makers[name]()
                    try:
                        S.measure(dialog, long_text)
                        minimum = dialog.get_preferred_width()[0]
                    finally:
                        dialog.destroy()
                result.append((name, nominal, opens, resizable, problem, minimum))
        return result


if __name__ == "__main__":
    unittest.main()
