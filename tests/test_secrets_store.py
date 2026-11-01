"""``passwords.json`` version 3 (encrypted with the keybox) and the "passwords off" store."""

import base64
import json
import logging
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from ophion import keybox as K
from ophion.credentials import CredentialStore
from ophion.secure_store import (
    DisabledSecureStore,
    EncryptedFileKeyring,
    KeyringError,
    SecureStore,
    SecureStoreError,
)

SECRET = "S3cret-sentinel-رمز"


class StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.path = self.folder / "passwords.json"
        self.box = K.Keybox.create("pw", self.folder / "keybox.json", scrypt_n=2 ** 15)

    def store(self, box=None) -> SecureStore:
        return SecureStore.for_keybox(box or self.box, self.path)


class SecretsStoreTests(StoreCase):
    def test_round_trip_unicode_large_and_empty(self) -> None:
        store = self.store()
        self.assertIsNone(store.get("h", 22, "u"))  # empty store
        store.set("h", 22, "u", SECRET)
        store.set("h", 22, "big", "x" * 200_000)
        reopened = self.store()
        self.assertEqual(reopened.get("h", 22, "u"), SECRET)
        self.assertEqual(len(reopened.get("h", 22, "big")), 200_000)
        reopened.delete("h", 22, "u")
        self.assertIsNone(self.store().get("h", 22, "u"))
        raw = self.path.read_bytes()
        self.assertNotIn(SECRET.encode(), raw)
        document = json.loads(raw)
        self.assertEqual((document["format"], document["version"], document["keybox_id"]),
                         ("ssh-manager-secrets", 3, self.box.id))
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertTrue(self.path.with_name("passwords.json.bak").exists())
        self.assertNotIn(SECRET.encode(), self.path.with_name("passwords.json.bak").read_bytes())

    def test_tampering_is_detected_and_the_file_kept(self) -> None:
        self.store().set("h", 22, "u", SECRET)
        original = json.loads(self.path.read_text())

        def flip(text):
            data = bytearray(base64.b64decode(text))
            data[0] ^= 1
            return base64.b64encode(bytes(data)).decode()

        for name, change in {"nonce": lambda d: d.update(nonce=flip(d["nonce"])),
                             "data": lambda d: d.update(data=flip(d["data"])),
                             "version": lambda d: d.update(version=4),
                             "keybox id": lambda d: d.update(keybox_id=base64.b64encode(b"x" * 16).decode())}.items():
            changed = json.loads(json.dumps(original))
            change(changed)
            self.path.write_text(json.dumps(changed))
            before = self.path.read_bytes()
            store = self.store()
            with self.subTest(name=name):
                self.assertTrue(store.backend.locked)
                with self.assertRaises(SecureStoreError):
                    store.get("h", 22, "u")
                with self.assertRaises(SecureStoreError):
                    store.set("h", 22, "other", "x")  # never overwrites what it cannot read
                self.assertEqual(self.path.read_bytes(), before)
        self.path.write_text(json.dumps(original))
        other_box = K.Keybox.create("pw", self.folder / "other.json", scrypt_n=2 ** 15)
        foreign = self.store(other_box)
        self.assertIn("different key", foreign.backend.error)

    def test_a_failed_write_keeps_the_previous_file(self) -> None:
        store = self.store()
        store.set("h", 22, "u", "first")
        before = self.path.read_bytes()
        with mock.patch("ophion.persistence.write_bytes_private", side_effect=OSError(28, "No space")):
            with self.assertRaises(SecureStoreError):
                store.set("h", 22, "u", "second")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.store().get("h", 22, "u"), "first")
        self.assertEqual([p.name for p in self.folder.iterdir() if p.name.endswith(".tmp")], [])

    def test_two_instances_and_threads_do_not_lose_updates(self) -> None:
        first, second = self.store(), self.store()
        first.set("h", 22, "a", "1")
        second.set("h", 22, "b", "2")  # re-reads before writing: "a" survives
        first.set("h", 22, "c", "3")
        self.assertEqual({k[1]: v for k, v in self.store().backend.items().items()},
                         {"h:22:a": "1", "h:22:b": "2", "h:22:c": "3"})
        stores = [self.store() for _ in range(4)]

        def writer(index):
            for n in range(10):
                stores[index].set("t", 22, f"{index}-{n}", "v")

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(self.store().backend.count(), 3 + 40)

    def test_credentials_use_it(self) -> None:
        creds = CredentialStore(self.store(), self.folder / "credentials.json")
        cred = creds.add("Ops", "deploy", SECRET)
        self.assertEqual(CredentialStore(self.store(), self.folder / "credentials.json").get_password(cred.id), SECRET)

    def test_disabled_store(self) -> None:
        store = DisabledSecureStore()
        self.assertTrue(store.saving_disabled)
        self.assertIsNone(store.get("h", 22, "u"))
        for call in (lambda: store.set("h", 22, "u", "x"), lambda: store.delete("h", 22, "u")):
            with self.assertRaises(SecureStoreError) as caught:
                call()
            self.assertEqual(str(caught.exception), "Saving passwords is turned off")
        creds = CredentialStore(store, self.folder / "c.json")
        with self.assertRaises(SecureStoreError):
            creds.add("Ops", "deploy", "pw")

    def test_no_secret_in_logs(self) -> None:
        with self.assertLogs(level=logging.DEBUG) as logs:
            logging.getLogger("ophion").debug("start")
            store = self.store()
            store.set("h", 22, "u", SECRET)
            store.get("h", 22, "u")
            self.path.write_text(self.path.read_text().replace('"data": "', '"data": "AA'))
            broken = self.store()
            with self.assertRaises(SecureStoreError) as caught:
                broken.get("h", 22, "u")
        self.assertNotIn(SECRET, "\n".join(logs.output) + str(caught.exception))
        self.assertIsInstance(EncryptedFileKeyring(self.box, self.folder / "fresh.json").count(), int)
        self.assertTrue(issubclass(KeyringError, Exception))


if __name__ == "__main__":
    unittest.main()
