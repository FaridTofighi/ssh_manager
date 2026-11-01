"""Importing an SSH Manager export: reading and validating, planning, users, conflicts, colours (no GTK)."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ophion import native_format as F
from ophion.exporters.native import build_export
from ophion.importers import native as N
from ophion.importers.mobaxterm import ConflictPolicy, Destination, ExistingInventory
from ophion.inventory.store import NewHost
from tests.native_fixture import FakeCredential, build_rich, make_store


def document(groups, *, credentials=(), version=1, integrity=True, **extra):
    doc = {"format": "ssh-manager-export", "version": version, "app_version": "test", "exported_at": "x",
           "scope": "all", "counts": {}, "options": {}, "groups": groups, "credentials": list(credentials),
           "smart_groups": [], **extra}
    if integrity:
        doc["integrity"] = {"sha256": F.integrity_hash(doc)}
    return doc


def raw(doc) -> bytes:
    return json.dumps(doc).encode()


def group(name, hosts=(), children=(), **kw):
    return {"name": name, "hosts": list(hosts), "children": list(children), **kw}


def host(alias, hostname="h.example", **kw):
    return {"ref": kw.pop("ref", alias), "alias": alias, "protocol": kw.pop("protocol", "ssh"),
            "hostname": hostname, **kw}


def codes(issues):
    return [i.code.value for i in issues]


class ReadTests(unittest.TestCase):
    def test_file_level_problems(self) -> None:
        cases = [
            (b"", "empty"),
            (b"\xff\xfe\x00garbage", "not_utf8"),
            (b'{"format": "ssh-manager-export", "version": 1, "groups": [', "truncated"),
            (b'{"format": "ssh-manager-export",\n "version": 1,, "groups": []}', "json"),
            (b'{"format": "something-else", "version": 1}', "not_native"),
            (b'[1, 2]', "not_native"),
            (raw(document([], version=2)), "newer"),
            (raw(document([], version="1")), "damaged"),
        ]
        for data, code in cases:
            with self.subTest(code=code), self.assertRaises(N.NativeFormatError) as caught:
                N.read_native(data)
            self.assertEqual(caught.exception.code, code)
        with self.assertRaises(N.NativeFormatError) as caught:
            N.read_native(b'{"format": "ssh-manager-export",\n "version": 1,, "groups": []}')
        self.assertIn("line 2, column", caught.exception.message)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(N, "MAX_FILE_BYTES", 10):
            big = Path(tmp) / "big.sshm"
            big.write_bytes(raw(document([])))
            with self.assertRaises(N.NativeFormatError) as caught:
                N.read_native(big)
            self.assertEqual(caught.exception.code, "too_large")
        self.assertEqual(N.sniff(raw(document([]))), "plain")
        self.assertEqual(N.sniff(b"[Bookmarks]\nSubRep=x"), "")

    def test_encrypted_files(self) -> None:
        data = F.encrypt_document(raw(document([group("G", [host("a")])])), "secret pass")
        self.assertEqual(N.sniff(data), "encrypted")
        with self.assertRaises(N.NativeFormatError) as caught:
            N.read_native(data)
        self.assertEqual(caught.exception.code, "encrypted")
        with self.assertRaises(N.NativeFormatError) as caught:
            N.read_native(data, "wrong")
        self.assertEqual(caught.exception.code, "wrong_passphrase")
        doc = N.read_native(data, "secret pass")
        self.assertTrue(doc.encrypted)
        self.assertEqual([h.alias for h in doc.hosts()], ["a"])

    def test_integrity(self) -> None:
        good = document([group("G")])
        self.assertEqual(N.read_native(raw(good)).integrity, "ok")
        edited = copy.deepcopy(good)
        edited["groups"][0]["name"] = "H"
        doc = N.read_native(raw(edited))
        self.assertEqual(doc.integrity, "mismatch")
        self.assertIn("integrity_mismatch", codes(doc.issues))
        self.assertEqual(N.read_native(raw(document([group("G")], integrity=False))).integrity, "missing")

    def test_limits(self) -> None:
        deep = group("level-33")
        for level in range(32, 0, -1):
            deep = group(f"level-{level}", children=[deep])
        doc = N.read_native(raw(document([deep])))
        self.assertEqual(sum(1 for _ in doc.walk()), 32)
        self.assertIn("group_too_deep", codes(doc.issues))
        with mock.patch.object(N, "MAX_HOSTS", 3):
            doc = N.read_native(raw(document([group("G", [host(f"h{i}") for i in range(5)])])))
        self.assertEqual(len(doc.hosts()), 3)
        self.assertEqual(codes(doc.issues).count("too_many_hosts"), 1)
        long = N.read_native(raw(document([group("G", [host("a", options={"x": "y" * 5000}), host("b"),
                                                       host("c", metadata={"notes": "n" * 70000})])])))
        self.assertEqual([h.alias for h in long.hosts()], ["b", "c"])
        self.assertNotIn("notes", long.hosts()[1].metadata)
        self.assertIn("field_too_long", codes(long.issues))
        self.assertIn("note_too_long", codes(long.issues))

    def test_hosts_are_validated(self) -> None:
        doc = N.read_native(raw(document([group("G", [
            host("ok", port=2200, user="alice", color="#a1b2c3"),
            host("empty", hostname=""),
            host("dash", hostname="-oProxyCommand=evil"),
            host("space", hostname="two words"),
            host("badport", port=70000),
            host("baduser", user="-x"),
            host("jump", options={"proxyjump": "-oFoo", "identityfile": "~/k"}),
            host("colour", color="red"),
            host("proto", protocol="NOT VALID"),
            host("future", protocol="mosh", hostname="m.example", metadata={"mosh": {"v": 9}}, extra_key=1),
            host("bidi‮evil", hostname="x.example", user="u‮"),
            host("multi", metadata={"owner": "a\nb"}),
            {"alias": 7},
        ])])))
        hosts = {h.alias: h for h in doc.hosts()}
        self.assertEqual(sorted(hosts), sorted(["ok", "badport", "baduser", "jump", "colour", "future",
                                                "bidi‮evil", "multi"]))
        self.assertEqual((hosts["ok"].port, hosts["ok"].user, hosts["ok"].color), (2200, "alice", "#A1B2C3"))
        self.assertEqual(hosts["badport"].port, 22)
        self.assertEqual(hosts["baduser"].user, "")
        self.assertEqual(hosts["jump"].options, {"identityfile": "~/k"})
        self.assertEqual(hosts["colour"].color, "")
        self.assertEqual((hosts["future"].protocol, hosts["future"].metadata), ("mosh", {"mosh": {"v": 9}}))
        self.assertEqual(hosts["bidi‮evil"].user, "u")  # control / bidi characters removed
        self.assertEqual(hosts["multi"].metadata["owner"], "a b")
        for code in ("empty_hostname", "invalid_hostname", "invalid_port", "invalid_user", "invalid_jump_host",
                     "invalid_color", "invalid_protocol", "invalid_item", "field_cleaned"):
            self.assertIn(code, codes(doc.issues), code)

    def test_unknown_keys_and_hand_edited_ids_are_handled(self) -> None:
        doc = N.read_native(raw(document([group("G", [host("r", protocol="rdp", metadata={
            "rdp": {"v": 1, "via_ssh": "deadbeef", "via_ssh_ref": "j"}, "new_feature": True})],
            future_group_key=True)], future_top_key={"x": 1})))
        rdp_host = doc.hosts()[0]
        self.assertEqual(rdp_host.metadata, {"rdp": {"v": 1}, "new_feature": True})
        self.assertEqual(rdp_host.via_ssh_ref, "j")


class PlanCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = make_store(Path(self.tmp.name))
        self.addCleanup(self.store.close)

    def existing(self):
        return ExistingInventory.from_store(self.store)

    def plan(self, doc, options=N.NativeImportOptions(), excluded=()):
        return N.plan_native(N.read_native(raw(doc)), options, excluded, self.existing())


class PlanTests(PlanCase):
    def test_names_order_duplicates_and_exclusion(self) -> None:
        doc = document([
            group("A/B", [host("x"), host("x"), host("")], [group("Child"), group("Child")]),
            group("Second", [host("y")], [group("Inner", [host("z")])]),
            group("Empty"),
        ])
        plan = self.plan(doc)
        self.assertEqual([g.path for g in plan.groups],
                         [("A∕B",), ("A∕B", "Child"), ("A∕B", "Child (2)"), ("Second",),
                          ("Second", "Inner"), ("Empty",)])
        self.assertEqual([h.alias for h in plan.hosts], ["x", "x (2)", "Unnamed", "y", "z"])
        self.assertIn("name_changed", codes(plan.issues))
        skipped = self.plan(doc, N.NativeImportOptions(skip_empty_groups=True), excluded=[(1, 0)])
        self.assertEqual([g.path for g in skipped.groups], [("A∕B",), ("Second",)])
        self.assertEqual(skipped.excluded_hosts, 1)

    def test_protocol_filters_and_destination(self) -> None:
        self.store.create_group(None, "Imported")
        doc = document([group("G", [host("s"), host("r", protocol="rdp"), host("f", protocol="ftps"),
                                    host("m", protocol="mosh")])])
        plan = self.plan(doc, N.NativeImportOptions(include_rdp=False, include_transfer=False,
                                                    destination=Destination.EXISTING_GROUP,
                                                    target_group=("imported",)))
        self.assertEqual([h.alias for h in plan.hosts], ["s", "m"])  # unknown protocols always kept
        self.assertEqual(plan.filtered_hosts, 2)
        self.assertEqual([g.path for g in plan.groups], [("Imported",), ("Imported", "G")])
        missing = self.plan(doc, N.NativeImportOptions(destination=Destination.EXISTING_GROUP,
                                                       target_group=("Gone",)))
        self.assertTrue(missing.destination_missing)

    def test_existing_groups_are_merged(self) -> None:
        prod = self.store.create_group(None, "PROD")
        self.store.create_group(prod.id, "Web")
        plan = self.plan(document([group("prod", children=[group("web", [host("a")]), group("New")])]))
        self.assertEqual([(g.path, g.exists) for g in plan.groups],
                         [(("PROD",), True), (("PROD", "Web"), True), (("PROD", "New"), False)])
        self.assertEqual(plan.new_groups, [("PROD", "New")])


class UserTests(PlanCase):
    DOC = document([group("G", [
        host("a", user="alice", credential={"ref": "c1", "mode": "shared"}),
        host("b", user="ignored", credential={"ref": "c1", "mode": "shared"}, protocol="sftp"),
        host("c", user="Alice"),
        host("d", user="bob", credential={"mode": "host-specific"}),
        host("e"),
        host("f", protocol="ftp", user="anonymous"),
        host("g", protocol="rdp", user="carol", credential={"ref": "c2", "mode": "shared"}),
    ])], credentials=[{"ref": "c1", "name": "Ops", "username": "alice"},
                      {"ref": "c2", "name": "Desk", "username": "carol"}])

    def test_effective_users_variants_and_counts(self) -> None:
        plan = self.plan(self.DOC)
        users = {u.key: u for u in plan.users}
        self.assertEqual(sorted(users), ["", "Alice", "alice", "bob", "carol"])
        self.assertEqual((users["alice"].count, users["alice"].protocols), (2, {"ssh", "sftp"}))
        self.assertEqual(users["alice"].case_variants, ["Alice"])
        self.assertEqual(users["Alice"].case_variants, ["alice"])
        self.assertIn("credential “Ops” (alice)", users["alice"].raw_forms)
        self.assertEqual(users[""].count, 1)  # "(no user)"; anonymous FTP is not listed
        self.assertEqual(plan.users[0].key, "alice")  # most used first
        self.assertEqual(plan.host_specific, 1)

    def test_suggestions(self) -> None:
        plan = self.plan(self.DOC)
        creds = [FakeCredential("x1", "Ops", "alice"), FakeCredential("x2", "Other", "alice"),
                 FakeCredential("x3", "Bob's", "bob"), FakeCredential("x4", "Carol A", "carol"),
                 FakeCredential("x5", "Carol B", "carol"), FakeCredential("p", "host:x", "bob", private=True)]
        self.assertEqual(N.suggest_credentials(plan, creds), {"alice": "x1", "bob": "x3"})


class MappingTests(PlanCase):
    def test_mapping_sets_user_and_bindings(self) -> None:
        plan = self.plan(UserTests.DOC)
        creds = {"x1": FakeCredential("x1", "Ops", "ops-user")}
        job = N.apply_mapping(plan, {"alice": "x1"}, creds, key_exists=lambda _p: True)
        entries = {e.alias: e for _p, e, _h in job.inserts}
        self.assertEqual(entries["a"].options["user"], "ops-user")
        self.assertEqual(entries["c"].options["user"], "Alice")  # None: as written
        self.assertEqual(entries["b"].options["user"], "ops-user")
        self.assertEqual(sorted(e.alias for e, cid in job.bindings if cid == "x1"), ["a", "b"])
        self.assertEqual([e.alias for e, _u in job.private], ["d"])
        self.assertNotIn("imported_from", entries["a"].metadata)
        with self.assertRaises(N.CredentialMissingError):
            N.apply_mapping(plan, {"alice": "gone"}, creds)
        defaulted = N.apply_mapping(plan, {}, creds, default="x1", key_exists=lambda _p: True)
        self.assertEqual({e.alias for e, cid in defaulted.bindings if cid}, {"a", "b", "c", "d", "e", "g"})


class ConflictTests(PlanCase):
    def setUp(self) -> None:
        super().setUp()
        top = self.store.create_group(None, "G")
        self.store.add_host(NewHost(top.id, "web", "web.example", 22, "deploy"))
        self.store.add_host(NewHost(top.id, "files", "web.example", 22, "deploy", protocol="sftp"))
        self.doc = document([group("G", [host("web", "WEB.example", port=22, user="Deploy"),
                                         host("web-other-user", "web.example", port=22, user="root"),
                                         host("web-ftps", "web.example", port=22, user="deploy", protocol="ftps"),
                                         host("files", "web.example", port=22, user="deploy", protocol="sftp")])])

    def job(self, policy):
        plan = self.plan(self.doc, N.NativeImportOptions(conflict_policy=policy))
        return N.apply_mapping(plan, {}, {}, key_exists=lambda _p: True)

    def test_matrix(self) -> None:
        skip = self.job(ConflictPolicy.SKIP)
        self.assertEqual(skip.skipped_existing, 2)  # same group, protocol, host (any case), port and user
        self.assertEqual([e.alias for _p, e, _h in skip.inserts], ["web-other-user", "web-ftps"])
        copy_ = self.job(ConflictPolicy.RENAME)
        self.assertEqual([e.alias for _p, e, _h in copy_.inserts],
                         ["web (2)", "web-other-user", "web-ftps", "files (2)"])
        update = self.job(ConflictPolicy.OVERWRITE)
        self.assertEqual([e.alias for e, _h in update.updates], ["web", "files"])  # the alias stays
        self.assertTrue(all(e.id for e, _h in update.updates))


class ColorPolicyTests(PlanCase):
    DOC = document([group("Top", [host("a", color="#111111")], [group("Sub", color="#222222")], color="#AABBCC"),
                    group("Top", color="#333333")])

    def job(self, policy=N.ColorPolicy.IF_EMPTY, **kw):
        options = N.NativeImportOptions(color_policy=policy, **kw)
        plan = self.plan(self.DOC, options)
        return N.apply_mapping(plan, {}, {}, existing_group_colors={"Top/Sub": "#ffffff"},
                               existing_host_colors={}, key_exists=lambda _p: True)

    def test_policies_and_keys(self) -> None:
        job = self.job()
        self.assertEqual(job.group_colors, {"Top": "#aabbcc", "Top (2)": "#333333"})  # Sub already has one
        self.assertEqual(job.host_colors, {"Top::::a": "#111111"})
        self.assertEqual(self.job(N.ColorPolicy.OVERWRITE).group_colors["Top/Sub"], "#222222")
        self.assertEqual(self.job(N.ColorPolicy.NONE).group_colors, {})
        self.assertEqual(self.job(import_colors=False).host_colors, {})
        self.store.create_group(None, "Mount")
        mounted = self.job(destination=Destination.EXISTING_GROUP, target_group=("Mount",))
        self.assertEqual(mounted.group_colors, {"Mount/Top": "#aabbcc", "Mount/Top/Sub": "#222222",
                                                "Mount/Top (2)": "#333333"})
        self.assertEqual(mounted.host_colors, {"Mount::Top::a": "#111111"})


class RoundTripPlanTests(unittest.TestCase):
    def test_the_rich_export_plans_completely(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = make_store(Path(tmp))
            self.addCleanup(store.close)
            _ids, gc, hc, creds, _unused = build_rich(store)
            doc, _ = build_export(store, group_colors=gc, host_colors=hc, credential_for=creds.get)
            plan = N.plan_native(N.read_native(F.dump_document(doc)))
            self.assertEqual(len(plan.hosts), 9)
            job = N.apply_mapping(plan, {}, {}, key_exists=lambda _p: False)
            self.assertEqual(len(job.tunnels), 1)
            self.assertIn("key_file_missing", codes(job.issues))
            text = N.render_native_report(plan, job, source_name="x.sshm")
            self.assertIn("Hosts planned: 9", text)


if __name__ == "__main__":
    unittest.main()
