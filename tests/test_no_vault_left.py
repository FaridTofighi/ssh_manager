"""The Credential Vault and its converter are gone: the word appears nowhere but the changelog history.

This is the one test file that has to spell the word: the guard's pattern, and the name of the file an
old installation may still have (``vault.json``), which must be ignored.
"""

import re
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.gtk_window_fixture import display_available

ROOT = Path(__file__).resolve().parents[1]
WORD = re.compile(r"vault", re.IGNORECASE)
LEFTOVER = "vault.json"


class NoVaultLeftTests(unittest.TestCase):
    def scan(self, paths) -> list:
        found = []
        for path in paths:
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if WORD.search(line):
                    found.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()}")
        return found

    def test_code_and_ui_strings(self) -> None:
        found = self.scan(sorted((ROOT / "ophion").rglob("*.py")) + sorted((ROOT / "tools").rglob("*.py")))
        self.assertEqual(found, [], "\n".join(found))

    def test_docs(self) -> None:
        """``docs/`` and the README; only ``CHANGELOG-UI.md`` keeps its history."""

        found = self.scan(sorted((ROOT / "docs").rglob("*.md")) + [ROOT / "README.md"])
        self.assertEqual(found, [], "\n".join(found))


def labels_of(widget) -> list:
    from gi.repository import Gtk  # type: ignore

    found = []
    if isinstance(widget, Gtk.Label):
        found.append(widget.get_text())
    elif isinstance(widget, Gtk.Button) and widget.get_label():
        found.append(widget.get_label())
    if isinstance(widget, Gtk.Container):
        for child in widget.get_children():
            found.extend(labels_of(child))
    return found


@unittest.skipUnless(display_available(), "needs a display")
class LeftoverFileTests(unittest.TestCase):
    """An old ``vault.json`` still on disk is ignored: no crash, no dialog, no deletion."""

    def setUp(self) -> None:
        from tests.gtk_window_fixture import WindowSandbox

        self.sandbox = WindowSandbox()
        self.addCleanup(self.sandbox.close)
        self.config = self.sandbox.root / "config" / "ophion"
        self.config.mkdir(parents=True, exist_ok=True)
        self.leftover = self.config / LEFTOVER
        self.raw = b'{"version": 1, "salt": "c2FsdA==", "master_hash": "aGFzaA==", "data": "ZGF0YQ=="}'
        self.leftover.write_bytes(self.raw)

    def assert_untouched(self) -> None:
        self.assertEqual(self.leftover.read_bytes(), self.raw)
        self.assertEqual([p.name for p in self.config.iterdir() if p.name.startswith(LEFTOVER)], [LEFTOVER])

    @staticmethod
    def pump(seconds: float = 0.3) -> None:
        from gi.repository import GLib  # type: ignore

        end = time.monotonic() + seconds
        while time.monotonic() < end:
            GLib.MainContext.default().iteration(False)

    def test_the_start_up_decision_ignores_it(self) -> None:
        from ophion import unlock as U
        from ophion.settings import AppSettings

        settings = AppSettings(config_path=self.config / "settings.json")
        self.assertIs(U.decide(U.inspect(self.config, settings)), U.Startup.FIRST_RUN)
        self.sandbox.use_keybox("pw")
        self.assertIs(U.decide(U.inspect(self.config, settings)), U.Startup.UNLOCK)
        self.assert_untouched()

    def test_the_app_starts_without_a_dialog(self) -> None:
        from gi.repository import Gtk  # type: ignore

        from ophion.gtk_app import SSHManagerApplication
        from ophion.gtkui.unlock_flow import StartupResult

        app = SSHManagerApplication(base_dir=self.sandbox.base)
        dialogs = []
        with mock.patch("ophion.gtk_app.StartupFlow") as flow, \
                mock.patch.object(Gtk.Dialog, "run", lambda d: dialogs.append(d) or Gtk.ResponseType.CANCEL), \
                mock.patch.object(Gtk.Window, "present", lambda w: None):
            flow.return_value.run.return_value = StartupResult(passwords_off=True)
            app.do_activate()
            window = app._window
            self.addCleanup(window.destroy)
            self.pump()  # the idle callbacks of the start
            self.assertEqual(dialogs, [])
            window.app_dialogs.open_credentials()  # the Credentials manager offers nothing extra
        self.assertEqual(len(dialogs), 1)
        self.assertFalse([text for text in labels_of(dialogs[0]) if WORD.search(text)])
        self.assertFalse(hasattr(window, "legacy_migration"))
        self.assert_untouched()

    def test_the_security_page_has_no_row_for_it(self) -> None:
        from ophion.gtkui.dialogs.security_page import SecurityPage

        self.sandbox.use_keybox("pw")
        page = SecurityPage(self.sandbox.window.security, self.sandbox.window)
        self.addCleanup(page.destroy)
        self.assertFalse([text for text in labels_of(page) if WORD.search(text)])
        self.assertIsNone(page.leftovers_button)
        self.assert_untouched()


if __name__ == "__main__":
    unittest.main()
