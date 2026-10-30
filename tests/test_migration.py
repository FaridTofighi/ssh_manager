"""Moving older data onto the keybox: the migration matrix (a)–(l), injected failures, tampering afterwards."""

import json
import logging
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ssh_manager import keybox as K
from ssh_manager import migration as MG
from ssh_manager import unlock as U
from ssh_manager.config import ConfigManager
from ssh_manager.credentials import CredentialStore
from ssh_manager.macros import Macro, MacroStore
from ssh_manager.master_auth import hash_master_password
from ssh_manager.secure_store import EncryptedFileKeyring, FileKeyring, InMemoryKeyring, SecureStore
from ssh_manager.settings import AppSettings

from tests.gtk_window_fixture import display_available

FAST = 2 ** 15
OLD = "Old-master-sentinel"
NEW = "New-master-sentinel"
HOST_SECRET = "host-secret-sentinel"
CRED_SECRET = "cred-secret-sentinel"
FILE_SECRET = "file-secret-sentinel"
MACRO_TEXT = "echo macro-sentinel"
SVC = "ssh-manager"


class Fixture:
    """A config folder of an older version (tests pick the parts) and a folder inventory with one host."""

    def __init__(self, root: Path) -> None:
        self.dir = root / "config"
        self.dir.mkdir()
        inventory = root / "inventory" / "prod"
        inventory.mkdir(parents=True)
        (inventory / "web.conf").write_text("Host web\n    hostname web.internal\n    user deploy\n")
        self.manager = ConfigManager(root / "inventory")
        self.settings = AppSettings(config_path=self.dir / "settings.json")
        self.keyring = InMemoryKeyring()
        self.cred_id = ""

    # ------------------------------------------------------------ parts
    def with_hash(self, disabled=False) -> "Fixture":
        self.settings.master_password_hash = hash_master_password(OLD)
        self.settings.master_password_disabled = disabled
        self.settings.save()
        return self

    def with_keyring_secrets(self) -> "Fixture":
        creds = CredentialStore(SecureStore(self.keyring), self.dir / "credentials.json")
        self.cred_id = creds.add("Ops", "ops", CRED_SECRET).id
        self.keyring.set_password(SVC, "web.internal:22:deploy", HOST_SECRET)
        return self

    def with_plain_file(self) -> "Fixture":
        (self.dir / "passwords.json").write_text(json.dumps({SVC: {"web.internal:22:deploy": FILE_SECRET}}))
        (self.dir / "passwords.json.bak").write_text(json.dumps({SVC: {"web.internal:22:deploy": FILE_SECRET}}))
        return self

    def with_encrypted_file(self) -> "Fixture":
        FileKeyring(self.dir / "passwords.json", master_password=OLD).set_password(SVC, "web.internal:22:deploy",
                                                                                   FILE_SECRET)
        assert U.json_version(self.dir / "passwords.json") == 2
        return self

    def with_macros(self) -> "Fixture":
        store = MacroStore(self.dir / "macros.enc")
        store.unlock(OLD)
        store.add(Macro(name="Deploy", steps=[{"type": "text", "text": MACRO_TEXT}]))
        store.save()
        return self

    def snapshot(self) -> dict:
        files = {p.name: p.read_bytes() for p in self.dir.iterdir() if p.is_file() and p.name != "settings.json"}
        return {"files": files, "keyring": dict(self.keyring._store)}

    # ------------------------------------------------------------ checks
    def store(self, box=None) -> EncryptedFileKeyring:
        box = box or self.unlocked()
        return EncryptedFileKeyring(box, self.dir / "passwords.json")

    def unlocked(self, password=None):
        box = K.Keybox.load(self.dir / "keybox.json")
        assert box.unlock(password or OLD) or box.unlock(NEW)
        return box


@unittest.skipUnless(display_available(), "needs a display")
class MigrationCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fx = Fixture(Path(self.tmp.name))
        patcher = mock.patch.object(K, "KEYBOX_SCRYPT_N", FAST)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.asked = []

    def flow(self, *, keyring="same", intro=True, current=OLD, new=("set", NEW), remove=True, fail_at=None):
        from ssh_manager.gtkui.migration_flow import MigrationFlow

        ring = self.fx.keyring if keyring == "same" else keyring
        flow = MigrationFlow(self.fx.settings, self.fx.dir, self.fx.manager, keyring_factory=lambda: ring,
                             app_version="test")
        flow.ask_intro = lambda: intro
        flow.ask_current_password = lambda verify: self.asked.append("current") or (
            current if current and verify(current) else None)
        flow.ask_new_password = lambda explain, allow_off: self.asked.append(("new", allow_off)) or new
        flow.ask_remove_keyring = lambda n: self.asked.append(("remove", n)) or remove
        flow.show_recovery = lambda code: True
        self.messages = []
        flow.message = lambda title, detail: self.messages.append(title)
        if fail_at:
            def after(step):
                if step == fail_at:
                    raise MG.MigrationError("write", f"injected after {step}")
            flow.after_step = after
        return flow

    def decision(self):
        return U.decide(U.inspect(self.fx.dir, self.fx.settings))

    def assert_finished(self, result, expected: dict, *, password=OLD) -> None:
        self.assertFalse(result.unfinished, self.messages)
        box = self.fx.unlocked(password)
        self.assertEqual({k[1]: v for k, v in self.fx.store(box).items().items()}, expected)
        self.assertEqual(self.decision(), U.Startup.UNLOCK)  # nothing left to do: a rerun is a no-op
        names = {p.name for p in self.fx.dir.iterdir()}
        self.assertFalse({n for n in names if n.endswith(".pre-keybox") or ".keybox-new" in n}, names)
        written = json.loads(self.fx.settings.config_path.read_text())
        self.assertNotIn("master_password_hash", written)
        raw = b"".join(p.read_bytes() for p in self.fx.dir.iterdir() if p.is_file())
        for secret in (HOST_SECRET, CRED_SECRET, FILE_SECRET, MACRO_TEXT, OLD, NEW):
            self.assertNotIn(secret.encode(), raw)
        rerun = MG.Migration(self.fx.dir, box, keyring=self.fx.keyring, manager=self.fx.manager).run()
        self.assertEqual(rerun.secrets, len(expected))
        self.assertEqual({k[1]: v for k, v in self.fx.store(box).items().items()}, expected)


class MatrixTests(MigrationCase):
    def test_a_keyring_secrets_and_hash(self) -> None:
        self.fx.with_hash().with_keyring_secrets()
        before = self.fx.snapshot()
        seen = {}

        def after(step):
            if step == "write":  # the new copy exists, nothing old has been touched
                seen.update(self.fx.snapshot())

        flow = self.flow()
        flow.after_step = after
        result = flow.run()
        self.assertEqual(seen["keyring"], before["keyring"])
        self.assertEqual({k: v for k, v in seen["files"].items() if k in before["files"]}, before["files"])
        self.assertEqual(self.asked, ["current", ("remove", 2)])
        self.assertEqual((result.report.from_keyring, result.report.keyring_removed), (2, 2))
        self.assertEqual(self.fx.keyring._store, {})
        self.assert_finished(result, {f"credential:{self.fx.cred_id}": CRED_SECRET,
                                      "web.internal:22:deploy": HOST_SECRET})

    def test_a_keep_the_keyring_copies(self) -> None:
        self.fx.with_hash().with_keyring_secrets()
        result = self.flow(remove=False).run()
        self.assertEqual(len(self.fx.keyring._store), 2)
        self.assertEqual(len(MG.keyring_copies(self.fx.keyring, self.fx.dir, self.fx.manager)), 2)
        self.assertFalse(result.unfinished)
        box = self.fx.unlocked()
        self.assertEqual(MG.remove_keyring_copies(self.fx.keyring, MG.keyring_copies(self.fx.keyring, self.fx.dir,
                                                                                     self.fx.manager)), 2)
        self.assertEqual(self.fx.store(box).count(), 2)

    def test_b_no_prompt_with_a_keyring_remembers_the_key(self) -> None:
        self.fx.with_hash(disabled=True).with_keyring_secrets().with_plain_file()
        result = self.flow().run()
        self.assertEqual(self.asked[0], ("new", False))
        self.assertTrue(U.is_remembered(self.fx.keyring, result.keybox.id))
        loaded = K.Keybox.load(self.fx.dir / "keybox.json")
        self.assertTrue(loaded.unlock_with_dek(U.remembered_dek(self.fx.keyring, loaded.id)))
        self.assertEqual(self.fx.store(loaded).get_password(SVC, "web.internal:22:deploy"), FILE_SECRET)
        self.assertNotIn(b"file-secret", b"".join(p.read_bytes() for p in self.fx.dir.iterdir() if p.is_file()))
        self.assertEqual(self.decision(), U.Startup.UNLOCK)
        # the remember entry is not an old copy: it survives the clean-up
        self.assertEqual(list(self.fx.keyring._store), [(U.REMEMBER_SERVICE, U.remember_key(loaded.id))])

    def test_c_no_prompt_without_a_keyring_cancel_is_safe_mode(self) -> None:
        self.fx.with_hash(disabled=True).with_plain_file()
        before = self.fx.snapshot()
        result = self.flow(keyring=None, new=("later", None)).run()
        self.assertTrue(result.unfinished)
        self.assertIsNone(result.keybox)
        self.assertEqual(self.fx.snapshot(), before)
        self.assertEqual(self.decision(), U.Startup.MIGRATE)
        result = self.flow(keyring=None).run()  # later: must type one (never remembered)
        self.assertFalse(result.unfinished)
        self.assertFalse(U.is_remembered(None, result.keybox.id))
        self.assert_finished(result, {"web.internal:22:deploy": FILE_SECRET}, password=NEW)

    def test_d_plain_file_without_hash(self) -> None:
        self.fx.with_plain_file()
        result = self.flow(keyring=None).run()
        self.assertEqual(self.asked[0], ("new", False))  # saved secrets exist: no "Don't save passwords"
        self.assert_finished(result, {"web.internal:22:deploy": FILE_SECRET}, password=NEW)
        self.assertFalse((self.fx.dir / "passwords.json.bak").exists() and
                         U.json_version(self.fx.dir / "passwords.json.bak") == 1)

    def test_e_encrypted_file_and_hash(self) -> None:
        self.fx.with_hash().with_encrypted_file()
        result = self.flow(keyring=None).run()
        self.assert_finished(result, {"web.internal:22:deploy": FILE_SECRET})

    def test_f_old_file_of_part_a_is_left_and_converts_afterwards(self) -> None:
        from tests.test_legacy_vault import write_old_vault
        from ssh_manager import legacy_vault as LV

        self.fx.with_hash().with_keyring_secrets()
        old = self.fx.dir / "vault.json"
        write_old_vault(old, OLD, [("v1", "Box", "root", "vault-pw-sentinel")])
        raw = old.read_bytes()
        result = self.flow().run()
        self.assertEqual(old.read_bytes(), raw)
        box = self.fx.unlocked()
        creds = CredentialStore(SecureStore.for_keybox(box, self.fx.dir / "passwords.json"),
                                self.fx.dir / "credentials.json")
        report = LV.convert_vault(LV.read_legacy_vault(old, OLD), creds, self.fx.manager)
        self.assertEqual(len(report.created), 1)
        self.assertNotIn(b"vault-pw-sentinel", (self.fx.dir / "passwords.json").read_bytes())
        found = [c for c in creds.list() if c.username == "root"]
        self.assertEqual(creds.get_password(found[0].id), "vault-pw-sentinel")
        self.assertFalse(result.unfinished)

    def test_g_macros_v1(self) -> None:
        self.fx.with_hash().with_macros()
        result = self.flow(keyring=None).run()
        self.assertEqual(result.report.macros, 1)
        box = self.fx.unlocked()
        store = MacroStore(self.fx.dir / "macros.enc")
        store.unlock_with_key(box.subkey("macros"), box.id)
        self.assertEqual([m.steps[0]["text"] for m in store.list_sorted()], [MACRO_TEXT])
        self.assert_finished(result, {})

    def test_h_fresh_install_and_dont_save_passwords(self) -> None:
        from ssh_manager.gtkui.unlock_flow import StartupFlow

        self.assertEqual(self.decision(), U.Startup.FIRST_RUN)
        flow = StartupFlow(self.fx.settings, self.fx.dir, keyring_factory=lambda: None)
        flow.ask_first_run = lambda: ("off", None)
        self.assertTrue(flow.run().passwords_off)
        self.assertEqual(self.decision(), U.Startup.PASSWORDS_OFF)
        self.fx.settings.master_password_disabled = True  # an old flag alone: "Don't save passwords" is offered
        self.fx.settings.passwords_off = False
        result = self.flow(new=("off", None)).run()
        self.assertEqual(self.asked, [("new", True)])
        self.assertTrue(result.passwords_off)

    def test_i_everything_at_once(self) -> None:
        self.fx.with_hash().with_keyring_secrets().with_encrypted_file().with_macros()
        (self.fx.dir / "vault.json").write_text("{}")
        result = self.flow().run()
        self.assertEqual((result.report.from_file, result.report.from_keyring, result.report.macros), (1, 2, 1))
        self.assertTrue((self.fx.dir / "vault.json").exists())
        # the file entry and the keyring entry of the same host: the file wins
        self.assert_finished(result, {f"credential:{self.fx.cred_id}": CRED_SECRET,
                                      "web.internal:22:deploy": FILE_SECRET})

    def test_j_a_failure_after_each_step_loses_nothing_and_a_rerun_converges(self) -> None:
        from ssh_manager.gtkui.unlock_flow import StartupFlow

        for step in MG.STEPS:
            with self.subTest(step=step):
                self.tearDown_fixture()
                self.fx.with_hash().with_keyring_secrets().with_encrypted_file().with_macros()
                result = self.flow(fail_at=step).run()
                self.assertTrue(result.unfinished)
                self.assertEqual(self.messages, ["Saved passwords could not be moved"])
                self.assertEqual(len(self.fx.keyring._store), 2)  # never removed on a failure
                self.assertEqual(self.decision(), U.Startup.RESUME)
                start = StartupFlow(self.fx.settings, self.fx.dir, keyring_factory=lambda: None)
                start.ask_password = lambda box: (OLD if box.unlock(OLD) else None, False)
                resumed = start.run()
                self.assertTrue(resumed.resume)
                result = self.flow().resume(resumed.keybox, resumed.typed)
                self.assert_finished(result, {f"credential:{self.fx.cred_id}": CRED_SECRET,
                                              "web.internal:22:deploy": FILE_SECRET})
                self.assertEqual(self.fx.keyring._store, {})

    def tearDown_fixture(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.fx = Fixture(Path(tmp.name))
        self.asked = []

    def test_j_disk_full_and_a_locked_keyring(self) -> None:
        self.fx.with_hash().with_keyring_secrets().with_encrypted_file()
        before = self.fx.snapshot()
        with mock.patch("ssh_manager.persistence.write_bytes_private", side_effect=OSError(28, "No space")):
            result = self.flow().run()
        self.assertTrue(result.unfinished)
        self.assertEqual({k: v for k, v in self.fx.snapshot()["files"].items() if k in before["files"]},
                         before["files"])
        self.tearDown_fixture()
        self.fx.with_hash().with_keyring_secrets()
        broken = mock.Mock()
        broken.get_password.side_effect = RuntimeError("locked")
        result = self.flow(keyring=broken).run()
        self.assertTrue(result.unfinished)
        self.assertEqual(len(self.fx.keyring._store), 2)
        self.assertEqual(self.decision(), U.Startup.RESUME)

    def test_j_wrong_password_and_cancel(self) -> None:
        self.fx.with_hash().with_encrypted_file()
        before = self.fx.snapshot()
        for flow in (self.flow(current="wrong"), self.flow(intro=False), self.flow(current=None)):
            result = flow.run()
            self.assertTrue(result.unfinished)
            self.assertEqual(self.fx.snapshot(), before)
            self.assertFalse((self.fx.dir / "keybox.json").exists())

    def test_k_keyring_without_some_entries(self) -> None:
        self.fx.with_hash().with_keyring_secrets()
        self.fx.keyring.delete_password(SVC, "web.internal:22:deploy")
        result = self.flow().run()
        self.assertEqual(result.report.from_keyring, 1)
        self.assertGreaterEqual(result.report.keyring_missing, 1)
        self.assert_finished(result, {f"credential:{self.fx.cred_id}": CRED_SECRET})

    def test_l_interrupted_migration_resumes(self) -> None:
        self.fx.with_hash().with_plain_file().with_macros()
        flow = self.flow(keyring=None, fail_at="switch")
        self.assertTrue(flow.run().unfinished)
        names = {p.name for p in self.fx.dir.iterdir()}
        self.assertIn("passwords.json.pre-keybox", names)
        self.assertEqual(U.json_version(self.fx.dir / "passwords.json"), 3)  # the new store is complete
        box = self.fx.unlocked()
        self.fx.store(box).set_password(SVC, "added:22:later", "x")  # saved meanwhile: kept
        self.asked.clear()
        result = self.flow(keyring=None).resume(box, None)
        self.assertEqual(self.asked, ["current"])  # macros.enc v1 needs the old password
        self.assert_finished(result, {"web.internal:22:deploy": FILE_SECRET, "added:22:later": "x"})


class TamperingTests(MigrationCase):
    def test_settings_changes_after_migration_do_nothing(self) -> None:
        self.fx.with_hash().with_keyring_secrets()
        self.flow().run()
        path = self.fx.settings.config_path
        for change in ({"master_password_disabled": True}, {"master_password_hash": ""},
                       {"master_password_hash": hash_master_password("attacker")}, {"passwords_off": True}):
            data = json.loads(path.read_text())
            data.update(change)
            path.write_text(json.dumps(data))
            with self.subTest(change=change):
                self.assertEqual(U.decide(U.inspect(self.fx.dir, AppSettings.load(path))), U.Startup.UNLOCK)
        self.assertEqual(json.loads((self.fx.dir / "passwords.json").read_text())["version"], 3)

    def test_missing_or_foreign_keybox(self) -> None:
        self.fx.with_hash().with_keyring_secrets()
        self.flow().run()
        (self.fx.dir / "keybox.json").rename(self.fx.dir / "moved.json")
        self.assertEqual(self.decision(), U.Startup.KEY_MISSING)
        foreign = K.Keybox.create(OLD, self.fx.dir / "keybox.json", scrypt_n=FAST)
        store = EncryptedFileKeyring(foreign, self.fx.dir / "passwords.json")
        self.assertTrue(store.locked)
        self.assertIn("different key", store.error)

    def test_no_secret_in_logs(self) -> None:
        self.fx.with_hash().with_keyring_secrets().with_encrypted_file().with_macros()
        with self.assertLogs(level=logging.DEBUG) as logs:
            logging.getLogger("ssh_manager").debug("start")
            self.flow(fail_at="keep_old").run()
            self.flow(current="wrong").resume(K.Keybox.load(self.fx.dir / "keybox.json"), None)
            box = self.fx.unlocked()
            self.flow().resume(box, OLD)
        text = "\n".join(logs.output) + "\n".join(self.messages)
        for secret in (HOST_SECRET, CRED_SECRET, FILE_SECRET, MACRO_TEXT, OLD, NEW):
            self.assertNotIn(secret, text)


class ReaderTests(unittest.TestCase):
    def test_the_old_reader_never_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "passwords.json"
            path.write_text(json.dumps({SVC: {"k": FILE_SECRET}}))
            before = path.read_bytes()
            self.assertEqual(MG.read_password_file(path, OLD), {(SVC, "k"): FILE_SECRET})
            self.assertEqual(path.read_bytes(), before)  # FileKeyring(path, password) would encrypt it in place
            FileKeyring(path, master_password=OLD)
            self.assertNotEqual(path.read_bytes(), before)
            with self.assertRaises(MG.MigrationError) as caught:
                MG.read_password_file(path, "wrong")
            self.assertEqual(caught.exception.code, "password")
            self.assertEqual(MG.read_password_file(path, OLD), {(SVC, "k"): FILE_SECRET})
            path.write_text(json.dumps({"version": 9, "data": "x"}))
            with self.assertRaises(MG.MigrationError):
                MG.read_password_file(path, OLD)


if __name__ == "__main__":
    unittest.main()
