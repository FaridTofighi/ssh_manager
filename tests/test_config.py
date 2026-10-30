import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ssh_manager.config import ConfigManager, HostEntry, load_config_tree, parse_ssh_config_file


class ParseIncludeTests(unittest.TestCase):
    def test_parser_accepts_equals_style_directives_and_ignores_comments(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = Path(tmpdir) / "config"
            cfg.write_text(
                "Host=app\n"
                "  # ordinary comments should not become options\n"
                "  HostName = app.internal\n"
                "  Port=2222\n",
                encoding="utf-8",
            )

            entries = parse_ssh_config_file(cfg, "parent", "config")

            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0].alias, "app")
            self.assertEqual(entries[0].options, {"hostname": "app.internal", "port": "2222"})

    def test_include_expands_home_and_env_vars(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir)
            cfg = home / "config"
            include_dir = home / ".ssh" / "conf.d"
            include_dir.mkdir(parents=True)
            include_file = include_dir / "child.conf"
            include_file.write_text("Host api\n  HostName api.example.com\n", encoding="utf-8")

            # Include using both $HOME and ~ should resolve to the same file once.
            cfg.write_text("Include $HOME/.ssh/conf.d/*.conf\nInclude ~/.ssh/conf.d/*.conf\n", encoding="utf-8")

            with mock.patch.dict(os.environ, {"HOME": str(home)}):
                entries = parse_ssh_config_file(cfg, "parent", "config")

            self.assertEqual(len(entries), 1)
            entry = entries[0]
            self.assertIsInstance(entry, HostEntry)
        self.assertEqual(entry.alias, "api")
        self.assertEqual(entry.options["hostname"], "api.example.com")


class ConfigMutationTests(unittest.TestCase):
    def _manager_for_temp_dir(self, tmpdir: str) -> ConfigManager:
        base = Path(tmpdir)
        (base / "bootstrap.conf").write_text(
            "Host bootstrap\n  HostName bootstrap.local\n",
            encoding="utf-8",
        )
        return ConfigManager(base)

    def test_permissions_metadata_round_trips_after_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = self._manager_for_temp_dir(tmpdir)
            ok, _ = manager.create_parent_group("prod")
            self.assertTrue(ok)
            ok, _ = manager.create_child_group("prod", "web.conf")
            self.assertTrue(ok)

            host = HostEntry(
                alias="app",
                options={"hostname": "app.prod"},
                metadata={
                    "owner": "platform",
                    "permissions": {
                        "can_view": True,
                        "can_connect": False,
                        "can_edit": False,
                    },
                },
            )
            ok, _ = manager.add_host("prod", "web.conf", host)
            self.assertTrue(ok)

            reloaded = ConfigManager(Path(tmpdir))
            loaded = reloaded.tree["prod"]["web.conf"].hosts[0]
            self.assertEqual(loaded.metadata["owner"], "platform")
            self.assertEqual(
                loaded.metadata["permissions"],
                {"can_view": True, "can_connect": False, "can_edit": False},
            )

    def test_nested_child_move_and_host_move_create_parent_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = self._manager_for_temp_dir(tmpdir)
            self.assertTrue(manager.create_parent_group("source")[0])
            self.assertTrue(manager.create_parent_group("dest")[0])
            self.assertTrue(manager.create_child_group("source", "nested/app.conf")[0])
            self.assertTrue(
                manager.add_host(
                    "source",
                    "nested/app.conf",
                    HostEntry(alias="app", options={"hostname": "app.internal"}),
                )[0]
            )

            ok, _ = manager.move_child_group("source", "nested/app.conf", "dest")
            self.assertTrue(ok)
            self.assertIn("nested/app.conf", manager.tree["dest"])
            self.assertFalse((Path(tmpdir) / "source" / "nested").exists())

            self.assertTrue(manager.create_child_group("source", "hosts.conf")[0])
            self.assertTrue(
                manager.add_host(
                    "source",
                    "hosts.conf",
                    HostEntry(alias="db", options={"hostname": "db.internal"}),
                )[0]
            )
            ok, _ = manager.move_hosts(
                [("source", "hosts.conf", "db")],
                "dest",
                "ops/db.conf",
                create_child=True,
            )
            self.assertTrue(ok)
            self.assertIn("ops/db.conf", manager.tree["dest"])
            self.assertEqual(manager.tree["dest"]["ops/db.conf"].hosts[0].alias, "db")

    def test_nested_child_delete_and_rename_remove_empty_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = self._manager_for_temp_dir(tmpdir)
            self.assertTrue(manager.create_parent_group("prod")[0])
            self.assertTrue(manager.create_child_group("prod", "a/b/c.conf")[0])

            ok, _ = manager.rename_child_group("prod", "a/b/c.conf", "renamed.conf")
            self.assertTrue(ok)
            self.assertFalse((Path(tmpdir) / "prod" / "a").exists())

            self.assertTrue(manager.create_child_group("prod", "x/y/z.conf")[0])
            ok, _ = manager.delete_child_group("prod", "x/y/z.conf")
            self.assertTrue(ok)
            self.assertFalse((Path(tmpdir) / "prod" / "x").exists())

    def test_move_hosts_rejects_directory_destination(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = self._manager_for_temp_dir(tmpdir)
            self.assertTrue(manager.create_parent_group("source")[0])
            self.assertTrue(manager.create_parent_group("dest")[0])
            self.assertTrue(manager.create_child_group("source", "hosts.conf")[0])
            self.assertTrue(
                manager.add_host(
                    "source",
                    "hosts.conf",
                    HostEntry(alias="app", options={"hostname": "app.internal"}),
                )[0]
            )
            (Path(tmpdir) / "dest" / "folder.conf").mkdir()

            ok, message = manager.move_hosts(
                [("source", "hosts.conf", "app")],
                "dest",
                "folder.conf",
            )
            self.assertFalse(ok)
            self.assertIn("not a file", message)

    def test_group_operations_reject_source_traversal_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = self._manager_for_temp_dir(tmpdir)
            self.assertTrue(manager.create_parent_group("prod")[0])
            self.assertTrue(manager.create_child_group("prod", "hosts.conf")[0])

            ok, message = manager.rename_parent_group("../prod", "new")
            self.assertFalse(ok)
            self.assertIn("Parent group", message)

            ok, message = manager.rename_child_group("prod", "../hosts.conf", "new.conf")
            self.assertFalse(ok)
            self.assertIn("Segment", message)

            ok, message = manager.move_hosts(
                [("../prod", "hosts.conf", "app")],
                "prod",
                "hosts.conf",
            )
            self.assertFalse(ok)
            self.assertIn("Parent group", message)

    def test_parent_mutations_report_filesystem_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = self._manager_for_temp_dir(tmpdir)

            with mock.patch("pathlib.Path.mkdir", side_effect=OSError("readonly")):
                ok, message = manager.create_parent_group("prod")
            self.assertFalse(ok)
            self.assertIn("Failed to create parent group", message)

            self.assertTrue(manager.create_parent_group("prod")[0])
            with mock.patch("pathlib.Path.rename", side_effect=OSError("busy")):
                ok, message = manager.rename_parent_group("prod", "ops")
            self.assertFalse(ok)
            self.assertIn("Failed to rename parent", message)

    def test_child_paths_reject_traversal_segments(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = self._manager_for_temp_dir(tmpdir)
            self.assertTrue(manager.create_parent_group("prod")[0])
            ok, message = manager.create_child_group("prod", "../bad.conf")
            self.assertFalse(ok)
            self.assertIn("Segment", message)

            ok, message = manager.create_child_group("prod", "nested\\bad.conf")
            self.assertFalse(ok)
            self.assertIn("path separators", message)

    def test_create_child_group_reports_write_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = self._manager_for_temp_dir(tmpdir)
            self.assertTrue(manager.create_parent_group("prod")[0])

            with mock.patch("ssh_manager.config.write_text_atomic", side_effect=OSError("disk full")):
                ok, message = manager.create_child_group("prod", "hosts.conf")

            self.assertFalse(ok)
            self.assertIn("Failed to create child group", message)

    def test_child_rename_rejects_directory_source_and_reports_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = self._manager_for_temp_dir(tmpdir)
            self.assertTrue(manager.create_parent_group("prod")[0])
            (Path(tmpdir) / "prod" / "folder.conf").mkdir()

            ok, message = manager.rename_child_group("prod", "folder.conf", "renamed.conf")
            self.assertFalse(ok)
            self.assertIn("not a file", message)

            self.assertTrue(manager.create_child_group("prod", "hosts.conf")[0])
            with mock.patch("pathlib.Path.rename", side_effect=OSError("busy")):
                ok, message = manager.rename_child_group("prod", "hosts.conf", "renamed.conf")
            self.assertFalse(ok)
            self.assertIn("Failed to rename child group", message)

    def test_duplicate_child_group_preserves_mode_and_reports_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = self._manager_for_temp_dir(tmpdir)
            self.assertTrue(manager.create_parent_group("prod")[0])
            self.assertTrue(manager.create_child_group("prod", "hosts.conf")[0])
            source = Path(tmpdir) / "prod" / "hosts.conf"
            os.chmod(source, 0o640)

            ok, _ = manager.duplicate_child_group("prod", "hosts.conf", "copy.conf")
            self.assertTrue(ok)
            copied_mode = stat.S_IMODE(os.stat(Path(tmpdir) / "prod" / "copy.conf").st_mode)
            self.assertEqual(copied_mode, 0o640)

            with mock.patch("ssh_manager.config.shutil.copy2", side_effect=OSError("readonly")):
                ok, message = manager.duplicate_child_group("prod", "hosts.conf", "other.conf")
            self.assertFalse(ok)
            self.assertIn("Failed to duplicate", message)

    def test_group_mutations_reject_single_file_base_without_crashing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = Path(tmpdir) / "config"
            cfg.write_text(
                "Host app\n  HostName app.internal\n",
                encoding="utf-8",
            )
            manager = ConfigManager(cfg)

            ok, message = manager.create_parent_group("prod")
            self.assertFalse(ok)
            self.assertIn("is a single file", message)

            parent = cfg.parent.name
            child = cfg.name
            updated = HostEntry(
                alias="app",
                options={"hostname": "app.changed"},
                parent_group=parent,
                child_group=child,
            )
            ok, _ = manager.update_host(parent, child, "app", updated)
            self.assertTrue(ok)
            self.assertIn("app.changed", cfg.read_text(encoding="utf-8"))

    def test_host_update_preserves_existing_config_file_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = Path(tmpdir) / "hosts.conf"
            cfg.write_text(
                "Host app\n  HostName app.internal\n",
                encoding="utf-8",
            )
            os.chmod(cfg, 0o644)
            manager = ConfigManager(Path(tmpdir))

            updated = HostEntry(alias="app", options={"hostname": "changed.internal"})
            ok, _ = manager.update_host(Path(tmpdir).name, "hosts.conf", "app", updated)

            self.assertTrue(ok)
            self.assertEqual(stat.S_IMODE(os.stat(cfg).st_mode), 0o644)

    def test_host_mutations_reject_line_injection(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = self._manager_for_temp_dir(tmpdir)
            self.assertTrue(manager.create_parent_group("prod")[0])
            self.assertTrue(manager.create_child_group("prod", "hosts.conf")[0])

            ok, message = manager.add_host(
                "prod",
                "hosts.conf",
                HostEntry(alias="app\nHost injected", options={"hostname": "app.internal"}),
            )
            self.assertFalse(ok)
            self.assertIn("Host alias", message)

            ok, message = manager.add_host(
                "prod",
                "hosts.conf",
                HostEntry(alias="app", options={"hostname": "app.internal\nProxyCommand bad"}),
            )
            self.assertFalse(ok)
            self.assertIn("line breaks", message)

            ok, message = manager.add_host(
                "prod",
                "hosts.conf",
                HostEntry(alias="app", metadata={"owner": "team\n# meta other=bad"}),
            )
            self.assertFalse(ok)
            self.assertIn("line breaks", message)

    def test_host_mutations_allow_openssh_patterns_without_whitespace(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = self._manager_for_temp_dir(tmpdir)
            self.assertTrue(manager.create_parent_group("prod")[0])
            self.assertTrue(manager.create_child_group("prod", "patterns.conf")[0])

            ok, message = manager.add_host(
                "prod",
                "patterns.conf",
                HostEntry(alias="*.example.com", options={"user": "deploy"}),
            )

            self.assertTrue(ok, message)
            self.assertIn("*.example.com", (Path(tmpdir) / "prod" / "patterns.conf").read_text(encoding="utf-8"))

    def test_host_mutations_report_write_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = self._manager_for_temp_dir(tmpdir)
            self.assertTrue(manager.create_parent_group("prod")[0])
            self.assertTrue(manager.create_child_group("prod", "hosts.conf")[0])

            with mock.patch("ssh_manager.config._write_group_file", side_effect=OSError("disk full")):
                ok, message = manager.add_host(
                    "prod",
                    "hosts.conf",
                    HostEntry(alias="app", options={"hostname": "app.internal"}),
                )

            self.assertFalse(ok)
            self.assertIn("Failed to write", message)

    def test_move_hosts_reports_write_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = self._manager_for_temp_dir(tmpdir)
            self.assertTrue(manager.create_parent_group("source")[0])
            self.assertTrue(manager.create_parent_group("dest")[0])
            self.assertTrue(manager.create_child_group("source", "hosts.conf")[0])
            self.assertTrue(manager.create_child_group("dest", "hosts.conf")[0])
            self.assertTrue(
                manager.add_host(
                    "source",
                    "hosts.conf",
                    HostEntry(alias="app", options={"hostname": "app.internal"}),
                )[0]
            )

            with mock.patch("ssh_manager.config._write_group_file", side_effect=OSError("readonly")):
                ok, message = manager.move_hosts(
                    [("source", "hosts.conf", "app")],
                    "dest",
                    "hosts.conf",
                )

            self.assertFalse(ok)
            self.assertIn("Failed to write moved hosts", message)

    def test_move_hosts_create_child_reports_write_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = self._manager_for_temp_dir(tmpdir)
            self.assertTrue(manager.create_parent_group("source")[0])
            self.assertTrue(manager.create_parent_group("dest")[0])
            self.assertTrue(manager.create_child_group("source", "hosts.conf")[0])
            self.assertTrue(
                manager.add_host(
                    "source",
                    "hosts.conf",
                    HostEntry(alias="app", options={"hostname": "app.internal"}),
                )[0]
            )

            with mock.patch("ssh_manager.config.write_text_atomic", side_effect=OSError("readonly")):
                ok, message = manager.move_hosts(
                    [("source", "hosts.conf", "app")],
                    "dest",
                    "new/hosts.conf",
                    create_child=True,
                )

            self.assertFalse(ok)
            self.assertIn("Failed to create child group", message)

    def test_missing_conf_d_directory_is_created_despite_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir) / ".ssh" / "conf.d"
            manager = ConfigManager(base)

            self.assertTrue(base.is_dir())
            ok, _ = manager.create_parent_group("prod")
            self.assertTrue(ok)
            self.assertTrue((base / "prod").is_dir())


class LoadConfigTreeTests(unittest.TestCase):
    def test_conf_d_tree_maps_all_files_and_folders(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir) / ".ssh" / "conf.d"
            base.mkdir(parents=True)

            (base / "root.conf").write_text(
                "Host root\n  HostName root.example\n",
                encoding="utf-8",
            )

            prod_dir = base / "prod"
            prod_dir.mkdir()
            (prod_dir / "web.conf").write_text(
                "Host web\n  HostName web.prod\n",
                encoding="utf-8",
            )

            nested_dir = prod_dir / "db"
            nested_dir.mkdir()
            (nested_dir / "db.conf").write_text(
                "Host db\n  HostName db.prod\n",
                encoding="utf-8",
            )

            tree = load_config_tree(base)

            root_children = tree[base.name]
            self.assertIn("root.conf", root_children)
            self.assertEqual(
                {h.alias for h in root_children["root.conf"].hosts},
                {"root"},
            )

            self.assertIn("prod", tree)
            prod_children = tree["prod"]
            self.assertIn("web.conf", prod_children)
            self.assertIn("db/db.conf", prod_children)
            self.assertEqual(
                {h.alias for h in prod_children["web.conf"].hosts},
                {"web"},
            )
            self.assertEqual(
                {h.alias for h in prod_children["db/db.conf"].hosts},
                {"db"},
            )

            self.assertEqual(prod_children["web.conf"].hosts[0].source_file, prod_dir / "web.conf")
            self.assertEqual(prod_children["db/db.conf"].hosts[0].source_file, nested_dir / "db.conf")

    def test_root_configs_are_not_overwritten_by_same_named_parent_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir) / "inventory"
            base.mkdir()
            (base / "root.conf").write_text(
                "Host root\n  HostName root.internal\n",
                encoding="utf-8",
            )
            same_named_dir = base / "inventory"
            same_named_dir.mkdir()
            (same_named_dir / "nested.conf").write_text(
                "Host nested\n  HostName nested.internal\n",
                encoding="utf-8",
            )

            tree = load_config_tree(base)

            self.assertIn("root.conf", tree["inventory"])
            self.assertIn("nested.conf", tree["inventory"])
            self.assertEqual(tree["inventory"]["root.conf"].hosts[0].alias, "root")
            self.assertEqual(tree["inventory"]["nested.conf"].hosts[0].alias, "nested")

    def test_directory_inventory_does_not_inline_includes_into_each_child_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir) / ".ssh" / "conf.d"
            prod_dir = base / "prod"
            shared_dir = prod_dir / "shared"
            shared_dir.mkdir(parents=True)
            (shared_dir / "common.conf").write_text(
                "Host common\n  HostName common.internal\n",
                encoding="utf-8",
            )
            (prod_dir / "a.conf").write_text(
                "Include shared/common.conf\nHost a\n  HostName a.internal\n",
                encoding="utf-8",
            )
            (prod_dir / "b.conf").write_text(
                "Include shared/common.conf\nHost b\n  HostName b.internal\n",
                encoding="utf-8",
            )

            tree = load_config_tree(base)
            prod_children = tree["prod"]
            self.assertEqual(
                {h.alias for h in prod_children["a.conf"].hosts},
                {"a"},
            )
            self.assertEqual(
                {h.alias for h in prod_children["b.conf"].hosts},
                {"b"},
            )

            entries = parse_ssh_config_file(
                prod_dir / "a.conf",
                "prod",
                "a.conf",
                follow_includes=True,
            )
            self.assertEqual({entry.alias for entry in entries}, {"common", "a"})

    def test_loader_ignores_hidden_backup_and_temp_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir) / ".ssh" / "conf.d"
            prod_dir = base / "prod"
            prod_dir.mkdir(parents=True)
            hidden_parent = base / ".hidden-parent"
            hidden_parent.mkdir()
            (base / ".ui_state.json").write_text("{}", encoding="utf-8")
            (base / "config.bak").write_text(
                "Host backup\n  HostName backup.internal\n",
                encoding="utf-8",
            )
            (hidden_parent / "hosts.conf").write_text(
                "Host hiddenparent\n  HostName hiddenparent.internal\n",
                encoding="utf-8",
            )
            (prod_dir / ".hidden.conf").write_text(
                "Host hidden\n  HostName hidden.internal\n",
                encoding="utf-8",
            )
            (prod_dir / "servers").write_text(
                "Host app\n  HostName app.internal\n",
                encoding="utf-8",
            )

            tree = load_config_tree(base)
            self.assertNotIn(".ui_state.json", tree.get(base.name, {}))
            self.assertNotIn("config.bak", tree.get(base.name, {}))
            self.assertNotIn(".hidden-parent", tree)
            self.assertIn("prod", tree)
            self.assertEqual(set(tree["prod"].keys()), {"servers"})

    def test_parser_skips_unreadable_config_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = Path(tmpdir) / "hosts.conf"
            cfg.write_text("Host app\n  HostName app.internal\n", encoding="utf-8")
            original_read_text = Path.read_text

            def guarded_read_text(path: Path, *args, **kwargs):
                if path == cfg:
                    raise OSError("permission denied")
                return original_read_text(path, *args, **kwargs)

            with mock.patch("pathlib.Path.read_text", guarded_read_text):
                entries = parse_ssh_config_file(cfg, "prod", "hosts.conf")

            self.assertEqual(entries, [])

    def test_loader_skips_unscannable_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir) / ".ssh" / "conf.d"
            prod_dir = base / "prod"
            prod_dir.mkdir(parents=True)
            (base / "root.conf").write_text(
                "Host root\n  HostName root.internal\n",
                encoding="utf-8",
            )
            original_iterdir = Path.iterdir

            def guarded_iterdir(path: Path):
                if path == prod_dir:
                    raise OSError("permission denied")
                return original_iterdir(path)

            with mock.patch("pathlib.Path.iterdir", guarded_iterdir):
                tree = load_config_tree(base)

            self.assertEqual(tree[base.name]["root.conf"].hosts[0].alias, "root")
            self.assertEqual(tree["prod"], {})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
