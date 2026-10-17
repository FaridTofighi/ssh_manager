import json
import unittest
from types import SimpleNamespace
import tempfile
from pathlib import Path
from unittest import mock

from ssh_manager.config import HostEntry
from ssh_manager.gtkui.constants import Icons, TreeColumn
from ssh_manager.gtkui.theme.loader import current_palette
from ssh_manager.gtkui.views.session_tree import render_count, render_icon, render_status
from ssh_manager.gtkui.widgets.icons import tinted_icon_surface


def _color_window(*, current_item=("", {}), group_colors=None, host_colors=None, calls=None):
    """Fake window with a real ColorController and a stub tree controller."""

    calls = calls if calls is not None else []
    win = SimpleNamespace(
        _ui=SimpleNamespace(group_colors=dict(group_colors or {}), host_colors=dict(host_colors or {})),
        _save_ui_state=lambda: calls.append(("save",)),
    )
    win.tree = SimpleNamespace(
        current_item=lambda: current_item,
        make_host_key=lambda parent, child, alias: f"{parent}::{child}::{alias}",
        rebuild=lambda refresh=False: calls.append(("rebuild", refresh)),
    )
    win.colors = ColorController(win)
    return win


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
from ssh_manager.gtkui.controllers.colors import ColorController
from ssh_manager.gtkui.controllers.session_controller import SessionController
from ssh_manager.gtkui.controllers.tree import TreeController
from ssh_manager.gtkui.controllers.ui_state import UiState, save_ui_state


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
        win = SimpleNamespace(
            get_application=lambda: app,
            creds=SimpleNamespace(host_key=lambda host: "prod::web::app"),
            _log_event=lambda message, success: inline_calls.append((message, success)),
        )

        SessionController(win).notify_connection_success(HostEntry(alias="app", parent_group="prod", child_group="web"))

        self.assertEqual(len(app.notifications), 1)
        self.assertEqual(app.notifications[0][0], "ssh-manager-connected-prod::web::app")
        self.assertEqual(inline_calls, [])

    def test_item_color_prefers_host_override_then_child_then_parent(self) -> None:
        win = _color_window(group_colors={"prod": "#111111", "prod/web": "#222222"},
                            host_colors={"prod::web::app": "#333333"})
        colors = win.colors

        self.assertEqual(colors.item_color_for("prod", "web", "app"), "#333333")
        self.assertEqual(colors.item_color_for("prod", "web", "db"), "#222222")
        self.assertEqual(colors.item_color_for("prod", "api", "worker"), "#111111")

    def test_change_color_on_host_updates_host_color_store(self) -> None:
        calls = []

        class FakeDialog:
            title = ""

            def __init__(self, title, transient_for) -> None:
                self.__class__.title = title

            def set_rgba(self, _rgba) -> None:
                pass

            def run(self):
                return Gtk.ResponseType.OK

            def get_rgba(self):
                rgba = Gdk.RGBA()
                rgba.parse("#445566")
                return rgba

            def destroy(self) -> None:
                calls.append(("destroy",))

        win = _color_window(current_item=("host", {"parent": "prod", "child": "web", "alias": "app"}), calls=calls)

        with mock.patch("ssh_manager.gtk_app.Gtk.ColorChooserDialog", FakeDialog):
            win.colors.change_host_color()

        self.assertEqual(FakeDialog.title, "Choose Host Color")
        self.assertEqual(win.colors.host_colors, {"prod::web::app": "#445566"})
        self.assertEqual(win.colors.group_colors, {})
        self.assertIn(("rebuild", False), calls)
        self.assertIn(("save",), calls)

    def test_clear_color_on_host_updates_host_color_store(self) -> None:
        calls = []
        win = _color_window(
            current_item=("host", {"parent": "prod", "child": "web", "alias": "app"}),
            group_colors={"prod/web": "#999999"},
            host_colors={"prod::web::app": "#111111"},
            calls=calls,
        )

        win.colors.clear_item_color()

        self.assertEqual(win.colors.host_colors, {})
        self.assertEqual(win.colors.group_colors, {"prod/web": "#999999"})
        self.assertIn(("rebuild", False), calls)

    def test_host_status_uses_separate_status_color_column(self) -> None:
        updates = []

        class FakeTreeStore:
            def set_value(self, tree_iter, column, value) -> None:
                updates.append((tree_iter, column, value))

        win = SimpleNamespace(tree_store=FakeTreeStore(), _ui=SimpleNamespace(filter_query=""))
        with mock.patch("ssh_manager.gtkui.controllers.tree.load_smart_groups", return_value=[]):
            tree = TreeController(win)
        tree.host_iters = {"prod::web::app": "iter-1"}

        tree.set_host_status_for_key("prod::web::app", "connected")

        self.assertEqual(tree.host_status["prod::web::app"], "connected")
        # The status column stores the state; the renderer maps it to glyph + color.
        self.assertEqual(updates, [("iter-1", TreeColumn.STATUS, "connected")])
        tree.set_host_status_for_key("prod::web::app", "bogus")
        self.assertEqual(tree.host_status["prod::web::app"], "unknown")

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
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / UI_STATE_FILE
            state = UiState(group_colors={"prod": "#111111"}, host_colors={"prod::web::app": "#222222"})

            self.assertTrue(save_ui_state(path, state.to_dict()))

            raw = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(raw["group_colors"], {"prod": "#111111"})
            self.assertEqual(raw["host_colors"], {"prod::web::app": "#222222"})

    def test_ui_state_round_trip_includes_new_keys(self) -> None:
        state = UiState(
            window_size=(1200, 800),
            sidebar_width=280,
            sidebar_visible=False,
            theme_mode="system",
            font_scale=1.15,
            ui_font_size=16,
            details_expanded=False,
            filter_query="tag:prod",
        )
        loaded = UiState.from_dict(json.loads(json.dumps(state.to_dict())))
        self.assertEqual(loaded, UiState.from_dict(state.to_dict()))
        self.assertEqual(loaded.window_size, (1200, 800))
        self.assertEqual(loaded.theme_mode, "system")
        self.assertFalse(loaded.details_expanded)
        self.assertEqual(loaded.filter_query, "tag:prod")

    def test_ui_state_tolerates_missing_and_wrong_typed_keys(self) -> None:
        loaded = UiState.from_dict(
            {
                "window_size": ["wide", 3],
                "sidebar_width": "300",
                "ui_font_size": True,
                "font_scale": "big",
                "details_expanded": "no",
                "filter_query": 5,
                "group_colors": ["not", "a", "dict"],
                "host_colors": {"a": 1, "b": "#123456"},
                "prefer_dark": "yes",
            }
        )
        self.assertIsNone(loaded.window_size)
        self.assertEqual((loaded.sidebar_width, loaded.ui_font_size, loaded.font_scale), (407, 11, 1.0))
        self.assertTrue(loaded.details_expanded)
        self.assertEqual(loaded.filter_query, "")
        self.assertEqual(loaded.group_colors, {})
        self.assertEqual(loaded.host_colors, {"b": "#123456"})
        self.assertIsNone(loaded.prefer_dark)
        self.assertEqual(UiState.from_dict("not a dict"), UiState())

    def test_public_names_are_reexported(self) -> None:
        self.assertTrue(issubclass(SSHManagerWindow, Gtk.ApplicationWindow))
        self.assertIsNotNone(Gdk.RGBA)
        self.assertIsNotNone(GdkPixbuf.Pixbuf)
        self.assertEqual(UI_STATE_FILE, ".ui_state.json")



if __name__ == "__main__":  # pragma: no cover
    unittest.main()
