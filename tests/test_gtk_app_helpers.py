import json
import unittest
import tempfile
from pathlib import Path
from unittest import mock

from ssh_manager.config import HostEntry
from ssh_manager.gtkui.constants import Icons, TreeColumn
from ssh_manager.gtkui.theme.loader import current_palette
from ssh_manager.gtkui.views.session_tree import render_count, render_icon, render_status
from ssh_manager.gtkui.widgets.icons import tinted_icon_surface


class _FakeCell:
    def __init__(self) -> None:
        self.props = {}

    def set_property(self, name, value) -> None:
        self.props[name] = value
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
                "COLUMN_STATUS": SSHManagerWindow.COLUMN_STATUS,
            },
        )()

        SSHManagerWindow._set_host_status_for_key(dummy, "prod::web::app", "connected")

        self.assertEqual(dummy._host_status["prod::web::app"], "connected")
        # The status column stores the state; the renderer maps it to glyph + color.
        self.assertEqual(updates, [("iter-1", TreeColumn.STATUS, "connected")])

    # The hand-painted pixbuf icons were replaced by tinted themed icons and the
    # status is no longer shown by coloring the row text; these tests cover the
    # replacements (see views/session_tree.py).
    def _fake_model(self, **values):
        class FakeModel:
            def get_value(self, _iter, column):
                return values.get(TreeColumn(column).name, "")

        return FakeModel()

    def test_render_icon_uses_tinted_theme_icon_with_row_color(self) -> None:
        cell = _FakeCell()
        model = self._fake_model(ICON=Icons.HOST, COLOR="#112233", KIND="host")
        render_icon(None, cell, model, object(), {})
        self.assertIsNotNone(cell.props["surface"])

    def test_render_icon_falls_back_to_token_color_without_row_color(self) -> None:
        cache = {}
        cell = _FakeCell()
        model = self._fake_model(ICON=Icons.GROUP, COLOR="", KIND="parent")
        render_icon(None, cell, model, object(), cache)
        self.assertIsNotNone(cell.props["surface"])
        self.assertEqual({key[1] for key in cache}, {current_palette().icon_group})

    def test_tinted_icon_surface_rejects_invalid_color(self) -> None:
        self.assertIsNone(tinted_icon_surface(Icons.HOST, "not-a-color"))
        self.assertIsNotNone(tinted_icon_surface(Icons.HOST, "#445566"))

    def test_status_glyph_shape_and_color_per_state(self) -> None:
        palette = current_palette()
        cell = _FakeCell()
        render_status(cell, self._fake_model(KIND="host", STATUS="connected"), object())
        self.assertEqual((cell.props["text"], cell.props["foreground"]), ("●", palette.success))
        cell = _FakeCell()
        render_status(cell, self._fake_model(KIND="host", STATUS="failed"), object())
        self.assertEqual((cell.props["text"], cell.props["foreground"]), ("✖", palette.danger))
        cell = _FakeCell()
        render_status(cell, self._fake_model(KIND="host", STATUS="unknown"), object())
        self.assertFalse(cell.props["visible"])
        cell = _FakeCell()
        render_status(cell, self._fake_model(KIND="parent", STATUS="connected"), object())
        self.assertFalse(cell.props["visible"])

    def test_count_badge_only_on_groups(self) -> None:
        class CountModel:
            def __init__(self, kind):
                self.kind = kind

            def get_value(self, _iter, column):
                return {TreeColumn.KIND: self.kind, TreeColumn.COUNT: 3}.get(column, "")

        cell = _FakeCell()
        render_count(cell, CountModel("child"), object())
        self.assertEqual((cell.props["visible"], cell.props["text"]), (True, "3"))
        cell = _FakeCell()
        render_count(cell, CountModel("host"), object())
        self.assertFalse(cell.props["visible"])

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
