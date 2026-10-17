import unittest
from typing import Callable, Dict, List

from ssh_manager.gtkui.controllers.notifications import Message, MessageHistory, ToastController
from ssh_manager.gtkui.menu_spec import MenuItemSpec, MenuSection, describe, menu_layout
from ssh_manager.gtkui.theme.tokens import Level
from ssh_manager.gtkui.validation import (
    validate_alias,
    validate_child_path,
    validate_group_name,
    validate_hostname,
    validate_port,
    validate_required,
)


def _display_available() -> bool:
    try:
        import gi

        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk  # type: ignore

        return bool(Gtk.init_check(None)[0])
    except Exception:
        return False


HAS_DISPLAY = _display_available()


class FakeScheduler:
    def __init__(self) -> None:
        self.now = 0.0
        self._next = 1
        self.pending: Dict[int, tuple] = {}

    def schedule(self, seconds: float, callback: Callable[[], None]) -> int:
        handle = self._next
        self._next += 1
        self.pending[handle] = (self.now + seconds, callback)
        return handle

    def cancel(self, handle: int) -> None:
        self.pending.pop(handle, None)

    def advance(self, seconds: float) -> None:
        self.now += seconds
        for handle, (due, callback) in sorted(self.pending.items(), key=lambda kv: kv[1][0]):
            if due <= self.now and handle in self.pending:
                del self.pending[handle]
                callback()


class ValidationTests(unittest.TestCase):
    def test_alias(self) -> None:
        self.assertIsNone(validate_alias("web-01"))
        self.assertIsNotNone(validate_alias(""))
        self.assertIsNotNone(validate_alias("web 01"))
        self.assertIsNotNone(validate_alias(" web"))
        self.assertIsNotNone(validate_alias("#web"))

    def test_port_range(self) -> None:
        self.assertIsNone(validate_port(""))
        self.assertIsNotNone(validate_port("", required=True))
        for good in ("1", "22", "65535"):
            self.assertIsNone(validate_port(good))
        for bad in ("0", "65536", "-1", "22a", "2 2"):
            self.assertIsNotNone(validate_port(bad), bad)

    def test_hostname(self) -> None:
        for good in ("example.com", "web-01", "10.0.0.1", "::1", "[fe80::1]", "host_name.internal", "%h.example.com"):
            self.assertIsNone(validate_hostname(good), good)
        for bad in ("bad host", "-leading.example", "exa$mple.com", "a" * 64 + ".com"):
            self.assertIsNotNone(validate_hostname(bad), bad)
        self.assertIsNotNone(validate_hostname("", required=True))

    def test_group_names(self) -> None:
        self.assertIsNone(validate_group_name("prod"))
        self.assertIsNotNone(validate_group_name("pr/od"))
        self.assertIsNotNone(validate_group_name(".."))
        self.assertIsNone(validate_child_path("db/primary.conf"))
        self.assertIsNotNone(validate_child_path("db/../x"))
        self.assertIsNotNone(validate_required("  ", "Name"))


class MenuSpecTests(unittest.TestCase):
    def test_sections_are_separated_and_empty_sections_skipped(self) -> None:
        spec = [
            MenuSection("connect", [MenuItemSpec("Connect", lambda: None)]),
            MenuSection("empty", []),
            MenuSection("hidden", [MenuItemSpec("Hidden", visible=False)]),
            MenuSection("edit", [MenuItemSpec("Edit", sensitive=False), MenuItemSpec("Duplicate")]),
            MenuSection("danger", [MenuItemSpec("Delete", destructive=True)]),
        ]
        self.assertEqual(
            describe(spec),
            [("Connect", True, False), "---", ("Edit", False, False), ("Duplicate", True, False), "---", ("Delete", True, True)],
        )

    def test_no_leading_or_trailing_separators(self) -> None:
        spec = [MenuSection("a", []), MenuSection("b", [MenuItemSpec("Only")]), MenuSection("c", [])]
        layout = menu_layout(spec)
        self.assertEqual([kind for kind, _ in layout], ["item"])


class ToastControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scheduler = FakeScheduler()
        self.history = MessageHistory()
        self.controller = ToastController(self.history, self.scheduler, clock=lambda: self.scheduler.now)
        self.shown: List[Message] = []
        self.dismissed: List[Message] = []
        self.controller.on_show(self.shown.append)
        self.controller.on_dismiss(self.dismissed.append)

    def test_info_and_success_auto_dismiss(self) -> None:
        info = self.controller.notify("Saved", Level.INFO)
        success = self.controller.notify("Connected", Level.SUCCESS)
        self.scheduler.advance(4.0)
        self.assertEqual([m.id for m in self.controller.visible], [info.id])
        self.scheduler.advance(1.0)
        self.assertEqual(self.controller.visible, [])
        self.assertEqual({m.id for m in self.dismissed}, {info.id, success.id})

    def test_warning_and_error_are_sticky(self) -> None:
        warning = self.controller.notify("Careful", Level.WARNING)
        error = self.controller.notify("Failed", Level.ERROR)
        self.scheduler.advance(3600)
        self.assertEqual([m.id for m in self.controller.visible], [warning.id, error.id])
        self.assertTrue(warning.sticky and error.sticky)
        self.controller.dismiss(error.id)
        self.assertEqual([m.id for m in self.controller.visible], [warning.id])

    def test_history_keeps_everything_newest_first(self) -> None:
        for idx in range(5):
            self.controller.notify(f"m{idx}", Level.ERROR)
        self.assertEqual(len(self.controller.visible), ToastController.MAX_VISIBLE)
        self.assertEqual([m.text for m in self.history.items()], ["m4", "m3", "m2", "m1", "m0"])

    def test_unread_count_and_mark_read(self) -> None:
        self.controller.notify("a", Level.ERROR)
        self.controller.notify("b", Level.INFO)
        self.assertEqual(self.history.unread_count(levels={Level.ERROR, Level.WARNING}), 1)
        self.history.mark_all_read()
        self.assertEqual(self.history.unread_count(), 0)

    def test_action_runs_and_dismisses(self) -> None:
        calls = []
        message = self.controller.notify("Failed", Level.ERROR, action_label="Retry", action=lambda: calls.append("retry"))
        self.controller.activate(message.id)
        self.assertEqual(calls, ["retry"])
        self.assertEqual(self.controller.visible, [])

    def test_dismiss_cancels_timer_and_shutdown_clears(self) -> None:
        message = self.controller.notify("x", Level.INFO)
        self.controller.dismiss(message.id)
        self.assertEqual(self.scheduler.pending, {})
        self.controller.notify("y", Level.SUCCESS)
        self.controller.shutdown()
        self.assertEqual(self.scheduler.pending, {})
        self.assertEqual(self.controller.visible, [])

    def test_explicit_timeout_overrides_level_default(self) -> None:
        self.controller.notify("sticky info", Level.INFO, timeout=None)
        self.scheduler.advance(60)
        self.assertEqual(len(self.controller.visible), 1)


@unittest.skipUnless(HAS_DISPLAY, "GTK display not available")
class DialogWidgetTests(unittest.TestCase):
    def setUp(self) -> None:
        from gi.repository import Gtk  # type: ignore

        self.Gtk = Gtk

    def _button_labels(self, dialog) -> List[str]:
        area = dialog.get_action_area()
        return [child.get_label() for child in area.get_children()]

    def test_confirm_dialog_labels_and_destructive_default(self) -> None:
        from ssh_manager.gtkui.dialogs.confirm import ConfirmDialog

        dialog = ConfirmDialog(None, "Delete host?", "Gone", confirm_label="_Delete Host", destructive=True)
        try:
            self.assertEqual(self._button_labels(dialog), ["_Cancel", "_Delete Host"])
            self.assertTrue(dialog.confirm_button.get_style_context().has_class("destructive-action"))
            self.assertIs(dialog._initial_focus, dialog.cancel_button)
        finally:
            dialog.destroy()

    def test_confirm_dialog_for_non_delete_question(self) -> None:
        from ssh_manager.gtkui.dialogs.confirm import ConfirmDialog

        dialog = ConfirmDialog(None, "Reconnect all?", confirm_label="_Reconnect")
        try:
            self.assertEqual(self._button_labels(dialog), ["_Cancel", "_Reconnect"])
            ctx = dialog.confirm_button.get_style_context()
            self.assertTrue(ctx.has_class("suggested-action"))
            self.assertFalse(ctx.has_class("destructive-action"))
            self.assertIs(dialog._initial_focus, dialog.confirm_button)
        finally:
            dialog.destroy()

    def test_confirm_response_mapping(self) -> None:
        from ssh_manager.gtkui.dialogs.base import is_accept

        Gtk = self.Gtk
        self.assertTrue(is_accept(Gtk.ResponseType.OK))
        self.assertFalse(is_accept(Gtk.ResponseType.CANCEL))
        self.assertFalse(is_accept(Gtk.ResponseType.DELETE_EVENT))

    def test_prompt_validator_keeps_dialog_open_on_invalid_value(self) -> None:
        from ssh_manager.gtkui.dialogs.prompt import PromptDialog
        from ssh_manager.gtkui.validation import validate_group_name

        dialog = PromptDialog(None, "New Group", "Name", default="bad/name", validator=validate_group_name)
        try:
            responses = []
            dialog.connect_after("response", lambda _d, r: responses.append(r))
            dialog.response(self.Gtk.ResponseType.OK)
            self.assertEqual(responses, [])  # emission stopped by validator
            self.assertTrue(dialog._row.error.get_visible() or dialog._row.error.get_text())
            dialog._entry.set_text("good")
            dialog.response(self.Gtk.ResponseType.OK)
            self.assertEqual(responses, [self.Gtk.ResponseType.OK])
        finally:
            dialog.destroy()

    def test_status_dot_states(self) -> None:
        from ssh_manager.gtkui.widgets.status_dot import StatusDot

        dot = StatusDot("connected")
        self.assertTrue(dot.get_style_context().has_class("connected"))
        dot.set_status("failed")
        ctx = dot.get_style_context()
        self.assertTrue(ctx.has_class("failed"))
        self.assertFalse(ctx.has_class("connected"))
        self.assertEqual(dot.get_tooltip_text(), "Connection failed")

    def test_build_menu_renders_separators_and_destructive_items(self) -> None:
        from ssh_manager.gtkui.widgets.menus import build_menu

        menu = build_menu(
            [
                MenuSection("a", [MenuItemSpec("Connect", lambda: None, accel="Enter")]),
                MenuSection("b", [MenuItemSpec("Delete", lambda: None, destructive=True)]),
            ]
        )
        children = menu.get_children()
        self.assertEqual(len(children), 3)
        self.assertIsInstance(children[1], self.Gtk.SeparatorMenuItem)
        self.assertTrue(children[2].get_style_context().has_class("destructive"))

    def test_chip_flow_replaces_chips(self) -> None:
        from ssh_manager.gtkui.widgets.chips import ChipFlow

        flow = ChipFlow()
        flow.set_chips(["prod", "web"])
        flow.set_chips(["db"])
        self.assertEqual(flow.chip_texts(), ["db"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
