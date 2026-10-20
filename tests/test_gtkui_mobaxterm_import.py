"""The MobaXterm import wizard on the real window (skipped without a display)."""

import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from tests.mxt_synthetic import ACME


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
class ImportWizardTests(unittest.TestCase):
    def setUp(self) -> None:
        from tests.gtk_window_fixture import WindowSandbox

        self.sandbox = WindowSandbox(store=True)  # imports go into the app inventory
        self.win = self.sandbox.window
        self.tmp = tempfile.TemporaryDirectory()
        self.export = Path(self.tmp.name) / "acme.mxtsessions"
        self.export.write_bytes(ACME.encode("utf-8"))
        self.report_dir = Path(self.tmp.name) / "reports"
        self.dialogs = []

    def tearDown(self) -> None:
        for dialog in self.dialogs:
            dialog.wait()
            dialog.destroy()
        self.tmp.cleanup()
        self.sandbox.close()

    # ------------------------------------------------------------ helpers
    def dialog(self):
        from ssh_manager.gtkui.dialogs.mobaxterm_import import MobaXtermImportDialog

        dialog = MobaXtermImportDialog(self.win, self.win.manager, self.win._credential_store,
                                       report_dir=self.report_dir, choose_file=lambda _parent: str(self.export))
        self.dialogs.append(dialog)
        return dialog

    def snapshot(self):
        conn = self.win.manager.store.conn
        rows = (conn.execute("SELECT * FROM groups ORDER BY id").fetchall()
                + conn.execute("SELECT * FROM hosts ORDER BY id").fetchall())
        creds = self.win._credential_store
        return [tuple(r) for r in rows], dict(creds._bindings), [c.id for c in creds.list()]

    def next(self, dialog) -> None:
        from ssh_manager.gtkui.dialogs.mobaxterm_import import RESPONSE_NEXT

        self.assertTrue(dialog.next_button.get_sensitive(), f"Next is disabled on {dialog.step.name}")
        dialog.response(RESPONSE_NEXT)
        dialog.wait()

    def to_credentials(self, dialog) -> None:
        dialog.choose_file()
        dialog.wait()
        self.next(dialog)  # → options
        self.next(dialog)  # → credentials (planned in a worker)

    def add_credential(self, name="Ops", username="ops"):
        return self.win._credential_store.add(name, username, "pw")

    # -------------------------------------------------------------- tests
    def test_step_gating(self) -> None:
        from ssh_manager.gtkui.dialogs.mobaxterm_import import Step

        ops = self.add_credential()
        dialog = self.dialog()
        self.assertIs(dialog.step, Step.FILE)
        self.assertFalse(dialog.next_button.get_sensitive())  # no file yet
        self.assertFalse(dialog.back_button.get_visible())
        self.to_credentials(dialog)
        self.assertIs(dialog.step, Step.CREDENTIALS)
        self.assertEqual(set(dialog.rows), {"alice", "bob", "Alice", "alice@corp.local", "svc_backup", ""})
        self.assertFalse(dialog.next_button.get_sensitive())  # rows undecided
        dialog.set_row_credential("alice", ops.id)
        self.assertFalse(dialog.next_button.get_sensitive())  # still undecided rows
        dialog.default_combo.set_active_id(ops.id)
        self.assertTrue(dialog.next_button.get_sensitive())  # the default decides the rest
        self.assertEqual(dialog.rows["bob"].status.get_text(), "default")
        self.assertEqual(dialog.rows["alice"].status.get_text(), "")  # chosen by hand
        self.assertFalse(dialog.empty_store_box.get_visible())
        self.next(dialog)
        self.assertIs(dialog.step, Step.REVIEW)
        self.assertIn("9 hosts to import", dialog.review_summary.get_text())
        self.assertIn("alice (3 host(s)) → Ops", dialog.report_text)

    def test_empty_credential_store_offers_a_way_out(self) -> None:
        self.assertEqual(self.win._credential_store.list(), [])
        dialog = self.dialog()
        self.to_credentials(dialog)
        self.assertTrue(dialog.empty_store_box.get_visible())
        self.assertFalse(dialog.next_button.get_sensitive())
        dialog.without_credentials_button.clicked()
        self.assertTrue(dialog.next_button.get_sensitive())
        self.assertTrue(all(row.credential_id is None for row in dialog.rows.values()))

    def test_unticking_a_folder_updates_counts_and_usernames(self) -> None:
        dialog = self.dialog()
        dialog.choose_file()
        dialog.wait()
        self.next(dialog)
        self.assertIn("9 SSH sessions", dialog.selection_label.get_text())
        dialog.set_folder_active(("Acme", "Team A"), False)
        self.assertIn("3 SSH sessions", dialog.selection_label.get_text())
        self.assertEqual(dialog.excluded_folders(), [("Acme", "Team A")])
        self.next(dialog)
        self.assertEqual(set(dialog.rows), {"alice", "bob", "Alice"})
        dialog.set_folder_active(("Acme", "Team A"), True)
        self.assertEqual(dialog.excluded_folders(), [])
        for path in (("Acme",), ("Beta",)):
            dialog.set_folder_active(path, False)
        self.assertIn("0 SSH sessions", dialog.selection_label.get_text())

    def test_full_import_writes_binds_and_reports(self) -> None:
        from ssh_manager.gtkui.dialogs.mobaxterm_import import Step
        from ssh_manager.importers.mobaxterm import ImportStatus

        ops = self.add_credential()
        dialog = self.dialog()
        self.to_credentials(dialog)
        dialog.default_combo.set_active_id(ops.id)
        self.next(dialog)
        self.next(dialog)  # Import
        self.assertIs(dialog.step, Step.RESULT)
        self.assertIs(dialog.report.status, ImportStatus.SUCCESS)
        tree = self.win.manager.tree
        self.assertIn("Team A/Servers", tree["Acme"])
        web = next(h for h in tree["Acme"]["Team A/Servers"].hosts if h.alias == "web 01")
        self.assertEqual(web.options["user"], "ops")
        self.assertEqual(self.win._credential_store.binding_for_host(web.id), ops.id)
        self.assertTrue(dialog.report.log_path.exists())
        self.assertEqual(oct(dialog.report.log_path.stat().st_mode & 0o777), "0o600")
        self.assertNotIn("pw", dialog.report.log_path.read_text().split())
        self.assertTrue(dialog.open_report_button.get_visible())
        dialog.show_all()  # e.g. a theme change: Back / Cancel stay hidden on the result step
        self.assertFalse(dialog.back_button.get_visible())
        self.assertFalse(dialog.cancel_button.get_visible())
        # A second run adds nothing.
        again = self.dialog()
        self.to_credentials(again)
        again.default_combo.set_active_id(ops.id)
        self.next(again)
        self.assertIn("Nothing new to import", again.review_notes.get_text())
        self.assertFalse(again.next_button.get_sensitive())

    def test_create_credential_from_a_row(self) -> None:
        from gi.repository import Gtk  # type: ignore

        from ssh_manager.gtkui.dialogs import mobaxterm_import

        dialog = self.dialog()
        self.to_credentials(dialog)

        class FakeEditor:
            def __init__(self, parent):
                self.name_entry, self.user_entry = Gtk.Entry(), Gtk.Entry()

            def run(self):
                return Gtk.ResponseType.OK

            def values(self):
                return self.name_entry.get_text(), self.user_entry.get_text(), "pw"

            def destroy(self):
                pass

        with mock.patch.object(mobaxterm_import, "CredentialEditDialog", FakeEditor):
            dialog.rows["svc_backup"].combo.set_active_id(mobaxterm_import.NEW_ID)
        created = [c for c in self.win._credential_store.list() if c.username == "svc_backup"]
        self.assertEqual(len(created), 1)  # pre-filled with the username from the file
        self.assertEqual(dialog.rows["svc_backup"].credential_id, created[0].id)
        self.assertIn(created[0].id, [row[1] for row in dialog.default_combo.get_model()])  # offered everywhere

    def test_cancel_at_every_step_changes_nothing(self) -> None:
        from gi.repository import Gtk  # type: ignore

        ops = self.add_credential()
        before = self.snapshot()
        for stop_at in range(4):
            with self.subTest(step=stop_at):
                dialog = self.dialog()
                if stop_at >= 1:
                    dialog.choose_file()
                    dialog.wait()
                    self.next(dialog)
                if stop_at >= 2:
                    self.next(dialog)
                    dialog.default_combo.set_active_id(ops.id)
                if stop_at >= 3:
                    self.next(dialog)
                dialog.response(Gtk.ResponseType.CANCEL)
                dialog.wait()
                self.assertIsNone(dialog.report)
                self.assertEqual(self.snapshot(), before)

    def test_closing_during_the_import_rolls_back(self) -> None:
        from gi.repository import Gtk  # type: ignore

        from ssh_manager.importers.mobaxterm import ImportStatus

        ops = self.add_credential()
        before = self.snapshot()
        dialog = self.dialog()
        self.to_credentials(dialog)
        dialog.default_combo.set_active_id(ops.id)
        self.next(dialog)
        gate = threading.Event()
        store = self.win.manager.store
        real_bulk = store.add_hosts_bulk

        def slow_bulk(hosts):
            gate.wait(5)  # still inside the import transaction
            return real_bulk(hosts)

        closed = []
        dialog.connect("response", lambda _d, response: closed.append(response))
        with mock.patch.object(store, "add_hosts_bulk", side_effect=slow_bulk):
            self.assertTrue(dialog.start_import())
            self.assertFalse(dialog.start_import())  # a second click does nothing
            self.assertTrue(dialog.busy)
            dialog.response(Gtk.ResponseType.CANCEL)  # refused while writing; cancels instead
            self.assertEqual(closed, [])
            gate.set()
            dialog.wait()
        self.assertIs(dialog.report.status, ImportStatus.CANCELLED)
        self.assertEqual(closed, [Gtk.ResponseType.CANCEL])  # closes once the rollback is done
        self.assertEqual(self.snapshot(), before)

    def test_parse_errors_stay_on_the_file_step(self) -> None:
        from ssh_manager.gtkui.dialogs.mobaxterm_import import Step

        bad = Path(self.tmp.name) / "notes.mxtsessions"
        bad.write_text("hello world\n")
        dialog = self.dialog()
        dialog.load_file(bad)
        dialog.wait()
        self.assertIs(dialog.step, Step.FILE)
        self.assertTrue(dialog.file_error.get_visible())
        self.assertIn("does not look like a MobaXterm", dialog.file_error.get_text())
        self.assertFalse(dialog.next_button.get_sensitive())
        dialog.load_file(Path(self.tmp.name))  # a folder
        dialog.wait()
        self.assertIn("folder", dialog.file_error.get_text())

    def test_accessible_names(self) -> None:
        dialog = self.dialog()
        self.to_credentials(dialog)
        for widget in (dialog.choose_button, dialog.next_button, dialog.back_button, dialog.parent_combo,
                       dialog.folder_view, dialog.default_combo, dialog.review_text, dialog.progress,
                       *[row.combo for row in dialog.rows.values()]):
            with self.subTest(widget=type(widget).__name__):
                self.assertTrue(widget.get_accessible().get_name())

    def test_entry_points(self) -> None:
        self.assertIsNotNone(self.win.lookup_action("import-mobaxterm"))
        from ssh_manager.gtkui.views.header import _avatar_menu_model

        menu = _avatar_menu_model()
        labels = []
        for section in range(menu.get_n_items()):
            model = menu.get_item_link(section, "section")
            for index in range(model.get_n_items()):
                sub = model.get_item_link(index, "submenu")
                if sub is not None:
                    labels += [sub.get_item_attribute_value(i, "action", None).get_string() for i in range(sub.get_n_items())]
        self.assertIn("win.import-mobaxterm", labels)
        first_run = self.win._sidebar.first_run
        self.assertIn("MobaXterm", first_run.primary_button.get_label())
        with mock.patch.object(self.win.importer, "open") as opened:
            first_run.primary_button.clicked()
        opened.assert_called_once()


if __name__ == "__main__":
    unittest.main()
