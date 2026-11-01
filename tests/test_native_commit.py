"""Committing an SSH Manager export import: atomic, lossless round trips, idempotent, fast enough."""

import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from ophion import native_format as F
from ophion.exporters.native import build_export
from ophion.importers import native as N
from ophion.importers.mobaxterm import ConflictPolicy, Destination, ExistingInventory
from ophion.inventory.manager import StoreConfigManager
from tests.native_fixture import FakeCredential, build_rich, make_store


class FakeCredentialStore:
    """The ``CredentialStore`` calls the import makes (bindings and private credentials kept in memory)."""

    def __init__(self, credentials=()):
        self.credentials = {c.id: c for c in credentials}
        self.bindings = {}
        self.fail_bind = False

    def list(self, include_private=False):
        return [c for c in self.credentials.values() if include_private or not c.private]

    def get(self, cred_id):
        return self.credentials.get(cred_id)

    def bind_many(self, mapping):
        if self.fail_bind:
            raise OSError("the credentials file cannot be written")
        for host_id, cred_id in mapping.items():
            if cred_id:
                self.bindings[host_id] = cred_id
            else:
                self.bindings.pop(host_id, None)

    def create_private(self, host_key, username, password, *, label="", based_on=None):
        assert password is None  # the import never has a password
        cred = FakeCredential(f"private-{host_key}", f"host:{label}", username, private=True)
        self.credentials[cred.id] = cred
        self.bindings[host_key] = cred.id
        return cred

    def credential_for(self, host_id):
        return self.credentials.get(self.bindings.get(host_id, ""))


def snapshot(store):
    rows = [(g.id, g.parent_id, g.name, g.position, g.note) for g in store.list_groups()]
    rows += [(h.id, h.group_id, h.alias, h.hostname, h.port, h.user, json.dumps(h.options, sort_keys=True),
              json.dumps(h.metadata, sort_keys=True), h.position, h.protocol) for h in store.list_hosts()]
    return hashlib.sha256(repr(sorted(rows, key=repr)).encode()).hexdigest(), len(store.list_groups()), \
        len(store.list_hosts())


class CommitCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        source_dir, target_dir = Path(self.tmp.name) / "a", Path(self.tmp.name) / "b"
        self.source = make_store(source_dir)
        self.addCleanup(self.source.close)
        self.ids, self.group_colors, self.host_colors, creds, _unused = build_rich(self.source)
        self.source_creds = creds
        self.target = make_store(target_dir)
        self.addCleanup(self.target.close)
        self.manager = StoreConfigManager(self.target)
        self.cred_store = FakeCredentialStore([FakeCredential("ops", "Ops account", "deploy"),
                                               FakeCredential("pg", "Postgres", "postgres")])

    def exported(self, store=None, *, creds=None, group_colors=None, host_colors=None):
        doc, _report = build_export(store or self.source,
                                    group_colors=self.group_colors if group_colors is None else group_colors,
                                    host_colors=self.host_colors if host_colors is None else host_colors,
                                    credential_for=(creds or self.source_creds).get if creds is None or isinstance(
                                        creds, dict) else creds, now=0, app_version="test")
        return doc

    def job(self, doc, *, options=N.NativeImportOptions(), mapping=None, colors=({}, {})):
        plan = N.plan_native(N.read_native(F.dump_document(doc)), options, (),
                             ExistingInventory.from_store(self.target))
        credentials = {c.id: c for c in self.cred_store.list()}
        mapping = mapping if mapping is not None else N.suggest_credentials(plan, self.cred_store.list())
        return N.apply_mapping(plan, mapping, credentials, existing_group_colors=colors[0],
                               existing_host_colors=colors[1], key_exists=lambda _p: True)

    def commit(self, job, **kw):
        return N.commit_native(job, self.manager, self.cred_store, **kw)


class CommitTests(CommitCase):
    def test_everything_is_written(self) -> None:
        report = self.commit(self.job(self.exported()))
        self.assertEqual(report.status, N.ImportStatus.SUCCESS, report.message)
        self.assertEqual((report.hosts, report.groups), (9, 7))
        prod = self.target.find_group(["Prod"])
        self.assertEqual(prod.note, "production servers")
        self.assertIsNotNone(self.target.find_group(["Prod", "Databases", "Replicas"]))
        self.assertIsNotNone(self.target.find_group(["سرورها"]))
        hosts = {h.alias: h for h in self.target.list_hosts()}
        self.assertEqual(hosts["dc-01"].metadata["rdp"]["via_ssh"], hosts["bastion"].id)  # remapped
        self.assertEqual(hosts["vnc-1"].protocol, "vnc")
        self.assertEqual(self.cred_store.bindings[hosts["web-01"].id], "ops")  # matched by name + username
        self.assertEqual(self.cred_store.bindings[hosts["db-02"].id], "pg")
        self.assertTrue(self.cred_store.credential_for(hosts["bastion"].id).private)  # host-specific, no secret
        self.assertEqual(report.group_colors["Prod/Databases/Replicas"], "#0000aa")
        self.assertEqual(report.host_colors["Prod::Databases/Replicas::db-02"], "#123456")
        self.assertEqual(self.manager.tree["Prod"]["Databases"].hosts[0].alias, "db-01")  # refreshed

    def test_any_failure_leaves_the_inventory_as_it_was(self) -> None:
        from ophion.inventory.store import InventoryError

        self.commit(self.job(self.exported()))  # something to protect
        before = snapshot(self.target)
        bindings_before = dict(self.cred_store.bindings)
        doc = self.exported()
        for group in doc["groups"]:
            group["name"] += " copy"  # all new
        failures = {
            "groups": mock.patch.object(self.target, "create_group", side_effect=[mock.DEFAULT, InventoryError("x")],
                                        wraps=self.target.create_group),
            "hosts": mock.patch.object(self.target, "add_hosts_bulk", side_effect=InventoryError("disk")),
            "positions": mock.patch.object(self.target, "set_position", side_effect=InventoryError("disk")),
        }
        for stage, patcher in failures.items():
            job = self.job(doc)
            with self.subTest(stage=stage), patcher:
                if stage == "positions":
                    job.inserts[0][2].file.position = 3
                report = self.commit(job)
            self.assertEqual(report.status, N.ImportStatus.FAILED, stage)
            self.assertTrue(report.message.startswith("Import failed, no changes were made"))
            self.assertEqual(snapshot(self.target), before, stage)
        self.cred_store.fail_bind = True
        report = self.commit(self.job(doc))
        self.assertEqual(report.status, N.ImportStatus.FAILED)
        self.assertEqual(snapshot(self.target), before)
        self.assertEqual(self.cred_store.bindings, bindings_before)
        self.cred_store.fail_bind = False
        cancelled = self.commit(self.job(doc), should_cancel=lambda: True)
        self.assertEqual(cancelled.status, N.ImportStatus.CANCELLED)
        self.assertEqual(snapshot(self.target), before)
        self.assertEqual(cancelled.group_colors, {})  # nothing to save for the UI either

    def test_credential_or_destination_deleted_meanwhile(self) -> None:
        before = snapshot(self.target)
        job = self.job(self.exported())
        del self.cred_store.credentials["ops"]
        report = self.commit(job)
        self.assertEqual(report.status, N.ImportStatus.FAILED)
        self.assertIn("credential", report.message)
        self.assertEqual(snapshot(self.target), before)
        self.cred_store.credentials["ops"] = FakeCredential("ops", "Ops account", "deploy")
        mount = self.target.create_group(None, "Mount")
        job = self.job(self.exported(), options=N.NativeImportOptions(destination=Destination.EXISTING_GROUP,
                                                                      target_group=("Mount",)))
        self.target.delete_group(mount.id)
        before = snapshot(self.target)
        report = self.commit(job)
        self.assertEqual(report.status, N.ImportStatus.FAILED)
        self.assertEqual(snapshot(self.target), before)

    def test_update_policy_backs_up_and_keeps_ids(self) -> None:
        self.commit(self.job(self.exported()))
        web = next(h for h in self.target.list_hosts() if h.alias == "web-01")
        doc = self.exported()
        prod = next(g for g in doc["groups"] if g["name"] == "Prod")
        next(h for h in prod["hosts"] if h["alias"] == "web-01")["metadata"]["owner"] = "new owner"
        with mock.patch.object(self.manager, "backup_before") as backup:
            report = self.commit(self.job(doc, options=N.NativeImportOptions(conflict_policy=ConflictPolicy.OVERWRITE)))
        backup.assert_called_once()
        self.assertEqual(report.status, N.ImportStatus.SUCCESS)
        updated = self.target.get_host(web.id)
        self.assertEqual(updated.metadata["owner"], "new owner")
        self.assertEqual(len(self.target.list_hosts()), 9)


class RoundTripTests(CommitCase):
    def test_export_import_export_is_lossless(self) -> None:
        first = self.exported()
        report = self.commit(self.job(first))
        self.assertEqual(report.status, N.ImportStatus.SUCCESS)
        second = self.exported(self.target, creds=self.cred_store.credential_for,
                               group_colors=report.group_colors, host_colors=report.host_colors)
        first.pop("exported_at")
        second.pop("exported_at")
        self.assertEqual(F.dump_document(first), F.dump_document(second))

    def test_importing_twice_changes_nothing(self) -> None:
        doc = self.exported()
        first = self.commit(self.job(doc))
        state = snapshot(self.target)
        bindings = dict(self.cred_store.bindings)
        job = self.job(doc, colors=(first.group_colors, first.host_colors))
        self.assertTrue(job.nothing_to_do)
        self.assertEqual((job.group_colors, job.host_colors), ({}, {}))
        report = self.commit(job)
        self.assertEqual(report.status, N.ImportStatus.NOTHING)
        self.assertEqual(snapshot(self.target), state)
        self.assertEqual(self.cred_store.bindings, bindings)


class SmartGroupTests(unittest.TestCase):
    def test_merge(self) -> None:
        from ophion.smart_groups import SmartGroup

        merged, added = N.merge_smart_groups([SmartGroup("Prod", "env:prod")],
                                             [{"name": "prod", "query": "x"}, {"name": "Web", "query": "tag:web"}])
        self.assertEqual([(g.name, g.query) for g in merged], [("Prod", "env:prod"), ("Web", "tag:web")])
        self.assertEqual(added, 1)


def big_document(hosts: int, per_group: int = 12):
    groups = []
    for g in range(max(1, hosts // per_group)):
        groups.append({"name": f"Group {g:05d}", "note": "", "color": "#336699" if g % 7 == 0 else "",
                       "position": 0, "children": [],
                       "hosts": [{"ref": f"h{g}-{i}", "alias": f"host-{g:05d}-{i:02d}", "protocol": "ssh",
                                  "hostname": f"10.{g // 250 % 250}.{g % 250}.{i + 1}", "port": 22,
                                  "user": f"user{i % 5}"} for i in range(per_group)]})
    doc = {"format": "ssh-manager-export", "version": 1, "app_version": "", "exported_at": "", "scope": "all",
           "counts": {}, "options": {}, "groups": groups, "credentials": [], "smart_groups": []}
    doc["integrity"] = {"sha256": F.integrity_hash(doc)}
    return F.dump_document(doc)


class PerformanceTests(unittest.TestCase):
    """Timings (printed for the report): read + plan + map + commit, and an export of the result."""

    def run_size(self, hosts: int) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            store = make_store(Path(tmp))
            try:
                manager = StoreConfigManager(store)
                data = big_document(hosts)
                times = {}
                started = time.perf_counter()
                document = N.read_native(data)
                times["read"] = time.perf_counter() - started
                started = time.perf_counter()
                plan = N.plan_native(document, existing=ExistingInventory.from_store(store))
                job = N.apply_mapping(plan, {}, {}, key_exists=lambda _p: True)
                times["plan"] = time.perf_counter() - started
                started = time.perf_counter()
                report = N.commit_native(job, manager, FakeCredentialStore())
                times["commit"] = time.perf_counter() - started
                self.assertEqual(report.status, N.ImportStatus.SUCCESS)
                started = time.perf_counter()
                exported, _ = build_export(store)
                F.dump_document(exported)
                times["export"] = time.perf_counter() - started
                self.assertEqual(exported["counts"]["hosts"], len(job.inserts))
                return times
            finally:
                store.close()

    def test_1200_and_10000_hosts(self) -> None:
        for hosts, budget in ((1200, 10.0), (10000, 60.0)):
            times = self.run_size(hosts)
            print(f"\n[native import/export] {hosts} hosts: " +
                  ", ".join(f"{k} {v * 1000:.0f} ms" for k, v in times.items()))
            self.assertLess(sum(times.values()), budget)


if __name__ == "__main__":
    unittest.main()
