"""Install SSH Key on hosts: the dialog against an in-process server (password login, then the key alone)."""

import tempfile
import unittest
from pathlib import Path

from tests.gtk_window_fixture import display_available
from tests.test_local_terminal import pump


@unittest.skipUnless(display_available(), "needs a display")
class DeployDialogTests(unittest.TestCase):
    def setUp(self) -> None:
        from tests.fake_sftp import FakeSftpServer
        from tests.gtk_window_fixture import WindowSandbox

        from ssh_manager import ssh_keys as K
        from ssh_manager.transfer.settings import TransferSettings
        from ssh_manager.transfer.sftp import SftpBackend

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        (root / "srv").mkdir()
        self.server = FakeSftpServer(root / "srv", users={"alice": "pw"})
        self.server.__enter__()
        self.addCleanup(self.server.__exit__)
        known = root / "known_hosts"
        key = self.server.host_key
        known.write_text(f"[127.0.0.1]:{self.server.port} {key.get_name()} {key.get_base64()}\n")
        self.sandbox = WindowSandbox(store=True)
        self.addCleanup(self.sandbox.close)
        self.win = self.sandbox.window
        self.win.ssh_keys.ssh_dir = self.sandbox.root / "home" / ".ssh"
        self.key = K.generate_key(self.win.ssh_keys.ssh_dir / "id_ed25519", comment="me@box")
        self.srv = root / "srv"

        def backend(password, key_file=""):
            return SftpBackend("127.0.0.1", self.server.port, "alice", password, known_hosts=known,
                               allow_agent=False, key_file=key_file, settings=TransferSettings(timeout_connect=5))

        self.win.multirun.connect = lambda target, timeout: backend(
            "wrong" if target.alias == "db-primary" else "pw").open_transport("t")
        self.backend = backend

    def dialog(self, aliases):
        from ssh_manager.gtkui.dialogs.ssh_keys_deploy import DeployKeyDialog

        hosts = self.win.multirun.host_by_key()
        keys = [k for k, h in hosts.items() if h.alias in aliases]
        dialog = DeployKeyDialog(None, self.win, key=self.key, host_keys=keys)
        dialog._verify = lambda key: (lambda target, timeout: self.backend("", str(key.path)).open_transport("v"))
        self.addCleanup(dialog.destroy)
        return dialog

    def finish(self, dialog):
        self.assertTrue(pump(30, until=lambda: dialog.run is not None and dialog.run.done and dialog._poll_id == 0))

    def test_install_verify_report_retry_and_set_identityfile(self) -> None:
        from ssh_manager import ssh_keys as K

        dialog = self.dialog({"web-01", "web-02", "db-primary"})
        self.assertEqual(dialog.hosts_label.get_text(), "3 hosts")
        asked = []
        dialog.confirm_run = lambda title, count: asked.append(title) or True
        dialog.start()
        self.finish(dialog)
        self.assertEqual(asked, ["Install key id_ed25519 on 3 hosts using their saved credentials?"])
        statuses = {r.alias: r.status for r in dialog.run.results()}
        self.assertEqual(statuses["db-primary"], K.AUTH_FAILED)
        self.assertEqual(sorted(statuses[a] for a in ("web-01", "web-02")), [K.ALREADY_PRESENT, K.INSTALLED])
        self.assertTrue(all(r.verified for r in dialog.run.results() if r.alias != "db-primary"))
        text = (self.srv / ".ssh" / "authorized_keys").read_text()
        self.assertEqual(text.count(self.key.public_line.split()[1]), 1)  # once, whatever the order
        report = dialog.copy_report()
        self.assertIn("db-primary\tAuth failed", report)
        self.assertTrue(dialog.retry_button.get_sensitive())
        retry = dialog.retry_failed()
        self.assertEqual([t.alias for t in retry.targets], ["db-primary"])
        self.finish(dialog)
        self.assertFalse(dialog.set_identity.get_active())  # off by default
        self.assertEqual(dialog.apply_identity(), 0)
        dialog.start()
        self.finish(dialog)
        dialog.set_identity.set_active(True)
        self.assertEqual(dialog.apply_identity(), 2)
        hosts = {h.alias: h for h in self.win.multirun.host_by_key().values()}
        self.assertEqual(hosts["web-01"].option("identityfile"), str(self.key.path))
        self.assertFalse(hosts["db-primary"].option("identityfile"))
        dialog.undo()
        hosts = {h.alias: h for h in self.win.multirun.host_by_key().values()}
        self.assertFalse(hosts["web-01"].option("identityfile"))

    def test_declining_the_confirmation_runs_nothing(self) -> None:
        dialog = self.dialog({"web-01"})
        dialog.confirm_run = lambda title, count: False
        self.assertIsNone(dialog.start())
        self.assertFalse((self.srv / ".ssh").exists())


if __name__ == "__main__":
    unittest.main()
