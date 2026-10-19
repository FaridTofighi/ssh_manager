"""Part A: an empty conf.d never makes ~/.ssh/config the (file) base of the inventory."""

import hashlib
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ssh_manager.config import ConfigManager, HostEntry


class EmptyConfDTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.ssh = self.home / ".ssh"
        self.confd = self.ssh / "conf.d"
        self.confd.mkdir(parents=True)
        self.config = self.ssh / "config"
        self.config.write_text("Host personal\n    HostName 10.0.0.9\n    User alice\n", encoding="utf-8")
        self.config_hash = hashlib.sha256(self.config.read_bytes()).hexdigest()
        self.home_patch = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        self.home_patch.start()

    def tearDown(self) -> None:
        self.home_patch.stop()
        self.tmp.cleanup()

    def assertConfigUntouched(self) -> None:
        self.assertEqual(hashlib.sha256(self.config.read_bytes()).hexdigest(), self.config_hash)

    def test_create_group_child_and_host_in_an_empty_conf_d(self) -> None:
        manager = ConfigManager(self.confd)
        self.assertEqual(manager.base_dir, self.confd)
        self.assertEqual(manager.tree, {})  # the ~/.ssh/config hosts are not adopted
        self.assertFalse(manager.read_only)
        self.assertEqual(manager.create_parent_group("prod")[0], True)
        self.assertEqual(manager.create_child_group("prod", "web")[0], True)
        ok, message = manager.add_host("prod", "web", HostEntry("web-01", {"hostname": "10.0.0.1"}))
        self.assertTrue(ok, message)
        self.assertTrue(manager.base_dir.is_dir())
        self.assertEqual(manager.base_dir, self.confd)
        self.assertConfigUntouched()

    def test_deleting_everything_leaves_a_working_empty_inventory(self) -> None:
        manager = ConfigManager(self.confd)
        manager.create_parent_group("prod")
        manager.create_child_group("prod", "web")
        self.assertTrue(manager.delete_parent_group("prod")[0])
        self.assertEqual(manager.base_dir, self.confd)
        self.assertEqual(manager.tree, {})
        manager.refresh()
        self.assertEqual(manager.base_dir, self.confd)
        ok, message = manager.create_parent_group("again")
        self.assertTrue(ok, message)
        self.assertNotIn("is a file", message)
        self.assertConfigUntouched()

    def test_missing_folder_is_created_private(self) -> None:
        base = self.home / "fresh" / "conf.d"
        ConfigManager(base)
        self.assertTrue(base.is_dir())
        self.assertEqual(stat.S_IMODE(base.stat().st_mode) & 0o077, 0)

    def test_a_file_base_is_read_only_and_never_raises(self) -> None:
        manager = ConfigManager(self.config)
        self.assertTrue(manager.read_only)
        self.assertEqual(manager.base_dir, self.config)
        ok, message = manager.create_parent_group("prod")
        self.assertFalse(ok)
        self.assertConfigUntouched()


def _display_available() -> bool:
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return False
    try:
        import gi

        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk  # type: ignore

        return Gtk.init_check(None)[0]
    except Exception:
        return False


@unittest.skipUnless(_display_available(), "needs a display")
class FileWorkspaceWindowTests(unittest.TestCase):
    def setUp(self) -> None:
        from tests.gtk_window_fixture import WindowSandbox

        self.sandbox = WindowSandbox()
        self.win = self.sandbox.window
        self.home = Path(os.environ["HOME"])
        self.config = self.home / ".ssh" / "config"
        self.config.parent.mkdir(parents=True, exist_ok=True)
        self.config.write_text("Host personal\n    HostName 10.0.0.9\n", encoding="utf-8")
        self.before = self.config.read_bytes()
        self.win.inventory.load_workspace(self.config)

    def tearDown(self) -> None:
        self.sandbox.close()

    def test_use_folder_switches_and_the_action_runs(self) -> None:
        self.assertTrue(self.win.manager.read_only)
        with mock.patch("ssh_manager.gtk_app.confirm", return_value=True) as asked, \
                mock.patch("ssh_manager.gtkui.controllers.group_actions.prompt_text", return_value="prod", create=True), \
                mock.patch.object(self.win.groups, "create_parent_group", wraps=self.win.groups.create_parent_group):
            self.assertTrue(self.win.ensure_editable_inventory())
        asked.assert_called_once()
        self.assertEqual(self.win.manager.base_dir, self.home / ".ssh" / "conf.d")
        self.assertFalse(self.win.manager.read_only)
        self.assertTrue(self.win.manager.create_parent_group("prod")[0])
        self.assertEqual(self.config.read_bytes(), self.before)

    def test_cancel_makes_the_action_a_no_op(self) -> None:
        with mock.patch("ssh_manager.gtk_app.confirm", return_value=False), \
                mock.patch.object(self.win.manager, "create_parent_group") as create, \
                mock.patch.object(self.win, "_notify_error") as error:
            self.win.groups.create_parent_group()
        create.assert_not_called()
        error.assert_not_called()  # no failing toast
        self.assertEqual(self.win.manager.base_dir, self.config)
        self.assertEqual(self.config.read_bytes(), self.before)


if __name__ == "__main__":
    unittest.main()
