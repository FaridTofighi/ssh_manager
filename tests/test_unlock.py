"""Start-up: what to do (``ssh_manager.unlock``) and the unlock flow (``gtkui/unlock_flow.py``) with fakes."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ssh_manager import keybox as K
from ssh_manager import unlock as U
from ssh_manager.secure_store import InMemoryKeyring
from ssh_manager.settings import AppSettings

from tests.gtk_window_fixture import display_available

FAST = 2 ** 15
PASSWORD = "Master-sentinel-1"


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name) / "config"
        self.dir.mkdir()
        self.settings = AppSettings(config_path=self.dir / "settings.json")
        patcher = mock.patch.object(K, "KEYBOX_SCRYPT_N", FAST)
        patcher.start()
        self.addCleanup(patcher.stop)

    def keybox(self, password=PASSWORD):
        return K.Keybox.create(password, self.dir / "keybox.json", scrypt_n=FAST)

    def decision(self):
        return U.decide(U.inspect(self.dir, self.settings))


class DecideTests(Case):
    def test_matrix(self) -> None:
        self.assertIs(self.decision(), U.Startup.FIRST_RUN)
        self.settings.passwords_off = True
        self.assertIs(self.decision(), U.Startup.PASSWORDS_OFF)
        self.settings.passwords_off = False
        self.settings.master_password_hash = "x"
        self.assertIs(self.decision(), U.Startup.MIGRATE)
        self.settings.master_password_hash = ""
        self.settings.master_password_disabled = True  # the old bypass flag never skips protection
        self.assertIs(self.decision(), U.Startup.MIGRATE)
        self.settings.master_password_disabled = False
        (self.dir / "passwords.json").write_text(json.dumps({"ssh-manager": {"h:22:u": "pw"}}))
        self.assertIs(self.decision(), U.Startup.MIGRATE)
        self.keybox()
        self.assertIs(self.decision(), U.Startup.RESUME)  # the plain file is still there
        (self.dir / "passwords.json").write_text(json.dumps({"version": 3, "data": "x"}))
        self.assertIs(self.decision(), U.Startup.UNLOCK)
        self.settings.passwords_off = True
        self.assertIs(self.decision(), U.Startup.UNLOCK)  # a key file always wins
        (self.dir / "x.pre-keybox").write_text("left over")
        self.assertIs(self.decision(), U.Startup.RESUME)
        (self.dir / "x.pre-keybox").unlink()
        (self.dir / "keybox.json").unlink()
        self.assertIs(self.decision(), U.Startup.KEY_MISSING)  # never a new key silently
        (self.dir / "passwords.json").unlink()
        (self.dir / "macros.enc").write_text(json.dumps({"version": 2, "data": "x"}))
        self.assertIs(self.decision(), U.Startup.KEY_MISSING)

    def test_credentials_and_damaged_files_count_as_older_data(self) -> None:
        (self.dir / "credentials.json").write_text(json.dumps({"credentials": [{"id": "c"}]}))
        self.assertIs(self.decision(), U.Startup.MIGRATE)
        (self.dir / "credentials.json").unlink()
        (self.dir / "passwords.json").write_text("{not json")
        self.assertEqual(U.json_version(self.dir / "passwords.json"), -1)
        self.assertIs(self.decision(), U.Startup.MIGRATE)

    def test_delay_and_orphans(self) -> None:
        self.assertEqual([U.unlock_delay(n) for n in range(4, 13)], [0, 1, 2, 4, 8, 16, 30, 30, 30])
        for name in ("passwords.json", "macros.enc", "macros.enc.bak"):
            (self.dir / name).write_text(name)
        moved = U.orphan_stores(self.dir)
        self.assertEqual(len(moved), 3)
        self.assertFalse((self.dir / "passwords.json").exists())
        self.assertEqual(sorted(p.read_text() for p in moved), ["macros.enc", "macros.enc.bak", "passwords.json"])

    def test_remember(self) -> None:
        box = self.keybox()
        ring = InMemoryKeyring()
        self.assertFalse(U.is_remembered(ring, box.id))
        self.assertIsNone(U.remembered_dek(None, box.id))
        U.remember(ring, box)
        self.assertEqual(U.remembered_dek(ring, box.id), box.dek())
        self.assertTrue(K.Keybox.load(box.path).unlock_with_dek(U.remembered_dek(ring, box.id)))
        U.forget_remembered(ring, box.id)
        self.assertFalse(U.is_remembered(ring, box.id))
        broken = mock.Mock()
        broken.get_password.side_effect = RuntimeError("locked")
        self.assertIsNone(U.remembered_dek(broken, box.id))  # falls back to the dialog

    def test_settings_stop_writing_the_obsolete_keys(self) -> None:
        self.settings.master_password_hash = "old-hash"
        self.settings.save()
        self.assertIn("master_password_hash", json.loads(self.settings.config_path.read_text()))
        self.keybox()
        self.settings.save()
        written = json.loads(self.settings.config_path.read_text())
        for key in ("master_password_hash", "master_password_disabled", "passwords_off"):
            self.assertNotIn(key, written)


@unittest.skipUnless(display_available(), "needs a display")
class StartupFlowTests(Case):
    def flow(self, **answers):
        from ssh_manager.gtkui import unlock_flow as F

        ring = answers.pop("keyring", None)
        flow = F.StartupFlow(self.settings, self.dir, keyring_factory=lambda: ring)
        self.messages = []
        flow.message = lambda title, detail: self.messages.append(title)
        flow.show_recovery = answers.pop("show_recovery", lambda code: True)
        for name, value in answers.items():
            setattr(flow, name, value)
        return flow

    def fail(self, *_a):
        raise AssertionError("not expected")

    def test_first_run_sets_the_password_and_offers_a_recovery_key(self) -> None:
        shown = []
        result = self.flow(ask_first_run=lambda: ("set", PASSWORD),
                           show_recovery=lambda code: shown.append(code) or True).run()
        self.assertFalse(result.keybox.locked)
        self.assertEqual(len(shown), 1)
        loaded = K.Keybox.load(self.dir / "keybox.json")
        self.assertTrue(loaded.has_recovery)
        self.assertTrue(loaded.unlock_with_recovery(shown[0]))
        self.assertNotIn(PASSWORD, (self.dir / "keybox.json").read_text())

    def test_a_skipped_recovery_key_is_removed(self) -> None:
        result = self.flow(ask_first_run=lambda: ("set", PASSWORD), show_recovery=lambda code: False).run()
        self.assertFalse(K.Keybox.load(result.keybox.path).has_recovery)

    def test_first_run_passwords_off_and_cancel(self) -> None:
        self.assertIsNone(self.flow(ask_first_run=lambda: ("cancel", None)).run())
        self.assertFalse((self.dir / "keybox.json").exists())
        result = self.flow(ask_first_run=lambda: ("off", None)).run()
        self.assertTrue(result.passwords_off)
        self.assertTrue(AppSettings.load(self.settings.config_path).passwords_off)
        self.assertTrue(self.flow(ask_first_run=self.fail).run().passwords_off)  # not asked again

    def test_unlock_with_the_password(self) -> None:
        self.keybox()

        def ask(box):
            self.assertFalse(box.unlock("wrong"))
            self.assertTrue(box.unlock(PASSWORD))
            return PASSWORD, False

        result = self.flow(ask_password=ask).run()
        self.assertFalse(result.keybox.locked)
        self.assertFalse(result.resume)
        self.assertIsNone(self.flow(ask_password=lambda box: (None, False)).run())  # cancel quits

    def test_remembered_key_skips_the_dialog_and_a_bad_one_falls_back(self) -> None:
        box = self.keybox()
        ring = InMemoryKeyring()
        U.remember(ring, box)
        self.assertFalse(self.flow(keyring=ring, ask_password=self.fail).run().keybox.locked)
        ring.set_password(U.REMEMBER_SERVICE, U.remember_key(box.id), "AAAA" * 11)  # wrong key
        asked = []
        result = self.flow(keyring=ring, ask_password=lambda b: asked.append(1) or (b.unlock(PASSWORD) and
                                                                                    PASSWORD, False)).run()
        self.assertEqual(asked, [1])
        self.assertFalse(result.keybox.locked)

    def test_forgot_password_with_the_recovery_key(self) -> None:
        box = self.keybox()
        dek = box.dek()
        code = box.add_recovery()
        answers = iter([(None, True), (None, True)])
        codes = iter(["AAAA-" * 7 + "AAAA", code])
        flow = self.flow(ask_password=lambda b: next(answers), ask_recovery_code=lambda: next(codes),
                         ask_new_password=lambda: "new master")
        result = flow.run()
        self.assertEqual(self.messages, ["The recovery key is not correct"])
        self.assertEqual(result.keybox.dek(), dek)
        loaded = K.Keybox.load(box.path)
        self.assertTrue(loaded.unlock("new master"))
        self.assertFalse(K.Keybox.load(box.path).unlock(PASSWORD))

    def test_key_missing_never_creates_a_key_silently(self) -> None:
        (self.dir / "passwords.json").write_text(json.dumps({"version": 3, "data": "x"}))
        before = (self.dir / "passwords.json").read_bytes()
        self.assertIsNone(self.flow(ask_key_missing=lambda: False, ask_first_run=self.fail).run())
        self.assertEqual((self.dir / "passwords.json").read_bytes(), before)
        self.assertFalse((self.dir / "keybox.json").exists())
        result = self.flow(ask_key_missing=lambda: True, ask_first_run=lambda: ("set", PASSWORD)).run()
        self.assertIsNotNone(result.keybox)
        orphan = [p for p in self.dir.iterdir() if p.name.startswith("passwords.json.orphaned-")]
        self.assertEqual([p.read_bytes() for p in orphan], [before])

    def test_damaged_key_file_quits_with_a_message(self) -> None:
        (self.dir / "keybox.json").write_text("{")
        self.assertIsNone(self.flow(ask_password=self.fail).run())
        self.assertEqual(self.messages, ["The key file cannot be used"])
        self.assertEqual((self.dir / "keybox.json").read_text(), "{")

    def test_migration_is_handed_on(self) -> None:
        self.settings.master_password_hash = "x"
        self.assertTrue(self.flow(ask_first_run=self.fail, ask_password=self.fail).run().migrate)


@unittest.skipUnless(display_available(), "needs a display")
class DialogTests(Case):
    def test_recovery_dialog_wants_the_last_two_groups(self) -> None:
        from ssh_manager.gtkui.dialogs.base import is_accept
        from ssh_manager.gtkui.unlock_flow import RecoveryKeyDialog

        dialog = RecoveryKeyDialog(None, "AAAA-BBBB-CCCC-DDDD-EEEE-FFFF-GGGG-HHHH")
        self.addCleanup(dialog.destroy)
        dialog.check.set_text("GGGG-HHHX")
        self.assertFalse(dialog._confirmed())
        self.assertTrue(dialog.error.get_text())
        dialog.check.set_text("gggg hhhh")
        self.assertTrue(dialog._confirmed())
        self.assertTrue(is_accept(-5))

    def test_unlock_dialog_in_a_thread(self) -> None:
        from gi.repository import Gtk  # type: ignore

        from ssh_manager.gtkui.dialogs.secrets import UnlockDialog

        box = self.keybox()
        loaded = K.Keybox.load(box.path)
        dialog = UnlockDialog(None, "t", "h", "d", loaded.unlock, in_thread=True, forgot_label="_Forgot")
        self.addCleanup(dialog.destroy)
        responses = []
        dialog.connect("response", lambda _d, r: responses.append(r))
        for attempt in ("wrong", PASSWORD):
            dialog.secret.set_text(attempt)
            self.assertFalse(dialog._validate(Gtk.ResponseType.OK))  # never blocks: the worker answers later
            self.assertTrue(dialog.busy)
            while dialog.busy:
                Gtk.main_iteration_do(True)
        self.assertEqual(dialog.failures, 1)
        self.assertEqual(dialog.accepted_password, PASSWORD)
        self.assertFalse(loaded.locked)

    def test_run_off_thread(self) -> None:
        from ssh_manager.gtkui.unlock_flow import run_off_thread

        self.assertEqual(run_off_thread(lambda: os.getpid()), os.getpid())
        with self.assertRaises(ValueError):
            run_off_thread(lambda: int("x"))


@unittest.skipUnless(display_available(), "needs a display")
class WindowStoreTests(unittest.TestCase):
    def test_keybox_and_passwords_off_choose_the_store(self) -> None:
        from tests.gtk_window_fixture import WindowSandbox

        from ssh_manager.secure_store import DisabledSecureStore, EncryptedFileKeyring

        sandbox = WindowSandbox()
        self.addCleanup(sandbox.close)
        win = sandbox.window
        with mock.patch.object(K, "KEYBOX_SCRYPT_N", FAST):
            box = K.Keybox.create(PASSWORD, sandbox.root / "config" / "ophion" / "keybox.json", scrypt_n=FAST)
        credentials = win._credential_store
        win._secure_store = DisabledSecureStore()
        credentials._secure_store = win._secure_store
        self.assertTrue(win.creds.password_store_problem())
        win.adopt_keybox(box)
        self.assertIs(win._credential_store, credentials)
        self.assertIsInstance(win._secure_store.backend, EncryptedFileKeyring)
        cred = credentials.add("Ops", "deploy", "pw-sentinel")
        self.assertEqual(credentials.get_password(cred.id), "pw-sentinel")
        self.assertIsNone(win.creds.password_store_problem())
        self.assertNotIn(b"pw-sentinel", (box.path.parent / "passwords.json").read_bytes())


if __name__ == "__main__":
    unittest.main()
