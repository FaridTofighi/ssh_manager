"""MobaXterm import: parser, planner, usernames, credential mapping, commit and rollback (no GTK)."""

import codecs
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from ssh_manager.config import ConfigManager, ImportTransaction, load_config_tree
from ssh_manager.credentials import CredentialStore
from ssh_manager.importers.mobaxterm import (
    NO_USERNAME,
    ConflictPolicy,
    CredentialMissingError,
    Destination,
    ExistingInventory,
    ImportOptions,
    ImportStatus,
    IssueCode,
    MxtFileError,
    apply_credential_mapping,
    build_folder_tree,
    clean_label,
    commit,
    decode_mxt_bytes,
    parse_mxtsessions,
    plan_import,
    render_report,
    sanitize_name,
    save_report,
)
from ssh_manager.secure_store import InMemoryKeyring, SecureStore, SecureStoreError
from ssh_manager.ssh_command import build_ssh_command
from tests.mxt_synthetic import ACME, make_export, session_value


def export(*sections) -> bytes:
    """``export(("Acme\\\\A", ["name=" + value, …]), …)`` → CRLF bytes."""

    lines = []
    for index, (subrep, sessions) in enumerate(sections):
        lines.append("[Bookmarks]" if index == 0 else f"[Bookmarks_{index}]")
        if subrep is not None:
            lines.append(f"SubRep={subrep}")
        lines.append("ImgNum=41")
        lines.extend(sessions)
        lines.append("")
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


def codes(issues):
    return [issue.code for issue in issues]


class ParserTests(unittest.TestCase):
    def test_acme_folders_sessions_and_fields(self) -> None:
        result = parse_mxtsessions(ACME.encode("utf-8"))
        self.assertEqual(result.encoding, "utf-8")
        self.assertEqual([f.path for f in result.folders], [
            ("Acme",), ("Acme", "Team A"), ("Acme", "Team A", "Servers"), ("Acme", "+ Empty container (old)"),
            ("Acme", "Team B", "00 - deep", "level 4"), ("Beta",)])
        self.assertEqual(len(result.sessions), 11)
        db = next(s for s in result.sessions if s.name == "db (primary)")  # space after '='
        self.assertEqual((db.type_code, db.icon, db.field(0), db.field(1), db.field(2)),
                         (0, 109, "10.0.0.11", "22", "[alice@corp.local]"))
        self.assertEqual(db.folder, ("Acme", "Team A", "Servers"))
        self.assertEqual(db.field(500), "")  # never IndexError
        self.assertEqual(result.problems, [])

    def test_line_endings_and_encodings(self) -> None:
        text = ACME.replace("\r\n", "\n")
        variants = {
            "crlf": (ACME.encode("utf-8"), "utf-8"),
            "lf": (text.encode("utf-8"), "utf-8"),
            "cr": (text.replace("\n", "\r").encode("utf-8"), "utf-8"),
            "bom": (codecs.BOM_UTF8 + ACME.encode("utf-8"), "utf-8-sig"),
            "utf16-bom": (ACME.encode("utf-16"), "utf-16"),
            "utf16le": (ACME.encode("utf-16-le"), "utf-16-le"),
            "utf16be": (ACME.encode("utf-16-be"), "utf-16-be"),
        }
        for name, (data, encoding) in variants.items():
            with self.subTest(variant=name):
                result = parse_mxtsessions(data)
                self.assertEqual(result.encoding, encoding)
                self.assertEqual(len(result.sessions), 11)
                self.assertEqual(len(result.folders), 6)

    def test_cp1252_and_latin1_fallbacks(self) -> None:
        data = export(("Acme\\Caf\u00e9", ["caf\u00e9 box=" + session_value("10.0.0.5")])).decode("utf-8")
        result = parse_mxtsessions(data.encode("cp1252"))
        self.assertEqual(result.encoding, "cp1252")
        self.assertEqual(result.folders[0].path, ("Acme", "Caf\u00e9"))
        self.assertEqual(decode_mxt_bytes(b"[Bookmarks]\r\nSubRep=A\x81\x8d\r\n")[1], "latin-1")

    def test_nuls_trailing_blank_lines_and_missing_final_newline(self) -> None:
        data = export(("Acme", ["a=" + session_value("10.0.0.1")])).rstrip(b"\r\n") + b"\x00"
        result = parse_mxtsessions(data + b"\r\n\r\n\r\n")
        self.assertEqual(len(result.sessions), 1)
        result = parse_mxtsessions(data.rstrip(b"\x00"))  # no final newline
        self.assertEqual(result.sessions[0].field(0), "10.0.0.1")

    def test_truncated_value_and_few_fields(self) -> None:
        result = parse_mxtsessions(export(("Acme", ["short=#109#0%10.0.0.9", "cut=#109#0%10.0.0.8%2"])))
        self.assertEqual([s.fields for s in result.sessions], [("10.0.0.9",), ("10.0.0.8", "2")])
        plan = plan_import(result)
        self.assertEqual([h.target.port for h in plan.hosts], [22, 2])
        self.assertIn(IssueCode.INVALID_PORT, codes(plan.issues))

    def test_names_containing_equals_and_unparseable_lines(self) -> None:
        result = parse_mxtsessions(export(("Acme", ["odd=name=#109#0%10.0.0.1%22", "garbage line", "x=not a session"])))
        self.assertEqual([s.name for s in result.sessions], [])  # "odd" with value "name=#…" does not match
        self.assertEqual(codes(result.problems).count(IssueCode.UNPARSEABLE_LINE), 3)
        for problem in result.problems:
            self.assertNotIn("10.0.0.1", problem.message)  # no raw values in issues

    def test_unknown_type_codes_are_counted(self) -> None:
        data = export(("Acme", ["vnc=" + session_value("10.0.0.1", type_code=5),
                                "odd=" + session_value("10.0.0.2", type_code=99),
                                "ssh=" + session_value("10.0.0.3")]))
        plan = plan_import(parse_mxtsessions(data))
        self.assertEqual(plan.skipped_types, {5: 1, 99: 1})
        self.assertEqual(len(plan.hosts), 1)
        self.assertEqual(codes(plan.issues).count(IssueCode.UNSUPPORTED_TYPE), 2)

    def test_orphan_sessions(self) -> None:
        data = ("orphan=" + session_value("10.0.0.1") + "\r\n").encode() + export(("Acme", []))
        result = parse_mxtsessions(data)
        self.assertEqual(codes(result.problems), [IssueCode.ORPHAN_SESSION])
        no_subrep = export((None, ["lost=" + session_value("10.0.0.2")]), ("Acme", ["ok=" + session_value("10.0.0.3")]))
        result = parse_mxtsessions(no_subrep)
        self.assertEqual([s.name for s in result.sessions], ["ok"])
        self.assertIn(IssueCode.ORPHAN_SESSION, codes(result.problems))

    def test_sessions_before_subrep_in_a_section_are_kept(self) -> None:
        data = b"[Bookmarks]\r\nfirst=" + session_value("10.0.0.1").encode() + b"\r\nSubRep=Acme\r\n"
        result = parse_mxtsessions(data)
        self.assertEqual(result.sessions[0].folder, ("Acme",))

    def test_duplicate_sections_merge(self) -> None:
        data = export(("Acme\\A", ["one=" + session_value("10.0.0.1")]), ("Acme\\A", ["two=" + session_value("10.0.0.2")]))
        result = parse_mxtsessions(data)
        self.assertEqual(len(result.folders), 1)
        self.assertEqual([s.folder for s in result.sessions], [("Acme", "A"), ("Acme", "A")])
        self.assertIn(IssueCode.DUPLICATE_FOLDER_SECTION, codes(result.problems))

    def test_weird_subreps(self) -> None:
        data = export(("\\Acme\\\\A\\", ["a=" + session_value("10.0.0.1")]),
                      ("", ["root=" + session_value("10.0.0.2")]),
                      ("..\\..\\etc", ["b=" + session_value("10.0.0.3")]),
                      ("C:\\Windows", ["c=" + session_value("10.0.0.4")]))
        result = parse_mxtsessions(data)
        self.assertEqual([f.path for f in result.folders],
                         [("Acme", "A"), ("MobaXterm",), ("..", "..", "etc"), ("C:", "Windows")])
        self.assertIn(IssueCode.EMPTY_SUBREP, codes(result.problems))

    def test_depth_12_and_too_deep(self) -> None:
        deep = "\\".join(f"L{i}" for i in range(12))
        too_deep = "\\".join(f"D{i}" for i in range(40))
        result = parse_mxtsessions(export((deep, ["a=" + session_value("10.0.0.1")]),
                                          (too_deep, ["b=" + session_value("10.0.0.2")])))
        self.assertEqual(len(result.sessions[0].folder), 12)
        self.assertEqual([s.name for s in result.sessions], ["a"])
        self.assertIn(IssueCode.FOLDER_TOO_DEEP, codes(result.problems))

    def test_persian_rtl_and_zero_width_names(self) -> None:
        name = "\u200f\u0633\u0631\u0648\u0631\u200b \u0627\u0635\u0644\u06cc\u202e"
        result = parse_mxtsessions(export(("Acme", [name + "=" + session_value("10.0.0.1")])))
        plan = plan_import(result)
        host = plan.hosts[0]
        self.assertRegex(host.alias, r"^unnamed-\d+$")
        self.assertEqual(host.original_name, "\u0633\u0631\u0648\u0631 \u0627\u0635\u0644\u06cc")
        self.assertEqual(clean_label("a\u200b\u202eb\tc\r\nd"), "ab c d")

    def test_line_too_long(self) -> None:
        data = export(("Acme", ["big=" + session_value("10.0.0.1") + "x" * 70000, "ok=" + session_value("10.0.0.2")]))
        result = parse_mxtsessions(data)
        self.assertEqual([s.name for s in result.sessions], ["ok"])
        self.assertIn(IssueCode.LINE_TOO_LONG, codes(result.problems))


class FileErrorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def assertFileError(self, source, code) -> None:
        with self.assertRaises(MxtFileError) as ctx:
            parse_mxtsessions(source)
        self.assertEqual(ctx.exception.code, code)
        self.assertTrue(ctx.exception.message)

    def test_file_level_errors(self) -> None:
        self.assertFileError(self.root / "missing.mxtsessions", "missing")
        self.assertFileError(self.root, "is_directory")
        empty = self.root / "empty.mxtsessions"
        empty.write_bytes(b"\r\n  \r\n")
        self.assertFileError(empty, "empty")
        self.assertFileError(b"[Other]\r\nkey=value\r\n", "not_mobaxterm")
        self.assertFileError(bytes(range(256)) * 40, "binary")

    def test_permission_denied(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("root can read anything")
        path = self.root / "locked.mxtsessions"
        path.write_bytes(ACME.encode())
        path.chmod(0)
        try:
            self.assertFileError(path, "permission")
        finally:
            path.chmod(0o600)

    def test_size_cap(self) -> None:
        with mock.patch("ssh_manager.importers.mobaxterm.MAX_FILE_BYTES", 100):
            self.assertFileError(ACME.encode(), "too_large")
            path = self.root / "big.mxtsessions"
            path.write_bytes(ACME.encode())
            self.assertFileError(path, "too_large")

    def test_wrong_extension_but_valid_content(self) -> None:
        path = self.root / "export.txt"
        path.write_bytes(ACME.encode())
        self.assertEqual(len(parse_mxtsessions(path).sessions), 11)


class SanitizeTests(unittest.TestCase):
    def test_rules(self) -> None:
        cases = {
            "web 01": "web-01", "00 - zone": "00-zone", "+ Ops - keep running": "Ops-keep-running",
            "0- legacy unused": "0-legacy-unused", "  ..hidden..  ": "hidden", "..": "", ".": "", "C:": "C",
            "#comment": "comment", "Caf\u00e9": "Cafe", "a/b\\c": "a-b-c", "x.bak": "x-bak", "__pycache__": "pycache",
        }
        for original, expected in cases.items():
            with self.subTest(original=original):
                self.assertEqual(sanitize_name(original)[0], expected)

    def test_truncation_is_stable_and_unique(self) -> None:
        a, issues = sanitize_name("x" * 100 + "-alpha")
        b, _ = sanitize_name("x" * 100 + "-beta")
        self.assertLessEqual(len(a), 64)
        self.assertIn(IssueCode.NAME_TRUNCATED, issues)
        self.assertNotEqual(a, b)
        self.assertEqual(a, sanitize_name("x" * 100 + "-alpha")[0])


class PlannerTests(unittest.TestCase):
    def plan(self, data=ACME.encode(), **kwargs):
        return plan_import(parse_mxtsessions(data), ImportOptions(**kwargs.pop("options", {})), **kwargs)

    def test_depth_mapping_general_and_hosts_collision(self) -> None:
        plan = self.plan()
        self.assertEqual([(g.parent, g.child) for g in plan.groups], [
            ("Acme", "General"), ("Acme", "Team-A/_hosts"), ("Acme", "Team-A/Servers"),
            ("Acme", "Team-B/00-deep/level-4"), ("Beta", "General")])
        self.assertEqual(plan.parents, ["Acme", "Beta"])
        self.assertEqual(len(plan.hosts), 9)

    def test_general_taken_by_a_real_subfolder(self) -> None:
        data = export(("Acme", ["root=" + session_value("10.0.0.1")]),
                      ("Acme\\General", ["sub=" + session_value("10.0.0.2")]))
        self.assertEqual([(g.child, [h.alias for h in g.hosts]) for g in self.plan(data).groups],
                         [("_hosts", ["root"]), ("General", ["sub"])])

    def test_empty_folders_option(self) -> None:
        self.assertNotIn("+-Empty-container-old", [g.child for g in self.plan().groups])
        plan = self.plan(options={"create_empty_folders": True})
        empty = [g for g in plan.groups if not g.hosts]
        self.assertIn(("Acme", "Empty-container-old"), [(g.parent, g.child) for g in empty])
        self.assertNotIn(("Acme", "Team-B"), [(g.parent, g.child) for g in plan.groups])  # containers: dirs only

    def test_sibling_names_stay_unique_case_insensitively(self) -> None:
        data = export(("Acme\\a b", ["x=" + session_value("10.0.0.1")]), ("Acme\\a-b", ["y=" + session_value("10.0.0.2")]),
                      ("Acme\\A B", ["z=" + session_value("10.0.0.3")]))
        self.assertEqual([g.child for g in self.plan(data).groups], ["a-b", "a-b-2", "A-B-3"])

    def test_alias_dedupe_case_insensitive_and_deterministic(self) -> None:
        data = export(("Acme", ["web 1=" + session_value("10.0.0.1"), "web-1=" + session_value("10.0.0.2"),
                                "WEB 1=" + session_value("10.0.0.3")]))
        first = [h.alias for h in self.plan(data).hosts]
        self.assertEqual(first, ["web-1", "web-1-2", "WEB-1-3"])
        self.assertEqual(first, [h.alias for h in self.plan(data).hosts])
        self.assertIn(IssueCode.ALIAS_DEDUPLICATED, codes(self.plan(data).issues))

    def test_existing_parent_destination(self) -> None:
        plan = self.plan(options={"destination": Destination.EXISTING_PARENT, "target_parent": "imported"})
        self.assertEqual({g.parent for g in plan.groups}, {"imported"})
        self.assertIn("Acme/_hosts", [g.child for g in plan.groups])
        self.assertIn("Acme/Team-A/Servers", [g.child for g in plan.groups])
        self.assertIn("Beta", [g.child for g in plan.groups])

    def test_selection_excludes_branches(self) -> None:
        plan = self.plan(excluded_folders=[("Acme", "Team A")])
        self.assertEqual([g.child for g in plan.groups], ["General", "Team-B/00-deep/level-4", "General"])
        self.assertEqual(plan.excluded_hosts, 6)

    def test_folder_tree_counts(self) -> None:
        roots = build_folder_tree(parse_mxtsessions(ACME.encode()))
        acme = roots[0]
        self.assertEqual((acme.label, acme.own_hosts, acme.total_hosts), ("Acme", 1, 8))
        team_a = next(c for c in acme.children if c.label == "Team A")
        self.assertEqual((team_a.own_hosts, team_a.total_hosts), (1, 6))  # RDP/FTP not counted
        self.assertIn("+ Empty container (old)", [c.label for c in acme.children])

    def test_path_traversal_never_leaves_base_dir(self) -> None:
        data = export(("..\\..\\etc", ["pwn=" + session_value("10.0.0.1")]),
                      ("/abs\\..\\x", ["a=" + session_value("10.0.0.2")]),
                      ("C:\\Windows\\..", ["b=" + session_value("10.0.0.3")]))
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "inv"
            manager = ConfigManager(base)
            plan = plan_import(parse_mxtsessions(data), existing=ExistingInventory.from_manager(manager))
            job = apply_credential_mapping(plan, {}, {})
            for write in job.writes:
                resolved = write.group.path.resolve()
                self.assertTrue(base.resolve() in resolved.parents, resolved)
            store = CredentialStore(SecureStore(InMemoryKeyring()), path=Path(tmp) / "c.json")
            self.assertIs(commit(job, manager, store).status, ImportStatus.SUCCESS)
            self.assertEqual(sorted(p.name for p in Path(tmp).iterdir() if p.name != "c.json"), ["inv"])
            self.assertEqual(sorted(manager.tree), ["C", "abs", "unnamed-1"])

    def test_hostname_edge_cases(self) -> None:
        data = export(("Acme", [
            "v6=" + session_value("[fe80::1]"), "v6b=" + session_value("2001:db8::7"),
            "withport=" + session_value("10.0.0.1:2200", ""), "both=" + session_value("10.0.0.1:2200", "22"),
            "upper=" + session_value("  WEB.EXAMPLE  "), "spaces=" + session_value("bad host"),
            "dash=" + session_value("-oProxyCommand=evil"), "empty=" + session_value(""),
            "port0=" + session_value("10.0.0.2", "0"), "portx=" + session_value("10.0.0.3", "abc"),
            "portbig=" + session_value("10.0.0.4", "70000"),
        ]))
        plan = self.plan(data)
        targets = {h.alias: (h.target.hostname, h.target.port) for h in plan.hosts}
        self.assertEqual(targets["v6"], ("fe80::1", 22))
        self.assertEqual(targets["v6b"], ("2001:db8::7", 22))
        self.assertEqual(targets["withport"], ("10.0.0.1", 2200))
        self.assertEqual(targets["upper"], ("WEB.EXAMPLE", 22))
        for alias in ("port0", "portx", "portbig"):
            self.assertEqual(targets[alias][1], 22)
        self.assertNotIn("both", targets)
        self.assertNotIn("spaces", targets)
        self.assertNotIn("dash", targets)  # never an ssh option
        self.assertNotIn("empty", targets)
        self.assertIn(IssueCode.EMPTY_HOSTNAME, codes(plan.issues))
        self.assertGreaterEqual(codes(plan.issues).count(IssueCode.INVALID_HOSTNAME), 3)

    def test_identity_file_best_effort(self) -> None:
        data = export(("Acme", [
            "linux=" + session_value("10.0.0.1", extra_fields=["", "", "/home/a/.ssh/id_ed25519"]),
            "win=" + session_value("10.0.0.2", extra_fields=["", "C:\\Users\\a\\.ssh\\key.ppk"]),
            "spaced=" + session_value("10.0.0.3", extra_fields=["C:\\My Keys\\a.pem"]),
            "none=" + session_value("10.0.0.4"),
        ]))
        plan = self.plan(data)
        identities = {h.alias: h.target.identity_file for h in plan.hosts}
        self.assertEqual(identities, {"linux": "/home/a/.ssh/id_ed25519", "win": "C:\\Users\\a\\.ssh\\key.ppk",
                                      "spaced": "", "none": ""})
        self.assertEqual(codes(plan.issues).count(IssueCode.WINDOWS_KEY_PATH), 2)


class GatewayTests(unittest.TestCase):
    def jump(self, **kwargs):
        data = export(("Acme", ["h=" + session_value("10.0.0.1", **kwargs)]))
        plan = plan_import(parse_mxtsessions(data))
        return plan.hosts[0].target.proxy_jump, plan.issues

    def test_formats(self) -> None:
        self.assertEqual(self.jump(gateway="gw.example", gateway_port="22", gateway_user="alice")[0], "alice@gw.example")
        self.assertEqual(self.jump(gateway="gw.example", gateway_port="2222")[0], "gw.example:2222")
        self.assertEqual(self.jump(gateway="fe80::9", gateway_port="2222", gateway_user="bob")[0], "bob@[fe80::9]:2222")
        self.assertEqual(self.jump(gateway="gw.example", gateway_port="")[0], "gw.example")

    def test_empty_gateway_ignores_port_and_user(self) -> None:
        jump, issues = self.jump(gateway="", gateway_port="1", gateway_user="alice")
        self.assertEqual(jump, "")
        self.assertNotIn(IssueCode.INVALID_GATEWAY, codes(issues))

    def test_invalid_gateway_degrades_with_a_warning(self) -> None:
        for kwargs in ({"gateway": "bad gw", "gateway_port": "22"}, {"gateway": "gw", "gateway_port": "99999"},
                       {"gateway": "-oX", "gateway_port": "22"}):
            with self.subTest(**kwargs):
                jump, issues = self.jump(**kwargs)
                self.assertEqual(jump, "")
                self.assertIn(IssueCode.INVALID_GATEWAY, codes(issues))
        jump, issues = self.jump(gateway="gw", gateway_port="22", gateway_user="a b")
        self.assertEqual(jump, "gw")

    def test_ssh_command_uses_the_jump_host(self) -> None:
        plan = plan_import(parse_mxtsessions(ACME.encode()))
        job = apply_credential_mapping(plan, {}, {})
        entry = next(h for w in job.writes for h in w.group.hosts if h.alias == "behind-jump")
        self.assertEqual(build_ssh_command(entry), ["ssh", "-p", "22", "-J", "alice@10.0.0.254", "bob@10.0.0.13"])


class UsernameTests(unittest.TestCase):
    def test_discovery(self) -> None:
        plan = plan_import(parse_mxtsessions(ACME.encode()))
        entries = {e.key: e for e in plan.usernames}
        self.assertEqual(set(entries), {"alice", "bob", "Alice", "alice@corp.local", "svc_backup", NO_USERNAME})
        self.assertEqual(entries["alice"].count, 3)
        self.assertEqual(entries["alice"].raw_forms, {"[alice]", "alice"})  # bracket + plain merged
        self.assertEqual(entries["alice"].case_variants, ["Alice"])  # case-sensitive, with a hint
        self.assertEqual(entries[NO_USERNAME].count, 1)
        self.assertEqual(plan.usernames[0].key, "alice")  # most used first
        self.assertNotIn("dev\\alice", entries)  # RDP usernames are not offered

    def test_only_selected_folders(self) -> None:
        plan = plan_import(parse_mxtsessions(ACME.encode()), excluded_folders=[("Acme", "Team A"), ("Beta",)])
        self.assertEqual({e.key: e.count for e in plan.usernames}, {"alice": 1, "bob": 1})


def make_creds(tmp: Path):
    store = CredentialStore(SecureStore(InMemoryKeyring()), path=tmp / "credentials.json")
    ops = store.add("Ops account", "ops", "s3cret")
    root = store.add("Root", "root", "s3cret")
    return store, ops, root


class CommitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.base = self.root / "inventory"
        self.manager = ConfigManager(self.base)
        self.store, self.ops, self.root_cred = make_creds(self.root)
        self.creds = {c.id: c for c in self.store.list()}

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def plan(self, data=ACME.encode(), **options):
        return plan_import(parse_mxtsessions(data), ImportOptions(**options), existing=ExistingInventory.from_manager(self.manager))

    def snapshot(self):
        return {p.relative_to(self.base): p.read_bytes() for p in sorted(self.base.rglob("*")) if p.is_file()}, \
            sorted(str(p.relative_to(self.base)) for p in self.base.rglob("*"))

    def run_import(self, mapping=None, default=None, **options):
        job = apply_credential_mapping(self.plan(**options), mapping or {}, self.creds, default=default)
        return job, commit(job, self.manager, self.store)

    def host(self, parent, child, alias):
        return next(h for h in self.manager.tree[parent][child].hosts if h.alias == alias)

    def test_round_trip_users_bindings_and_metadata(self) -> None:
        job, report = self.run_import({"alice": self.ops.id, "bob": None}, default=self.root_cred.id)
        self.assertIs(report.status, ImportStatus.SUCCESS)
        tree = load_config_tree(self.base)
        self.assertEqual(sum(len(g.hosts) for groups in tree.values() for g in groups.values()), 9)
        web = self.host("Acme", "Team-A/Servers", "web-01")
        self.assertEqual(web.options["user"], "ops")  # [alice] → Ops account
        self.assertEqual(web.metadata["display_name"], "web 01")
        self.assertEqual(web.metadata["imported_from"], "mobaxterm")
        self.assertEqual(web.metadata["mobaxterm_folder"], "Acme\\Team A\\Servers")
        self.assertEqual(self.store.binding_for_host("Acme::Team-A/Servers::web-01"), self.ops.id)
        # "None" + literal username keeps it; "None" + [label] drops it.
        self.assertEqual(self.host("Acme", "Team-A/Servers", "behind-jump").options["user"], "bob")
        self.assertNotIn("user", self.host("Acme", "Team-B/00-deep/level-4", "deep-host").options)
        self.assertIsNone(self.store.binding_for_host("Acme::Team-B/00-deep/level-4::deep-host"))
        # Unmapped rows use the default credential.
        self.assertEqual(self.host("Acme", "Team-A/Servers", "backup").options["user"], "root")
        self.assertEqual(self.host("Acme", "Team-A/Servers", "no-user").options["user"], "root")
        self.assertEqual(self.host("Acme", "Team-A/Servers", "backup").options["port"], "2222")

    def test_second_run_adds_nothing(self) -> None:
        self.run_import()
        before = self.snapshot()
        job, report = self.run_import()
        self.assertIs(report.status, ImportStatus.NOTHING)
        self.assertEqual(job.host_count, 0)
        self.assertEqual(self.snapshot(), before)

    def test_conflict_policies(self) -> None:
        self.run_import()
        job, report = self.run_import(conflict_policy=ConflictPolicy.SKIP, skip_duplicates=False)
        self.assertIs(report.status, ImportStatus.NOTHING)
        self.assertEqual(job.skipped_existing, 9)
        job, report = self.run_import(conflict_policy=ConflictPolicy.RENAME, skip_duplicates=False)
        self.assertEqual(job.host_count, 9)
        self.assertIn("web-01-2", [h.alias for h in self.manager.tree["Acme"]["Team-A/Servers"].hosts])
        job, report = self.run_import({"alice": self.ops.id}, conflict_policy=ConflictPolicy.OVERWRITE,
                                      skip_duplicates=False)
        self.assertEqual(job.overwritten, 9)
        self.assertEqual(self.host("Acme", "Team-A/Servers", "web-01").options["user"], "ops")
        self.assertEqual(len(self.manager.tree["Acme"]["Team-A/Servers"].hosts), 10)  # 5 + 5 renamed

    def test_existing_hosts_in_a_group_are_kept(self) -> None:
        self.manager.create_parent_group("Acme")
        self.manager.create_child_group("Acme", "General")
        from ssh_manager.config import HostEntry

        self.manager.add_host("Acme", "General", HostEntry("mine", {"hostname": "10.9.9.9"}))
        self.run_import()
        self.assertEqual([h.alias for h in self.manager.tree["Acme"]["General"].hosts], ["acme-bastion", "mine"])

    def test_disk_collisions_with_existing_files_and_folders(self) -> None:
        (self.base / "Acme").mkdir(parents=True)
        (self.base / "Acme" / "Team-A").write_text("")  # a group file where the import needs a folder
        (self.base / "Acme" / "General").mkdir()  # a folder where the import needs a file
        (self.base / "Acme" / "General" / "x").write_text("")
        self.manager.refresh()
        job, report = self.run_import()
        self.assertIs(report.status, ImportStatus.SUCCESS)
        children = set(self.manager.tree["Acme"])
        self.assertIn("General/_hosts", children)
        self.assertIn("Team-A-2/Servers", children)
        self.assertIn(IssueCode.FOLDER_COLLISION_RESOLVED, codes(job.plan.issues))

    def test_rollback_on_injected_oserror_leaves_tree_identical(self) -> None:
        self.run_import()  # some existing content to protect
        before = self.snapshot()
        real = __import__("ssh_manager.config", fromlist=["write_text_atomic"]).write_text_atomic
        calls = {"n": 0}

        def failing(path, text, **kwargs):
            calls["n"] += 1
            if calls["n"] == 3:
                raise OSError(28, "No space left on device")
            return real(path, text, **kwargs)

        job = apply_credential_mapping(self.plan(conflict_policy=ConflictPolicy.RENAME, skip_duplicates=False,
                                                 create_empty_folders=True), {}, self.creds)
        self.assertGreaterEqual(len(job.writes), 3)
        with mock.patch("ssh_manager.config.write_text_atomic", side_effect=failing):
            report = commit(job, self.manager, self.store)
        self.assertIs(report.status, ImportStatus.FAILED)
        self.assertIn("no changes were made", report.message)
        self.assertEqual(self.snapshot(), before)

    def test_binding_failure_rolls_back_files(self) -> None:
        before = self.snapshot()
        job = apply_credential_mapping(self.plan(), {"alice": self.ops.id}, self.creds)
        with mock.patch.object(self.store, "_save", side_effect=SecureStoreError("disk full")):
            report = commit(job, self.manager, self.store)
        self.assertIs(report.status, ImportStatus.FAILED)
        self.assertEqual(self.snapshot(), before)
        self.assertIsNone(self.store.binding_for_host("Acme::Team-A/Servers::web-01"))

    def test_read_only_target(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("root ignores permissions")
        self.base.chmod(0o500)
        try:
            job = apply_credential_mapping(self.plan(), {}, self.creds)
            report = commit(job, self.manager, self.store)
        finally:
            self.base.chmod(0o700)
        self.assertIs(report.status, ImportStatus.FAILED)
        self.assertEqual(list(self.base.iterdir()), [])

    def test_concurrent_modification_is_detected(self) -> None:
        self.run_import()
        job = apply_credential_mapping(self.plan(conflict_policy=ConflictPolicy.RENAME, skip_duplicates=False), {}, self.creds)
        target = job.writes[0].group.path
        target.write_text(target.read_text() + "\nHost sneaky\n    hostname 10.1.1.1\n")
        before = self.snapshot()
        report = commit(job, self.manager, self.store)
        self.assertIs(report.status, ImportStatus.FAILED)
        self.assertIn("changed since", report.message)
        self.assertEqual(self.snapshot(), before)

    def test_deleted_credential_fails_cleanly(self) -> None:
        job = apply_credential_mapping(self.plan(), {"alice": self.ops.id}, self.creds)
        self.store.delete(self.ops.id)
        before = self.snapshot()
        report = commit(job, self.manager, self.store)
        self.assertIs(report.status, ImportStatus.FAILED)
        self.assertEqual(self.snapshot(), before)
        with self.assertRaises(CredentialMissingError):
            apply_credential_mapping(self.plan(), {"alice": "gone"}, self.creds)

    def test_cancel_rolls_back(self) -> None:
        before = self.snapshot()
        job = apply_credential_mapping(self.plan(), {}, self.creds)
        calls = {"n": 0}

        def cancel_after_two():
            calls["n"] += 1
            return calls["n"] > 2

        report = commit(job, self.manager, self.store, should_cancel=cancel_after_two)
        self.assertIs(report.status, ImportStatus.CANCELLED)
        self.assertEqual(self.snapshot(), before)

    def test_refresh_once_and_no_per_host_writes(self) -> None:
        job = apply_credential_mapping(self.plan(), {"alice": self.ops.id}, self.creds)
        with mock.patch.object(self.manager, "refresh", wraps=self.manager.refresh) as refresh, \
                mock.patch.object(self.manager, "add_host", side_effect=AssertionError("per-host write")), \
                mock.patch.object(self.store, "_save", wraps=self.store._save) as save:
            report = commit(job, self.manager, self.store)
        self.assertIs(report.status, ImportStatus.SUCCESS)
        self.assertEqual(refresh.call_count, 1)
        self.assertEqual(save.call_count, 1)

    def test_report_has_no_secrets_and_is_private(self) -> None:
        job, report = self.run_import({"alice": self.ops.id})
        text = render_report(job.plan, job, report, source_name="acme.mxtsessions")
        self.assertNotIn("s3cret", text)
        self.assertNotIn("#109#", text)  # no raw session values
        self.assertIn("alice (3 host(s)) → Ops account", text)
        self.assertIn("unsupported_type", text)
        path = save_report(text, self.root / "imports")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertTrue(path.name.startswith("mobaxterm-"))


class StoreTargetTests(unittest.TestCase):
    """Importing into the app store: readable names, no General/_hosts, one transaction."""

    def setUp(self) -> None:
        from ssh_manager.inventory.manager import StoreConfigManager
        from ssh_manager.inventory.store import InventoryStore

        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = InventoryStore(self.root / "inventory.db")
        self.manager = StoreConfigManager(self.store)
        self.creds, self.ops, self.root_cred = make_creds(self.root)

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def run_import(self, mapping=None, **options):
        plan = plan_import(parse_mxtsessions(ACME.encode()), ImportOptions(**options),
                           existing=ExistingInventory.from_manager(self.manager))
        job = apply_credential_mapping(plan, mapping or {}, {c.id: c for c in self.creds.list()})
        return plan, job, commit(job, self.manager, self.creds)

    def test_original_names_and_structure(self) -> None:
        plan, job, report = self.run_import({"alice": self.ops.id})
        self.assertIs(report.status, ImportStatus.SUCCESS)
        tree = self.manager.tree
        self.assertEqual([h.alias for h in tree["Acme"][""].hosts], ["acme bastion"])  # directly in the folder
        self.assertEqual([h.alias for h in tree["Acme"]["Team A"].hosts], ["team-a-gw"])  # hosts + subfolders
        self.assertEqual({h.alias for h in tree["Acme"]["Team A/Servers"].hosts},
                         {"web 01", "db (primary)", "backup", "behind-jump", "no user"})
        self.assertIn("Team B/00 - deep/level 4", tree["Acme"])
        self.assertIn("\u0633\u0631\u0648\u0631 \u0627\u0635\u0644\u06cc", [h.alias for h in tree["Beta"][""].hosts])
        self.assertNotIn("General", tree["Acme"])
        self.assertFalse(any("_hosts" in child for child in tree["Acme"]))
        web = next(h for h in tree["Acme"]["Team A/Servers"].hosts if h.alias == "web 01")
        self.assertEqual(web.options["user"], "ops")
        self.assertEqual(self.creds.binding_for_host(web.id), self.ops.id)  # bound by the stable id

    def test_second_run_adds_nothing(self) -> None:
        self.run_import()
        _plan, job, report = self.run_import()
        self.assertIs(report.status, ImportStatus.NOTHING)
        self.assertEqual(job.host_count, 0)

    def test_overwrite_keeps_the_host_id(self) -> None:
        self.run_import()
        before = {h.alias: h.id for h in self.manager.tree["Acme"]["Team A/Servers"].hosts}
        _plan, job, report = self.run_import({"alice": self.ops.id}, conflict_policy=ConflictPolicy.OVERWRITE,
                                             skip_duplicates=False)
        self.assertEqual(job.overwritten, 9)
        after = {h.alias: h.id for h in self.manager.tree["Acme"]["Team A/Servers"].hosts}
        self.assertEqual(before, after)
        self.assertEqual(self.creds.binding_for_host(after["web 01"]), self.ops.id)

    def test_binding_failure_and_cancel_roll_back(self) -> None:
        with mock.patch.object(self.creds, "_save", side_effect=SecureStoreError("disk")):
            _plan, _job, report = self.run_import({"alice": self.ops.id})
        self.assertIs(report.status, ImportStatus.FAILED)
        self.assertEqual(self.manager.tree, {})
        plan = plan_import(parse_mxtsessions(ACME.encode()), existing=ExistingInventory.from_manager(self.manager))
        job = apply_credential_mapping(plan, {}, {})
        report = commit(job, self.manager, self.creds, should_cancel=lambda: True)
        self.assertIs(report.status, ImportStatus.CANCELLED)
        self.assertEqual(self.manager.tree, {})


class BindManyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store, self.ops, self.root = make_creds(Path(self.tmp.name))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_saves_once(self) -> None:
        with mock.patch.object(self.store, "_save", wraps=self.store._save) as save:
            self.store.bind_many({"p::c::a": self.ops.id, "p::c::b": self.root.id})
        self.assertEqual(save.call_count, 1)
        self.assertEqual(self.store.binding_for_host("p::c::b"), self.root.id)
        self.store.bind_many({"p::c::a": None})
        self.assertIsNone(self.store.binding_for_host("p::c::a"))

    def test_rolls_back_on_secure_store_error(self) -> None:
        self.store.bind_many({"p::c::a": self.ops.id})
        with mock.patch.object(self.store, "_save", side_effect=SecureStoreError("fail")):
            with self.assertRaises(SecureStoreError):
                self.store.bind_many({"p::c::a": self.root.id, "p::c::b": self.ops.id})
        self.assertEqual(self.store.binding_for_host("p::c::a"), self.ops.id)
        self.assertIsNone(self.store.binding_for_host("p::c::b"))

    def test_unknown_credential_changes_nothing(self) -> None:
        with self.assertRaises(KeyError):
            self.store.bind_many({"p::c::a": self.ops.id, "p::c::b": "nope"})
        self.assertIsNone(self.store.binding_for_host("p::c::a"))


class TransactionTests(unittest.TestCase):
    def test_rollback_only_touches_what_it_created(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            keep = root / "keep.txt"
            keep.write_text("mine")
            txn = ImportTransaction()
            new_dir = root / "new"
            new_dir.mkdir()
            txn.created_dirs.append(new_dir)
            created = new_dir / "file"
            created.write_text("x")
            txn.created_files.append(created)
            txn.overwritten[keep] = b"mine"
            keep.write_text("changed")
            self.assertEqual(txn.rollback(), [])
            self.assertEqual(keep.read_text(), "mine")
            self.assertFalse(new_dir.exists())


class PerformanceTests(unittest.TestCase):
    def test_large_synthetic_export(self) -> None:
        data = make_export(sessions=1500, folders=800, depth=12)
        with tempfile.TemporaryDirectory() as tmp:
            manager = ConfigManager(Path(tmp) / "inv")
            store = CredentialStore(SecureStore(InMemoryKeyring()), path=Path(tmp) / "c.json")
            cred = store.add("Ops", "ops", "pw")
            start = time.perf_counter()
            result = parse_mxtsessions(data)
            plan = plan_import(result, existing=ExistingInventory.from_manager(manager))
            job = apply_credential_mapping(plan, {}, {cred.id: cred}, default=cred.id)
            with mock.patch.object(manager, "refresh", wraps=manager.refresh) as refresh:
                report = commit(job, manager, store)
            elapsed = time.perf_counter() - start
            self.assertIs(report.status, ImportStatus.SUCCESS)
            self.assertEqual(refresh.call_count, 1)
            imported = sum(len(g.hosts) for groups in manager.tree.values() for g in groups.values())
            self.assertEqual(imported, len(plan.hosts))
            self.assertGreater(imported, 1400)
            self.assertLess(elapsed, 10.0, f"{elapsed:.2f}s")


if __name__ == "__main__":
    unittest.main()
