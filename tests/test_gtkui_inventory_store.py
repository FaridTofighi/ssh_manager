"""The main window on the app-owned inventory (skipped without a display), plus start-up decisions."""

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ssh_manager.inventory.startup import Choice, open_inventory_store
from ssh_manager.inventory.store import SCHEMA_VERSION, InventoryStore

UNICODE = "Prod DB (10.0.0.5) — مرکز"


class StartupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "inventory.db"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_fresh_and_existing(self) -> None:
        store = open_inventory_store(self.path, lambda kind, message: self.fail(kind))
        self.assertTrue(store.created)
        store.close()
        store = open_inventory_store(self.path, lambda kind, message: self.fail(kind))
        self.assertFalse(store.created)
        store.close()

    def test_locked_retry_then_read_only_or_quit(self) -> None:
        InventoryStore(self.path).close()
        answers = iter([Choice.RETRY, Choice.QUIT])
        asked = []
        with mock.patch("ssh_manager.inventory.startup.InventoryStore",
                        side_effect=[__import__("ssh_manager.inventory.store", fromlist=["x"]).InventoryLocked("busy")] * 2):
            result = open_inventory_store(self.path, lambda kind, message: asked.append(kind) or next(answers))
        self.assertIsNone(result)
        self.assertEqual(asked, ["locked", "locked"])

    def test_corrupt_restore_and_start_empty(self) -> None:
        store = InventoryStore(self.path)
        store.create_group(None, "keep")
        store.backup("before")
        store.close()
        self.path.write_bytes(b"broken" * 500)
        restored = open_inventory_store(self.path, lambda kind, message: Choice.RESTORE)
        self.assertEqual([g.name for g in restored.list_groups()], ["keep"])
        restored.close()
        self.path.write_bytes(b"broken" * 500)
        empty = open_inventory_store(self.path, lambda kind, message: Choice.START_EMPTY)
        self.assertEqual(empty.list_groups(), [])
        empty.close()
        self.assertTrue(any(p.name.startswith("inventory.db.damaged-") for p in self.path.parent.iterdir()))

    def test_too_new_opens_empty_read_only_and_never_touches_the_file(self) -> None:
        conn = sqlite3.connect(self.path)
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION + 5}")
        conn.commit()
        conn.close()
        before = self.path.read_bytes()
        store = open_inventory_store(self.path, lambda kind, message: Choice.READ_ONLY)
        self.assertTrue(store.read_only)
        self.assertIn("newer", store.read_only_reason)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertIsNone(open_inventory_store(self.path, lambda kind, message: Choice.QUIT))

    def test_unwritable_location_runs_read_only(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("root ignores permissions")
        locked = Path(self.tmp.name) / "ro"
        locked.mkdir(mode=0o500)
        try:
            store = open_inventory_store(locked / "x" / "inventory.db", lambda kind, message: Choice.READ_ONLY)
        finally:
            locked.chmod(0o700)
        self.assertTrue(store.read_only)
        self.assertTrue(store.read_only_reason)


def _display_available() -> bool:
    from tests.gtk_window_fixture import display_available

    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")) and display_available()


@unittest.skipUnless(_display_available(), "needs a display")
class StoreWindowTests(unittest.TestCase):
    def setUp(self) -> None:
        from tests.gtk_window_fixture import WindowSandbox

        self.sandbox = WindowSandbox(store=True)
        self.win = self.sandbox.window
        self.home = Path(os.environ["HOME"])

    def tearDown(self) -> None:
        self.sandbox.close()

    def rows(self):
        found = []
        self.win.tree_store.foreach(lambda model, path, it: found.append(
            (model.get_value(it, 2), model.get_value(it, 1))) or False)
        return found

    def test_sample_is_in_the_store_with_folder_icons(self) -> None:
        from ssh_manager.gtkui.constants import TreeColumn

        self.assertIn("web.conf", self.win.manager.tree["prod"])
        self.assertIn(("host", "web-01"), self.rows())
        file_backed = []
        self.win.tree_store.foreach(lambda m, p, it: file_backed.append(m.get_value(it, TreeColumn.FILE_BACKED))
                                    if m.get_value(it, 2) == "child" else False)
        self.assertTrue(file_backed)
        self.assertFalse(any(file_backed))  # app-store groups are folders

    def test_unicode_groups_and_hosts_through_the_ui(self) -> None:
        from ssh_manager.config import HostEntry

        with mock.patch.object(self.win, "_prompt_text", return_value=UNICODE):
            self.win.groups.create_parent_group()
        self.assertIn(UNICODE, self.win.manager.tree)
        self.win.tree.select_parent_group(UNICODE)
        with mock.patch.object(self.win, "_prompt_text", return_value="Team A — EU"):
            self.win.groups.create_child_group_for_current()
        self.assertIn("Team A — EU", self.win.manager.tree[UNICODE])
        ok, message = self.win.manager.add_host(UNICODE, "Team A — EU",
                                                HostEntry("Web 01 (primary)", {"hostname": "10.0.0.5"}))
        self.assertTrue(ok, message)
        self.win.tree.rebuild(refresh=False)
        self.assertIn(("host", "Web 01 (primary)"), self.rows())
        self.assertIn(("child", "Team A — EU"), self.rows())
        # Search finds it; the host page breadcrumb and title show the names as typed.
        self.win._sidebar.search_entry.set_text("primary")
        self.win.tree.rebuild(refresh=False)
        self.assertIn(("host", "Web 01 (primary)"), self.rows())
        self.assertNotIn(("host", "web-01"), self.rows())
        self.win._sidebar.search_entry.set_text("")
        self.win.tree.rebuild(refresh=False)
        self.win.tree.select_host(UNICODE, "Team A — EU", "Web 01 (primary)")
        content = self.win.host_page.build_content()
        self.assertEqual(content.title, "Web 01 (primary)")
        self.win.tree.select_child_group(UNICODE, "Team A — EU")
        crumbs = [crumb.label for crumb in self.win.host_page.build_content().crumbs]
        self.assertEqual(crumbs[1:], [UNICODE, "Team A — EU"])
        # Rename keeps working with spaces and Unicode.
        self.win.tree.select_child_group(UNICODE, "Team A — EU")
        with mock.patch.object(self.win, "_prompt_text", return_value="Team B — مرکز"):
            self.win.groups.rename_child_group()
        self.assertIn("Team B — مرکز", self.win.manager.tree[UNICODE])

    def test_store_validators_in_dialogs(self) -> None:
        dialog = self.win.hosts.host_dialog(None)
        try:
            self.assertIsNone(dialog._alias_validator("Web 01 (primary)"))
            self.assertIsNotNone(dialog._alias_validator("a/b"))
        finally:
            dialog.destroy()

    def test_bindings_follow_hosts_through_ui_moves(self) -> None:
        cred = self.win._credential_store.add("Ops", "ops", "pw")
        host = next(h for h in self.win.manager.tree["prod"]["web.conf"].hosts if h.alias == "web-01")
        self.win.creds.bind_credential(self.win.creds.credential_key(host), cred.id)
        ok, _message = self.win.manager.move_hosts([("prod", "web.conf", "web-01")], "staging", "hosts.conf")
        self.assertTrue(ok)
        moved = next(h for h in self.win.manager.tree["staging"]["hosts.conf"].hosts if h.alias == "web-01")
        self.assertEqual(self.win.creds.credential_for_host(moved).id, cred.id)
        self.win.tree.select_child_group("staging", "hosts.conf")
        with mock.patch.object(self.win, "_confirm", return_value=True):
            self.win.groups.delete_child_group()
        self.win.tree.rebuild(refresh=False)
        self.assertIsNone(self.win._credential_store.binding_for_host(moved.id))  # pruned
        self.assertIsNotNone(self.win._credential_store.get(cred.id))  # the credential stays

    def test_read_only_store_explains_instead_of_failing(self) -> None:
        self.win.manager.store.read_only = True
        self.win.manager.store.read_only_reason = "The disk is full."
        with mock.patch("ssh_manager.gtk_app.show_message") as shown, \
                mock.patch.object(self.win.manager, "create_parent_group") as create:
            self.win.groups.create_parent_group()
        create.assert_not_called()
        self.assertIn("The disk is full.", shown.call_args[0])

    def test_external_changes_reload_the_tree(self) -> None:
        other = InventoryStore(self.win.manager.base_dir)
        other.create_group(None, "from another window")
        other.close()
        self.win._check_external_changes()
        self.assertIn(("parent", "from another window"), self.rows())

    def test_first_run_offer_imports_once_with_bindings(self) -> None:
        confd = self.home / ".ssh" / "conf.d" / "legacy"
        confd.mkdir(parents=True)
        (confd / "servers.conf").write_text("Host old-1\n    HostName 10.1.1.1\n", encoding="utf-8")
        (self.home / ".ssh" / "config").write_text("Host personal\n    HostName 10.2.2.2\n", encoding="utf-8")
        cred = self.win._credential_store.add("Ops", "ops", "pw")
        self.win._credential_store.bind_host("legacy::servers.conf::old-1", cred.id)
        asked = []
        self.assertTrue(self.win.store_actions.offer_first_run_import(
            ask=lambda title, text: asked.append(text) or True, home=self.home))
        self.assertIn("conf.d (1 host)", asked[0])
        old = self.win.manager.tree["legacy"]["servers.conf"].hosts[0]
        self.assertEqual(self.win._credential_store.binding_for_host(old.id), cred.id)
        self.assertIn("OpenSSH config", self.win.manager.tree)
        self.assertFalse(self.win.store_actions.offer_first_run_import(
            ask=lambda *a: self.fail("asked twice"), home=self.home))

    def test_first_run_no_changes_nothing_and_never_asks_again(self) -> None:
        (self.home / ".ssh").mkdir(parents=True, exist_ok=True)
        (self.home / ".ssh" / "config").write_text("Host personal\n    HostName 10.2.2.2\n", encoding="utf-8")
        before = dict(self.win.manager.tree)
        self.assertFalse(self.win.store_actions.offer_first_run_import(ask=lambda *a: False, home=self.home))
        self.assertEqual(list(self.win.manager.tree), list(before))
        self.assertFalse(self.win.store_actions.offer_first_run_import(ask=lambda *a: self.fail("again"), home=self.home))

    def test_openssh_import_dialog_and_export_with_include(self) -> None:
        from gi.repository import Gtk  # type: ignore

        source = self.home / "extra.conf"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("Host extra\n    HostName 10.3.3.3\nHost *\n    User x\n", encoding="utf-8")
        self.win.store_actions.import_openssh(initial=source, run_dialog=lambda dialog: (
            self.assertIn("1 host", dialog.summary.get_text()) or Gtk.ResponseType.OK))
        self.assertIn("extra.conf", self.win.manager.tree)
        target = self.home / ".config" / "ssh-manager" / "ssh_config"
        (self.home / ".ssh").mkdir(parents=True, exist_ok=True)
        config = self.home / ".ssh" / "config"
        config.write_text("Host personal\n    HostName 10.2.2.2\n", encoding="utf-8")
        with mock.patch.object(Path, "home", return_value=self.home):
            path = self.win.store_actions.export_openssh(target=target, ask=lambda title, text: "+Include" in text)
        self.assertEqual(path, target)
        self.assertIn("Host extra", target.read_text())
        self.assertTrue(config.read_text().startswith("Include ~/.config/ssh-manager/ssh_config"))
        self.assertTrue(any(p.name.startswith("config.sshm-backup-") for p in config.parent.iterdir()))

    def test_preferences_inventory_page(self) -> None:
        from ssh_manager.gtkui.dialogs.preferences import PreferencesDialog

        info = self.win.app_dialogs.inventory_info()
        self.assertEqual(info["location"], str(self.win.manager.base_dir))
        dialog = PreferencesDialog(self.win, self.win._theme_mode, 1.0, 11, self.win._settings, inventory=info)
        try:
            self.assertEqual(set(dialog.inventory_buttons), {"backup", "reveal", "export"})
            dialog.inventory_buttons["backup"].clicked()
        finally:
            dialog.destroy()
        self.assertTrue(self.win.manager.store.list_backups())


if __name__ == "__main__":
    unittest.main()
