"""The SSH Keys dialog against a temporary ~/.ssh (real files, the sandbox's password store)."""

import stat
import unittest
from pathlib import Path

from tests.gtk_window_fixture import display_available
from tests.test_local_terminal import pump

PASS = "Ui-key-passphrase-sentinel"


@unittest.skipUnless(display_available(), "needs a display")
class SshKeysUiCase(unittest.TestCase):
    def setUp(self) -> None:
        from tests.gtk_window_fixture import WindowSandbox

        self.sandbox = WindowSandbox(store=True)
        self.addCleanup(self.sandbox.close)
        self.win = self.sandbox.window
        self.box = self.sandbox.use_keybox("pw")  # passphrases go to the encrypted password store
        self.controller = self.win.ssh_keys
        self.controller.ssh_dir = self.sandbox.root / "home" / ".ssh"

    def dialog(self):
        from ssh_manager.gtkui.dialogs.ssh_keys import SshKeysDialog

        dialog = SshKeysDialog(None, self.controller)
        self.addCleanup(dialog.destroy)
        return dialog

    def names(self, dialog):
        return [row[1] for row in dialog.store]


class KeysDialogTests(SshKeysUiCase):
    def test_generate_lists_and_remembers_the_passphrase_encrypted(self) -> None:
        from ssh_manager.gtkui.dialogs.ssh_keys import GenerateKeyDialog

        generate = GenerateKeyDialog(None, self.controller)
        self.addCleanup(generate.destroy)
        self.assertEqual(generate.name.get_text(), "id_ed25519")
        generate.passphrase.entry.set_text(PASS)
        self.assertFalse(generate.primary_button.get_sensitive())  # not confirmed yet
        generate.confirm.entry.set_text(PASS)
        self.assertTrue(generate.primary_button.get_sensitive())
        self.assertTrue(generate.remember.get_active())  # on when there is a passphrase
        generate.start()
        self.assertTrue(pump(20, until=lambda: generate.result is not None), generate.warning.get_text())
        key = self.controller.ssh_dir / "id_ed25519"
        self.assertEqual(stat.S_IMODE(key.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.controller.ssh_dir.stat().st_mode), 0o700)
        self.assertEqual(self.controller.saved_passphrase(key), PASS)
        config = Path(self.win._settings.config_path).parent
        for path in config.rglob("*"):
            if path.is_file():
                self.assertNotIn(PASS.encode(), path.read_bytes(), path)
        dialog = self.dialog()
        self.assertEqual(self.names(dialog), ["id_ed25519"])
        row = dialog.store[0]
        self.assertEqual((row[2], row[5], row[6]), ("ed25519", "yes (saved)", "yes"))
        second = GenerateKeyDialog(None, self.controller)
        self.addCleanup(second.destroy)
        self.assertEqual(second.name.get_text(), "id_ed25519_2")  # never over an existing key
        second.name.set_text("id_ed25519")
        self.assertFalse(second.primary_button.get_sensitive())
        self.assertIn("already exists", second.warning.get_text())
        self.assertIn("No passphrase", second.warning.get_text())

    def test_delete_confirms_and_shows_the_hosts_that_use_it(self) -> None:
        from ssh_manager import ssh_keys as K

        info = K.generate_key(self.controller.ssh_dir / "used", comment="t")
        host = next(h for h in self.controller.hosts() if h.alias == "web-01")
        host.options["IdentityFile"] = str(info.path)
        self.win.manager.update_host(host.parent_group, host.child_group, host.alias, host)
        dialog = self.dialog()
        self.assertTrue(dialog.select(str(info.path)))
        self.assertEqual(dialog.store[0][7], "1 host")
        asked = []
        dialog.confirm = lambda title, text: asked.append(text) or False
        self.assertFalse(dialog.delete())
        self.assertTrue(info.path.exists())
        self.assertIn("1 host uses this key", asked[0])
        dialog.confirm = lambda title, text: True
        self.assertTrue(dialog.delete())
        self.assertFalse(info.path.exists())
        self.assertFalse(info.path.with_name("used.pub").exists())
        rows = [(row[0], row[1], row[7]) for row in dialog.store]
        self.assertEqual(rows, [("dialog-warning-symbolic", "used", "1 host")])  # still referenced: warned

    def test_import_copy_and_public_key(self) -> None:
        from ssh_manager import ssh_keys as K

        outside = self.sandbox.root / "elsewhere"
        outside.mkdir()
        K.generate_key(outside / "work_key", comment="w")
        dialog = self.dialog()
        dialog.choose_import = lambda: (str(outside / "work_key"), True)
        info = dialog.import_key()
        self.assertEqual(info.path, self.controller.ssh_dir / "work_key")
        self.assertEqual(stat.S_IMODE(info.path.stat().st_mode), 0o600)
        self.assertEqual(dialog.copy_public(), info.public_line)
        dialog.choose_import = lambda: (str(outside / "work_key"), False)  # used where it is
        referenced = dialog.import_key()
        self.assertEqual(referenced.path, outside / "work_key")
        self.assertEqual(sorted(self.names(dialog)), ["work_key", "work_key"])


if __name__ == "__main__":
    unittest.main()
