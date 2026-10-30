"""The Known Hosts manager dialog (temporary files only; the verify test uses an in-process SSH server)."""

import tempfile
import time
import unittest
from pathlib import Path

from tests.gtk_window_fixture import display_available
from tests.test_known_hosts import ED25519, hashed_name


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
        time.sleep(0.005)


@unittest.skipUnless(display_available(), "needs a display")
class KnownHostsDialogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / ".ssh" / "known_hosts"
        self.path.parent.mkdir()
        self.toasts = []

    def dialog(self, hosts=(), **kwargs):
        from ssh_manager.gtkui.dialogs.known_hosts import KnownHostsDialog

        dialog = KnownHostsDialog(None, self.path, hosts,
                                  notify=lambda text, level, **kw: self.toasts.append((text, kw)), **kwargs)
        self.addCleanup(dialog.destroy)
        self.assertTrue(pump(10, until=lambda: not dialog.loading), "the file did not load")
        return dialog

    def rows(self, dialog, column=0):
        return [row[column] for row in dialog.filtered]

    def test_ten_thousand_lines_views_and_filter(self) -> None:
        lines = [f"h{i}.example.org ssh-ed25519 {ED25519}\n" for i in range(10_000)]
        lines += ["garbage line\n", f"{hashed_name('secret.example')} ssh-ed25519 {ED25519}\n",
                  f"h1.example.org ssh-rsa {ED25519}\n"]
        self.path.write_text("".join(lines))
        hosts = [("k1", "one", "h1.example.org", 22), ("k2", "nokey", "never.example.org", 22),
                 ("k3", "hidden", "secret.example", 22)]
        started = time.perf_counter()
        dialog = self.dialog(hosts)
        elapsed = time.perf_counter() - started
        self.assertEqual(len(dialog.filtered), 10_003)  # entries + the malformed line
        self.assertLess(elapsed, 5.0)
        garbage = [row for row in dialog.filtered if not row[7]]
        self.assertEqual(len(garbage), 1)  # greyed
        hidden = [row for row in dialog.filtered if row[4] == 10_002][0]
        self.assertEqual(hidden[0], "hidden")  # the hashed entry is matched to its inventory host
        dialog.search.set_text("h9999.")
        self.assertTrue(pump(2, until=lambda: len(dialog.filtered) == 1))
        dialog.search.set_text("")
        dialog.set_view("missing")
        self.assertTrue(pump(2, until=lambda: self.rows(dialog) == ["nokey"]))
        dialog.set_view("duplicates")
        self.assertTrue(pump(2, until=lambda: sorted(self.rows(dialog, 4)) == [2, 10_003]))
        dialog.set_view("hashed")
        pump(0.3)
        self.assertEqual(self.rows(dialog, 4), [])  # the one hashed entry belongs to "hidden"

    def test_delete_with_backup_and_undo(self) -> None:
        from ssh_manager import known_hosts as KH

        self.path.write_text(f"# keep me\na.example ssh-ed25519 {ED25519}\nb.example ssh-ed25519 {ED25519}\r\n")
        before = self.path.read_bytes()
        dialog = self.dialog()
        asked = []
        dialog.confirm = lambda title, text, label, listing="": asked.append((title, listing)) or True
        selection = dialog.tree.get_selection()
        selection.select_path(1)  # b.example
        self.assertTrue(dialog.delete_selected())
        self.assertEqual(asked[0][0], "Delete 1 known host key?")
        self.assertIn("b.example", asked[0][1])
        self.assertEqual(self.path.read_bytes(), f"# keep me\na.example ssh-ed25519 {ED25519}\n".encode())
        self.assertEqual(len(KH.backups(self.path)), 1)
        text, options = self.toasts[-1]
        self.assertEqual((options["action_label"], options["timeout"]), ("Undo", 30.0))
        options["action"]()
        self.assertEqual(self.path.read_bytes(), before)

    def test_cancel_changes_nothing_and_forget_for_a_host(self) -> None:
        self.path.write_text(f"a.example ssh-ed25519 {ED25519}\n[a.example]:2222 ssh-ed25519 {ED25519}\n"
                             f"b.example ssh-ed25519 {ED25519}\n")
        before = self.path.read_bytes()
        dialog = self.dialog([("ka", "a", "a.example", 22)], host_key="ka")
        self.assertTrue(dialog.forget_button.get_visible() or not dialog.forget_button.get_no_show_all())
        dialog.confirm = lambda *a, **k: False
        self.assertFalse(dialog.forget_host())
        self.assertEqual(self.path.read_bytes(), before)
        dialog.confirm = lambda *a, **k: True
        self.assertTrue(dialog.forget_host())  # only a.example on port 22
        self.assertEqual(self.path.read_bytes(), before.replace(f"a.example ssh-ed25519 {ED25519}\n".encode(), b""))

    def test_import_export_and_copy(self) -> None:
        self.path.write_text(f"a.example ssh-ed25519 {ED25519}\n")
        other = Path(self.tmp.name) / "other"
        other.write_text(f"a.example ssh-ed25519 {ED25519}\nnew.example ssh-ed25519 {ED25519}\n")
        dialog = self.dialog()
        dialog.choose_open = lambda: str(other)
        asked = []
        dialog.confirm = lambda title, text, label, listing="": asked.append((title, text)) or True
        self.assertTrue(dialog.import_file())
        self.assertEqual(asked, [("Add 1 entry?", "1 duplicate is skipped.")])
        self.assertTrue(pump(5, until=lambda: not dialog.loading and len(dialog.filtered) == 2))
        dialog.tree.get_selection().select_path(1)
        target = Path(self.tmp.name) / "exported"
        dialog.choose_save = lambda: str(target)
        self.assertTrue(dialog.export_selected())
        self.assertEqual(target.read_text(), f"new.example ssh-ed25519 {ED25519}\n")
        self.assertTrue(dialog.copy_fingerprints().startswith("SHA256:"))

    def test_verify_host_key_now(self) -> None:
        import paramiko

        from tests.fake_sftp import FakeSftpServer

        server = FakeSftpServer(Path(self.tmp.name))
        server.__enter__()
        self.addCleanup(server.__exit__)
        name = f"[127.0.0.1]:{server.port}"
        hosts = [("k", "local", "127.0.0.1", server.port)]
        cases = (
            (f"{name} {server.host_key.get_name()} {server.host_key.get_base64()}\n", "match"),
            (f"{name} ecdsa-sha2-nistp256 {paramiko.ECDSAKey.generate().get_base64()}\n", "different"),
            ("", "not_stored"),
        )
        for text, verdict in cases:
            with self.subTest(verdict=verdict):
                self.path.write_text(text)
                before = self.path.read_bytes()
                dialog = self.dialog(hosts)
                messages = []
                dialog.message = lambda title, body: messages.append(title)
                if verdict == "not_stored":
                    dialog.set_view("missing")
                    pump(0.3)
                dialog.tree.get_selection().select_path(0)
                dialog.verify_selected()
                self.assertTrue(pump(10, until=lambda: messages), "no answer")
                self.assertEqual(dialog.last_verdict, verdict)
                self.assertEqual(self.path.read_bytes(), before)  # never writes


@unittest.skipUnless(display_available(), "needs a display")
class PreferencesHintTests(unittest.TestCase):
    def test_auto_add_hint_follows_the_policy(self) -> None:
        from ssh_manager.gtkui.dialogs.preferences import PreferencesDialog
        from ssh_manager.gtkui.theme.loader import ThemeMode
        from ssh_manager.settings import AppSettings

        with tempfile.TemporaryDirectory() as tmp:
            settings = AppSettings(config_path=Path(tmp) / "settings.json")
            dialog = PreferencesDialog(None, ThemeMode.LIGHT, 1.0, 11, settings)
            self.addCleanup(dialog.destroy)
            self.assertFalse(dialog._auto_add_hint.get_visible())
            dialog._policy_auto.set_active(True)
            self.assertTrue(dialog._auto_add_hint.get_visible())
            self.assertIn("changed key is still refused", dialog._auto_add_hint.get_text())
            dialog._policy_strict.set_active(True)
            self.assertFalse(dialog._auto_add_hint.get_visible())
            self.assertEqual(dialog.values().host_key_policy, "reject")


if __name__ == "__main__":
    unittest.main()
