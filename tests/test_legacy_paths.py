"""Moving "SSH Manager"'s folders to Ophion's (legacy_paths), against a temporary HOME / XDG."""

import errno
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from ophion import legacy_paths as L

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "pre_rename"
PASSWORD = "pre-rename-master"
HOSTS = 1200


def tree_digest(root: Path) -> dict:
    """Relative path → (mode, sha256) of every file (what a move must keep)."""

    found = {}
    for folder, _dirs, files in os.walk(root):
        for name in files:
            path = Path(folder) / name
            found[str(path.relative_to(root))] = (stat.S_IMODE(path.stat().st_mode),
                                                  hashlib.sha256(path.read_bytes()).hexdigest())
    return found


class LegacyCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        env = {"HOME": str(self.root / "home"), "XDG_CONFIG_HOME": str(self.root / "config"),
               "XDG_DATA_HOME": str(self.root / "data"), "XDG_STATE_HOME": str(self.root / "state")}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(L.use_legacy_dirs, False)

    def old(self, kind: str) -> Path:
        return L.legacy_dir(kind)

    def new(self, kind: str) -> Path:
        return L.new_dir(kind)

    def populate(self, hosts: int = HOSTS) -> str:
        """The old folders as SSH Manager left them; returns a digest of the inventory's hosts."""

        from ophion.credentials import CredentialStore
        from ophion.inventory.store import InventoryStore, NewHost
        from ophion.keybox import Keybox
        from ophion.secure_store import SecureStore

        config, data, state = self.old("config"), self.old("data"), self.old("state")
        for folder in (config, data, state):
            folder.mkdir(mode=0o700, parents=True)
        for name in ("keybox.json", "passwords.json", "macros.enc"):
            shutil.copy2(FIXTURES / name, config / name)
            os.chmod(config / name, 0o600)
        (config / "settings.json").write_text(json.dumps(
            {"host_key_policy": "auto-add", "audit_log_path": str(data / "audit.log")}), encoding="utf-8")
        os.chmod(config / "settings.json", 0o600)
        box = Keybox.load(config / "keybox.json")
        self.assertTrue(box.unlock(PASSWORD))
        creds = CredentialStore(SecureStore.for_keybox(box, config / "passwords.json"), config / "credentials.json")
        creds.add("deploy", "deploy", "credential-secret")
        store = InventoryStore(data / "inventory.db")
        groups = [store.create_group(None, f"group-{g:02d}") for g in range(12)]
        store.add_hosts_bulk(NewHost(groups[i % 12].id, f"host-{i:04d}", hostname=f"10.0.{i // 250}.{i % 250}")
                             for i in range(hosts))
        digest = self.hosts_digest(store)
        store.close()
        (state / "protocol.log").write_text("old log\n", encoding="utf-8")
        return digest

    @staticmethod
    def hosts_digest(store) -> str:
        rows = sorted((h.alias, h.hostname, h.group_id) for h in store.list_hosts())
        return f"{len(store.list_groups())}:{len(rows)}:" + hashlib.sha256(repr(rows).encode()).hexdigest()

    def migrate(self, **kw):
        kw.setdefault("app_running", lambda: False)
        return L.migrate(**kw)

    def assert_app_reads_everything(self, inventory_digest: str) -> None:
        """Settings, keybox, saved passwords, credentials, macros and the inventory, where the code looks."""

        from ophion.credentials import CredentialStore
        from ophion.inventory.store import InventoryStore, default_inventory_path
        from ophion.keybox import Keybox, default_keybox_path
        from ophion.macros import MacroStore, default_macros_path
        from ophion.secure_store import SecureStore
        from ophion.settings import AppSettings

        settings = AppSettings.load()
        self.assertEqual(settings.host_key_policy, "auto-add")
        self.assertEqual(Path(settings.audit_log_path), L.app_dir("data") / "audit.log")
        box = Keybox.load(default_keybox_path())
        self.assertTrue(box.unlock(PASSWORD))
        secrets = SecureStore.for_keybox(box)
        self.assertEqual(secrets.backend.get_password("ssh-manager", "web-01"), "pre-rename-secret")
        self.assertEqual([c.name for c in CredentialStore(secrets).list()], ["deploy"])
        macros = MacroStore(default_macros_path())
        macros.unlock_with_key(box.subkey("macros"), box.id)
        self.assertEqual([m.name for m in macros.list_sorted()], ["greet"])
        store = InventoryStore(default_inventory_path())
        try:
            self.assertEqual(self.hosts_digest(store), inventory_digest)
        finally:
            store.close()


class MigrationTests(LegacyCase):
    def test_fresh_install_does_nothing(self) -> None:
        result = self.migrate()
        self.assertEqual(result.status, L.NOTHING)
        for kind in L.KINDS:
            self.assertFalse(self.new(kind).exists())
            self.assertFalse(self.old(kind).exists())

    def test_rename_symlink_marker_and_everything_opens(self) -> None:
        digest = self.populate()
        before = {kind: tree_digest(self.old(kind)) for kind in L.KINDS}
        started = time.monotonic()
        result = self.migrate(clock=lambda: 0)
        seconds = time.monotonic() - started
        self.assertEqual(result.status, L.DONE)
        self.assertEqual(sorted((m.kind, m.method) for m in result.moved),
                         [("config", "rename"), ("data", "rename"), ("state", "rename")])
        for kind in L.KINDS:
            self.assertTrue(self.old(kind).is_symlink())
            self.assertEqual(self.old(kind).resolve(), self.new(kind).resolve())
            marker = json.loads((self.new(kind) / L.MARKER).read_text())
            self.assertEqual((marker["from"], marker["method"]), (str(self.old(kind)), "rename"))
            after = tree_digest(self.new(kind))
            after.pop(L.MARKER)
            self.assertEqual(after, before[kind])  # same files, same modes (0600 stays 0600)
            self.assertEqual(stat.S_IMODE(self.new(kind).stat().st_mode), 0o700)
        self.assert_app_reads_everything(digest)
        self.assertLess(seconds, 5)
        self.assertEqual(self.migrate().status, L.NOTHING)  # idempotent: never again

    def test_new_folder_present_leaves_both_untouched(self) -> None:
        self.populate(hosts=3)
        self.new("config").mkdir(parents=True)
        (self.new("config") / "settings.json").write_text("{}", encoding="utf-8")
        before = tree_digest(self.old("config"))
        result = self.migrate()
        self.assertEqual({m.kind for m in result.moved}, {"data", "state"})  # config: never merged
        self.assertFalse(self.old("config").is_symlink())
        self.assertEqual(tree_digest(self.old("config")), before)
        self.assertEqual((self.new("config") / "settings.json").read_text(), "{}")

    def test_old_app_running_changes_nothing(self) -> None:
        self.populate(hosts=3)
        before = {kind: tree_digest(self.old(kind)) for kind in L.KINDS}
        self.assertEqual(self.migrate(app_running=lambda: True).status, L.BLOCKED)
        for kind in L.KINDS:
            self.assertFalse(self.new(kind).exists())
            self.assertEqual(tree_digest(self.old(kind)), before[kind])

    def test_the_open_database_of_the_old_app_is_detected(self) -> None:
        self.populate(hosts=3)
        database = self.old("data") / "inventory.db"
        self.assertFalse(L.database_in_use(database))
        holder = subprocess.Popen([sys.executable, "-c", (
            "import sqlite3, sys, time\n"
            f"c = sqlite3.connect({str(database)!r}, isolation_level=None)\n"
            "c.execute('PRAGMA journal_mode=WAL'); c.execute('SELECT count(*) FROM hosts').fetchall()\n"
            "print('open', flush=True); sys.stdin.read()\n")], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "open")
            with mock.patch.object(L, "bus_name_owned", lambda name=L.LEGACY_APP_ID: False):
                self.assertTrue(L.legacy_app_running())
                self.assertEqual(L.migrate().status, L.BLOCKED)
        finally:
            holder.stdin.close()
            holder.wait(10)
            holder.stdout.close()
        self.assertFalse(L.database_in_use(database))

    def test_rename_impossible_falls_back_to_a_verified_copy(self) -> None:
        digest = self.populate()
        before = {kind: tree_digest(self.old(kind)) for kind in L.KINDS}
        real_rename = os.rename
        olds = {str(self.old(kind)) for kind in L.KINDS}

        def cross_device(source, target):
            if str(source) in olds:
                raise OSError(errno.EXDEV, "Invalid cross-device link")
            return real_rename(source, target)

        with mock.patch.object(L.os, "rename", cross_device):
            result = self.migrate()
        self.assertEqual(result.status, L.COPIED)
        self.assertEqual({m.method for m in result.moved}, {"copy"})
        for kind in L.KINDS:
            self.assertFalse(self.old(kind).is_symlink())  # the old folder is left exactly as it was
            self.assertEqual(tree_digest(self.old(kind)), before[kind])
            copied = tree_digest(self.new(kind))
            copied.pop(L.MARKER)
            self.assertEqual(copied, before[kind])  # same bytes, same modes
            self.assertFalse(list(self.new(kind).parent.glob("*.partial-*")))
        self.assert_app_reads_everything(digest)

    def test_a_damaged_copy_aborts_and_this_run_uses_the_old_folders(self) -> None:
        self.populate(hosts=3)
        before = {kind: tree_digest(self.old(kind)) for kind in L.KINDS}
        real_copy = shutil.copy2

        def damaging_copy(source, target, *a, **kw):
            result = real_copy(source, target, *a, **kw)
            if Path(source).name == "keybox.json":
                with open(target, "r+b") as handle:
                    handle.write(b"X")
            return result

        with mock.patch.object(L.os, "rename", side_effect=OSError(errno.EXDEV, "cross-device")), \
                mock.patch.object(L.shutil, "copy2", damaging_copy):
            result = L.migrate(app_running=lambda: False)
        self.assertEqual(result.status, L.FAILED)
        self.assertTrue(L.using_legacy_dirs())
        self.assertEqual(L.app_dir("config"), self.old("config"))
        from ophion.settings import AppSettings

        self.assertEqual(Path(AppSettings.load().config_path).parent, self.old("config"))  # never empty
        for kind in L.KINDS:
            self.assertFalse(self.new(kind).exists())
            self.assertEqual(tree_digest(self.old(kind)), before[kind])
            self.assertFalse(list(self.new(kind).parent.glob("*.partial-*")))



class NoticeTests(LegacyCase):
    def test_one_time_messages(self) -> None:
        from tests.gtk_window_fixture import display_available

        if not display_available():
            self.skipTest("needs a display")
        from ophion.gtk_app import migration_notice
        from ophion.gtkui.theme.tokens import Level

        copied = L.MigrationResult(L.COPIED, [L.Moved("config", self.old("config"), self.new("config"), "copy"),
                                              L.Moved("data", self.old("data"), self.new("data"), "copy")])
        text, level = migration_notice(copied)
        self.assertEqual(level, Level.INFO)
        self.assertIn("ophion", text)
        self.assertIn("can be deleted", text)
        text, level = migration_notice(L.MigrationResult(L.FAILED))
        self.assertEqual(level, Level.WARNING)
        self.assertIn("SSH Manager", text)
        for status in (L.NOTHING, L.DONE):
            self.assertIsNone(migration_notice(L.MigrationResult(status)))
        self.assertIsNone(migration_notice(None))

    def test_main_exits_cleanly_while_the_old_app_runs(self) -> None:
        from tests.gtk_window_fixture import display_available

        if not display_available():
            self.skipTest("needs a display")
        from ophion import gtk_main

        shown = []
        with mock.patch.object(L, "migrate", lambda: L.MigrationResult(L.BLOCKED)), \
                mock.patch.object(gtk_main, "show_legacy_app_running", lambda: shown.append(1)), \
                mock.patch.object(gtk_main, "SSHManagerApplication", side_effect=AssertionError("must not start")):
            self.assertEqual(gtk_main.main([]), 0)
        self.assertEqual(shown, [1])

if __name__ == "__main__":
    unittest.main()
