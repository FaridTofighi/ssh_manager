import json
import unittest
import tempfile
from pathlib import Path
from unittest import mock

from ssh_manager.config import HostEntry
from ssh_manager.gtk_app import (
    Gdk,
    GdkPixbuf,
    Gtk,
    SSHManagerWindow,
    UI_STATE_FILE,
    audit_log_key,
    audit_log_value,
    ui_state_path_for_base,
)


class GtkAppHelperTests(unittest.TestCase):
    def test_ui_state_path_uses_parent_for_file_base(self) -> None:
        base = Path("/tmp/example/config")
        self.assertEqual(ui_state_path_for_base(base), base / UI_STATE_FILE)

        with tempfile.TemporaryDirectory() as tmpdir:
            file_base = Path(tmpdir) / "config"
            file_base.write_text("Host app\n", encoding="utf-8")
            self.assertEqual(ui_state_path_for_base(file_base), Path(tmpdir) / UI_STATE_FILE)

    def test_load_ui_state_ignores_wrong_json_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / UI_STATE_FILE
            state_path.write_text("[]", encoding="utf-8")

            dummy = type("DummyWindow", (), {"_ui_state_path": state_path})()

            self.assertEqual(SSHManagerWindow._load_ui_state(dummy), {})

    def test_audit_log_fields_are_tokenized(self) -> None:
        self.assertEqual(audit_log_value("app\nroot user"), "app_root_user")
        self.assertEqual(audit_log_value("", default="unknown"), "unknown")
        self.assertEqual(audit_log_key("Changed Fields!"), "changed_fields")

    def test_connection_success_uses_application_notification(self) -> None:
        class FakeApp:
            def __init__(self) -> None:
                self.notifications = []

            def send_notification(self, notification_id, notification) -> None:
                self.notifications.append((notification_id, notification))

        app = FakeApp()
        inline_calls = []
        dummy = type(
            "DummyWindow",
            (),
            {
                "get_application": lambda self: app,
                "_host_key": lambda self, host: "prod::web::app",
                "_log_event": lambda self, message, success: inline_calls.append((message, success)),
            },
        )()

        SSHManagerWindow._notify_connection_success(dummy, HostEntry(alias="app", parent_group="prod", child_group="web"))

        self.assertEqual(len(app.notifications), 1)
        self.assertEqual(app.notifications[0][0], "ssh-manager-connected-prod::web::app")
        self.assertEqual(inline_calls, [])

    def test_item_color_prefers_host_override_then_child_then_parent(self) -> None:
        dummy = type(
            "DummyWindow",
            (),
            {
                "_group_colors": {"prod": "#111111", "prod/web": "#222222"},
                "_host_colors": {"prod::web::app": "#333333"},
                "_make_host_key": SSHManagerWindow._make_host_key,
                "_host_color_for": SSHManagerWindow._host_color_for,
                "_group_color_for": SSHManagerWindow._group_color_for,
                "_resolved_group_color_for": SSHManagerWindow._resolved_group_color_for,
            },
        )()

        self.assertEqual(SSHManagerWindow._item_color_for(dummy, "prod", "web", "app"), "#333333")
        self.assertEqual(SSHManagerWindow._item_color_for(dummy, "prod", "web", "db"), "#222222")
        self.assertEqual(SSHManagerWindow._item_color_for(dummy, "prod", "api", "worker"), "#111111")

    def test_change_color_on_host_updates_host_color_store(self) -> None:
        calls = []

        class FakeDialog:
            title = ""

            def __init__(self, title, transient_for) -> None:
                self.__class__.title = title

            def run(self):
                return Gtk.ResponseType.OK

            def get_rgba(self):
                return object()

            def destroy(self) -> None:
                calls.append(("destroy",))

        dummy = type(
            "DummyWindow",
            (),
            {
                "_current_item": lambda self: (
                    "host",
                    {"parent": "prod", "child": "web", "alias": "app"},
                ),
                "_item_color_for": lambda self, parent, child, alias="": "",
                "_rgba_to_hex": lambda self, rgba: "#445566",
                "_set_host_color": lambda self, parent, child, alias, color: calls.append(
                    ("host", parent, child, alias, color)
                ),
                "_set_group_color": lambda self, parent, child, color: calls.append(
                    ("group", parent, child, color)
                ),
                "_color_pixbuf_cache": {},
                "_rebuild_tree": lambda self, refresh=False: calls.append(("rebuild", refresh)),
            },
        )()

        with mock.patch("ssh_manager.gtk_app.Gtk.ColorChooserDialog", FakeDialog):
            SSHManagerWindow._change_group_color(dummy)

        self.assertEqual(FakeDialog.title, "Choose Host Color")
        self.assertIn(("host", "prod", "web", "app", "#445566"), calls)
        self.assertNotIn(("group", "prod", "web", "#445566"), calls)
        self.assertIn(("rebuild", False), calls)

    def test_clear_color_on_host_updates_host_color_store(self) -> None:
        calls = []
        dummy = type(
            "DummyWindow",
            (),
            {
                "_current_item": lambda self: (
                    "host",
                    {"parent": "prod", "child": "web", "alias": "app"},
                ),
                "_set_host_color": lambda self, parent, child, alias, color: calls.append(
                    ("host", parent, child, alias, color)
                ),
                "_set_group_color": lambda self, parent, child, color: calls.append(
                    ("group", parent, child, color)
                ),
                "_color_pixbuf_cache": {"#111111": object()},
                "_rebuild_tree": lambda self, refresh=False: calls.append(("rebuild", refresh)),
            },
        )()

        SSHManagerWindow._clear_group_color(dummy)

        self.assertEqual(dummy._color_pixbuf_cache, {})
        self.assertIn(("host", "prod", "web", "app", None), calls)
        self.assertNotIn(("group", "prod", "web", None), calls)
        self.assertIn(("rebuild", False), calls)

    def test_host_status_uses_separate_status_color_column(self) -> None:
        updates = []

        class FakeTreeStore:
            def set_value(self, tree_iter, column, value) -> None:
                updates.append((tree_iter, column, value))

        dummy = type(
            "DummyWindow",
            (),
            {
                "_host_status": {},
                "_host_iters": {"prod::web::app": "iter-1"},
                "tree_store": FakeTreeStore(),
                "_status_color": SSHManagerWindow._status_color,
                "COLUMN_STATUS_COLOR": SSHManagerWindow.COLUMN_STATUS_COLOR,
            },
        )()

        SSHManagerWindow._set_host_status_for_key(dummy, "prod::web::app", "connected")

        self.assertEqual(dummy._host_status["prod::web::app"], "connected")
        self.assertEqual(updates, [("iter-1", SSHManagerWindow.COLUMN_STATUS_COLOR, "#22c55e")])

    def test_render_icon_uses_app_drawn_pixbuf_when_row_has_color(self) -> None:
        class FakeCell:
            def __init__(self) -> None:
                self.props = {}

            def set_property(self, name, value) -> None:
                self.props[name] = value

        class FakeModel:
            def get_value(self, _iter, column):
                if column == SSHManagerWindow.COLUMN_ICON:
                    return SSHManagerWindow.ICON_HOST
                if column == SSHManagerWindow.COLUMN_COLOR:
                    return "#112233"
                return ""

        pixbuf = object()
        dummy = type(
            "DummyWindow",
            (),
            {
                "COLUMN_ICON": SSHManagerWindow.COLUMN_ICON,
                "COLUMN_COLOR": SSHManagerWindow.COLUMN_COLOR,
                "_tree_icon_pixbuf": lambda self, icon_name, color: pixbuf
                if (icon_name, color) == (SSHManagerWindow.ICON_HOST, "#112233")
                else None,
            },
        )()

        cell = FakeCell()
        SSHManagerWindow._render_icon(dummy, None, cell, FakeModel(), object(), None)

        self.assertTrue(cell.props["visible"])
        self.assertIs(cell.props["pixbuf"], pixbuf)
        self.assertIsNone(cell.props["icon-name"])

    def test_render_icon_falls_back_to_icon_name_without_app_pixbuf(self) -> None:
        class FakeCell:
            def __init__(self) -> None:
                self.props = {}

            def set_property(self, name, value) -> None:
                self.props[name] = value

        class FakeModel:
            def get_value(self, _iter, column):
                if column == SSHManagerWindow.COLUMN_ICON:
                    return SSHManagerWindow.ICON_HOST
                if column == SSHManagerWindow.COLUMN_COLOR:
                    return "#112233"
                return ""

        dummy = type(
            "DummyWindow",
            (),
            {
                "COLUMN_ICON": SSHManagerWindow.COLUMN_ICON,
                "COLUMN_COLOR": SSHManagerWindow.COLUMN_COLOR,
                "_tree_icon_pixbuf": lambda self, icon_name, color: None,
            },
        )()

        cell = FakeCell()
        SSHManagerWindow._render_icon(dummy, None, cell, FakeModel(), object(), None)

        self.assertTrue(cell.props["visible"])
        self.assertIsNone(cell.props["pixbuf"])
        self.assertEqual(cell.props["icon-name"], SSHManagerWindow.ICON_HOST)

    def test_tree_icon_pixbuf_draws_default_icon_without_custom_color(self) -> None:
        dummy = type(
            "DummyWindow",
            (),
            {
                "_color_pixbuf_cache": {},
                "_default_tree_icon_color": SSHManagerWindow._default_tree_icon_color,
                "_draw_colored_tree_icon_pixbuf": SSHManagerWindow._draw_colored_tree_icon_pixbuf,
                "_pixbuf_has_visible_pixels": SSHManagerWindow._pixbuf_has_visible_pixels,
                "_scale_rgb": SSHManagerWindow._scale_rgb,
                "_blend_rgb": SSHManagerWindow._blend_rgb,
                "_paint_rect": SSHManagerWindow._paint_rect,
                "ICON_GROUP": SSHManagerWindow.ICON_GROUP,
                "ICON_SUBGROUP": SSHManagerWindow.ICON_SUBGROUP,
                "ICON_HOST": SSHManagerWindow.ICON_HOST,
            },
        )()

        pixbuf = SSHManagerWindow._tree_icon_pixbuf(dummy, SSHManagerWindow.ICON_GROUP, "")

        self.assertIsNotNone(pixbuf)
        self.assertTrue(SSHManagerWindow._pixbuf_has_visible_pixels(dummy, pixbuf))

    def test_draw_colored_tree_icon_pixbuf_builds_visible_folder_icon(self) -> None:
        rgba = Gdk.RGBA()
        self.assertTrue(rgba.parse("#112233"))
        dummy = type(
            "DummyWindow",
            (),
            {
                "_scale_rgb": SSHManagerWindow._scale_rgb,
                "_blend_rgb": SSHManagerWindow._blend_rgb,
                "_paint_rect": SSHManagerWindow._paint_rect,
                "ICON_GROUP": SSHManagerWindow.ICON_GROUP,
                "ICON_SUBGROUP": SSHManagerWindow.ICON_SUBGROUP,
                "ICON_HOST": SSHManagerWindow.ICON_HOST,
            },
        )()

        pixbuf = SSHManagerWindow._draw_colored_tree_icon_pixbuf(dummy, SSHManagerWindow.ICON_SUBGROUP, rgba)

        self.assertTrue(SSHManagerWindow._pixbuf_has_visible_pixels(object(), pixbuf))
        pixels = bytes(pixbuf.get_pixels())
        self.assertIn(bytes([17, 34, 51, 255]), [pixels[i : i + 4] for i in range(0, len(pixels), 4)])

    def test_draw_colored_tree_icon_pixbuf_builds_visible_host_icon(self) -> None:
        rgba = Gdk.RGBA()
        self.assertTrue(rgba.parse("#112233"))
        dummy = type(
            "DummyWindow",
            (),
            {
                "_scale_rgb": SSHManagerWindow._scale_rgb,
                "_blend_rgb": SSHManagerWindow._blend_rgb,
                "_paint_rect": SSHManagerWindow._paint_rect,
                "ICON_GROUP": SSHManagerWindow.ICON_GROUP,
                "ICON_SUBGROUP": SSHManagerWindow.ICON_SUBGROUP,
                "ICON_HOST": SSHManagerWindow.ICON_HOST,
            },
        )()

        pixbuf = SSHManagerWindow._draw_colored_tree_icon_pixbuf(
            dummy,
            SSHManagerWindow.ICON_HOST,
            rgba,
        )

        self.assertTrue(SSHManagerWindow._pixbuf_has_visible_pixels(object(), pixbuf))
        pixels = bytes(pixbuf.get_pixels())
        self.assertIn(bytes([17, 34, 51, 255]), [pixels[i : i + 4] for i in range(0, len(pixels), 4)])

    def test_pixbuf_visible_check_rejects_fully_transparent_icons(self) -> None:
        transparent = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, True, 8, 2, 2)
        transparent.fill(0x11223300)
        visible = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, True, 8, 2, 2)
        visible.fill(0x11223344)
        no_alpha = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8, 2, 2)
        no_alpha.fill(0x112233ff)

        self.assertFalse(SSHManagerWindow._pixbuf_has_visible_pixels(object(), transparent))
        self.assertTrue(SSHManagerWindow._pixbuf_has_visible_pixels(object(), visible))
        self.assertTrue(SSHManagerWindow._pixbuf_has_visible_pixels(object(), no_alpha))

    def test_render_text_clears_status_foreground_when_no_status_color(self) -> None:
        class FakeCell:
            def __init__(self) -> None:
                self.props = {}

            def set_property(self, name, value) -> None:
                self.props[name] = value

        class FakeModel:
            def __init__(self, status_color: str, item_color: str = "") -> None:
                self.status_color = status_color
                self.item_color = item_color

            def get_value(self, _iter, column):
                if column == SSHManagerWindow.COLUMN_TEXT:
                    return "app"
                if column == SSHManagerWindow.COLUMN_COLOR:
                    return self.item_color
                if column == SSHManagerWindow.COLUMN_STATUS_COLOR:
                    return self.status_color
                return ""

        cell = FakeCell()
        SSHManagerWindow._render_text(SSHManagerWindow, None, cell, FakeModel("#ef4444"), object(), None)
        self.assertEqual(cell.props["foreground"], "#ef4444")
        self.assertTrue(cell.props["foreground-set"])

        cell = FakeCell()
        SSHManagerWindow._render_text(SSHManagerWindow, None, cell, FakeModel(""), object(), None)
        self.assertFalse(cell.props["foreground-set"])

        cell = FakeCell()
        SSHManagerWindow._render_text(SSHManagerWindow, None, cell, FakeModel("", "#112233"), object(), None)
        self.assertFalse(cell.props["foreground-set"])

        cell = FakeCell()
        SSHManagerWindow._render_text(SSHManagerWindow, None, cell, FakeModel("#ef4444", "#112233"), object(), None)
        self.assertEqual(cell.props["foreground"], "#ef4444")
        self.assertTrue(cell.props["foreground-set"])

    def test_save_ui_state_persists_host_colors(self) -> None:
        class FakePaned:
            def get_position(self) -> int:
                return 280

        class FakeTabs:
            default_font_scale = 1.15

        with tempfile.TemporaryDirectory() as tmpdir:
            dummy = type(
                "DummyWindow",
                (),
                {
                    "_ui_state_path": Path(tmpdir) / UI_STATE_FILE,
                    "get_size": lambda self: (1200, 800),
                    "is_maximized": lambda self: False,
                    "_paned": FakePaned(),
                    "_sidebar_visible": True,
                    "_sidebar_width_saved": 320,
                    "_prefer_dark": True,
                    "terminal_tabs": FakeTabs(),
                    "_ui_font_size": 14,
                    "_sidebar_visible_before_max": True,
                    "_terminal_maximized": False,
                    "_group_colors": {"prod": "#111111"},
                    "_host_colors": {"prod::web::app": "#222222"},
                },
            )()

            SSHManagerWindow._save_ui_state(dummy)

            state = json.loads((Path(tmpdir) / UI_STATE_FILE).read_text(encoding="utf-8"))
            self.assertEqual(state["group_colors"], {"prod": "#111111"})
            self.assertEqual(state["host_colors"], {"prod::web::app": "#222222"})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
