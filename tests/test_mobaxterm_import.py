"""MobaXterm import into the app inventory: parser, planner, usernames, credentials, commit (no GTK)."""

import codecs
import hashlib
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from ophion.config import HostEntry
from ophion.credentials import CredentialStore
from ophion.importers.mobaxterm import (
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
    save_report,
    unique_name,
    verbatim_name,
)
from ophion.inventory.manager import StoreConfigManager
from ophion.inventory.store import InventoryStore
from ophion.secure_store import InMemoryKeyring, SecureStore, SecureStoreError
from ophion.ssh_command import build_ssh_command
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
        # Kept verbatim: only the bidi override (U+202E) goes; RLM/ZWSP are format characters the store allows.
        self.assertEqual(host.alias, "\u200f\u0633\u0631\u0648\u0631\u200b \u0627\u0635\u0644\u06cc")
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
        with mock.patch("ophion.importers.mobaxterm.MAX_FILE_BYTES", 100):
            self.assertFileError(ACME.encode(), "too_large")
            path = self.root / "big.mxtsessions"
            path.write_bytes(ACME.encode())
            self.assertFileError(path, "too_large")

    def test_wrong_extension_but_valid_content(self) -> None:
        path = self.root / "export.txt"
        path.write_bytes(ACME.encode())
        self.assertEqual(len(parse_mxtsessions(path).sessions), 11)


class NameTests(unittest.TestCase):
    def test_verbatim(self) -> None:
        for original in ("web 01", "00 - zone", "+ Ops - keep running", "0- legacy unused", "db (primary)",
                         "\u0633\u0631\u0648\u0631 \u0627\u0635\u0644\u06cc", "\u0645\u06cc\u200c\u0647\u0627", "Caf\u00e9",
                         "a.b_c-d", "x  y"):
            with self.subTest(original=original):
                self.assertEqual(verbatim_name(original), (original, []))

    def test_only_what_the_store_rejects_changes(self) -> None:
        self.assertEqual(verbatim_name("  a/b  "), ("a\u2215b", [IssueCode.SLASH_REPLACED]))
        self.assertEqual(verbatim_name("line\r\nbreak\ttab")[0], "line break tab")
        self.assertEqual(verbatim_name("rtl\u202eoverride\x01")[0], "rtloverride")
        long, issues = verbatim_name("x" * 200)
        self.assertEqual((len(long), issues), (128, [IssueCode.NAME_TRUNCATED]))
        self.assertEqual(verbatim_name("   ")[0], "")

    def test_numbering(self) -> None:
        self.assertEqual(unique_name("web", {"web"}), "web (2)")
        self.assertEqual(unique_name("Web", {"web", "web (2)"}), "Web (3)")
        self.assertEqual(len(unique_name("x" * 128, {"x" * 128})), 128)


class PlannerTests(unittest.TestCase):
    def plan(self, data=ACME.encode(), **kwargs):
        return plan_import(parse_mxtsessions(data), ImportOptions(**kwargs.pop("options", {})), **kwargs)

    def paths(self, plan):
        return [g.path for g in plan.groups]

    def test_folders_map_one_to_one_with_verbatim_names(self) -> None:
        plan = self.plan()
        self.assertEqual(self.paths(plan), [
            ("Acme",), ("Acme", "Team A"), ("Acme", "Team A", "Servers"), ("Acme", "+ Empty container (old)"),
            ("Acme", "Team B"), ("Acme", "Team B", "00 - deep"), ("Acme", "Team B", "00 - deep", "level 4"),
            ("Beta",)])
        hosts = {h.alias: h.group for h in plan.hosts}
        self.assertEqual(hosts["acme bastion"], ("Acme",))  # directly in the top-level group
        self.assertEqual(hosts["team-a-gw"], ("Acme", "Team A"))  # a folder with hosts and subfolders
        self.assertEqual(hosts["db (primary)"], ("Acme", "Team A", "Servers"))
        self.assertEqual(plan.top_level, ["Acme", "Beta"])
        self.assertEqual(plan.renamed, [])

    def test_skip_empty_folders_option(self) -> None:
        plan = self.plan(options={"skip_empty_folders": True})
        self.assertNotIn(("Acme", "+ Empty container (old)"), self.paths(plan))
        self.assertIn(("Acme", "Team B"), self.paths(plan))  # leads to a host

    def test_eleven_levels_equal_the_source_tree(self) -> None:
        deep = [f"level {i} (+x)" for i in range(11)]
        sections = [("\\".join(deep[: i + 1]), []) for i in range(11)]
        sections[-1] = (sections[-1][0], ["leaf host=" + session_value("10.0.0.1")])
        sections.append(("\\".join(deep[:4] + ["side branch"]), ["side=" + session_value("10.0.0.2")]))
        result = parse_mxtsessions(export(*sections))
        plan = self.plan(export(*sections))
        source = {f.path for f in result.folders}
        self.assertEqual(set(self.paths(plan)), source)

    def test_slash_and_collisions(self) -> None:
        data = export(("Acme\\a/b", ["x=" + session_value("10.0.0.1")]),
                      ("Acme\\Team", ["web=" + session_value("10.0.0.2"), "WEB=" + session_value("10.0.0.3")]),
                      ("Acme\\team", ["web=" + session_value("10.0.0.4")]),
                      ("Other", ["web=" + session_value("10.0.0.5")]))
        plan = self.plan(data)
        self.assertIn(("Acme", "a\u2215b"), self.paths(plan))
        self.assertIn(("Acme", "team (2)"), self.paths(plan))  # a different folder with the same name
        aliases = [(h.group, h.alias) for h in plan.hosts]
        self.assertIn((("Acme", "Team"), "WEB (2)"), aliases)
        self.assertIn((("Other",), "web"), aliases)  # same name in another folder: not renamed
        codes = [i.code for i in plan.issues]
        self.assertIn(IssueCode.SLASH_REPLACED, codes)
        self.assertIn(IssueCode.NAME_DEDUPLICATED, codes)
        self.assertTrue(plan.renamed)

    def test_unnamed(self) -> None:
        data = export(("Acme", [" =" + session_value("10.0.0.1"), "=" + session_value("10.0.0.2")]))
        self.assertEqual([h.alias for h in self.plan(data).hosts], ["Unnamed", "Unnamed 2"])

    def test_selection_excludes_branches(self) -> None:
        plan = self.plan(excluded_folders=[("Acme", "Team A")])
        self.assertNotIn(("Acme", "Team A"), self.paths(plan))
        self.assertEqual(plan.excluded_hosts, 8)  # 6 SSH + the FTP and RDP sessions

    def test_folder_tree_counts(self) -> None:
        roots = build_folder_tree(parse_mxtsessions(ACME.encode()))
        acme = roots[0]
        self.assertEqual((acme.label, acme.own_hosts, acme.total_hosts), ("Acme", 1, 10))
        team_a = next(c for c in acme.children if c.label == "Team A")
        self.assertEqual((team_a.own_hosts, team_a.total_hosts), (1, 8))  # FTP and RDP counted
        ssh_only = build_folder_tree(parse_mxtsessions(ACME.encode()), include_transfer=False, include_rdp=False)
        self.assertEqual(ssh_only[0].total_hosts, 8)

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
        entry = next(entry for _path, entry in job.inserts if entry.alias == "behind-jump")
        self.assertEqual(build_ssh_command(entry), ["ssh", "-p", "22", "-J", "alice@10.0.0.254", "bob@10.0.0.13"])


class UsernameTests(unittest.TestCase):
    def test_discovery(self) -> None:
        plan = plan_import(parse_mxtsessions(ACME.encode()))
        entries = {e.key: e for e in plan.usernames}
        self.assertEqual(set(entries), {"alice", "bob", "Alice", "alice@corp.local", "svc_backup", NO_USERNAME})
        self.assertEqual(entries["alice"].count, 4)  # + dev\\alice of the RDP session
        self.assertEqual(entries["alice"].raw_forms, {"[alice]", "alice", "dev\\alice"})  # merged forms
        self.assertEqual(entries["alice"].protocols, {"ssh", "rdp"})
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


class StoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = InventoryStore(self.root / "inventory.db")
        self.manager = StoreConfigManager(self.store)
        self.creds, self.ops, self.root_cred = make_creds(self.root)

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def plan(self, data=ACME.encode(), excluded=(), **options):
        return plan_import(parse_mxtsessions(data), ImportOptions(**options), excluded,
                           existing=ExistingInventory.from_manager(self.manager))

    def run_import(self, mapping=None, data=ACME.encode(), default=None, **options):
        job = apply_credential_mapping(self.plan(data, **options), mapping or {},
                                       {c.id: c for c in self.creds.list()}, default=default)
        return job, commit(job, self.manager, self.creds)

    def snapshot(self):
        """Row counts and a content hash of everything an import could change."""

        conn = self.store.conn
        groups = conn.execute("SELECT id, parent_id, name FROM groups ORDER BY id").fetchall()
        hosts = conn.execute("SELECT id, group_id, alias, hostname, port, user, options_json, metadata_json "
                             "FROM hosts ORDER BY id").fetchall()
        digest = hashlib.sha256(repr([tuple(r) for r in groups + hosts]).encode()).hexdigest()
        return len(groups), len(hosts), digest, dict(self.creds._bindings)

    def host(self, path, alias):
        child = "/".join(path[1:])
        return next(h for h in self.manager.tree[path[0]][child].hosts if h.alias == alias)


class CommitTests(StoreTestCase):
    def test_round_trip_names_users_bindings_metadata(self) -> None:
        job, report = self.run_import({"alice": self.ops.id, "bob": None}, default=self.root_cred.id)
        self.assertIs(report.status, ImportStatus.SUCCESS)
        tree = self.manager.tree
        self.assertIn("+ Empty container (old)", tree["Acme"])  # empty folders are created
        self.assertEqual(tree["Acme"]["+ Empty container (old)"].hosts, [])
        persian = "\u0633\u0631\u0648\u0631 \u0627\u0635\u0644\u06cc"
        self.assertIn(persian, [h.alias for h in tree["Beta"][""].hosts])
        web = self.host(("Acme", "Team A", "Servers"), "web 01")
        self.assertEqual(web.options["user"], "ops")
        self.assertEqual(web.metadata, {"imported_from": "mobaxterm", "mobaxterm_folder": "Acme\\Team A\\Servers"})
        self.assertEqual(self.creds.binding_for_host(web.id), self.ops.id)  # bound by the new host id
        self.assertEqual(self.host(("Acme", "Team A", "Servers"), "behind-jump").options["user"], "bob")  # literal kept
        self.assertNotIn("user", self.host(("Acme", "Team B", "00 - deep", "level 4"), "deep host").options)
        self.assertEqual(self.host(("Acme", "Team A", "Servers"), "backup").options["user"], "root")  # default
        self.assertEqual(self.host(("Acme", "Team A", "Servers"), "backup").options["port"], "2222")

    def test_bindings_survive_a_later_rename_and_move(self) -> None:
        self.run_import({"alice": self.ops.id})
        web = self.host(("Acme", "Team A", "Servers"), "web 01")
        self.manager.update_host("Acme", "Team A/Servers", "web 01", HostEntry("web one", dict(web.options)))
        self.manager.move_hosts([("Acme", "Team A/Servers", "web one")], "Beta", "")
        moved = self.host(("Beta",), "web one")
        self.assertEqual(moved.id, web.id)
        self.assertEqual(self.creds.binding_for_host(moved.id), self.ops.id)

    def test_second_run_changes_nothing(self) -> None:
        self.run_import({"alice": self.ops.id})
        before = self.snapshot()
        job, report = self.run_import({"alice": self.ops.id})
        self.assertIs(report.status, ImportStatus.NOTHING)
        self.assertEqual(job.skipped_existing, 11)  # 9 SSH + 1 FTP + 1 RDP
        self.assertEqual(self.snapshot(), before)

    def test_conflict_policies(self) -> None:
        self.run_import()
        job, report = self.run_import(conflict_policy=ConflictPolicy.RENAME)
        self.assertEqual(job.renamed, 11)  # 9 SSH + 1 FTP + 1 RDP
        self.assertIn("web 01 (2)", [h.alias for h in self.manager.tree["Acme"]["Team A/Servers"].hosts])
        before = {h.alias: h.id for h in self.manager.tree["Acme"]["Team A/Servers"].hosts}
        changed = ACME.replace("10.0.0.254", "10.0.0.253").encode()  # the jump host changed in MobaXterm
        job, report = self.run_import(data=changed, conflict_policy=ConflictPolicy.OVERWRITE)
        self.assertIs(report.status, ImportStatus.SUCCESS)
        self.assertEqual((job.host_count, job.overwritten), (0, 11))  # 9 SSH + 1 FTP + 1 RDP
        after = {h.alias: h.id for h in self.manager.tree["Acme"]["Team A/Servers"].hosts}
        self.assertEqual(before, after)  # updated in place: same ids, same aliases
        self.assertEqual(self.host(("Acme", "Team A", "Servers"), "behind-jump").options["proxyjump"],
                         "alice@10.0.0.253")
        self.assertEqual(self.host(("Acme", "Team A", "Servers"), "behind-jump (2)").options["proxyjump"],
                         "alice@10.0.0.254")  # the copy is not the one that matched
        # A different user is a different host: it is added, never merged.
        job, report = self.run_import({"alice": self.ops.id}, conflict_policy=ConflictPolicy.OVERWRITE)
        self.assertEqual(job.host_count, 4)  # the 3 SSH alice hosts + the RDP dev\\alice host
        self.assertTrue(any("before-import-overwrite" in p.name for p in self.store.list_backups()))

    def test_alias_taken_by_a_different_host(self) -> None:
        self.manager.create_parent_group("Acme")
        self.manager.add_host("Acme", "", HostEntry("acme bastion", {"hostname": "192.0.2.1"}))
        job, report = self.run_import()
        aliases = sorted(h.alias for h in self.manager.tree["Acme"][""].hosts)
        self.assertEqual(aliases, ["acme bastion", "acme bastion (2)"])

    def test_import_under_an_existing_group(self) -> None:
        self.manager.create_parent_group("Imports")
        self.manager.create_child_group("Imports", "2026")
        job, report = self.run_import(destination=Destination.EXISTING_GROUP, target_group=("imports", "2026"))
        self.assertIs(report.status, ImportStatus.SUCCESS)
        self.assertIn("2026/Acme/Team A/Servers", self.manager.tree["Imports"])
        self.assertIn("2026/Beta", self.manager.tree["Imports"])

    def test_destination_deleted_meanwhile(self) -> None:
        self.manager.create_parent_group("Imports")
        job = apply_credential_mapping(self.plan(destination=Destination.EXISTING_GROUP, target_group=("Imports",)),
                                       {}, {})
        self.manager.delete_parent_group("Imports")
        before = self.snapshot()
        report = commit(job, self.manager, self.creds)
        self.assertIs(report.status, ImportStatus.FAILED)
        self.assertIn("no changes were made", report.message)
        self.assertEqual(self.snapshot(), before)
        missing = self.plan(destination=Destination.EXISTING_GROUP, target_group=("Nope",))
        self.assertTrue(missing.destination_missing)

    def test_rollback_on_injected_failure_binding_failure_and_cancel(self) -> None:
        self.run_import()
        before = self.snapshot()
        options = {"conflict_policy": ConflictPolicy.RENAME}
        with mock.patch.object(self.store, "update_host", side_effect=RuntimeError("boom")), \
                mock.patch.object(self.store, "add_hosts_bulk", side_effect=RuntimeError("disk I/O error")):
            job, report = self.run_import(**options)
        self.assertIs(report.status, ImportStatus.FAILED)
        self.assertEqual(self.snapshot(), before)
        with mock.patch.object(self.creds, "_save", side_effect=SecureStoreError("disk")):
            job, report = self.run_import({"alice": self.ops.id}, **options)
        self.assertIs(report.status, ImportStatus.FAILED)
        self.assertEqual(self.snapshot(), before)
        job = apply_credential_mapping(self.plan(**options), {}, {})
        calls = {"n": 0}

        def cancel_later():
            calls["n"] += 1
            return calls["n"] > 3

        report = commit(job, self.manager, self.creds, should_cancel=cancel_later)
        self.assertIs(report.status, ImportStatus.CANCELLED)
        self.assertEqual(self.snapshot(), before)

    def test_deleted_credential_fails_cleanly(self) -> None:
        job = apply_credential_mapping(self.plan(), {"alice": self.ops.id}, {c.id: c for c in self.creds.list()})
        self.creds.delete(self.ops.id)
        before = self.snapshot()
        self.assertIs(commit(job, self.manager, self.creds).status, ImportStatus.FAILED)
        self.assertEqual(self.snapshot(), before)
        with self.assertRaises(CredentialMissingError):
            apply_credential_mapping(self.plan(), {"alice": "gone"}, {})

    def test_one_transaction_one_refresh_one_binding_save(self) -> None:
        job = apply_credential_mapping(self.plan(), {"alice": self.ops.id}, {c.id: c for c in self.creds.list()})
        statements = []
        self.store.conn.set_trace_callback(statements.append)
        try:
            with mock.patch.object(self.manager, "refresh", wraps=self.manager.refresh) as refresh, \
                    mock.patch.object(self.creds, "_save", wraps=self.creds._save) as save:
                report = commit(job, self.manager, self.creds)
        finally:
            self.store.conn.set_trace_callback(None)
        self.assertIs(report.status, ImportStatus.SUCCESS)
        self.assertEqual(sum(1 for s in statements if s.strip().upper() == "BEGIN IMMEDIATE"), 1)
        self.assertEqual(sum(1 for s in statements if s.strip().upper() == "COMMIT"), 1)
        self.assertEqual(refresh.call_count, 1)
        self.assertEqual(save.call_count, 1)

    def test_report_has_no_secrets_and_is_private(self) -> None:
        job, report = self.run_import({"alice": self.ops.id})
        text = render_report(job.plan, job, report, source_name="acme.mxtsessions")
        self.assertNotIn("s3cret", text)
        self.assertNotIn("#109#", text)
        self.assertIn("alice (4 host(s)) → Ops account", text)  # dev\\alice (RDP) is alice
        path = save_report(text, self.root / "imports")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_legacy_inventory_is_refused(self) -> None:
        from ophion.config import ConfigManager

        with self.assertRaises(TypeError):
            ExistingInventory.from_manager(ConfigManager(self.root / "folder"))


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


class PerformanceTests(StoreTestCase):
    def test_1500_hosts_800_folders_in_one_transaction(self) -> None:
        data = make_export(sessions=1500, folders=800, depth=12)
        start = time.perf_counter()
        result = parse_mxtsessions(data)
        plan = plan_import(result, existing=ExistingInventory.from_manager(self.manager))
        job = apply_credential_mapping(plan, {}, {self.ops.id: self.ops}, default=self.ops.id)
        report = commit(job, self.manager, self.creds)
        elapsed = time.perf_counter() - start
        self.assertIs(report.status, ImportStatus.SUCCESS)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM hosts").fetchone()[0], len(plan.hosts))
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM groups").fetchone()[0], 800)
        self.assertGreater(len(plan.hosts), 1400)
        self.assertLess(elapsed, 2.0, f"{elapsed:.2f}s")


if __name__ == "__main__":
    unittest.main()


TRANSFER_EXPORT = export(("Acme\\Files", [
    "ftp anon=" + session_value("10.0.0.40", "21", "", type_code=6, icon=130),
    "ftp anon2=" + session_value("10.0.0.41", "", "anonymous", type_code=6, icon=130),
    "ftp bob=" + session_value("10.0.0.42", "2121", "bob", type_code=6, icon=130),
    "sftp alice=" + session_value("10.0.0.43", "22", "[alice]", type_code=7, icon=140,
                                  gateway="10.0.0.254", gateway_port="22", gateway_user="alice"),
    "ssh alice=" + session_value("10.0.0.43", "22", "[alice]"),
]))


class TransferSessionTests(StoreTestCase):
    """MobaXterm FTP (6) and SFTP (7) sessions become ftp / sftp hosts (synthetic data only)."""

    def test_planned_protocols_ports_and_ftps_warning(self) -> None:
        plan = self.plan(TRANSFER_EXPORT)
        targets = {h.alias: h.target for h in plan.hosts}
        self.assertEqual({a: t.protocol for a, t in targets.items()},
                         {"ftp anon": "ftp", "ftp anon2": "ftp", "ftp bob": "ftp", "sftp alice": "sftp",
                          "ssh alice": "ssh"})
        self.assertEqual(targets["ftp anon2"].port, 21)  # no port: the protocol default
        self.assertEqual(targets["ftp bob"].port, 2121)
        self.assertEqual(targets["sftp alice"].proxy_jump, "")  # only f0..f2 are read for FTP / SFTP
        self.assertEqual(targets["ssh alice"].proxy_jump, "")
        self.assertTrue(targets["ftp anon"].anonymous and targets["ftp anon2"].anonymous)
        self.assertFalse(targets["ftp bob"].anonymous or targets["sftp alice"].anonymous)
        warnings = [i for i in plan.issues if i.code is IssueCode.FTPS_UNKNOWN]
        self.assertEqual(len(warnings), 3)
        self.assertIn("check whether this server uses FTPS", warnings[0].message)
        self.assertEqual(plan.skipped_types, {})

    def test_usernames_cover_transfer_hosts_but_not_anonymous(self) -> None:
        keys = {e.key: e.count for e in self.plan(TRANSFER_EXPORT).usernames}
        self.assertEqual(keys, {"alice": 2, "bob": 1})

    def test_option_off_skips_them(self) -> None:
        plan = self.plan(TRANSFER_EXPORT, include_transfer=False)
        self.assertEqual([h.alias for h in plan.hosts], ["ssh alice"])
        self.assertEqual(plan.skipped_types, {6: 3, 7: 1})

    def test_commit_stores_protocol_and_anonymous_settings(self) -> None:
        job, report = self.run_import({"alice": self.ops.id, "bob": None}, data=TRANSFER_EXPORT,
                                      default=self.root_cred.id)
        self.assertIs(report.status, ImportStatus.SUCCESS)
        rows = {h.alias: h for h in self.store.list_hosts()}
        self.assertEqual(rows["ftp anon"].protocol, "ftp")
        self.assertEqual(rows["ftp anon"].user, "")
        self.assertEqual(rows["ftp anon"].metadata["transfer"], {"v": 1, "anonymous": True})
        self.assertIsNone(self.creds.binding_for_host(rows["ftp anon"].id))  # no credential for anonymous
        self.assertEqual((rows["ftp bob"].protocol, rows["ftp bob"].port, rows["ftp bob"].user), ("ftp", 2121, "bob"))
        self.assertEqual(rows["sftp alice"].protocol, "sftp")
        self.assertEqual(self.creds.binding_for_host(rows["sftp alice"].id), self.ops.id)
        self.assertEqual(rows["ssh alice"].protocol, "ssh")
        text = render_report(job.plan, job)
        self.assertIn("FTP / SFTP hosts planned: 4", text)

    def test_sftp_session_does_not_match_the_ssh_host_on_the_same_port(self) -> None:
        self.run_import({"alice": self.ops.id}, data=TRANSFER_EXPORT)
        job, report = self.run_import({"alice": self.ops.id}, data=TRANSFER_EXPORT)
        self.assertIs(report.status, ImportStatus.NOTHING)
        self.assertEqual(job.skipped_existing, 5)  # each one matched its own protocol, not the other

    def test_sftp_session_next_to_an_ssh_host_is_imported(self) -> None:
        ssh = export(("Acme", ["web=" + session_value("10.0.0.50", "22", "alice")]))
        sftp = export(("Acme", ["web files=" + session_value("10.0.0.50", "22", "alice", type_code=7)]))
        self.run_import(data=ssh)
        job, report = self.run_import(data=sftp)
        self.assertEqual((job.host_count, job.skipped_existing), (1, 0))  # not the SSH host on 10.0.0.50:22


RDP_EXPORT = export(("Acme\\Desktops", [
    "label upn=" + session_value("10.0.0.60", "3389", "[alice@corp.local]", type_code=4, icon=91),
    "label short=" + session_value("10.0.0.61", "", "[bob]", type_code=4, icon=91),
    "domain user=" + session_value("10.0.0.62", "3390", "dev\\alice", type_code=4, icon=91),
    "plain=" + session_value("10.0.0.63", "3389", "carol", type_code=4, icon=91),
    "nobody=" + session_value("10.0.0.64", "3389", "", type_code=4, icon=91),
    "bad domain=" + session_value("10.0.0.65", "3389", "a\\b\\c", type_code=4, icon=91),
]))


class RdpSessionTests(StoreTestCase):
    """MobaXterm RDP (type 4) sessions become rdp hosts; only host, port and user are read."""

    def test_targets_and_usernames(self) -> None:
        plan = self.plan(RDP_EXPORT)
        targets = {h.alias: h.target for h in plan.hosts}
        self.assertTrue(all(t.protocol == "rdp" for t in targets.values()))
        self.assertEqual(targets["label short"].port, 3389)  # empty port: the RDP default
        self.assertEqual((targets["domain user"].user_key, targets["domain user"].domain), ("alice", "dev"))
        self.assertEqual((targets["label upn"].user_key, targets["label upn"].user_is_reference),
                         ("alice@corp.local", True))  # a credential label, never a login name
        self.assertEqual(targets["bad domain"].user_key, "")
        self.assertIn(IssueCode.INVALID_FIELD, codes(plan.issues))
        entries = {e.key: e for e in plan.usernames}
        self.assertEqual(entries["alice"].protocols, {"rdp"})
        self.assertEqual(entries["bob"].references, 1)

    def test_commit_and_report(self) -> None:
        job, report = self.run_import({"bob": self.ops.id}, data=RDP_EXPORT)
        self.assertIs(report.status, ImportStatus.SUCCESS)
        rows = {h.alias: h for h in self.store.list_hosts()}
        self.assertEqual((rows["domain user"].protocol, rows["domain user"].user, rows["domain user"].port),
                         ("rdp", "alice", 3390))
        self.assertEqual(rows["domain user"].metadata["rdp"], {"v": 1, "domain": "dev"})
        self.assertEqual(rows["plain"].metadata["rdp"], {"v": 1})
        self.assertEqual(rows["label short"].user, "ops")  # the label was mapped to a credential
        self.assertEqual(self.creds.binding_for_host(rows["label short"].id), self.ops.id)
        self.assertEqual(rows["label upn"].user, "")  # an unmapped label is never used as the login name
        text = render_report(job.plan, job)
        self.assertIn("RDP hosts planned: 6", text)
        self.assertIn("were not imported", text)
        again, second = self.run_import({"bob": self.ops.id}, data=RDP_EXPORT)
        self.assertIs(second.status, ImportStatus.NOTHING)  # idempotent

    def test_option_off(self) -> None:
        plan = self.plan(RDP_EXPORT, include_rdp=False)
        self.assertEqual(plan.hosts, [])
        self.assertEqual(plan.skipped_types, {4: 6})
