"""InventoryStore: the SQLite inventory (no GTK)."""

import os
import sqlite3
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from ophion.inventory.store import (
    SCHEMA_VERSION,
    InventoryCorrupt,
    InventoryError,
    InventoryLocked,
    InventoryStore,
    InventoryTooNew,
    InventoryUnavailable,
    NewHost,
    ValidationError,
    default_inventory_path,
    validate_alias,
    validate_name,
)

UNICODE = "Prod DB (10.0.0.5) — مرکز"


class StoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "data" / "ophion"
        self.path = self.dir / "inventory.db"
        self.store = InventoryStore(self.path)

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()


class LifecycleTests(StoreTestCase):
    def test_file_permissions_wal_and_schema(self) -> None:
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.dir.stat().st_mode), 0o700)
        self.assertEqual(self.store.conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(self.store.conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertEqual(self.store.schema_version, SCHEMA_VERSION)

    def test_default_path_respects_xdg(self) -> None:
        with mock.patch.dict(os.environ, {"XDG_DATA_HOME": "/x/data"}):
            self.assertEqual(default_inventory_path(), Path("/x/data/ophion/inventory.db"))

    def test_migration_from_v0_fixture(self) -> None:
        v0 = Path(self.tmp.name) / "v0.db"
        conn = sqlite3.connect(v0)
        conn.execute("CREATE TABLE legacy_marker (x)")  # an old/empty database at user_version 0
        conn.commit()
        conn.close()
        store = InventoryStore(v0)
        self.assertEqual(store.schema_version, SCHEMA_VERSION)
        store.create_group(None, "works")
        store.close()

    def test_newer_schema_is_refused_untouched(self) -> None:
        self.store.close()
        conn = sqlite3.connect(self.path)
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
        conn.commit()
        conn.close()
        before = self.path.read_bytes()
        with self.assertRaises(InventoryTooNew) as ctx:
            InventoryStore(self.path)
        self.assertIn("newer", str(ctx.exception))
        self.assertEqual(self.path.read_bytes(), before)
        self.store = InventoryStore(Path(self.tmp.name) / "other.db")

    def test_corruption_is_detected(self) -> None:
        self.store.close()
        bad = Path(self.tmp.name) / "bad.db"
        bad.write_bytes(b"this is not a database" * 100)
        with self.assertRaises(InventoryCorrupt):
            InventoryStore(bad)
        self.store = InventoryStore(Path(self.tmp.name) / "fresh.db")

    def test_unwritable_location(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("root ignores permissions")
        locked = Path(self.tmp.name) / "locked"
        locked.mkdir(mode=0o500)
        try:
            with self.assertRaises(InventoryUnavailable):
                InventoryStore(locked / "sub" / "inventory.db")
        finally:
            locked.chmod(0o700)

    def test_read_only_open(self) -> None:
        self.store.create_group(None, "prod")
        self.store.close()
        store = InventoryStore(self.path, read_only=True)
        self.assertEqual([g.name for g in store.list_groups()], ["prod"])
        with self.assertRaises(InventoryUnavailable):
            store.create_group(None, "nope")
        store.close()
        self.store = InventoryStore(self.path)

    def test_second_writer_gets_a_friendly_lock_error(self) -> None:
        other = sqlite3.connect(self.path, isolation_level=None, timeout=0.1)
        other.execute("BEGIN IMMEDIATE")
        try:
            with mock.patch("ophion.inventory.store.BUSY_TIMEOUT_S", 0.1):
                self.store.conn.execute("PRAGMA busy_timeout=100")
                with self.assertRaises(InventoryLocked) as ctx:
                    self.store.create_group(None, "blocked")
            self.assertIn("another Ophion", str(ctx.exception))
        finally:
            other.execute("ROLLBACK")
            other.close()
        self.store.create_group(None, "after")  # retry works

    def test_data_version_changes_on_external_commits(self) -> None:
        before = self.store.data_version()
        other = InventoryStore(self.path)
        other.create_group(None, "from elsewhere")
        other.close()
        self.assertNotEqual(self.store.data_version(), before)


class GroupTests(StoreTestCase):
    def test_crud_unicode_and_paths(self) -> None:
        prod = self.store.create_group(None, f"  {UNICODE}  ")
        self.assertEqual(prod.name, UNICODE)  # trimmed, kept as typed
        leaf = self.store.create_group_path("Team A/Servers — EU", prod.id)
        self.assertEqual(self.store.group_path(leaf.id), (UNICODE, "Team A", "Servers — EU"))
        self.assertEqual(self.store.find_group([UNICODE, "team a", "servers — eu"]).id, leaf.id)
        renamed = self.store.rename_group(leaf.id, "Servers")
        self.assertEqual(renamed.name, "Servers")
        self.assertEqual(self.store.create_group_path([UNICODE, "Team A", "Servers"]).id, leaf.id)  # get-or-create

    def test_case_insensitive_uniqueness_including_unicode(self) -> None:
        self.store.create_group(None, "Prod")
        for name in ("prod", "PROD", " prod "):
            with self.subTest(name=name), self.assertRaises(ValidationError):
                self.store.create_group(None, name)
        self.store.create_group(None, "Ärzte")
        with self.assertRaises(ValidationError):
            self.store.create_group(None, "ärzte")
        a = self.store.create_group(None, "A")
        self.store.create_group(a.id, "prod")  # same name under another parent is fine
        b = self.store.create_group(None, "B")
        with self.assertRaises(ValidationError):
            self.store.rename_group(b.id, "a")

    def test_name_rules(self) -> None:
        for bad in ("", "   ", "a/b", "x" * 129, "line\nbreak", "tab\x01", "rtl‮override"):
            with self.subTest(bad=bad):
                self.assertIsNotNone(validate_name(bad))
                with self.assertRaises(ValidationError):
                    self.store.create_group(None, bad)
        for good in ("web 01", UNICODE, "می‌ها", "x" * 128, "a.b-c_d (1)"):
            with self.subTest(good=good):
                self.assertIsNone(validate_name(good))

    def test_container_with_hosts_and_subgroups_and_empty_groups(self) -> None:
        prod = self.store.create_group(None, "prod")
        self.store.add_host(NewHost(prod.id, "bastion", "10.0.0.1"))
        web = self.store.create_group(prod.id, "web")
        empty = self.store.create_group(prod.id, "empty")
        self.assertEqual([h.alias for h in self.store.list_hosts(prod.id)], ["bastion"])
        self.assertEqual({g.name for g in self.store.children(prod.id)}, {"web", "empty"})
        self.assertEqual(self.store.count_hosts(empty.id), 0)
        self.assertIsNotNone(self.store.get_group(web.id))

    def test_move_cycle_and_duplicate(self) -> None:
        a = self.store.create_group(None, "a")
        b = self.store.create_group(a.id, "b")
        c = self.store.create_group(b.id, "c")
        for target in (a.id, b.id, c.id):
            with self.subTest(target=target), self.assertRaises(ValidationError):
                self.store.move_group(a.id, target)
        top = self.store.move_group(c.id, None)
        self.assertIsNone(top.parent_id)
        self.store.add_host(NewHost(b.id, "h1", "10.0.0.1"))
        copy, host_map = self.store.duplicate_group(a.id, None, "a copy")
        self.assertEqual(self.store.group_path(self.store.find_group(["a copy", "b"]).id), ("a copy", "b"))
        self.assertEqual(len(host_map), 1)
        old_id, new_id = next(iter(host_map.items()))
        self.assertNotEqual(old_id, new_id)
        self.assertEqual(self.store.get_host(new_id).alias, "h1")

    def test_cascade_delete(self) -> None:
        a = self.store.create_group(None, "a")
        b = self.store.create_group(a.id, "b")
        h1 = self.store.add_host(NewHost(a.id, "h1", "10.0.0.1"))
        h2 = self.store.add_host(NewHost(b.id, "h2", "10.0.0.2"))
        self.assertEqual(self.store.count_hosts(a.id), 2)
        deleted = self.store.delete_group(a.id)
        self.assertEqual(set(deleted), {h1.id, h2.id})
        self.assertEqual(self.store.list_groups(), [])
        self.assertEqual(self.store.list_hosts(), [])

    def test_depth_50(self) -> None:
        names = [f"level {i}" for i in range(50)]
        leaf = self.store.create_group_path(names)
        self.assertEqual(self.store.group_path(leaf.id), tuple(names))
        self.assertEqual(len(self.store.descendants(self.store.find_group(names[:1]).id)), 49)


class HostTests(StoreTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.group = self.store.create_group(None, "prod")

    def test_crud_and_options(self) -> None:
        host = self.store.add_host(NewHost(self.group.id, "Web 01 — EU", "10.0.0.1", 2222, "alice",
                                           {"identityfile": "~/.ssh/id", "proxyjump": "gw"},
                                           {"tags": "a,b", "permissions": {"can_edit": False}}))
        loaded = self.store.get_host(host.id)
        self.assertEqual((loaded.alias, loaded.hostname, loaded.port, loaded.user),
                         ("Web 01 — EU", "10.0.0.1", 2222, "alice"))
        self.assertEqual(loaded.options, {"identityfile": "~/.ssh/id", "proxyjump": "gw"})
        self.assertEqual(loaded.metadata["permissions"], {"can_edit": False})
        updated = self.store.update_host(host.id, alias="web-01", port=None)
        self.assertEqual((updated.alias, updated.port), ("web-01", None))
        self.store.delete_hosts([host.id])
        self.assertIsNone(self.store.get_host(host.id))

    def test_alias_rules(self) -> None:
        self.store.add_host(NewHost(self.group.id, "Web", "10.0.0.1"))
        with self.assertRaises(ValidationError):
            self.store.add_host(NewHost(self.group.id, "web", "10.0.0.2"))
        with self.assertRaises(ValidationError):  # no host name: the alias is the ssh target
            self.store.add_host(NewHost(self.group.id, "my server", ""))
        self.assertIsNotNone(validate_alias("-oProxyCommand=x"))
        self.store.add_host(NewHost(self.group.id, "db.internal", ""))
        for kwargs in ({"hostname": "-evil"}, {"hostname": "bad host"}, {"port": 0}, {"port": 70000},
                       {"user": "a b"}):
            with self.subTest(**kwargs), self.assertRaises(ValidationError):
                self.store.add_host(NewHost(self.group.id, "x", **{"hostname": "h", **kwargs}))
        with self.assertRaises(ValidationError):
            self.store.add_host(NewHost(self.group.id, "y", "h", options={"note": "two\nlines"}))

    def test_move_hosts_checks_aliases(self) -> None:
        other = self.store.create_group(None, "other")
        h1 = self.store.add_host(NewHost(self.group.id, "web", "10.0.0.1"))
        self.store.add_host(NewHost(other.id, "WEB", "10.0.0.9"))
        with self.assertRaises(ValidationError):
            self.store.move_hosts([h1.id], other.id)
        self.assertEqual(self.store.get_host(h1.id).group_id, self.group.id)
        h2 = self.store.add_host(NewHost(self.group.id, "db", "10.0.0.2"))
        self.store.move_hosts([h2.id], other.id)
        self.assertEqual(self.store.get_host(h2.id).group_id, other.id)

    def test_bulk_rolls_back_on_injected_failure(self) -> None:
        before = len(self.store.list_hosts())
        hosts = [NewHost(self.group.id, f"h{i}", f"10.0.0.{i}") for i in range(10)]
        hosts[7] = NewHost(self.group.id, "h3", "10.0.0.99")  # duplicate alias mid-way
        with self.assertRaises(ValidationError):
            self.store.add_hosts_bulk(hosts)
        self.assertEqual(len(self.store.list_hosts()), before)
        with self.assertRaises(RuntimeError):
            with self.store.transaction():
                self.store.create_group(None, "half")
                self.store.add_host(NewHost(self.group.id, "half", "10.0.0.1"))
                raise RuntimeError("boom")
        self.assertIsNone(self.store.find_group(["half"]))
        self.assertIsNone(self.store.find_host(self.group.id, "half"))

    def test_disk_full_is_a_clear_error(self) -> None:
        with mock.patch.object(self.store, "_conn") as conn:
            conn.execute.side_effect = sqlite3.OperationalError("database or disk is full")
            with self.assertRaises(InventoryError) as ctx:
                self.store.set_meta("k", "v")
        self.assertIn("full", str(ctx.exception))


class BackupTests(StoreTestCase):
    def test_backup_rotation_and_restore(self) -> None:
        self.store.create_group(None, "keep me")
        paths = []
        for index in range(7):
            paths.append(self.store.backup(f"r{index}"))
            time.sleep(0.01)
        self.assertEqual(len(self.store.list_backups()), 5)
        self.assertTrue(self.path.with_name("inventory.db.bak").exists())
        self.assertEqual(stat.S_IMODE(paths[-1].stat().st_mode), 0o600)
        self.store.close()
        self.path.write_bytes(b"garbage" * 1000)
        with self.assertRaises(InventoryCorrupt):
            InventoryStore(self.path)
        used = InventoryStore.restore_latest_backup(self.path)
        self.assertIsNotNone(used)
        self.assertTrue(any(p.name.startswith("inventory.db.damaged-") for p in self.dir.iterdir()))
        self.store = InventoryStore(self.path)
        self.assertEqual([g.name for g in self.store.list_groups()], ["keep me"])


class PerformanceTests(StoreTestCase):
    """The best of three runs: a busy machine (the full suite, its disk writes) can stall one; a regression
    makes every run slow. Usually about 0.07 s to insert and 0.03 s to load."""

    def test_bulk_insert_and_load(self) -> None:
        inserts = []
        for attempt in range(3):  # each in a new database: the insert is measured from empty
            store = self.store if attempt == 0 else InventoryStore(self.dir / f"attempt-{attempt}" / "inventory.db")
            if store is not self.store:
                self.addCleanup(store.close)
            start = time.perf_counter()
            groups = [store.create_group(None, f"group {i}") for i in range(50)]
            store.add_hosts_bulk(NewHost(groups[i % 50].id, f"host {i}", f"10.{i // 65536}.{i // 256 % 256}.{i % 256}")
                                 for i in range(2000))
            inserts.append(time.perf_counter() - start)
            if attempt == 0:
                store.add_hosts_bulk(NewHost(groups[i % 50].id, f"more {i}", "10.9.9.9") for i in range(3000))
        self.assertLess(min(inserts), 1.0, f"bulk insert {inserts}")
        loads = []
        for _attempt in range(3):
            start = time.perf_counter()
            hosts = self.store.list_hosts()
            groups = self.store.list_groups()
            loads.append(time.perf_counter() - start)
        self.assertEqual(len(hosts), 5000)
        self.assertLess(min(loads), 0.3, f"load {loads}")


if __name__ == "__main__":
    unittest.main()
