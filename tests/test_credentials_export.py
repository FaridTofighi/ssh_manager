"""Encrypted credentials export / import (``*.sshcred``) and the shared AEAD / KDF helper."""

import base64
import json
import logging
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ssh_manager import aead
from ssh_manager.credentials import CredentialStore
from ssh_manager.credentials_export import (
    CredentialExportError,
    export_credentials,
    import_credentials,
    read_credentials_export,
)
from ssh_manager.secure_store import InMemoryKeyring, SecureStore
from tests.gtk_window_fixture import display_available

SECRET = "Cr3d-sentinel-پسورد"


class AeadTests(unittest.TestCase):
    def test_kdf_minimums_and_round_trip(self) -> None:
        kdf = aead.new_kdf()
        self.assertEqual((kdf["name"], kdf["n"], kdf["r"], kdf["p"]), ("scrypt", 32768, 8, 1))
        key = aead.derive("pw", kdf)
        self.assertEqual(len(key), 32)
        header = {"format": "x", "kdf": kdf}
        nonce, data = aead.seal(key, b"hello", header)
        self.assertEqual(aead.open_sealed(key, nonce, data, header), b"hello")
        for bad, code in ((dict(kdf, n=2 ** 14), "unsafe"), (dict(kdf, n=40000), "unsafe"),
                          (dict(kdf, r=1), "unsafe"), (dict(kdf, salt=aead.b64(b"short")), "unsafe"),
                          ({"name": "pbkdf2-sha256", "iterations": 100_000, "salt": kdf["salt"]}, "unsafe"),
                          ({"name": "md5", "salt": kdf["salt"]}, "unsupported"), ("nope", "damaged")):
            with self.subTest(bad=bad), self.assertRaises(aead.AeadError) as caught:
                aead.check_kdf(bad)
            self.assertEqual(caught.exception.code, code)
        argon = {"name": "argon2id", "time_cost": 3, "memory_kib": 65536, "parallelism": 4, "salt": kdf["salt"]}
        if aead.argon2_available():
            aead.check_kdf(argon)
        else:  # a clear error, not a crash
            with self.assertRaises(aead.AeadError) as caught:
                aead.derive("pw", argon)
            self.assertEqual(caught.exception.code, "unsupported")
        with self.assertRaises(aead.AeadError) as caught:
            aead.open_sealed(key, nonce, data, dict(header, format="y"))
        self.assertEqual(caught.exception.code, "auth")


class ExportCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = CredentialStore(SecureStore(InMemoryKeyring()), self.root / "a.json")
        self.source.add("Ops", "deploy", SECRET)
        self.source.add("DB", "postgres", "pg")
        self.source.create_private("host-1", "root", "private-pw")
        self.target = CredentialStore(SecureStore(InMemoryKeyring()), self.root / "b.json")
        self.file = self.root / "out.sshcred"


class ExportTests(ExportCase):
    def test_round_trip_shared_only_private_file(self) -> None:
        self.assertEqual(export_credentials(self.source, "export passphrase", self.file), 2)
        self.assertEqual(stat.S_IMODE(self.file.stat().st_mode), 0o600)
        raw = self.file.read_bytes()
        for plain in (SECRET.encode(), b"private-pw", b"Ops", b"deploy"):
            self.assertNotIn(plain, raw)
        document = json.loads(raw)
        self.assertEqual(set(document), {"format", "version", "kdf", "nonce", "data"})
        self.assertEqual(document["kdf"]["n"], 32768)
        items = read_credentials_export(self.file, "export passphrase")
        self.assertEqual([(i.name, i.username, i.password) for i in items],
                         [("DB", "postgres", "pg"), ("Ops", "deploy", SECRET)])
        self.assertNotIn(SECRET, repr(items))

    def test_errors_write_nothing(self) -> None:
        export_credentials(self.source, "export passphrase", self.file)
        document = json.loads(self.file.read_bytes())
        cases = [
            ("wrong", document, "wrong_passphrase"),
            ("export passphrase", dict(document, kdf=dict(document["kdf"], n=65536)), "wrong_passphrase"),
            ("export passphrase", dict(document, kdf=dict(document["kdf"], n=1024)), "unsafe"),
            ("export passphrase", dict(document, version=2), "newer"),
            ("export passphrase", dict(document, format="other"), "not_export"),
            ("export passphrase", dict(document, data=base64.b64encode(b"x" * 40).decode()), "wrong_passphrase"),
        ]
        for passphrase, changed, code in cases:
            path = self.root / f"case-{code}.sshcred"
            path.write_text(json.dumps(changed))
            with self.subTest(code=code), self.assertRaises(CredentialExportError) as caught:
                read_credentials_export(path, passphrase)
            self.assertEqual(caught.exception.code, code)
        self.assertEqual(self.target.list(), [])

    def test_policies(self) -> None:
        export_credentials(self.source, "pp-pp-pp-pp", self.file)
        items = read_credentials_export(self.file, "pp-pp-pp-pp")
        self.target.add("ops", "deploy", SECRET)  # same name (any case), same username and password
        self.target.add("DB", "someone", "else")  # same name, different data
        skip = import_credentials(self.target, items, "skip")
        self.assertEqual((skip.created, skip.skipped), ([], ["DB", "Ops"]))
        reuse = import_credentials(self.target, items, "reuse")
        self.assertEqual((reuse.reused, reuse.created), (["ops"], ["DB (2)"]))
        rename = import_credentials(self.target, items, "rename")
        self.assertEqual(rename.created, ["DB (3)", "Ops (2)"])
        stored = {c.name: c for c in self.target.list()}
        self.assertEqual(self.target.get_password(stored["Ops (2)"].id), SECRET)

    def test_no_secret_in_logs(self) -> None:
        with self.assertLogs(level=logging.DEBUG) as logs:
            logging.getLogger("ssh_manager").debug("start")
            export_credentials(self.source, "pp-pp-pp-pp", self.file)
            items = read_credentials_export(self.file, "pp-pp-pp-pp")
            import_credentials(self.target, items)
            with self.assertRaises(CredentialExportError):
                read_credentials_export(self.file, "wrong")
        self.assertNotIn(SECRET, "\n".join(logs.output))
        self.assertNotIn("pp-pp-pp-pp", "\n".join(logs.output))


@unittest.skipUnless(display_available(), "needs a display")
class DialogTests(ExportCase):
    def test_export_and_import_flows(self) -> None:
        from ssh_manager.gtkui.dialogs import credentials_transfer as T

        messages = []
        with mock.patch.object(T, "_message", lambda _p, text, error=False: messages.append((text, error))):
            self.assertEqual(T.run_export(None, self.source, ask_passphrase=lambda: "pp-pp-pp-pp",
                                          choose_path=lambda: str(self.root / "plain-name")), 2)
            self.assertTrue((self.root / "plain-name.sshcred").exists())
            answers = iter(["wrong", "pp-pp-pp-pp"])
            prompts = []

            def ask(message):
                prompts.append(message)
                return next(answers)

            result = T.run_import(None, self.target, choose_path=lambda: str(self.root / "plain-name.sshcred"),
                                  ask_passphrase=ask, choose_items=lambda items: (items[:1], "rename"))
        self.assertEqual(result.created, ["DB"])
        self.assertEqual(prompts[1], "The passphrase is wrong, or the file was changed.")  # asked again
        self.assertIn(("Imported 1 credential.", False), messages)

    def test_passphrase_and_selection_dialogs(self) -> None:
        from gi.repository import Gtk  # type: ignore

        from ssh_manager.credentials_export import ExportedCredential
        from ssh_manager.gtkui.dialogs.credentials_transfer import CredentialImportDialog, PassphraseDialog

        dialog = PassphraseDialog(None, "x", confirm=True)
        self.addCleanup(dialog.destroy)
        closed = []
        dialog.connect_after("response", lambda _d, r: closed.append(r))
        dialog.first.set_text("long enough phrase")
        dialog.second.set_text("long enough phrasf")
        dialog.response(Gtk.ResponseType.OK)
        self.assertEqual((closed, dialog.error.get_text()), ([], "The two passphrases differ."))
        chooser = CredentialImportDialog(None, [ExportedCredential("A", "a", "1"), ExportedCredential("B", "b", "2")])
        self.addCleanup(chooser.destroy)
        chooser.model[0][0] = False
        self.assertEqual([i.name for i in chooser.selected()], ["B"])

    def test_the_manager_has_the_buttons(self) -> None:
        from ssh_manager.gtkui.dialogs.credentials import CredentialManagerDialog

        dialog = CredentialManagerDialog(None, self.source)
        self.addCleanup(dialog.destroy)
        with mock.patch("ssh_manager.gtkui.dialogs.credentials_transfer.run_export") as export:
            dialog.export_button.clicked()
        export.assert_called_once()


if __name__ == "__main__":
    unittest.main()
