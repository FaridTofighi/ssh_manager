"""The keybox: one data key wrapped by the master password (and an optional recovery code)."""

import base64
import json
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from ssh_manager import keybox as K

FAST = 2 ** 15  # the minimum the reader accepts; the default is slower on purpose


class KeyboxCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name) / "config"
        self.path = self.folder / "keybox.json"
        patcher = mock.patch.object(K, "KEYBOX_SCRYPT_N", FAST)
        patcher.start()
        self.addCleanup(patcher.stop)

    def create(self, password="correct horse"):
        return K.Keybox.create(password, self.path, scrypt_n=FAST)


class KeyboxTests(KeyboxCase):
    def test_create_file_and_unlock(self) -> None:
        box = self.create()
        self.assertFalse(box.locked)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.folder.stat().st_mode), 0o700)
        document = json.loads(self.path.read_text())
        self.assertEqual((document["format"], document["version"]), ("ssh-manager-keybox", 1))
        self.assertEqual([u["type"] for u in document["unlockers"]], ["passphrase"])
        self.assertEqual(len(base64.b64decode(document["unlockers"][0]["kdf"]["salt"])), 16)
        self.assertNotIn("correct horse", self.path.read_text())
        subkey = box.subkey("secrets")
        loaded = K.Keybox.load(self.path)
        self.assertTrue(loaded.locked)
        self.assertFalse(loaded.unlock("wrong"))
        self.assertTrue(loaded.locked)
        self.assertTrue(loaded.unlock("correct horse"))
        self.assertEqual(loaded.subkey("secrets"), subkey)  # stable
        self.assertEqual(len({loaded.subkey(name) for name in ("secrets", "macros", "export")}), 3)  # per store
        loaded.forget()
        self.assertTrue(loaded.locked)
        with self.assertRaises(K.KeyboxError):
            loaded.subkey("secrets")
        with self.assertRaises(K.KeyboxError):
            K.Keybox.create("x", self.path)  # never replaced

    def test_tampering_and_damage(self) -> None:
        self.create()
        original = json.loads(self.path.read_text())

        def variant(change):
            document = json.loads(json.dumps(original))
            change(document)
            self.path.write_text(json.dumps(document))
            return document

        unlock_fails = {
            "kdf n raised": lambda d: d["unlockers"][0]["kdf"].update(n=2 ** 16),
            "salt": lambda d: d["unlockers"][0]["kdf"].update(salt=base64.b64encode(os.urandom(16)).decode()),
            "id": lambda d: d.update(id=base64.b64encode(os.urandom(16)).decode()),
            "nonce": lambda d: d["unlockers"][0].update(nonce=base64.b64encode(os.urandom(12)).decode()),
            "wrapped": lambda d: d["unlockers"][0].update(wrapped=base64.b64encode(os.urandom(48)).decode()),
            "check": lambda d: d["check"].update(data=base64.b64encode(os.urandom(40)).decode()),
        }
        for name, change in unlock_fails.items():
            variant(change)
            with self.subTest(name=name):
                self.assertFalse(K.Keybox.load(self.path).unlock("correct horse"))
        refused = {
            "weak scrypt": (lambda d: d["unlockers"][0]["kdf"].update(n=1024), "unsafe"),
            "weak pbkdf2": (lambda d: d["unlockers"][0].update(kdf={"name": "pbkdf2-sha256", "iterations": 1000,
                                                                    "salt": d["unlockers"][0]["kdf"]["salt"]}), "unsafe"),
            "weak argon2": (lambda d: d["unlockers"][0].update(kdf={"name": "argon2id", "time_cost": 3,
                                                                    "memory_kib": 1024, "parallelism": 4,
                                                                    "salt": d["unlockers"][0]["kdf"]["salt"]}), "unsafe"),
            "unknown type": (lambda d: d["unlockers"][0].update(type="magic"), "damaged"),
            "newer": (lambda d: d.update(version=2), "newer"),
            "format": (lambda d: d.update(format="x"), "damaged"),
        }
        for name, (change, code) in refused.items():
            variant(change)
            with self.subTest(name=name), self.assertRaises(K.KeyboxError) as caught:
                K.Keybox.load(self.path)
            self.assertEqual(caught.exception.code, code)
        for broken in (b"", b"{", self.path.read_bytes()[:50]):
            self.path.write_bytes(broken)
            with self.assertRaises(K.KeyboxError):
                K.Keybox.load(self.path)
        self.path.unlink()
        with self.assertRaises(K.KeyboxError) as caught:
            K.Keybox.load(self.path)
        self.assertEqual(caught.exception.code, "missing")

    def test_argon2_without_support_is_a_clear_error(self) -> None:
        self.create()
        document = json.loads(self.path.read_text())
        document["unlockers"][0]["kdf"] = {"name": "argon2id", "time_cost": 3, "memory_kib": 65536,
                                           "parallelism": 4, "salt": document["unlockers"][0]["kdf"]["salt"]}
        self.path.write_text(json.dumps(document))
        with mock.patch("ssh_manager.aead.argon2_available", return_value=False):
            with self.assertRaises(K.KeyboxError) as caught:
                K.Keybox.load(self.path)
        self.assertEqual(caught.exception.code, "unsupported")

    def test_change_password_keeps_the_key(self) -> None:
        box = self.create("old password")
        subkeys = {name: box.subkey(name) for name in K.STORES}
        self.assertFalse(box.change_password("wrong", "new password"))
        self.assertTrue(box.change_password("old password", "new password"))
        loaded = K.Keybox.load(self.path)
        self.assertFalse(loaded.unlock("old password"))
        self.assertTrue(loaded.unlock("new password"))
        self.assertEqual({name: loaded.subkey(name) for name in K.STORES}, subkeys)
        self.assertTrue(self.path.with_name("keybox.json.bak").exists())

    def test_a_failed_change_keeps_the_old_password(self) -> None:
        box = self.create("old password")
        before = self.path.read_bytes()
        with mock.patch("ssh_manager.persistence.write_bytes_private", side_effect=OSError(28, "No space")):
            with self.assertRaises(K.KeyboxError) as caught:
                box.change_password("old password", "new password")
        self.assertEqual(caught.exception.code, "write")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertTrue(K.Keybox.load(self.path).unlock("old password"))
        self.assertEqual([p.name for p in self.folder.iterdir() if p.name.endswith(".tmp")], [])

    def test_recovery_code(self) -> None:
        box = self.create()
        dek = box.dek()
        code = box.add_recovery()
        groups = code.split("-")
        self.assertEqual((len(groups), {len(g) for g in groups}), (8, {4}))
        self.assertNotIn(code.replace("-", ""), self.path.read_text())
        loaded = K.Keybox.load(self.path)
        self.assertTrue(loaded.has_recovery)
        self.assertFalse(loaded.unlock_with_recovery("AAAA-" * 7 + "AAAA"))
        self.assertTrue(loaded.unlock_with_recovery(code.lower().replace("-", " ")))  # forgiving input
        self.assertEqual(loaded.dek(), dek)
        loaded.reset_password("brand new")  # "forgot master password"
        again = K.Keybox.load(self.path)
        self.assertTrue(again.unlock("brand new"))
        self.assertFalse(again.unlock("correct horse"))
        newer = again.add_recovery()
        self.assertFalse(K.Keybox.load(self.path).unlock_with_recovery(code))  # regenerated: the old one stops
        self.assertTrue(K.Keybox.load(self.path).unlock_with_recovery(newer))
        again.remove_recovery()
        self.assertFalse(K.Keybox.load(self.path).has_recovery)
        self.assertFalse(K.Keybox.load(self.path).unlock_with_recovery(newer))

    def test_dek_from_elsewhere_is_verified(self) -> None:
        box = self.create()
        dek = box.dek()
        loaded = K.Keybox.load(self.path)
        self.assertFalse(loaded.unlock_with_dek(os.urandom(32)))
        self.assertFalse(loaded.unlock_with_dek(b"short"))
        self.assertTrue(loaded.unlock_with_dek(dek))

    def test_record_migration_and_timing(self) -> None:
        box = self.create()
        box.record_migration({"from": "test", "counts": {"secrets": 3}})
        self.assertEqual(K.Keybox.load(self.path).migrated["counts"], {"secrets": 3})
        started = time.perf_counter()
        default_path = self.folder / "default.json"
        with mock.patch.object(K, "KEYBOX_SCRYPT_N", 2 ** 17):
            K.Keybox.create("pw", default_path, scrypt_n=2 ** 17)
            created = time.perf_counter() - started
            started = time.perf_counter()
            self.assertTrue(K.Keybox.load(default_path).unlock("pw"))
        print(f"\n[keybox] scrypt n=2^17 r=8 p=1: create {created * 1000:.0f} ms, unlock "
              f"{(time.perf_counter() - started) * 1000:.0f} ms")


if __name__ == "__main__":
    unittest.main()
