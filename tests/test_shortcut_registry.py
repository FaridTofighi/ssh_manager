"""Customisable keyboard shortcuts: the registry (defaults, overrides, conflicts, persistence)."""

import json
import tempfile
import unittest
from pathlib import Path

from ophion.gtkui.controllers.shortcut_registry import ShortcutRegistry, canonical
from ophion.gtkui.controllers.shortcuts import ACTIONS
from ophion.settings import AppSettings


class RegistryTests(unittest.TestCase):
    def test_defaults_are_the_actions(self) -> None:
        registry = ShortcutRegistry()
        for spec in ACTIONS:
            if spec.parameter:
                self.assertIsNone(registry.spec(spec.name))  # goto-tab, new-host-template: not customisable
                continue
            with self.subTest(action=spec.name):
                self.assertEqual(registry.effective_accels(spec.name), tuple(canonical(a) for a in spec.accels))
                self.assertTrue(registry.is_default(spec.name))
        self.assertEqual(registry.to_dict(), {})

    def test_new_terminal_actions(self) -> None:
        registry = ShortcutRegistry()
        self.assertEqual(registry.effective_accels("paste-primary"), (canonical("<Shift>Insert"),))
        self.assertIn(canonical("<Primary>Insert"), registry.effective_accels("copy"))
        for name in ("terminal-menu", "select-all-terminal", "clear-scrollback", "reset-terminal"):
            self.assertEqual(registry.effective_accels(name), ())

    def test_override_clear_reset(self) -> None:
        registry = ShortcutRegistry()
        changes = []
        registry.add_listener(lambda: changes.append(1))
        registry.set_accels("paste-primary", ["<Control><Alt>v"])
        self.assertEqual(registry.effective_accels("paste-primary"), (canonical("<Primary><Alt>v"),))
        self.assertEqual(registry.to_dict(), {"paste-primary": [canonical("<Primary><Alt>v")]})
        registry.set_accels("copy", [])  # disabled by the user
        self.assertEqual(registry.effective_accels("copy"), ())
        self.assertEqual(registry.to_dict()["copy"], [])
        registry.reset("copy")
        self.assertEqual(registry.effective_accels("copy"), (canonical("<Primary><Shift>c"), canonical("<Primary>Insert")))
        registry.set_accels("paste", ["<Primary><Shift>v"])  # the default again: no override stored
        self.assertNotIn("paste", registry.to_dict())
        registry.reset_all()
        self.assertEqual(registry.to_dict(), {})
        self.assertEqual(len(changes), 5)

    def test_conflicts(self) -> None:
        registry = ShortcutRegistry()
        self.assertEqual(registry.conflicts("<Control><Shift>c"), ["copy"])  # <Control> == <Primary>
        self.assertEqual(registry.conflicts("<Primary><Shift>c", ignoring="copy"), [])
        self.assertEqual(registry.conflicts("<Primary><Alt>F12"), [])
        self.assertEqual(registry.conflicts("not an accel"), [])

    def test_invalid_entries_are_ignored_one_by_one(self) -> None:
        registry = ShortcutRegistry()
        with self.assertLogs("ophion.gtkui.controllers.shortcut_registry", "WARNING") as logs:
            registry.from_dict({"no-such-action": ["<Primary>x"], "paste": ["<Primary>@@@", "<Primary><Alt>p"],
                                "copy": ["garbage"], "reconnect": "<Primary><Alt>r", "quit": 42})
            registry.set_accels("paste-primary", ["nonsense", "<Shift>F2"])
        self.assertGreaterEqual(len(logs.output), 5)
        self.assertEqual(registry.effective_accels("paste"), (canonical("<Primary><Alt>p"),))
        self.assertTrue(registry.is_default("copy"))  # only invalid entries: the defaults stay
        self.assertEqual(registry.effective_accels("reconnect"), (canonical("<Primary><Alt>r"),))
        self.assertTrue(registry.is_default("quit"))
        self.assertEqual(registry.effective_accels("paste-primary"), (canonical("<Shift>F2"),))

    def test_settings_round_trip_and_old_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text(json.dumps({"host_key_policy": "reject"}))  # before customisable shortcuts
            settings = AppSettings.load(path)
            self.assertEqual(settings.shortcuts, {})
            registry = ShortcutRegistry(overrides=settings.shortcuts)
            registry.set_accels("paste-primary", ["<Primary><Alt>v"])
            registry.set_accels("copy", [])
            settings.shortcuts = registry.to_dict()
            settings.save()
            loaded = AppSettings.load(path)
            again = ShortcutRegistry(overrides=loaded.shortcuts)
            self.assertEqual(again.to_dict(), registry.to_dict())
            path.write_text(json.dumps({"shortcuts": ["not", "a", "mapping"]}))
            self.assertEqual(AppSettings.load(path).shortcuts, {})



class ExtraEntryTests(unittest.TestCase):
    """Macros' shortcuts: extra entries (never in the settings) that take part in conflicts and apply()."""

    def test_conflicts_titles_and_settings(self) -> None:
        from ophion.gtkui.controllers.shortcut_registry import ExtraEntry

        registry = ShortcutRegistry()
        changes = []
        registry.add_listener(lambda: changes.append(1))
        registry.set_extras({"macro:a1": ExtraEntry("Deploy", "Macros", "win.run-macro('a1')", ("<Control><Alt>d",))})
        self.assertEqual(changes, [1])
        self.assertEqual(registry.effective_accels("macro:a1"), (canonical("<Primary><Alt>d"),))
        self.assertEqual(registry.conflicts("<Primary><Alt>d"), ["macro:a1"])
        self.assertEqual(registry.conflicts("<Primary><Alt>d", ignoring="macro:a1"), [])
        self.assertEqual(registry.title("macro:a1"), "Deploy")
        self.assertEqual(registry.title("copy"), "Copy")
        self.assertTrue(registry.is_extra("macro:a1"))
        self.assertEqual(registry.to_dict(), {})  # stored in the encrypted macro file, not the settings
        registry.set_extras(registry.extras())  # unchanged: no notification
        self.assertEqual(changes, [1])
        registry.set_extras({"macro:a2": ExtraEntry("Copyish", "Macros", "win.run-macro('a2')", ("<Primary><Shift>c",))})
        self.assertEqual(sorted(registry.conflicts("<Primary><Shift>c")), ["copy", "macro:a2"])

    def test_apply_sets_and_frees_the_accelerators(self) -> None:
        from unittest import mock

        from ophion.gtkui.controllers.shortcut_registry import ExtraEntry

        registry = ShortcutRegistry()
        app = mock.Mock()
        registry.set_extras({"macro:a1": ExtraEntry("A", "Macros", "win.run-macro('a1')", ("F7",))})
        registry.apply(app, [])
        app.set_accels_for_action.assert_called_with("win.run-macro('a1')", ["F7"])
        app.reset_mock()
        registry.set_extras({})
        registry.apply(app, [])
        app.set_accels_for_action.assert_called_once_with("win.run-macro('a1')", [])  # a deleted macro frees it


if __name__ == "__main__":
    unittest.main()
