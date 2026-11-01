"""Terminal mouse rules (decide_mouse_action) and the terminal_input settings section (pure)."""

import json
import tempfile
import unittest
from pathlib import Path

from ophion.settings import AppSettings, TerminalInputSettings
from ophion.terminal_input import MouseAction as A, decide_mouse_action

LEFT, MIDDLE, RIGHT = 1, 2, 3


def settings(**values):
    return TerminalInputSettings(**values)


class DecideMouseActionTests(unittest.TestCase):
    def test_shift_right_click_always_opens_the_menu(self) -> None:
        for tracking in (False, True):
            for right in ("menu", "paste_clipboard", "paste_primary", "none"):
                with self.subTest(tracking=tracking, right=right):
                    self.assertEqual(decide_mouse_action(RIGHT, True, False, tracking, settings(right_click=right)),
                                     A.SHOW_MENU)

    def test_right_click_follows_the_setting_unless_the_program_reads_the_mouse(self) -> None:
        expected = {"menu": A.SHOW_MENU, "paste_clipboard": A.PASTE_CLIPBOARD, "paste_primary": A.PASTE_PRIMARY,
                    "none": A.NONE}
        for right, action in expected.items():
            with self.subTest(right=right):
                self.assertEqual(decide_mouse_action(RIGHT, False, False, False, settings(right_click=right)), action)
                self.assertEqual(decide_mouse_action(RIGHT, False, False, True, settings(right_click=right)),
                                 A.PASS_TO_TERMINAL)

    def test_middle_click(self) -> None:
        expected = {"paste_primary": A.PASTE_PRIMARY, "paste_clipboard": A.PASTE_CLIPBOARD, "none": A.NONE}
        for middle, action in expected.items():
            with self.subTest(middle=middle):
                s = settings(middle_click=middle)
                self.assertEqual(decide_mouse_action(MIDDLE, False, False, False, s), action)
                self.assertEqual(decide_mouse_action(MIDDLE, False, False, True, s), A.PASS_TO_TERMINAL)
                self.assertEqual(decide_mouse_action(MIDDLE, True, False, True, s), action)  # Shift bypasses reporting

    def test_other_buttons_go_to_the_terminal(self) -> None:
        for button in (LEFT, 4, 5, 8, 9):
            for shift in (False, True):
                self.assertEqual(decide_mouse_action(button, shift, False, False, settings()), A.PASS_TO_TERMINAL)

    def test_ctrl_does_not_change_the_result(self) -> None:
        for button in (LEFT, MIDDLE, RIGHT):
            for shift in (False, True):
                for tracking in (False, True):
                    self.assertEqual(decide_mouse_action(button, shift, True, tracking, settings()),
                                     decide_mouse_action(button, shift, False, tracking, settings()))

    def test_defaults(self) -> None:
        self.assertEqual(decide_mouse_action(RIGHT, False, False, False, settings()), A.SHOW_MENU)
        self.assertEqual(decide_mouse_action(MIDDLE, False, False, False, settings()), A.PASTE_PRIMARY)


class TerminalInputSettingsTests(unittest.TestCase):
    def test_defaults_and_validation(self) -> None:
        self.assertEqual(TerminalInputSettings().to_dict(), {"copy_on_select": True, "copy_on_select_clipboard": False,
                                                             "middle_click": "paste_primary", "right_click": "menu"})
        with self.assertLogs("ophion.settings", "WARNING") as logs:
            loaded = TerminalInputSettings.from_dict({"middle_click": "explode", "right_click": 3,
                                                      "copy_on_select_clipboard": "yes", "unknown": 1})
        self.assertEqual(loaded, TerminalInputSettings())
        self.assertEqual(len(logs.output), 3)
        good = TerminalInputSettings.from_dict({"middle_click": "none", "right_click": "paste_clipboard",
                                                "copy_on_select_clipboard": True})
        self.assertEqual((good.middle_click, good.right_click, good.copy_on_select_clipboard),
                         ("none", "paste_clipboard", True))

    def test_round_trip_and_old_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text(json.dumps({"host_key_policy": "reject"}))  # written before this section existed
            self.assertEqual(AppSettings.load(path).terminal_input, TerminalInputSettings())
            settings_ = AppSettings.load(path)
            settings_.terminal_input.right_click = "paste_primary"
            settings_.save()
            self.assertEqual(json.loads(path.read_text())["terminal_input"]["right_click"], "paste_primary")
            self.assertEqual(AppSettings.load(path).terminal_input.right_click, "paste_primary")
            path.write_text(json.dumps({"terminal_input": "not a section"}))
            self.assertEqual(AppSettings.load(path).terminal_input, TerminalInputSettings())


if __name__ == "__main__":
    unittest.main()
