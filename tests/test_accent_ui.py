"""Preferences → Appearance → Accent colour: the picker, live preview, Cancel / OK, widgets that follow it."""

import unittest
from unittest import mock

from tests.gtk_window_fixture import display_available
from tests.test_local_terminal import pump


class AccentCase(unittest.TestCase):
    def setUp(self) -> None:
        from ophion.gtkui.theme.loader import get_theme_manager

        self.theme = get_theme_manager()
        self.addCleanup(self.theme.apply, self.theme.mode)
        self.addCleanup(self.theme.set_accent, self.theme.accent)
        self.theme.set_accent("default")

    def offscreen(self, widget):
        from gi.repository import Gtk

        window = Gtk.OffscreenWindow()
        self.addCleanup(window.destroy)
        window.get_style_context().add_class("sm-dialog")
        window.add(widget)
        window.show_all()
        pump(0.05)
        return window


@unittest.skipUnless(display_available(), "needs a display")
class PickerTests(AccentCase):
    def picker(self, value="default"):
        from ophion.gtkui.widgets.accent_picker import AccentPicker

        changes = []
        picker = AccentPicker(value, on_change=changes.append)
        self.offscreen(picker)
        return picker, changes

    def test_radio_group_names_and_selection(self) -> None:
        picker, changes = self.picker()
        swatches = list(picker.swatches.values()) + [picker.custom]
        self.assertEqual(len(swatches), 11)
        self.assertEqual(len(swatches[0].get_group()), 11)  # one radio group: exactly one is checked
        self.assertTrue(picker.swatches["default"].get_active())
        self.assertEqual(picker.swatches["default"].get_accessible().get_name(), "Green accent")
        self.assertEqual(picker.custom.get_accessible().get_name(), "Custom accent colour")
        self.assertEqual(picker.swatches["blue"].get_tooltip_text(), "Blue accent")
        picker.swatches["blue"].set_active(True)
        self.assertEqual((picker.value, changes), ("blue", ["blue"]))
        self.assertFalse(picker.swatches["default"].get_active())
        picker.reset_button.clicked()
        self.assertEqual((picker.value, changes[-1]), ("default", "default"))
        self.assertTrue(picker.swatches["default"].get_active())

    def test_keyboard_order_and_activation(self) -> None:
        """Arrow keys / Tab follow the row (GTK moves focus; an offscreen window never has it); Space selects."""

        picker, changes = self.picker()
        swatches = list(picker.swatches.values()) + [picker.custom]
        self.assertTrue(all(swatch.get_can_focus() for swatch in swatches))
        row = swatches[0].get_parent()
        self.assertEqual(row.get_children(), swatches)  # the focus order is the visual order
        picker.swatches["teal"].activate()  # Space / Enter on a focused swatch
        pump(0.4)  # a button activated from the keyboard clicks after its press animation
        self.assertEqual((picker.value, changes), ("teal", ["teal"]))

    def test_custom_colour(self) -> None:
        picker, changes = self.picker()
        with mock.patch.object(picker, "choose_color", return_value="#1a2B3c"):
            picker.pick_custom()
        self.assertEqual((picker.value, changes), ("#1A2B3C", ["#1A2B3C"]))
        self.assertTrue(picker.custom.get_active())
        for answer in (None, "not a colour"):  # cancelled or invalid: the previous choice stays
            with mock.patch.object(picker, "choose_color", return_value=answer):
                picker.pick_custom()
            self.assertEqual(picker.value, "#1A2B3C")
        self.assertEqual(len(changes), 1)
        picker.swatches["red"].set_active(True)
        picker.custom.set_active(True)  # back to the last custom colour (arrow keys), no chooser
        self.assertEqual(picker.value, "#1A2B3C")

    def test_broadcast_hint_for_violet_only(self) -> None:
        picker, _changes = self.picker()
        self.assertFalse(picker.hint.get_visible())
        picker.swatches["purple"].set_active(True)
        self.assertTrue(picker.hint.get_visible())
        self.assertIn("Broadcast indicator", picker.hint.get_text())
        picker.swatches["default"].set_active(True)
        self.assertFalse(picker.hint.get_visible())


@unittest.skipUnless(display_available(), "needs a display")
class PreferencesTests(AccentCase):
    def setUp(self) -> None:
        super().setUp()
        from tests.gtk_window_fixture import WindowSandbox

        self.sandbox = WindowSandbox(store=True)
        self.addCleanup(self.sandbox.close)
        self.win = self.sandbox.window

    def run_settings(self, choose, response):
        from gi.repository import Gtk

        from ophion.gtkui.dialogs.preferences import PreferencesDialog
        from ophion.gtkui.theme.loader import current_palette

        seen = []

        def run(dialog):
            choose(dialog.accent_picker)
            seen.append(current_palette().accent)  # applied live, before OK
            return response

        with mock.patch.object(PreferencesDialog, "run", run), mock.patch.object(Gtk.Dialog, "run", run):
            self.win.app_dialogs.open_settings()
        return seen

    def test_live_preview_cancel_restores_ok_saves(self) -> None:
        from gi.repository import Gtk

        from ophion.gtkui.theme.loader import current_palette
        from ophion.gtkui.theme.tokens import palette_for

        default = current_palette().accent
        seen = self.run_settings(lambda picker: picker.swatches["orange"].set_active(True), Gtk.ResponseType.CANCEL)
        self.assertNotEqual(seen[0], default)
        self.assertEqual(current_palette().accent, default)  # Cancel: back to what it was
        self.assertEqual(self.win._ui.accent_color, "default")
        self.run_settings(lambda picker: picker.swatches["orange"].set_active(True), Gtk.ResponseType.OK)
        self.assertEqual(self.win._ui.accent_color, "orange")
        self.assertEqual(current_palette().accent, palette_for(self.theme.is_dark, "orange").accent)


@unittest.skipUnless(display_available(), "needs a display")
class WidgetTests(AccentCase):
    def colour(self, widget, prop="background-color"):
        ctx = widget.get_style_context()
        return ctx.get_property(prop, ctx.get_state()).to_string()

    @staticmethod
    def rgba(color):
        from gi.repository import Gdk

        value = Gdk.RGBA()
        value.parse(color)
        return value.to_string()

    def test_focused_pane_and_checked_toggle_follow_the_accent(self) -> None:
        from gi.repository import Gtk

        from ophion.gtkui.theme.loader import current_palette
        from ophion.gtkui.widgets.pane_frame import PaneFrame

        frame = PaneFrame("web-01 · pane 1", on_zoom=lambda: None, on_close=lambda: None)
        frame.set_body(Gtk.Label(label="terminal"))
        toggle = Gtk.ToggleButton(label="Sidebar")
        box = Gtk.Box()
        box.pack_start(frame, True, True, 0)
        box.pack_start(toggle, False, False, 0)
        window = self.offscreen(box)
        window.get_style_context().add_class("sm-window")
        frame.set_focused(True)
        toggle.set_active(True)
        for value in ("blue", "amber", "default"):
            self.theme.set_accent(value)
            pump(0.6)  # CSS transitions
            palette = current_palette()
            self.assertEqual(self.colour(frame.box, "border-top-color"), self.rgba(palette.pane_border_focus),
                             value)
            self.assertEqual(self.colour(toggle), self.rgba(palette.accent_tint), value)

    def test_tree_selection_reads_the_palette(self) -> None:
        import cairo

        from ophion.gtkui.theme.loader import current_palette
        from ophion.gtkui.views.session_tree import SessionTreeView  # noqa: F401  (drawn with current_palette)

        self.theme.set_accent("red")
        palette = current_palette()
        surface = cairo.ImageSurface(cairo.FORMAT_RGB24, 4, 4)
        cr = cairo.Context(surface)
        from ophion.gtkui.views import session_tree

        renderer = next(value for value in vars(session_tree).values()
                        if isinstance(value, type) and hasattr(value, "do_render") and hasattr(value, "_channels"))
        cr.set_source_rgba(*renderer._channels(palette.bg_selected))
        cr.paint()
        data = surface.get_data()
        self.assertEqual("#{:02X}{:02X}{:02X}".format(data[2], data[1], data[0]), palette.bg_selected)


if __name__ == "__main__":
    unittest.main()
