"""OpenSSH import/export and the legacy conf.d migration (no GTK)."""

import hashlib
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ssh_manager.credentials import CredentialStore
from ssh_manager.inventory.manager import StoreConfigManager
from ssh_manager.inventory.openssh import (
    OPENSSH_CONFIG_GROUP,
    Policy,
    apply_include_line,
    commit_openssh_import,
    import_sources_found,
    include_line_change,
    plan_openssh_import,
    render_export,
    write_export,
)
from ssh_manager.inventory.store import InventoryStore
from ssh_manager.secure_store import InMemoryKeyring, SecureStore, SecureStoreError


def digest(root: Path) -> dict:
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob("*")) if p.is_file()}


class OpenSSHTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.ssh = self.home / ".ssh"
        self.confd = self.ssh / "conf.d"
        (self.confd / "prod").mkdir(parents=True)
        (self.confd / "prod" / "web.conf").write_text(
            "Host web-01\n    HostName 10.0.0.1\n    User deploy\n# meta tags=web\n\n"
            "Host web-02\n\tHostName=10.0.0.2\n\tPort 2222\n", encoding="utf-8")
        (self.confd / "prod" / "db").mkdir()
        (self.confd / "prod" / "db" / "primary.conf").write_text("Host db-1\n    HostName 10.0.4.12\n", encoding="utf-8")
        (self.confd / "prod" / "empty.conf").write_text("", encoding="utf-8")
        self.config = self.ssh / "config"
        self.config.write_bytes(
            b"\xef\xbb\xbfInclude conf.d/prod/*.conf\r\n"
            b"Host *\r\n    ServerAliveInterval 30\r\n"
            b"Host personal box.example\r\n    HostName \"box.example\"\r\n    IdentityFile \"~/.ssh/my key\"\r\n"
            b"Host personal\r\n    HostName dup.example\r\n"
            b"Match host *.corp\r\n    User corp\r\n")
        self.store = InventoryStore(Path(self.tmp.name) / "data" / "inventory.db")
        self.manager = StoreConfigManager(self.store)
        self.creds = CredentialStore(SecureStore(InMemoryKeyring()), path=Path(self.tmp.name) / "credentials.json")
        self.cred = self.creds.add("Ops", "ops", "pw")

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()


class ImportTests(OpenSSHTestCase):
    def test_folder_import_keeps_names_and_layout(self) -> None:
        before = digest(self.ssh)
        plan = plan_openssh_import([self.confd])
        self.assertEqual(plan.host_count, 3)
        self.assertIn(("prod", "empty.conf"), plan.groups)  # empty child files are empty groups
        report = commit_openssh_import(plan, self.manager)
        self.assertEqual(report.added, 3)
        tree = self.manager.tree
        self.assertEqual([h.alias for h in tree["prod"]["web.conf"].hosts], ["web-01", "web-02"])
        web2 = tree["prod"]["web.conf"].hosts[1]
        self.assertEqual((web2.options["hostname"], web2.options["port"]), ("10.0.0.2", "2222"))  # tab and '='
        self.assertEqual(tree["prod"]["web.conf"].hosts[0].metadata["tags"], "web")
        self.assertIn("db/primary.conf", tree["prod"])
        self.assertEqual(tree["prod"]["empty.conf"].hosts, [])
        self.assertEqual(digest(self.ssh), before)  # sources are never written

    def test_config_file_import_skips_patterns_and_match(self) -> None:
        plan = plan_openssh_import([self.config], exclude_under=[self.confd])
        aliases = [h.entry.alias for h in plan.hosts]
        self.assertIn("personal", aliases)
        self.assertIn("box.example", aliases)
        self.assertNotIn("*", aliases)
        self.assertNotIn("web-01", aliases)  # Included from conf.d, which is imported on its own
        messages = " ".join(n.message for n in plan.notes)
        self.assertIn("pattern", messages)
        self.assertIn("Match", messages)
        self.assertEqual(aliases.count("personal"), 1)  # duplicate Host blocks merge (first value wins)
        personal = next(h for h in plan.hosts if h.entry.alias == "personal")
        self.assertEqual(personal.entry.options["hostname"], "box.example")  # quotes stripped, BOM handled
        self.assertEqual(personal.entry.options["identityfile"], "~/.ssh/my key")
        report = commit_openssh_import(plan, self.manager)
        group = self.manager.tree[OPENSSH_CONFIG_GROUP][""]
        self.assertEqual(report.renamed + report.skipped_existing + report.added, len(plan.hosts))
        self.assertTrue(any(h.alias == "personal" for h in group.hosts))

    def test_reimport_is_idempotent_and_policies(self) -> None:
        commit_openssh_import(plan_openssh_import([self.confd]), self.manager)
        report = commit_openssh_import(plan_openssh_import([self.confd]), self.manager)
        self.assertEqual((report.added, report.skipped_existing), (0, 3))
        (self.confd / "prod" / "web.conf").write_text("Host web-01\n    HostName 10.9.9.9\n", encoding="utf-8")
        changed = plan_openssh_import([self.confd])
        self.assertEqual(commit_openssh_import(changed, self.manager, policy=Policy.SKIP).added, 0)
        self.assertEqual(commit_openssh_import(changed, self.manager, policy=Policy.RENAME).renamed, 1)
        self.assertIn("web-01-2", [h.alias for h in self.manager.tree["prod"]["web.conf"].hosts])
        # web-01-2 now has that exact host/port/user: re-importing it is a no-op whatever the policy.
        self.assertEqual(commit_openssh_import(changed, self.manager, policy=Policy.OVERWRITE).skipped_existing, 2)  # + db-1
        (self.confd / "prod" / "web.conf").write_text("Host web-01\n    HostName 10.8.8.8\n", encoding="utf-8")
        report = commit_openssh_import(plan_openssh_import([self.confd]), self.manager, policy=Policy.OVERWRITE)
        self.assertEqual(report.overwritten, 1)
        self.assertTrue(any("before-import-overwrite" in p.name for p in self.store.list_backups()))
        web1 = next(h for h in self.manager.tree["prod"]["web.conf"].hosts if h.alias == "web-01")
        self.assertEqual(web1.options["hostname"], "10.8.8.8")

    def test_legacy_bindings_are_carried_and_unknown_ones_reported(self) -> None:
        legacy = {"prod::web.conf::web-01": self.cred.id, "prod::gone.conf::old": self.cred.id}
        report = commit_openssh_import(plan_openssh_import([self.confd]), self.manager, self.creds,
                                       legacy_bindings=legacy)
        web1 = next(h for h in self.manager.tree["prod"]["web.conf"].hosts if h.alias == "web-01")
        self.assertEqual(self.creds.binding_for_host(web1.id), self.cred.id)
        self.assertEqual(report.bindings_carried, 1)
        self.assertEqual(report.bindings_unmatched, ["prod::gone.conf::old"])

    def test_binding_failure_rolls_everything_back(self) -> None:
        legacy = {"prod::web.conf::web-01": self.cred.id}
        with mock.patch.object(self.creds, "_save", side_effect=SecureStoreError("disk")):
            with self.assertRaises(SecureStoreError):
                commit_openssh_import(plan_openssh_import([self.confd]), self.manager, self.creds, legacy_bindings=legacy)
        self.manager.refresh()
        self.assertEqual(self.manager.tree, {})

    def test_include_loop_unreadable_and_huge_files(self) -> None:
        loop = self.home / "loop"
        loop.write_text(f"Include {loop}\nHost l\n    HostName 10.0.0.7\n", encoding="utf-8")
        self.assertEqual([h.entry.alias for h in plan_openssh_import([loop]).hosts], ["l"])
        missing = plan_openssh_import([self.home / "nope"])
        self.assertEqual(missing.host_count, 0)
        self.assertTrue(missing.notes)
        with mock.patch("ssh_manager.inventory.openssh.MAX_SOURCE_BYTES", 10):
            huge = plan_openssh_import([self.config])
        self.assertEqual(huge.host_count, 0)
        self.assertIn("20 MB", huge.notes[0].message)

    def test_first_run_sources(self) -> None:
        found = dict(import_sources_found(self.home))
        self.assertEqual(found[self.confd], 3)
        self.assertEqual(found[self.config], 2)  # its Included conf.d hosts are not counted twice


class ExportTests(OpenSSHTestCase):
    def setUp(self) -> None:
        super().setUp()
        m = self.manager
        m.create_parent_group("Prod — EU")
        m.create_child_group("Prod — EU", "Team A")
        from ssh_manager.config import HostEntry

        m.add_host("Prod — EU", "Team A", HostEntry("Web 01 (primary)", {"hostname": "10.0.0.1", "user": "ops",
                                                                             "identityfile": "~/.ssh/my key"},
                                                       {"tags": "x"}))
        m.add_host("Prod — EU", "", HostEntry("web-01-primary", {"hostname": "10.0.0.2"}))
        m.add_host("Prod — EU", "", HostEntry("db.internal", {}))

    def test_export_is_valid_unique_and_round_trips(self) -> None:
        text = render_export(self.manager.tree)
        self.assertTrue(text.startswith("# Generated by Ophion"))
        self.assertIn("Host web-01-primary\n", text)
        self.assertIn("Host Web-01-primary-2\n", text)  # unique (case-insensitively)
        self.assertIn('identityfile "~/.ssh/my key"', text)
        self.assertNotIn("tags", text)
        target = self.home / ".config" / "ssh-manager" / "ssh_config"
        write_export(self.manager.tree, target)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        plan = plan_openssh_import([target])
        self.assertEqual(sorted(h.entry.options.get("hostname", h.entry.alias) for h in plan.hosts),
                         ["10.0.0.1", "10.0.0.2", "db.internal"])
        identity = next(h for h in plan.hosts if h.entry.options.get("hostname") == "10.0.0.1")
        self.assertEqual(identity.entry.options["identityfile"], "~/.ssh/my key")

    def test_never_over_ssh_config(self) -> None:
        with mock.patch.dict(os.environ, {"HOME": str(self.home)}):
            with self.assertRaises(ValueError):
                write_export(self.manager.tree, self.home / ".ssh" / "config")

    def test_include_line_is_idempotent_at_the_top_with_a_backup(self) -> None:
        target = self.home / ".config" / "ophion" / "ssh_config"
        with mock.patch.dict(os.environ, {"HOME": str(self.home)}):
            new, diff = include_line_change(self.config, target)
            self.assertTrue(new.startswith("Include ~/.config/ophion/ssh_config  # added by Ophion\n"))
            self.assertIn("+Include", diff)
            original = self.config.read_bytes()
            backup = apply_include_line(self.config, target)
            self.assertEqual(backup.read_bytes(), original)
            self.assertIsNone(apply_include_line(self.config, target))  # already there
            self.assertEqual(self.config.read_text().count("Include ~/.config/ophion/ssh_config"), 1)

    def test_the_old_include_line_is_replaced_once(self) -> None:
        old_line = "Include ~/.config/ssh-manager/ssh_config  # added by SSH Manager\n"
        self.config.write_text(old_line + "Host a\n    HostName 10.0.0.1\n")
        target = self.home / ".config" / "ophion" / "ssh_config"
        with mock.patch.dict(os.environ, {"HOME": str(self.home)}):
            apply_include_line(self.config, target)
            text = self.config.read_text()
            self.assertEqual(text, "Include ~/.config/ophion/ssh_config  # added by Ophion\nHost a\n    HostName 10.0.0.1\n")
            self.assertIsNone(apply_include_line(self.config, target))  # once
            self.assertNotIn("SSH Manager", self.config.read_text())


if __name__ == "__main__":
    unittest.main()
