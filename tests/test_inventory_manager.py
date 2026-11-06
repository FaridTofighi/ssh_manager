"""StoreConfigManager: the ConfigManager API over the SQLite store (no GTK)."""

import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from ophion.config import HostEntry
from ophion.credentials import CredentialStore
from ophion.gtkui.constants import Icons, RowKind
from ophion.gtkui.controllers.tree_controller import TreeCallbacks, build_tree_rows
from ophion.inventory.manager import StoreConfigManager
from ophion.inventory.store import InventoryStore
from ophion.secure_store import InMemoryKeyring, SecureStore
from ophion.ssh_command import build_ssh_command

UNICODE = "Prod DB (10.0.0.5) — مرکز"


def host(alias, hostname="10.0.0.1", **options):
    return HostEntry(alias, {"hostname": hostname, **options})


class ManagerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = InventoryStore(self.root / "inventory.db")
        self.manager = StoreConfigManager(self.store)

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def aliases(self, parent, child=""):
        return [h.alias for h in self.manager.tree[parent][child].hosts]


class TreeShapeTests(ManagerTestCase):
    def test_every_node_is_an_entry_and_top_level_hosts_use_the_empty_path(self) -> None:
        m = self.manager
        self.assertTrue(m.create_parent_group("prod")[0])
        self.assertTrue(m.create_child_group("prod", "web/eu")[0])  # holders are created too
        self.assertTrue(m.create_child_group("prod", "empty")[0])
        self.assertTrue(m.add_host("prod", "", host("bastion"))[0])
        self.assertTrue(m.add_host("prod", "web", host("lb"))[0])
        self.assertTrue(m.add_host("prod", "web/eu", host("web-01"))[0])
        self.assertEqual(list(m.tree["prod"]), ["", "empty", "web", "web/eu"])
        self.assertEqual(self.aliases("prod"), ["bastion"])
        self.assertEqual(self.aliases("prod", "web"), ["lb"])  # a container that also holds hosts
        self.assertEqual(m.tree["prod"]["empty"].hosts, [])  # empty groups are real
        entry = m.tree["prod"]["web/eu"]
        self.assertTrue(entry.id)
        self.assertFalse(entry.file_backed)
        h = entry.hosts[0]
        self.assertTrue(h.id)
        self.assertEqual((h.parent_group, h.child_group), ("prod", "web/eu"))

    def test_unicode_and_space_names(self) -> None:
        m = self.manager
        self.assertTrue(m.create_parent_group(UNICODE)[0])
        self.assertTrue(m.create_child_group(UNICODE, "Team A/Servers — EU")[0])
        self.assertTrue(m.add_host(UNICODE, "Team A/Servers — EU", host("Web 01 (primary)"))[0])
        self.assertEqual(self.aliases(UNICODE, "Team A/Servers — EU"), ["Web 01 (primary)"])
        entry = m.tree[UNICODE]["Team A/Servers — EU"].hosts[0]
        self.assertEqual(build_ssh_command(entry)[-1], "10.0.0.1")  # the alias is never the target here

    def test_tree_rows_nest_without_duplicates(self) -> None:
        m = self.manager
        m.create_parent_group("prod")
        m.create_child_group("prod", "web/eu")
        m.add_host("prod", "", host("bastion"))
        m.add_host("prod", "web", host("lb"))
        m.add_host("prod", "web/eu", host("web-01"))
        rows = build_tree_rows([(m.tree, False)], callbacks=TreeCallbacks())
        prod = rows[0]
        self.assertEqual([(r.kind, r.text) for r in prod.children],
                         [(RowKind.CHILD, "web"), (RowKind.HOST, "bastion")])  # groups first, then hosts
        web = prod.children[0]
        self.assertEqual([(r.kind, r.text) for r in web.children], [(RowKind.CHILD, "eu"), (RowKind.HOST, "lb")])
        self.assertEqual((prod.count, web.count, web.children[0].count), (3, 2, 1))
        self.assertEqual(web.icon, Icons.FOLDER)  # app-store groups are folders, not *.conf files
        self.assertFalse(web.file_backed)


class GroupOperationTests(ManagerTestCase):
    def setUp(self) -> None:
        super().setUp()
        m = self.manager
        m.create_parent_group("prod")
        m.create_child_group("prod", "web")
        m.add_host("prod", "web", host("web-01"))

    def test_duplicates_are_inline_errors(self) -> None:
        m = self.manager
        ok, message = m.create_parent_group("PROD")
        self.assertFalse(ok)
        self.assertIn("already exists", message)
        self.assertFalse(m.create_child_group("prod", "Web")[0])
        ok, message = m.add_host("prod", "web", host("WEB-01"))
        self.assertFalse(ok)
        self.assertIn("already exists", message)
        for bad in ("", "   ", "a\nb", "x" * 129):
            with self.subTest(bad=bad):
                self.assertFalse(m.create_parent_group(bad)[0])

    def test_rename_move_duplicate_delete(self) -> None:
        m = self.manager
        self.assertTrue(m.rename_child_group("prod", "web", "frontend")[0])
        self.assertEqual(self.aliases("prod", "frontend"), ["web-01"])
        self.assertTrue(m.rename_child_group("prod", "frontend", "tier/front")[0])  # rename into a new holder
        self.assertIn("tier/front", m.tree["prod"])
        m.create_parent_group("staging")
        self.assertTrue(m.move_child_group("prod", "tier/front", "staging")[0])
        self.assertEqual(self.aliases("staging", "front"), ["web-01"])
        self.assertTrue(m.duplicate_child_group("staging", "front", "front copy")[0])
        self.assertEqual(self.aliases("staging", "front copy"), ["web-01"])
        self.assertTrue(m.duplicate_parent_group("staging", "staging 2")[0])
        self.assertIn("front copy", m.tree["staging 2"])
        self.assertTrue(m.rename_parent_group("staging 2", "QA")[0])
        self.assertEqual(m.child_host_count("staging"), 2)
        self.assertTrue(m.delete_child_group("staging", "front copy")[0])
        self.assertTrue(m.delete_parent_group("QA")[0])
        self.assertNotIn("QA", m.tree)
        self.assertFalse(m.move_child_group("staging", "front", "staging", "front")[0])

    def test_backup_before_deleting_hosts(self) -> None:
        m = self.manager
        m.create_parent_group("empty")
        m.delete_parent_group("empty")  # no hosts lost: no snapshot
        self.assertEqual(self.store.list_backups(), [])
        m.delete_child_group("prod", "web")  # deletes web-01
        self.assertEqual(len(self.store.list_backups()), 1)
        self.assertIn("before-delete", self.store.list_backups()[0].name)

    def test_moving_a_group_into_its_own_subgroup_is_refused(self) -> None:
        m = self.manager
        m.create_child_group("prod", "web/eu")
        ok, message = m.move_child_group("prod", "web", "prod", "web/eu/web")
        self.assertFalse(ok)
        self.assertIn("itself", message)

    def test_hosts_update_delete_move(self) -> None:
        m = self.manager
        original = m.tree["prod"]["web"].hosts[0]
        updated = HostEntry("web 01", {"hostname": "10.0.0.9", "port": "2222", "user": "alice",
                                       "identityfile": "~/.ssh/id"}, {"tags": "x"})
        self.assertTrue(m.update_host("prod", "web", "web-01", updated)[0])
        again = m.tree["prod"]["web"].hosts[0]
        self.assertEqual(again.id, original.id)  # the id survives the rename
        self.assertEqual((again.alias, again.options["port"], again.options["user"]), ("web 01", "2222", "alice"))
        self.assertEqual(again.metadata, {"tags": "x"})
        self.assertTrue(m.move_hosts([("prod", "web", "web 01")], "prod", "new/place", create_child=True)[0])
        self.assertEqual(m.tree["prod"]["new/place"].hosts[0].id, original.id)
        self.assertFalse(m.move_hosts([("prod", "new/place", "web 01")], "prod", "nowhere")[0])
        self.assertTrue(m.delete_host("prod", "new/place", "web 01")[0])
        self.assertEqual(m.tree["prod"]["new/place"].hosts, [])

    def test_host_rules(self) -> None:
        m = self.manager
        self.assertFalse(m.add_host("prod", "web", HostEntry("my server", {}))[0])  # alias would be the target
        self.assertTrue(m.add_host("prod", "web", HostEntry("db.internal", {}))[0])
        self.assertFalse(m.add_host("prod", "web", HostEntry("x", {"hostname": "h", "port": "abc"}))[0])
        self.assertFalse(m.add_host("prod", "web", HostEntry("y", {"hostname": "h", "notes": "a\nb"}))[0])
        self.assertTrue(m.add_host("prod", "web", HostEntry("perm", {"hostname": "h"},
                                                            {"permissions": {"can_edit": False}}))[0])
        perm = next(h for h in m.tree["prod"]["web"].hosts if h.alias == "perm")
        self.assertEqual(perm.metadata["permissions"], {"can_edit": False})

    def test_external_changes_are_noticed(self) -> None:
        self.assertFalse(self.manager.changed_externally())
        other = InventoryStore(self.store.path)
        other.create_group(None, "from another window")
        other.close()
        self.assertTrue(self.manager.changed_externally())
        self.manager.refresh()
        self.assertIn("from another window", self.manager.tree)
        self.assertFalse(self.manager.changed_externally())

    def test_locked_database_is_a_message(self) -> None:
        from ophion.inventory.store import InventoryLocked

        with mock.patch.object(self.store, "create_group", side_effect=InventoryLocked("busy, try again")):
            ok, message = self.manager.create_parent_group("x")
        self.assertFalse(ok)
        self.assertIn("try again", message)

    def test_bulk_helpers(self) -> None:
        m = self.manager
        group_id = m.create_group_path("imported", "a/b")
        hosts = [(group_id, host(f"h{i}", f"10.0.1.{i}")) for i in range(5)]
        with mock.patch.object(m, "refresh", wraps=m.refresh) as refresh:
            m.add_hosts_bulk(hosts)
            refresh.assert_not_called()
        self.assertTrue(all(h.id for _g, h in hosts))
        m.refresh()
        self.assertEqual(len(m.tree["imported"]["a/b"].hosts), 5)


class BindingTests(ManagerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.creds = CredentialStore(SecureStore(InMemoryKeyring()), path=self.root / "credentials.json")
        self.cred = self.creds.add("Ops", "ops", "pw")
        m = self.manager
        m.create_parent_group("prod")
        m.create_child_group("prod", "web")
        entry = host("web-01")
        m.add_host("prod", "web", entry)
        self.host_id = entry.id
        self.creds.bind_host(entry.id, self.cred.id)

    def binding(self):
        found = [h for groups in self.manager.tree.values() for g in groups.values() for h in g.hosts]
        return {h.alias: self.creds.binding_for_host(h.id) for h in found}

    def test_bindings_follow_the_id_across_rename_and_moves(self) -> None:
        m = self.manager
        m.update_host("prod", "web", "web-01", HostEntry("web one", {"hostname": "10.0.0.1"}))
        m.rename_child_group("prod", "web", "frontend")
        m.create_parent_group("staging")
        m.move_child_group("prod", "frontend", "staging")
        m.rename_parent_group("staging", "Staging — EU")
        self.assertEqual(self.binding(), {"web one": self.cred.id})
        self.assertEqual(m.tree["Staging — EU"]["frontend"].hosts[0].id, self.host_id)

    def test_duplicates_get_new_ids_and_delete_prunes_bindings(self) -> None:
        m = self.manager
        m.duplicate_child_group("prod", "web", "web copy")
        new_id = m.last_duplicate_map[self.host_id]
        self.assertNotEqual(new_id, self.host_id)
        self.creds.bind_many({new: self.creds.binding_for_host(old) for old, new in m.last_duplicate_map.items()})
        self.assertEqual(self.creds.binding_for_host(new_id), self.cred.id)
        m.delete_parent_group("prod")
        removed = self.creds.prune_bindings(m.all_host_ids())
        self.assertEqual(removed, 2)
        self.assertIsNotNone(self.creds.get(self.cred.id))  # the credential itself is kept
        self.creds.bind_host("prod::web::legacy", self.cred.id)
        self.assertEqual(self.creds.prune_bindings(m.all_host_ids()), 0)  # legacy path keys are left alone


class PerformanceTests(ManagerTestCase):
    def test_tree_load_5000_hosts(self) -> None:
        store = self.store
        groups = []
        for i in range(25):
            top = store.create_group(None, f"group {i}")
            groups += [store.create_group_path(f"sub {j}/leaf", top.id) for j in range(4)]
        from ophion.inventory.store import NewHost

        store.add_hosts_bulk(NewHost(groups[i % len(groups)].id, f"host {i}", f"10.{i // 65536}.{i // 256 % 256}.{i % 256}",
                                     22, "ops") for i in range(5000))
        timings = []
        for _attempt in range(3):  # the best of three: a busy machine (the full suite) can stall one run
            start = time.perf_counter()
            self.manager.refresh()
            timings.append(time.perf_counter() - start)
        self.assertEqual(sum(len(g.hosts) for c in self.manager.tree.values() for g in c.values()), 5000)
        self.assertLess(min(timings), 0.3, f"tree load {min(timings):.3f}s")


if __name__ == "__main__":
    unittest.main()
