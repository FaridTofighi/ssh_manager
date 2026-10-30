"""Preferences → Security: the page and its actions on a real window (fake keyring, sandboxed config)."""

import json
import unittest
from unittest import mock

from ssh_manager import keybox as K
from ssh_manager import unlock as U
from ssh_manager.secure_store import DisabledSecureStore, EncryptedFileKeyring, InMemoryKeyring

from tests.gtk_window_fixture import display_available

PASSWORD = "Security-page-sentinel"


@unittest.skipUnless(display_available(), "needs a display")
class SecurityPageTests(unittest.TestCase):
    def setUp(self) -> None:
        from tests.gtk_window_fixture import WindowSandbox

        self.sandbox = WindowSandbox()
        self.addCleanup(self.sandbox.close)
        self.win = self.sandbox.window
        self.box = self.sandbox.use_keybox(PASSWORD)
        self.keyring = InMemoryKeyring()
        self.actions = self.win.security
        self.actions.keyring_factory = lambda: self.keyring
        self.actions.ask_password = lambda _text: PASSWORD
        self.confirms = []
        self.actions.confirm = lambda title, detail, label: self.confirms.append(title) or self.answer
        self.answer = True
        self.shown = []
        self.actions.show_recovery = lambda code: self.shown.append(code) or True

    def page(self):
        from ssh_manager.gtkui.dialogs.security_page import SecurityPage

        page = SecurityPage(self.actions, self.win)
        self.addCleanup(page.destroy)
        return page

    def test_rows_and_saved_count(self) -> None:
        self.win._credential_store.add("Ops", "ops", "pw-1")
        self.win._credential_store.add("Db", "db", "pw-2")
        page = self.page()
        self.assertIsNotNone(page.change_button)
        self.assertTrue(page.ask_radio.get_active())
        self.assertEqual(page.saved_label.get_text(), "2 stored · encrypted")
        self.assertFalse(page.off_switch.get_active())
        self.assertIsNone(page.finish_button)
        self.assertIsNone(page.leftovers_button)

    def test_remember_on_this_computer(self) -> None:
        page = self.page()
        page.remember_radio.set_active(True)
        self.assertTrue(U.is_remembered(self.keyring, self.box.id))
        self.assertEqual(U.remembered_dek(self.keyring, self.box.id), self.box.dek())
        self.assertTrue(page.remember_radio.get_active())
        page.ask_radio.set_active(True)
        self.assertFalse(U.is_remembered(self.keyring, self.box.id))
        # A cancelled confirmation changes nothing.
        self.actions.ask_password = lambda _text: None
        page.remember_radio.set_active(True)
        self.assertFalse(U.is_remembered(self.keyring, self.box.id))
        self.assertTrue(page.ask_radio.get_active())
        # Without a keyring the option is shown disabled.
        self.actions.keyring_factory = lambda: None
        page.refresh()
        self.assertFalse(page.remember_radio.get_sensitive())

    def test_recovery_key(self) -> None:
        page = self.page()
        page.recovery_button.clicked()
        self.assertTrue(K.Keybox.load(self.box.path).has_recovery)
        first = self.shown[-1]
        self.answer = False  # "Create a new recovery key?" → no
        page.recovery_button.clicked()
        self.assertEqual(len(self.shown), 1)
        self.answer = True
        page.recovery_button.clicked()
        self.assertFalse(K.Keybox.load(self.box.path).unlock_with_recovery(first))  # the old one stopped working
        self.assertTrue(K.Keybox.load(self.box.path).unlock_with_recovery(self.shown[-1]))

    def test_dont_save_passwords_and_back(self) -> None:
        self.win._credential_store.add("Ops", "ops", "pw-1")
        page = self.page()
        self.answer = False
        page.off_switch.set_active(True)
        self.assertEqual(self.confirms, ["Delete 1 saved password?"])
        self.assertTrue(self.box.path.exists())
        self.assertFalse(page.off_switch.get_active())
        self.answer = True
        U.remember(self.keyring, self.box)
        page.off_switch.set_active(True)
        config = self.box.path.parent
        self.assertFalse(self.box.path.exists())
        self.assertFalse((config / "passwords.json").exists())
        self.assertFalse(U.is_remembered(self.keyring, self.box.id))
        self.assertIsInstance(self.win._secure_store, DisabledSecureStore)
        self.assertTrue(json.loads((config / "settings.json").read_text())["passwords_off"])
        self.assertTrue(page.off_switch.get_active())
        self.assertIsNone(page.change_button)
        with mock.patch("ssh_manager.gtkui.unlock_flow.NewSecretDialog") as new:
            new.return_value.ask.return_value = "back on"
            page.off_switch.set_active(False)
        self.assertIsInstance(self.win._secure_store.backend, EncryptedFileKeyring)
        self.assertTrue(K.Keybox.load(config / "keybox.json").unlock("back on"))
        self.assertIsNotNone(page.change_button)

    def test_keyring_leftovers_are_removed_only_when_moved(self) -> None:
        cred = self.win._credential_store.add("Ops", "ops", "moved-pw")
        self.keyring.set_password("ssh-manager", f"credential:{cred.id}", "old copy")
        self.box.record_migration({"counts": {"from_keyring": 1}})
        page = self.page()
        self.assertIsNotNone(page.leftovers_button)
        page.leftovers_button.clicked()
        self.assertEqual(self.keyring._store, {})
        self.assertEqual(K.Keybox.load(self.box.path).migrated["keyring_removed"], 1)
        self.assertIsNone(page.leftovers_button)
        self.assertEqual(self.win._credential_store.get_password(cred.id), "moved-pw")

    def test_pending_migration(self) -> None:
        self.win.migration_pending = True
        self.win._secure_store = DisabledSecureStore("migration")
        page = self.page()
        self.assertIsNotNone(page.finish_button)
        self.assertIsNone(page.change_button)
        self.assertIsNone(page.off_switch)
        self.assertEqual(page.saved_label.get_text(), "Unavailable until the migration is finished.")
        self.assertTrue(self.win.security_banner.get_visible() or not self.win.get_mapped())
        with mock.patch.object(self.win, "finish_migration", return_value=True) as finish:
            page.finish_button.clicked()
        finish.assert_called_once()

    def test_preferences_has_the_page_and_no_old_checkbox(self) -> None:
        from ssh_manager.gtkui.dialogs.preferences import PreferencesDialog, PreferencesValues
        from ssh_manager.gtkui.theme.loader import ThemeMode

        dialog = PreferencesDialog(None, ThemeMode.LIGHT, 1.0, 11, self.win._settings, security=self.actions)
        self.addCleanup(dialog.destroy)
        self.assertIsNotNone(dialog.security_page)
        self.assertFalse(hasattr(dialog, "_disable_master_check"))
        self.assertNotIn("master_password_disabled", PreferencesValues.__dataclass_fields__)
        dialog.layout.show_page("security")
        values = dialog.values()
        self.win.app_dialogs.apply_preferences(values)
        written = json.loads(self.win._settings.config_path.read_text())
        self.assertNotIn("master_password_disabled", written)


if __name__ == "__main__":
    unittest.main()
