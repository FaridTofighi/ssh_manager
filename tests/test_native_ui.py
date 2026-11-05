"""SSH Manager export / import in the window: the export dialog, menus, the written file, the import wizard."""

import json
import os
import stat
import time
import unittest
from pathlib import Path
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

        from ophion.gtkui.dialogs.native_export import NativeExportDialog, passphrase_strength

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
        from ophion import native_format as F
        from ophion.exporters.native import ExportOptions
        from ophion.importers.native import read_native

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
        with mock.patch("ophion.gtkui.controllers.native_transfer.show_text") as show:
            toast.action()
        self.assertIn("Hosts: 4", show.call_args.args[2])
        self.run_export(("all", ExportOptions(), "a long passphrase"))
        self.assertEqual(json.loads(self.target.read_bytes())["format"], "ssh-manager-export-encrypted")
        self.assertEqual(len(read_native(self.target, "a long passphrase").hosts()), 4)
        self.assertEqual(sorted(os.listdir(self.target.parent)), ["inventory.sshm"])

    def test_group_context_menu_and_selected_hosts(self) -> None:
        from ophion.exporters.native import ExportOptions
        from ophion.gtkui.constants import RowKind

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
        from ophion.exporters.native import ExportOptions

        self.native.choose_path = lambda _name: "/proc/forbidden/inventory.sshm"
        self.run_export(("all", ExportOptions(), None))
        self.assertTrue(self.win._toasts.history.items()[0].text.startswith("The export could not be written:"))

    def test_actions_and_menu_entries(self) -> None:
        from ophion.gtkui.controllers.shortcuts import ACTION_BY_NAME

        self.assertIn("export-native", ACTION_BY_NAME)
        self.assertIsNotNone(self.win.lookup_action("export-native"))
        from ophion.gtkui.views.header import _avatar_menu_model

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
        self.assertIn("Ophion Export (.ophion)…", labels)



@unittest.skipUnless(display_available(), "needs a display")
class ImportWizardTests(unittest.TestCase):
    """The wizard end to end against the sandbox window's real inventory and credential store."""

    def setUp(self) -> None:
        import tempfile

        from ophion import native_format as F
        from ophion.exporters.native import build_export
        from tests.gtk_window_fixture import WindowSandbox
        from tests.native_fixture import build_rich, make_store

        self.sandbox = WindowSandbox(store=True)
        self.addCleanup(self.sandbox.close)
        self.win = self.sandbox.window
        self.manager = self.win.manager
        self.creds = self.win._credential_store
        self.ops = self.creds.add("Ops account", "deploy", "pw")  # matches the file's credential reference
        source_dir = tempfile.TemporaryDirectory()
        self.addCleanup(source_dir.cleanup)
        source = make_store(Path(source_dir.name))
        self.addCleanup(source.close)
        _ids, gc, hc, creds, _unused = build_rich(source)
        doc, _ = build_export(source, group_colors=gc, host_colors=hc, credential_for=creds.get)
        self.plain = self.sandbox.root / "rich.sshm"
        self.plain.write_bytes(F.dump_document(doc))
        self.encrypted = self.sandbox.root / "rich-protected.sshm"
        self.encrypted.write_bytes(F.encrypt_document(F.dump_document(doc), "open sesame"))

    def wizard(self):
        from ophion.gtkui.dialogs.native_import import NativeImportDialog

        dialog = NativeImportDialog(self.win, self.manager, self.creds, report_dir=self.sandbox.root / "reports",
                                    group_colors=self.win.colors.group_colors,
                                    host_colors=self.win.colors.host_colors)
        self.addCleanup(dialog.destroy)
        dialog.confirm = lambda *a, **kw: True
        return dialog

    def next(self, dialog) -> None:
        from ophion.gtkui.dialogs.native_import import RESPONSE_NEXT

        self.assertTrue(dialog.next_button.get_sensitive(), f"Next is disabled on {dialog.step.name}")
        dialog.response(RESPONSE_NEXT)
        dialog.wait()

    def counts(self):
        store = self.manager.store
        return len(store.list_groups()), len(store.list_hosts())

    def test_end_to_end_under_an_existing_group(self) -> None:
        from ophion.gtkui.dialogs.native_import import NONE_ID, Step

        before = self.counts()
        dialog = self.wizard()
        dialog.load_file(self.plain)
        dialog.wait()
        self.assertIn("9 hosts", dialog.file_summary.get_text())
        self.assertIn("intact", dialog.file_summary.get_text())
        self.next(dialog)
        self.assertEqual(dialog.step, Step.OPTIONS)
        dialog.under_radio.set_active(True)
        self.assertFalse(dialog.next_button.get_sensitive())  # no group chosen yet
        self.assertTrue(dialog.destination.select_value("staging"))
        self.next(dialog)
        self.assertEqual(dialog.step, Step.SELECT)
        windows = next(g for g in dialog.document.groups if g.name == "Windows")
        dialog.set_group_active(windows.key, False)  # the RDP host is left out
        self.assertIn("8 hosts selected", dialog.selection_label.get_text())
        self.next(dialog)
        self.assertEqual(dialog.step, Step.CREDENTIALS)
        self.assertEqual(dialog.rows["deploy"].combo.get_active_id(), self.ops.id)  # matched
        self.assertEqual(dialog.rows["deploy"].status.get_text(), "matched")
        self.assertFalse(dialog.next_button.get_sensitive())  # the other users are undecided
        dialog.default_combo.set_active_id(NONE_ID)
        self.assertTrue(dialog.next_button.get_sensitive())
        self.next(dialog)
        self.assertEqual(dialog.step, Step.CONFLICTS)
        self.assertEqual(dialog.conflict_summary.get_text(), "None of the hosts exists yet.")
        self.next(dialog)
        self.assertEqual(dialog.step, Step.REVIEW)
        self.assertIn("8 hosts to import", dialog.review_summary.get_text())
        self.next(dialog)
        dialog.wait()
        self.assertEqual(dialog.report.status.value, "success", dialog.report.message)
        groups, hosts = self.counts()
        self.assertEqual(hosts, before[1] + 8)
        store = self.manager.store
        self.assertIsNotNone(store.find_group(["staging", "Prod", "Databases", "Replicas"]))
        self.assertIsNone(store.find_group(["staging", "Windows"]))
        web = store.find_host(store.find_group(["staging", "Prod"]).id, "web-01")  # the sandbox has its own web-01
        self.assertEqual(self.creds.binding_for_host(web.id), self.ops.id)
        self.assertEqual(dialog.report.group_colors["staging/Prod"], "#aa0000")
        warnings = self.win.native.apply_after_commit(dialog.report)
        self.assertEqual(warnings, [])
        self.assertEqual(self.win.colors.group_colors["staging/Prod/Databases/Replicas"], "#0000aa")
        self.assertEqual(self.win.colors.group_color_for("staging", "Prod/Databases"), "#00aa00")

    def run_to(self, dialog, step_name):
        dialog.load_file(self.plain)
        dialog.wait()
        while dialog.step.name != step_name:
            if dialog.step.name == "CREDENTIALS":
                dialog.default_combo.set_active_id("__none__")
            self.next(dialog)

    def test_top_level_then_again_reports_the_conflicts(self) -> None:
        first = self.wizard()
        self.run_to(first, "REVIEW")
        self.next(first)
        first.wait()
        self.assertEqual(first.report.status.value, "success")
        state = self.counts()
        again = self.wizard()
        self.run_to(again, "CONFLICTS")
        self.assertEqual(again.conflict_summary.get_text(), "9 hosts of the file already exist.")
        self.next(again)
        self.assertIn("Nothing new to import.", again.review_notes.get_text())
        self.assertFalse(again.next_button.get_sensitive())  # Import is off: nothing would change
        self.assertEqual(self.counts(), state)

    def test_encrypted_file_asks_for_the_passphrase(self) -> None:
        dialog = self.wizard()
        dialog.load_file(self.encrypted)
        dialog.wait()
        self.assertTrue(dialog.pass_box.get_visible())
        self.assertIsNone(dialog.document)
        dialog.pass_entry.set_text("wrong")
        dialog.unlock()
        dialog.wait()
        self.assertEqual(dialog.file_error.get_text(), "The passphrase is wrong, or the file was changed.")
        self.assertFalse(dialog.unlock_button.get_sensitive())  # a short pause before the next try
        self.assertTrue(pump(3, until=dialog.unlock_button.get_sensitive))
        dialog.pass_entry.set_text("open sesame")
        dialog.unlock()
        dialog.wait()
        self.assertIsNotNone(dialog.document)
        self.assertEqual(dialog.pass_entry.get_text(), "")  # not kept

    def test_cancel_at_every_step_changes_nothing(self) -> None:
        from gi.repository import Gtk  # type: ignore

        before = self.counts()
        for steps in range(6):
            dialog = self.wizard()
            dialog.load_file(self.plain)
            dialog.wait()
            for _step in range(steps):
                if dialog.step.name == "CREDENTIALS":
                    dialog.default_combo.set_active_id("__none__")
                self.next(dialog)
            dialog.response(Gtk.ResponseType.CANCEL)
            self.assertEqual(self.counts(), before, steps)

    def test_a_large_file_stays_responsive(self) -> None:
        from tests.test_native_commit import big_document

        large = self.sandbox.root / "large.sshm"
        large.write_bytes(big_document(5000, per_group=5))
        dialog = self.wizard()
        started = time.monotonic()
        dialog.load_file(large)
        dialog.wait()
        self.assertLess(time.monotonic() - started, 3.0)
        self.assertEqual(len(dialog.group_store), 1000)  # one model row per group, no widget per row
        started = time.monotonic()
        dialog.group_search.set_text("Group 00999")
        dialog._filter_groups()
        self.assertLess(time.monotonic() - started, 1.0)


@unittest.skipUnless(display_available(), "needs a display")
class ImportControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        from tests.gtk_window_fixture import WindowSandbox

        self.sandbox = WindowSandbox(store=True)
        self.addCleanup(self.sandbox.close)
        self.win = self.sandbox.window

    def test_colours_are_saved_after_the_commit_and_failures_are_warnings(self) -> None:
        from ophion.importers.native import ImportStatus, NativeImportReport

        report = NativeImportReport(ImportStatus.SUCCESS, "ok", 1, 1, ["prod"],
                                    group_colors={"prod": "#123456"}, host_colors={"prod::web.conf::web-01": "#654321"})

        class FakeWizard:
            def __init__(self, *a, **kw):
                self.report, self.busy = report, False

            def run(self):
                return 0

            def destroy(self):
                pass

        with mock.patch("ophion.gtkui.dialogs.native_import.NativeImportDialog", FakeWizard), \
                mock.patch.object(self.win, "_save_ui_state") as save, \
                mock.patch.object(self.win.tree, "reveal_imported") as reveal:
            self.win.lookup_action("import-native").activate(None)
        save.assert_called_once()
        reveal.assert_called_once_with(["prod"])
        self.assertEqual(self.win.colors.group_colors["prod"], "#123456")
        self.assertIn("Imported 1 host from the Ophion export.", [m.text for m in self.win._toasts.history.items()])
        with mock.patch.object(self.win, "_save_ui_state", side_effect=OSError("disk full")):
            warnings = self.win.native.apply_after_commit(report)
        self.assertEqual(warnings, ["The hosts were imported, but their colours could not be saved."])

    def test_entry_points(self) -> None:
        self.assertIsNotNone(self.win.lookup_action("import-native"))
        link = self.win.sidebar.native_import_link if hasattr(self.win, "sidebar") else None
        if link is None:
            link = next(w for w in _walk(self.win) if getattr(w, "get_label", lambda: "")() ==
                        "Import an Ophion e_xport…")
        with mock.patch.object(self.win.native, "import_file") as opened:
            link.clicked()
        opened.assert_called_once()


def _walk(widget):
    from gi.repository import Gtk  # type: ignore

    yield widget
    if isinstance(widget, Gtk.Container):
        for child in widget.get_children():
            yield from _walk(child)

if __name__ == "__main__":
    unittest.main()
