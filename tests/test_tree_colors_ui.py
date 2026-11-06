"""Preferences → Appearance → Default group / host colour, and the context menu's "Use Default Colour"."""

import unittest
from unittest import mock

from tests.gtk_window_fixture import display_available
from tests.test_local_terminal import pump


class ThemeCase(unittest.TestCase):
    def setUp(self) -> None:
        from ophion.gtkui.theme.loader import ThemeMode, get_theme_manager

        self.theme = get_theme_manager()
        self.addCleanup(self.theme.apply, self.theme.mode)
        self.addCleanup(self.theme.set_accent, self.theme.accent)
        self.theme.set_accent("default")
        self.theme.apply(ThemeMode.LIGHT)


@unittest.skipUnless(display_available(), "needs a display")
class PickerTests(ThemeCase):
    def picker(self, kind="group", value=""):
        from gi.repository import Gtk

        from ophion.gtkui.widgets.accent_picker import IconColorPicker

        changes = []
        picker = IconColorPicker(kind, value, on_change=changes.append)
        window = Gtk.OffscreenWindow()
        self.addCleanup(window.destroy)
        window.add(picker)
        window.show_all()
        pump(0.05)
        return picker, changes

    def test_swatches_names_and_choices(self) -> None:
        from ophion.gtkui.theme.loader import current_palette
        from ophion.gtkui.widgets.accent_picker import icon_presets

        picker, changes = self.picker()
        swatches = [picker.automatic, *picker.swatches.values(), picker.custom]
        self.assertEqual(len(swatches), len(icon_presets()) + 2)
        self.assertEqual(len(picker.automatic.get_group()), len(swatches))  # one radio group
        self.assertTrue(picker.automatic.get_active())
        self.assertEqual(picker.automatic.get_tooltip_text(), "Automatic (follows the theme)")
        self.assertEqual(picker.automatic.get_accessible().get_name(), "Default group colour: Automatic")
        self.assertEqual(picker.swatches["#F28C28"].get_accessible().get_name(), "Default group colour: Orange")
        self.assertEqual(picker.custom.get_accessible().get_name(), "Default group colour: Custom colour")
        self.assertEqual(picker.preview_icon._color, current_palette().icon_default)
        picker.swatches["#2F6FEB"].set_active(True)
        self.assertEqual((picker.value, changes), ("#2F6FEB", ["#2F6FEB"]))
        self.assertEqual(picker.preview_icon._color, "#2F6FEB")  # readable as it is on white: unchanged
        self.assertFalse(picker.hint.get_visible())
        picker.reset_button.clicked()
        self.assertEqual((picker.value, changes[-1]), ("", ""))
        self.assertTrue(picker.automatic.get_active())

    def test_custom_invalid_and_the_readability_hint(self) -> None:
        picker, changes = self.picker("host")
        with mock.patch.object(picker, "choose_color", return_value="#ffffe0"):
            picker.pick_custom()
        self.assertEqual((picker.value, changes), ("#FFFFE0", ["#FFFFE0"]))
        self.assertTrue(picker.hint.get_visible())  # pale yellow on white: drawn darker
        self.assertEqual(picker.hint.get_text(), "Adjusted for readability in the light theme.")
        self.assertNotEqual(picker.preview_icon._color, "#FFFFE0")
        for answer in (None, "nope"):
            with mock.patch.object(picker, "choose_color", return_value=answer):
                picker.pick_custom()
        self.assertEqual((picker.value, len(changes)), ("#FFFFE0", 1))

    def test_keyboard_order_and_activation(self) -> None:
        picker, changes = self.picker()
        swatches = [picker.automatic, *picker.swatches.values(), picker.custom]
        row = swatches[0].get_parent()  # a grid: GTK moves focus in reading order
        cells = [(row.child_get_property(s, "top-attach"), row.child_get_property(s, "left-attach")) for s in swatches]
        self.assertEqual(cells, sorted(set(cells)))  # focus order = visual order
        self.assertTrue(all(swatch.get_can_focus() for swatch in swatches))
        swatches[1].activate()
        pump(0.4)
        self.assertEqual(changes, [swatches[1].key])


@unittest.skipUnless(display_available(), "needs a display")
class PreferencesTests(ThemeCase):
    def setUp(self) -> None:
        super().setUp()
        from tests.gtk_window_fixture import WindowSandbox

        self.sandbox = WindowSandbox(store=True)
        self.addCleanup(self.sandbox.close)
        self.win = self.sandbox.window
        self.win.tree.rebuild()

    def prod_colour(self):
        from ophion.gtkui.constants import RowKind, TreeColumn

        path = self.win.tree.find_tree_path(lambda m, it: m.get_value(it, TreeColumn.KIND) == RowKind.PARENT.value
                                            and m.get_value(it, TreeColumn.PARENT) == "prod")
        store = self.win.tree_store
        return store.get_value(store.get_iter(path), TreeColumn.COLOR) or ""

    def run_settings(self, choose, response):
        from gi.repository import Gtk

        from ophion.gtkui.dialogs.preferences import PreferencesDialog

        live = []

        def run(dialog):
            choose(dialog)
            live.append(self.prod_colour())
            return response

        with mock.patch.object(PreferencesDialog, "run", run), mock.patch.object(Gtk.Dialog, "run", run):
            self.win.app_dialogs.open_settings()
        return live

    def test_live_cancel_and_ok(self) -> None:
        from gi.repository import Gtk

        pick = lambda dialog: dialog.group_color_picker.swatches["#2F6FEB"].set_active(True)  # noqa: E731
        self.assertEqual(self.run_settings(pick, Gtk.ResponseType.CANCEL), ["#2F6FEB"])  # the tree, live
        self.assertEqual((self.prod_colour(), self.win._ui.default_group_color), ("", ""))  # Cancel: undone
        self.run_settings(pick, Gtk.ResponseType.OK)
        self.assertEqual((self.prod_colour(), self.win._ui.default_group_color), ("#2F6FEB", "#2F6FEB"))


@unittest.skipUnless(display_available(), "needs a display")
class ContextMenuTests(ThemeCase):
    def setUp(self) -> None:
        super().setUp()
        from tests.gtk_window_fixture import WindowSandbox

        self.sandbox = WindowSandbox(store=True)
        self.addCleanup(self.sandbox.close)
        self.win = self.sandbox.window
        self.win.tree.rebuild()

    def labels(self):
        return [item.label for section in self.win.tree.context_menu_spec() for item in section.items]

    def test_use_default_colour_removes_only_the_own_colour(self) -> None:
        colors = self.win.colors
        colors.set_group_color("prod", "", "#AA0000")
        colors.set_host_color("prod", "web.conf", "web-01", "#0000AA")
        self.win.tree.rebuild()
        self.win.tree.select_host("prod", "web.conf", "web-01")
        self.assertIn("Use Default Colour", self.labels())
        self.assertNotIn("Clear Host Color", self.labels())
        colors.clear_item_color()
        self.assertEqual(colors.item_color_for("prod", "web.conf", "web-01"), "#AA0000")  # the group's again
        self.win.tree.select_parent_group("prod")
        self.assertIn("Use Default Colour", self.labels())
        colors.clear_item_color()
        self.assertEqual(colors.group_colors, {})

    def test_the_colour_dialog_starts_from_the_effective_colour(self) -> None:
        from gi.repository import Gtk

        colors = self.win.colors
        colors.set_group_color("prod", "", "#AA0000")
        self.win.tree.rebuild()
        self.win.tree.select_child_group("prod", "web.conf")  # no own colour: inherits #AA0000
        shown = []

        class Chooser:
            def __init__(self, **_kw):
                pass

            def set_rgba(self, rgba):
                shown.append(rgba.to_string())

            def run(self):
                return Gtk.ResponseType.CANCEL

            def destroy(self):
                pass

        with mock.patch.object(Gtk, "ColorChooserDialog", Chooser):
            colors.change_item_color()
        self.assertEqual(shown, ["rgb(170,0,0)"])


if __name__ == "__main__":
    unittest.main()
