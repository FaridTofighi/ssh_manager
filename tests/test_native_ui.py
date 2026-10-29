"""SSH Manager export / import in the window: the export dialog, menus, the written file, the import wizard."""

import json
import os
import stat
import time
import unittest
from unittest import mock

from tests.gtk_window_fixture import display_available


def pump(seconds=0.0, until=None):
    from gi.repository import Gtk  # type: ignore

    deadline = time.monotonic() + seconds
    while True:
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        if until is not None and until():
            return True
        if time.monotonic() >= deadline:
            return until() if until is not None else True
        time.sleep(0.01)


@unittest.skipUnless(display_available(), "needs a display")
class ExportDialogTests(unittest.TestCase):
    def test_scope_options_and_passphrase(self) -> None:
        from gi.repository import Gtk  # type: ignore

        from ssh_manager.gtkui.dialogs.native_export import NativeExportDialog, passphrase_strength

        dialog = NativeExportDialog(None, group_label="prod", host_count=0, scope="groups")
        self.addCleanup(dialog.destroy)
        self.assertTrue(dialog.group_radio.get_active())
        self.assertFalse(dialog.hosts_radio.get_sensitive())
        self.assertFalse(dialog.smart_check.get_sensitive())
        self.assertTrue(dialog.notice.get_visible())
        scope, options, passphrase = dialog.values()
        self.assertEqual((scope, options.colors, options.notes, options.credential_refs, options.smart_groups,
                          passphrase), ("groups", True, True, True, False, None))
        closed = []
        dialog.connect_after("response", lambda _d, r: closed.append(r))
        dialog.protect_check.set_active(True)
        self.assertFalse(dialog.notice.get_visible())
        dialog.pass1.set_text("correct horse battery")
        dialog.pass2.set_text("correct horse batterx")
        dialog.response(Gtk.ResponseType.OK)
        self.assertEqual(closed, [])  # mismatch: stays open
        self.assertEqual(dialog.error.get_text(), "The two passphrases differ.")
        dialog.pass2.set_text("correct horse battery")
        dialog.response(Gtk.ResponseType.OK)
        self.assertEqual(closed, [Gtk.ResponseType.OK])
        self.assertEqual(dialog.values()[2], "correct horse battery")
        dialog.forget()
        self.assertEqual(dialog.pass1.get_text(), "")
        self.assertEqual(passphrase_strength("short")[0], 0)
        self.assertEqual(passphrase_strength("Tr0ub4dor&3-long")[0], 3)


@unittest.skipUnless(display_available(), "needs a display")
class ExportFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        from tests.gtk_window_fixture import WindowSandbox

        self.sandbox = WindowSandbox(store=True)
        self.addCleanup(self.sandbox.close)
        self.win = self.sandbox.window
        self.native = self.win.native
        self.target = self.sandbox.root / "out" / "inventory.sshm"
        self.target.parent.mkdir()
        self.native.choose_path = lambda _name: str(self.target)

    def run_export(self, answer, scope="all"):
        self.native.ask_export = lambda **kw: (self.asked.append(kw) or answer)
        self.asked = []
        self.native.export(scope)
        self.native.wait()
        return self.asked

    def test_whole_inventory_plain_and_encrypted(self) -> None:
        from ssh_manager import native_format as F
        from ssh_manager.exporters.native import ExportOptions
        from ssh_manager.importers.native import read_native

        self.win.colors.group_colors["prod/web.conf"] = "#112233"
        self.run_export(("all", ExportOptions(), None))
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o600)
        document = json.loads(self.target.read_bytes())
        self.assertEqual(document["counts"]["hosts"], 4)
        self.assertEqual(F.check_integrity(document), "ok")
        prod = next(g for g in document["groups"] if g["name"] == "prod")
        self.assertEqual(next(c for c in prod["children"] if c["name"] == "web.conf")["color"], "#112233")
        toast = self.win._toasts.history.items()[0]
        self.assertEqual(toast.text, "Exported 4 hosts to inventory.sshm.")
        with mock.patch("ssh_manager.gtkui.controllers.native_transfer.show_text") as show:
            toast.action()
        self.assertIn("Hosts: 4", show.call_args.args[2])
        self.run_export(("all", ExportOptions(), "a long passphrase"))
        self.assertEqual(json.loads(self.target.read_bytes())["format"], "ssh-manager-export-encrypted")
        self.assertEqual(len(read_native(self.target, "a long passphrase").hosts()), 4)
        self.assertEqual(sorted(os.listdir(self.target.parent)), ["inventory.sshm"])

    def test_group_context_menu_and_selected_hosts(self) -> None:
        from ssh_manager.exporters.native import ExportOptions
        from ssh_manager.gtkui.constants import RowKind

        with mock.patch.object(self.win.tree, "current_item", return_value=(RowKind.PARENT.value, {"parent": "prod"})), \
                mock.patch.object(self.win.tree, "selected_hosts", return_value=[]):
            spec = self.win.tree.context_menu_spec()
            item = next(i for s in spec for i in s.items if i.label == "E_xport Group…")
            self.native.ask_export = lambda **kw: (self.asked.append(kw) or ("groups", ExportOptions(), None))
            self.asked = []
            item.callback()
            self.native.wait()
        self.assertEqual(self.asked[0]["group_label"], "prod")
        self.assertEqual(self.asked[0]["scope"], "groups")
        document = json.loads(self.target.read_bytes())
        self.assertEqual([g["name"] for g in document["groups"]], ["prod"])
        self.assertEqual(document["scope"], "groups")
        host = self.win.tree.find_host_by_alias("web-01")
        with mock.patch.object(self.win.tree, "selected_hosts", return_value=[host]):
            self.run_export(("hosts", ExportOptions(), None), scope="hosts")
        self.assertEqual(self.asked[0]["host_count"], 1)
        self.assertEqual(json.loads(self.target.read_bytes())["counts"]["hosts"], 1)

    def test_unwritable_folder_shows_the_os_error(self) -> None:
        from ssh_manager.exporters.native import ExportOptions

        self.native.choose_path = lambda _name: "/proc/forbidden/inventory.sshm"
        self.run_export(("all", ExportOptions(), None))
        self.assertTrue(self.win._toasts.history.items()[0].text.startswith("The export could not be written:"))

    def test_actions_and_menu_entries(self) -> None:
        from ssh_manager.gtkui.controllers.shortcuts import ACTION_BY_NAME

        self.assertIn("export-native", ACTION_BY_NAME)
        self.assertIsNotNone(self.win.lookup_action("export-native"))
        from ssh_manager.gtkui.views.header import _avatar_menu_model

        menu = _avatar_menu_model()
        labels = []

        def collect(model):
            for index in range(model.get_n_items()):
                label = model.get_item_attribute_value(index, "label", None)
                if label is not None:
                    labels.append(label.unpack())
                for link in ("section", "submenu"):
                    child = model.get_item_link(index, link)
                    if child is not None:
                        collect(child)

        collect(menu)
        self.assertIn("SSH Manager Export (.sshm)…", labels)


if __name__ == "__main__":
    unittest.main()
