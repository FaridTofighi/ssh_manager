"""Dialogs keep their standard width: wrapping labels wrap, and every dialog is clamped to the screen / parent.

Measured before (password dialog, message "‘Develop - 192.168.112.23’ rejected the saved password (credential
‘ftofighi’). Enter the password for ftofighi@192.168.112.23:22."): the heading's natural width was 1065 px
(831 px with Inter), so the non-resizable dialog opened 1101 px wide instead of the SMALL width (357 / 420 px):
a wrapping label without width_chars / max_width_chars asks for its unwrapped width.
"""

import unittest
from unittest import mock

from tests.gtk_window_fixture import display_available
from tests.test_local_terminal import pump

TOLERANCE = 1.08


def request(alias="Develop - 192.168.112.23", target="ftofighi@192.168.112.23:22", attempt=1,
            rejected="saved password (credential ‘ftofighi’)"):
    from ophion.auth_flow import PasswordRequest

    return PasswordRequest(alias=alias, target=target, attempt=attempt, rejected=rejected)


class DensityCase(unittest.TestCase):
    def densities(self):
        from ophion.gtkui.theme import density

        self.addCleanup(density.set_active, density.active())
        for preset in (density.Density.COMFORTABLE, density.Density.COMPACT):
            density.set_active(preset)
            yield preset.value

    @staticmethod
    def limit():
        from ophion.gtkui.dialogs.base import DialogSize, dialog_width

        return dialog_width(DialogSize.SMALL) * TOLERANCE


@unittest.skipUnless(display_available(), "needs a display")
class PasswordDialogWidthTests(DensityCase):
    def dialog(self, req=None):
        from ophion.gtkui.dialogs.password import PasswordDialog

        dialog = PasswordDialog()
        self.addCleanup(dialog.destroy)
        self.fill(dialog, req or request())
        return dialog

    @staticmethod
    def fill(dialog, req):
        """``show_for`` as the terminal calls it, answered at once (the dialog is filled, then cancelled)."""

        from gi.repository import Gtk

        host = mock.Mock(host="192.168.112.23", port=22, username="ftofighi", parent_window=None)
        with mock.patch.object(type(dialog), "run_keep", return_value=Gtk.ResponseType.CANCEL):
            dialog.show_for(host, request=req)
        dialog.get_content_area().show_all()
        dialog._error_label.set_visible(bool(dialog._error_label.get_text()))

    def test_the_reported_message_wraps_inside_the_small_width(self) -> None:
        for name in self.densities():
            with self.subTest(density=name):
                dialog = self.dialog()
                natural = dialog.get_preferred_width()[1]
                self.assertLessEqual(natural, self.limit(), f"{name}: {natural} px")
                self.assertGreaterEqual(dialog._heading.get_layout().get_line_count(), 1)

    def test_width_is_stable_across_attempts_and_pathological_names(self) -> None:
        dialog = self.dialog()
        widths, heights = [], []
        for req in (request(attempt=1), request(attempt=2, rejected="password you entered"),
                    request(attempt=3, rejected="password you entered"),
                    request(alias="h" * 300, target=("u" * 150) + "@" + ("x" * 150) + ":22", attempt=3)):
            self.fill(dialog, req)
            widths.append(dialog.get_preferred_width()[1])
            heights.append(dialog.get_preferred_height_for_width(widths[-1])[1])
        self.assertEqual(len(set(widths)), 1, widths)  # the reused dialog never jumps in width
        self.assertLessEqual(widths[0], self.limit())
        self.assertGreater(heights[-1], heights[0])  # the long names make it taller, not wider

    def test_a_large_ui_font_makes_it_taller_not_wider(self) -> None:
        from gi.repository import Gtk

        settings = Gtk.Settings.get_default()
        original = settings.get_property("gtk-font-name")
        self.addCleanup(settings.set_property, "gtk-font-name", original)
        normal = self.dialog()
        normal_width = normal.get_preferred_width()[1]
        normal_height = normal.get_preferred_height_for_width(normal_width)[1]
        settings.set_property("gtk-font-name", "Sans 20")  # far above the default 11 pt
        pump(0.1)
        large = self.dialog()
        self.assertGreater(large.get_preferred_height_for_width(normal_width)[1], normal_height)  # taller
        large.show_all()  # the form's own controls grow with the font: the clamp keeps the width
        pump(0.3)
        minimum = large.get_preferred_width()[0]  # the form's labels and entries cannot be narrower
        self.assertLessEqual(large.get_allocated_width(), max(self.limit(), minimum + 1), "20 pt font")

    def test_behaviour_is_kept(self) -> None:
        from gi.repository import Gtk

        dialog = self.dialog()
        self.assertFalse(dialog._connect_button.get_sensitive())
        dialog._password_entry.set_text("secret")
        self.assertTrue(dialog._connect_button.get_sensitive())
        self.assertEqual(dialog._error_label.get_text(), "Attempt 1 of 3")
        self.assertTrue(dialog._error_label.get_visible())
        self.assertIs(dialog._initial_focus, dialog._password_entry)
        self.assertEqual(dialog.get_default_response() if hasattr(dialog, "get_default_response") else Gtk.ResponseType.OK,
                         Gtk.ResponseType.OK)


class WrappedLabelTests(unittest.TestCase):
    @unittest.skipUnless(display_available(), "needs a display")
    def test_natural_width_is_bounded_by_max_chars(self) -> None:
        from gi.repository import Pango

        from ophion.gtkui.widgets.forms import wrapped_label
        label = wrapped_label(" ".join(["word"] * 100), chars=40, max_chars=52)
        metrics = label.get_pango_context().get_metrics(None, None)
        char = max(metrics.get_approximate_char_width(), metrics.get_approximate_digit_width()) / Pango.SCALE
        self.assertLessEqual(label.get_preferred_width()[1], 52 * char + 2)
        self.assertTrue(label.get_line_wrap())
        self.assertEqual((label.get_xalign(), label.get_selectable()), (0.0, False))


@unittest.skipUnless(display_available(), "needs a display")
class ClampTests(DensityCase):
    """BaseDialog: never wider than its standard width, 85 % of the work area, or a narrower parent (≥ 320 px)."""

    def show(self, dialog, *, work=(1366, 768), parent=None):
        from ophion.gtkui.dialogs.base import BaseDialog

        with mock.patch.object(BaseDialog, "_work_area_height", return_value=work[1]), \
                mock.patch.object(BaseDialog, "_work_area_width", return_value=work[0], create=True):
            if parent is not None:
                dialog.set_transient_for(parent)
            dialog.show_all()
            pump(0.3)
        return dialog.get_allocated_width(), dialog.get_allocated_height()

    def wide_dialog(self):
        from gi.repository import Gtk

        from ophion.gtkui.dialogs.base import BaseDialog, DialogSize

        dialog = BaseDialog(None, "Wide", size=DialogSize.SMALL, resizable=False)
        self.addCleanup(dialog.destroy)
        label = Gtk.Label(label=" ".join(["unwrapped"] * 60))  # a wrapping label without width_chars
        label.set_line_wrap(True)
        dialog.add_body(label)
        dialog.add_cancel()
        dialog.add_primary("_OK")
        return dialog

    def test_monitor_sizes(self) -> None:
        from gi.repository import Gtk

        from ophion.gtkui.dialogs.base import DialogSize, dialog_width

        for work in ((800, 600), (1024, 600), (1366, 768), (1920, 1080), (3840, 2160)):
            with self.subTest(work=work):
                dialog = self.wide_dialog()
                width, height = self.show(dialog, work=work)
                self.assertLessEqual(width, min(dialog_width(DialogSize.SMALL), work[0] * 0.85) + 1)
                self.assertLessEqual(height, work[1] * 0.85 + 1)
                for button in dialog.get_action_area().get_children():
                    if isinstance(button, Gtk.Button):
                        self.assertTrue(button.get_mapped())

    def test_a_narrow_parent_bounds_the_dialog(self) -> None:
        from gi.repository import Gtk

        parent = Gtk.Window()
        self.addCleanup(parent.destroy)
        parent.set_default_size(330, 400)
        parent.show()
        pump(0.3)
        dialog = self.wide_dialog()
        width, _height = self.show(dialog, parent=parent)
        self.assertLessEqual(width, max(320, parent.get_allocated_width()) + 1)

    def test_the_standard_width_is_kept_on_a_huge_screen(self) -> None:
        from ophion.gtkui.dialogs.base import BaseDialog, DialogSize, dialog_width

        dialog = BaseDialog(None, "Plain", size=DialogSize.MEDIUM)
        self.addCleanup(dialog.destroy)
        dialog.add_cancel()
        width, _height = self.show(dialog, work=(3840, 2160))
        self.assertEqual(width, dialog_width(DialogSize.MEDIUM))  # no stretching on large screens


if __name__ == "__main__":
    unittest.main()
