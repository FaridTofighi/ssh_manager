"""Files written by "SSH Manager" before the rename to Ophion still open (tests/fixtures/pre_rename/README.md)."""

import shutil
import tempfile
import unittest
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "pre_rename"
PASSWORD, EXPORT_PASS = "pre-rename-master", "pre-rename-export"


class PreRenameFixtureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        for path in FIXTURES.iterdir():  # the tests may write (locks): never into the committed fixtures
            shutil.copy2(path, self.dir / path.name)

    def keybox(self):
        from ssh_manager.keybox import Keybox

        box = Keybox.load(self.dir / "keybox.json")
        self.assertTrue(box.unlock(PASSWORD))
        return box

    def test_keybox_secrets_and_macros_decrypt(self) -> None:
        from ssh_manager.macros import MacroStore
        from ssh_manager.secure_store import EncryptedFileKeyring

        box = self.keybox()
        secrets = EncryptedFileKeyring(box, self.dir / "passwords.json")
        self.assertEqual(secrets.get_password("ssh-manager", "web-01"), "pre-rename-secret")
        macros = MacroStore(self.dir / "macros.enc")
        macros.unlock_with_key(box.subkey("macros"), box.id)
        self.assertEqual([m.name for m in macros.list_sorted()], ["greet"])

    def test_native_exports_plain_and_encrypted(self) -> None:
        from ssh_manager.importers.native import read_native

        plain = read_native(self.dir / "export-plain.sshm")
        encrypted = read_native(self.dir / "export-encrypted.sshm", EXPORT_PASS)
        self.assertTrue(plain.groups)
        self.assertEqual(len(plain.groups), len(encrypted.groups))
        self.assertEqual(plain.app_version, "pre-rename")

    def test_old_openssh_export_and_include_line_are_recognised(self) -> None:
        from ssh_manager.inventory.openssh import plan_openssh_import

        plan = plan_openssh_import([self.dir / "openssh-export.conf"])
        self.assertEqual([h.entry.alias for h in plan.hosts], ["web-01"])
        text = (self.dir / "ssh_config-with-include").read_text(encoding="utf-8")
        self.assertIn("# added by SSH Manager", text)


if __name__ == "__main__":
    unittest.main()
