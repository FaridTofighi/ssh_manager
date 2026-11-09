"""Characterization (§2.4.6): what the app reads and writes, against committed goldens (R8, R9).

The inputs are ``tests/refactor/fixtures`` (``make_fixtures.py``, written once on the baseline). Each test
loads with the current code, saves, and compares what was written:

* ``settings.json``, ``ui_state.json``, ``smart_groups.json``: load → save → the JSON written;
* the inventory database: schema (``sqlite_master``), row counts, ``user_version`` and the content (ids
  and timestamps replaced by stable names);
* ``.ophion`` exports, plain and encrypted: import into a fresh store, export again;
* the keybox, ``passwords.json`` and ``macros.enc``: unlock with the known master password and read.

Files are 0600 and folders 0700 where the app makes them so.
"""

import dataclasses
import json
import os
import shutil
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.refactor import golden
from tests.refactor.make_fixtures import EXPORT_PASSPHRASE, FIXTURES, MASTER_PASSWORD, PASSWORDS


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        env = {"HOME": str(self.tmp / "home"), "XDG_CONFIG_HOME": str(self.tmp / "config"),
               "XDG_DATA_HOME": str(self.tmp / "data"), "XDG_STATE_HOME": str(self.tmp / "state")}
        patch = mock.patch.dict(os.environ, env)
        patch.start()
        self.addCleanup(patch.stop)

    def copy(self, name: str) -> Path:
        target = self.tmp / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(FIXTURES / name, target)
        return target


class SettingsFileTests(Case):
    def test_settings_round_trip(self) -> None:
        from ophion.settings import AppSettings

        path = self.tmp / "config" / "ophion" / "settings.json"
        path.parent.mkdir(parents=True)
        shutil.copyfile(FIXTURES / "settings.json", path)
        settings = AppSettings.load(path)
        settings.save()
        self.assertEqual(mode(path), 0o600)
        golden.check(self, "persist_settings", json.loads(path.read_text(encoding="utf-8")))

    def test_settings_defaults(self) -> None:
        from ophion.settings import AppSettings

        path = self.tmp / "config" / "ophion" / "settings.json"
        AppSettings.load(path).save()
        written = json.loads(path.read_text(encoding="utf-8"))
        written["audit_log_path"] = written["audit_log_path"].replace(str(self.tmp), "<TMP>")
        golden.check(self, "persist_settings_defaults", written)

    def test_ui_state_round_trip(self) -> None:
        from ophion.gtkui.controllers.ui_state import UiState, load_ui_state, save_ui_state

        source = self.copy("ui_state.json")
        state = UiState.from_dict(load_ui_state(source))
        target = self.tmp / "out" / ".ui_state.json"
        self.assertTrue(save_ui_state(target, state.to_dict()))
        self.assertEqual(mode(target), 0o600)
        golden.check(self, "persist_ui_state", json.loads(target.read_text(encoding="utf-8")))

    def test_smart_groups_round_trip(self) -> None:
        from ophion.smart_groups import load_smart_groups, save_smart_groups

        groups = load_smart_groups(self.copy("smart_groups.json"))
        target = self.tmp / "out" / "smart_groups.json"
        target.parent.mkdir(parents=True)
        self.assertTrue(save_smart_groups(groups, target))
        self.assertEqual(mode(target), 0o600)
        golden.check(self, "persist_smart_groups", json.loads(target.read_text(encoding="utf-8")))


class InventoryTests(Case):
    def dump(self, db: Path) -> dict:
        connection = sqlite3.connect(db)
        try:
            schema = sorted([kind, name, sql] for kind, name, sql in connection.execute(
                "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"))
            tables = [name for kind, name, _sql in schema if kind == "table"]
            counts = {name: connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] for name in tables}
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            groups = connection.execute("SELECT id, parent_id, name, name_key, position, note FROM groups").fetchall()
            hosts = connection.execute("SELECT id, group_id, alias, alias_key, hostname, port, user, options_json, "
                                       "metadata_json, position, protocol FROM hosts").fetchall()
            meta = sorted(connection.execute("SELECT key, value FROM meta").fetchall())
        finally:
            connection.close()
        names = {}
        by_id = {row[0]: row for row in groups}

        def group_path(group_id):
            parts = []
            while group_id:
                row = by_id[group_id]
                parts.append(row[2])
                group_id = row[1]
            return "/".join(reversed(parts))

        for row in groups:
            names[row[0]] = f"<group:{group_path(row[0])}>"
        for row in hosts:
            names[row[0]] = f"<host:{group_path(row[1])}/{row[2]}>"

        def stable(value):
            text = json.dumps(value, ensure_ascii=False)
            for raw, name in names.items():
                text = text.replace(raw, name)
            return json.loads(text)

        content = {
            "groups": sorted(stable([group_path(r[0]), r[3], r[4], r[5]]) for r in groups),
            "hosts": sorted((stable([group_path(r[1]), r[2], r[3], r[4], r[5], r[6], json.loads(r[7] or "{}"),
                                     json.loads(r[8] or "{}"), r[9], r[10]]) for r in hosts), key=json.dumps),
        }
        return {"schema": schema, "counts": counts, "user_version": version,
                "meta": [k for k, _v in meta], "content": content}

    def test_populated_database(self) -> None:
        from tests.native_fixture import build_rich, make_store

        folder = self.tmp / "data" / "ophion"
        store = make_store(folder)
        try:
            build_rich(store)
        finally:
            store.close()
        db = folder / "inventory.db"
        self.assertEqual(mode(db), 0o600)
        self.assertEqual(mode(folder), 0o700)
        golden.check(self, "persist_inventory_db", self.dump(db))


class ExportTests(Case):
    def reexport(self, source: Path, passphrase=None) -> dict:
        from ophion import native_format as F
        from ophion.exporters.native import build_export
        from ophion.importers import native as N
        from ophion.importers.mobaxterm import ExistingInventory
        from ophion.inventory.manager import StoreConfigManager
        from tests.native_fixture import make_store
        from tests.test_native_commit import FakeCredential, FakeCredentialStore

        document = N.read_native(source.read_bytes(), passphrase)
        store = make_store(Path(tempfile.mkdtemp(dir=self.tmp)))  # a fresh store per import
        self.addCleanup(store.close)
        manager = StoreConfigManager(store)
        credentials = FakeCredentialStore([FakeCredential("ops", "Ops account", "deploy"),
                                           FakeCredential("pg", "Postgres", "postgres")])
        plan = N.plan_native(document, N.NativeImportOptions(), (), ExistingInventory.from_store(store))
        mapping = N.suggest_credentials(plan, credentials.list())
        job = N.apply_mapping(plan, mapping, {c.id: c for c in credentials.list()}, key_exists=lambda _p: True)
        report = N.commit_native(job, manager, credentials)
        self.assertEqual(report.status, N.ImportStatus.SUCCESS, report.message)
        by_id = {c.id: c for c in credentials.list(include_private=True)}
        bound = {host_id: by_id.get(cred_id) for host_id, cred_id in credentials.bindings.items()}
        doc, _report = build_export(store, group_colors=report.group_colors, host_colors=report.host_colors,
                                    credential_for=bound.get, now=0, app_version="fixture")
        return json.loads(F.dump_document(doc))

    def test_plain_export_imports_and_exports_again(self) -> None:
        source = json.loads((FIXTURES / "rich.ophion").read_text(encoding="utf-8"))
        again = self.reexport(FIXTURES / "rich.ophion")
        golden.check(self, "persist_ophion_reexport", again)
        self.assertEqual(again["counts"], source["counts"])

    def test_encrypted_export_decrypts_to_the_plain_one(self) -> None:
        from ophion import native_format as F
        from ophion.importers import native as N

        plain = N.read_native((FIXTURES / "rich.ophion").read_bytes())
        decrypted = N.read_native((FIXTURES / "rich-encrypted.ophion").read_bytes(), EXPORT_PASSPHRASE)
        read_time = {"encrypted", "size"}  # describe the file, not its content
        self.assertTrue(decrypted.encrypted and not plain.encrypted)
        self.assertEqual({k: v for k, v in dataclasses.asdict(decrypted).items() if k not in read_time},
                         {k: v for k, v in dataclasses.asdict(plain).items() if k not in read_time})
        self.assertEqual(self.reexport(FIXTURES / "rich-encrypted.ophion", EXPORT_PASSPHRASE),
                         self.reexport(FIXTURES / "rich.ophion"))
        with self.assertRaises(Exception):
            N.read_native((FIXTURES / "rich-encrypted.ophion").read_bytes(), "wrong passphrase")
        header = json.loads((FIXTURES / "rich-encrypted.ophion").read_text(encoding="utf-8"))
        self.assertEqual((header["format"], F.ENCRYPTED_FORMAT), ("ssh-manager-export-encrypted",) * 2)


class KeyboxTests(Case):
    def keys(self) -> Path:
        folder = self.tmp / "keys"
        shutil.copytree(FIXTURES / "keybox", folder)
        folder.chmod(0o700)
        for path in folder.iterdir():
            path.chmod(0o600)
        return folder

    def test_the_stores_open_with_the_master_password(self) -> None:
        from ophion.keybox import Keybox
        from ophion.macros import MacroStore
        from ophion.secure_store import SecureStore

        folder = self.keys()
        keybox = Keybox.load(folder / "keybox.json")
        self.assertFalse(keybox.unlock("not the password"))
        self.assertTrue(keybox.unlock(MASTER_PASSWORD))
        secrets = SecureStore.for_keybox(keybox, folder / "passwords.json")
        self.assertEqual({key: secrets.get(*key) for key in PASSWORDS}, PASSWORDS)
        macros = MacroStore(folder / "macros.enc")
        macros.unlock_with_key(keybox.subkey("macros"), keybox.id)
        listed = [[m.name, m.shortcut, m.steps] for m in macros.list_sorted()]
        golden.check(self, "persist_macros", golden.mask(listed, ("macro-secret",)))
        self.assertIn("macro-secret", json.dumps(listed))  # the secret step decrypts to the original text

    def test_frozen_identifiers(self) -> None:
        """R8: the format identifiers no refactor may change."""

        import base64

        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF

        from ophion import keybox, migration, native_format, secure_store, unlock
        from ophion.inventory import store

        box = keybox.Keybox.load(self.keys() / "keybox.json")
        self.assertTrue(box.unlock(MASTER_PASSWORD))
        for name in ("secrets", "macros", "export"):  # HKDF info "ssh-manager/<store>/v1", salt = the keybox id
            expected = HKDF(algorithm=hashes.SHA256(), length=32, salt=base64.b64decode(box.id),
                            info=f"ssh-manager/{name}/v1".encode("ascii")).derive(bytes(box._dek))
            self.assertEqual(box.subkey(name), expected, name)

        document = json.loads((FIXTURES / "keybox" / "keybox.json").read_text(encoding="utf-8"))
        found = {
            "keybox_format": keybox.FORMAT,
            "keybox_file_format": document.get("format"),
            "export_format": native_format.FORMAT,
            "export_encrypted_format": native_format.ENCRYPTED_FORMAT,
            "schema_version": store.SCHEMA_VERSION,
            "keyring_services": [secure_store.SecureStore.service_name, migration.SERVICE, unlock.REMEMBER_SERVICE],
        }
        golden.check(self, "persist_frozen_identifiers", found)


if __name__ == "__main__":
    unittest.main()
