"""Exports are named ``*.ophion``; ``*.sshm`` (and ``*.json``) files from earlier versions still import.

Only the file name extension changed: the content (``format`` ids, versions, the encryption header that is
authenticated as AES-GCM associated data, the integrity rules) is byte-compatible in both directions, so an
``.ophion`` file renamed to ``.sshm`` opens in an older version. Measured before: EXTENSION was ".sshm" and
the default name ``ophion-YYYYMMDD-HHMM.sshm``.
"""

import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.gtk_window_fixture import display_available

LEGACY = Path(__file__).resolve().parent / "fixtures" / "pre_rename"  # written by SSH Manager (before the rename)
LEGACY_PASS = "pre-rename-export"


class ConstantsTests(unittest.TestCase):
    def test_extension_names_and_frozen_format_ids(self) -> None:
        from ophion import native_format as F

        self.assertEqual(F.EXTENSION, ".ophion")
        self.assertEqual(F.LEGACY_EXTENSIONS, (".sshm", ".json"))
        self.assertEqual(F.ACCEPTED_EXTENSIONS, (".ophion", ".sshm", ".json"))
        self.assertEqual(F.MIME_TYPE, "application/x-ophion+json")
        self.assertEqual(F.LEGACY_MIME_TYPES, ("application/x-ssh-manager+json",))
        self.assertEqual((F.FORMAT, F.ENCRYPTED_FORMAT, F.VERSION, F.ENCRYPTED_VERSION),
                         ("ssh-manager-export", "ssh-manager-export-encrypted", 1, 1))  # frozen
        self.assertRegex(F.default_file_name(0), r"^ophion-\d{8}-\d{4}\.ophion$")


class CompatibilityTests(unittest.TestCase):
    """Content decides, not the name: old files, renamed files and any extension."""

    def setUp(self) -> None:
        from ophion import native_format as F
        from ophion.exporters.native import build_export
        from tests.native_fixture import build_rich, make_store

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        source = make_store(self.dir / "source")
        self.addCleanup(source.close)
        _ids, gc, hc, creds, _unused = build_rich(source)
        self.doc, _ = build_export(source, group_colors=gc, host_colors=hc, credential_for=creds.get)
        self.plain = F.dump_document(self.doc)
        self.encrypted = F.encrypt_document(self.plain, "open sesame")

    def write(self, name: str, data: bytes) -> Path:
        path = self.dir / name
        path.write_bytes(data)
        return path

    def test_round_trip_through_an_ophion_file(self) -> None:
        from ophion.importers.native import read_native

        plain = read_native(self.write("inventory.ophion", self.plain))
        locked = read_native(self.write("protected.ophion", self.encrypted), "open sesame")
        self.assertEqual(json.loads(self.plain)["format"], "ssh-manager-export")
        self.assertEqual(json.loads(self.encrypted)["format"], "ssh-manager-export-encrypted")
        self.assertEqual((plain.integrity, locked.integrity), ("ok", "ok"))
        self.assertEqual(len(plain.hosts()), len(locked.hosts()))
        self.assertEqual(len(plain.hosts()), self.doc["counts"]["hosts"])

    def test_renamed_and_odd_extensions_still_import(self) -> None:
        from ophion.importers.native import read_native

        for name in ("renamed.sshm", "export.json", "backup.bin", "no-extension"):
            with self.subTest(name=name):
                self.assertEqual(read_native(self.write(name, self.plain)).integrity, "ok")

    def test_invalid_content_is_rejected_whatever_the_name(self) -> None:
        from ophion.importers.native import read_native
        from ophion.native_format import NativeFormatError

        with self.assertRaises(NativeFormatError) as caught:
            read_native(self.write("fake.ophion", b'{"format": "something-else"}'))
        self.assertEqual(caught.exception.code, "not_native")

    def test_legacy_sshm_files_plain_and_encrypted(self) -> None:
        from ophion.importers.native import read_native

        plain = read_native(LEGACY / "export-plain.sshm")
        encrypted = read_native(LEGACY / "export-encrypted.sshm", LEGACY_PASS)
        self.assertEqual((plain.integrity, encrypted.integrity), ("ok", "ok"))
        self.assertEqual(len(plain.hosts()), len(encrypted.hosts()))
        self.assertTrue(plain.hosts())


@unittest.skipUnless(display_available(), "needs a display")
class ExportNameTests(unittest.TestCase):
    def setUp(self) -> None:
        from tests.gtk_window_fixture import WindowSandbox

        self.sandbox = WindowSandbox(store=True)
        self.addCleanup(self.sandbox.close)
        self.win = self.sandbox.window
        self.out = self.sandbox.root / "out"
        self.out.mkdir()

    def export_as(self, typed: str) -> list:
        from ophion.exporters.native import ExportOptions

        native = self.win.native
        suggested = []
        native.choose_path = lambda name: suggested.append(name) or str(self.out / typed)
        native.ask_export = lambda **_kw: ("all", ExportOptions(), None)
        native.export("all")
        native.wait()
        return suggested

    def test_suggested_name_and_extension_rules(self) -> None:
        suggested = self.export_as("inventory")  # no extension typed: .ophion is added
        self.assertRegex(suggested[0], r"^ophion-\d{8}-\d{4}\.ophion$")
        self.export_as("old-style.sshm")  # typed explicitly: kept, still a valid export
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), ["inventory.ophion", "old-style.sshm"])


@unittest.skipUnless(display_available(), "needs a display")
class ImportFacingTextTests(unittest.TestCase):
    """Labels say Ophion / .ophion; .sshm only in the one compatibility sentence."""

    def setUp(self) -> None:
        from tests.gtk_window_fixture import WindowSandbox

        self.sandbox = WindowSandbox(store=True)
        self.addCleanup(self.sandbox.close)
        self.win = self.sandbox.window

    # Measured: the import side still says *.sshm (expected failure until the next commit).
    @unittest.expectedFailure
    def test_file_chooser_filter(self) -> None:
        from ophion.gtkui.dialogs import native_import
        from ophion.gtkui.dialogs.native_import import NativeImportDialog

        dialog = NativeImportDialog(self.win, self.win.manager, self.win._credential_store,
                                    report_dir=self.sandbox.root / "reports")
        self.addCleanup(dialog.destroy)
        with mock.patch.object(native_import, "choose_open_file", return_value=None) as choose:
            dialog._default_choose_file(self.win)
        self.assertEqual(choose.call_args.args[1], "Choose an Ophion Export")
        self.assertEqual(choose.call_args.kwargs["filters"],
                         [("Ophion exports (*.ophion)", ["*.ophion", "*.sshm", "*.json"])])

    # Measured: the import side still says *.sshm (expected failure until the next commit).
    @unittest.expectedFailure
    def test_wizard_help_menus_tooltips_and_actions(self) -> None:
        from gi.repository import Gtk

        from ophion.gtkui.controllers import shortcuts
        from ophion.gtkui.dialogs.native_import import NativeImportDialog
        from ophion.gtkui.views import header

        dialog = NativeImportDialog(self.win, self.win.manager, self.win._credential_store,
                                    report_dir=self.sandbox.root / "reports")
        self.addCleanup(dialog.destroy)
        texts = []

        def walk(widget):
            if isinstance(widget, Gtk.Label):
                texts.append(widget.get_text())
            if isinstance(widget, Gtk.Container):
                widget.forall(walk)
        walk(dialog)
        help_text = next(text for text in texts if text.startswith("Choose a file exported by Ophion"))
        self.assertIn("(*.ophion)", help_text)
        self.assertIn("Files exported by SSH Manager (*.sshm) can be imported too.", help_text)
        self.assertEqual(dialog.get_title(), "Import an Ophion Export")

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
        collect(header._avatar_menu_model())
        self.assertIn("From Ophion Export (.ophion)…", labels)
        self.assertIn("Ophion Export (.ophion)…", labels)
        specs = {spec.name: spec.title for spec in shortcuts.ACTIONS}
        self.assertEqual(specs["export-native"], "Export as Ophion file (.ophion)")
        self.assertEqual(specs["import-native"], "Import an Ophion export (.ophion)")
        source = "\n".join(Path(module.__file__).read_text(encoding="utf-8") for module in (
            shortcuts, header, __import__("ophion.gtkui.controllers.context_menus", fromlist=["x"]),
            __import__("ophion.gtkui.dialogs.native_import", fromlist=["x"]),
            __import__("ophion.gtkui.controllers.native_transfer", fromlist=["x"])))
        strings = re.findall(r'_\("((?:[^"\\]|\\.)*)"\)', source)
        legacy = [text for text in strings if ".sshm" in text or "SSH Manager" in text]
        self.assertEqual(legacy, ["Files exported by SSH Manager (*.sshm) can be imported too."])


if __name__ == "__main__":
    unittest.main()
