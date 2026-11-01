"""Phase F1: the host dialog's File transfer page and "Test connection"."""

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from ophion.config import HostEntry


def _display() -> bool:
    try:
        import gi

        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk  # type: ignore

        return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")) and Gtk.init_check(None)[0]
    except Exception:
        return False


def _pump_until(predicate, timeout=10.0) -> bool:
    from gi.repository import Gtk

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        if predicate():
            return True
        time.sleep(0.02)
    return False


@unittest.skipUnless(_display(), "needs a display")
class TransferPageTests(unittest.TestCase):
    def dialog(self, host=None, **kwargs):
        from ophion.gtkui.dialogs.host_dialog import HostDialog

        dialog = HostDialog(None, host, metadata=host.metadata if host else {}, **kwargs)
        self.addCleanup(dialog.destroy)
        return dialog

    @staticmethod
    def visible(row) -> bool:
        return row.control.get_visible()

    def rows(self, dialog, widget):
        return next(row for section in dialog.transfer_page.sections for row in section.rows if row.control is widget)

    def test_page_follows_the_protocol(self) -> None:
        dialog = self.dialog()
        self.assertFalse(dialog.layout.page_visible("transfer"))
        dialog.protocol_combo.set_active_id("ftp")
        page = dialog.transfer_page
        self.assertTrue(dialog.layout.page_visible("transfer"))
        self.assertFalse(self.visible(self.rows(dialog, page.tls_verify)))
        self.assertTrue(self.visible(self.rows(dialog, page.passive)))
        dialog.protocol_combo.set_active_id("ftps")
        self.assertTrue(self.visible(self.rows(dialog, page.tls_verify)))
        dialog.protocol_combo.set_active_id("sftp")
        self.assertFalse(self.visible(self.rows(dialog, page.passive)))
        self.assertFalse(self.visible(self.rows(dialog, page.encoding)))
        self.assertTrue(self.visible(self.rows(dialog, page.timeout_connect)))

    def test_implicit_ftps_defaults_to_port_990(self) -> None:
        dialog = self.dialog()
        dialog.protocol_combo.set_active_id("ftps")
        self.assertEqual(dialog.port_entry.get_placeholder_text(), "21")
        dialog.transfer_page.tls_mode.set_active_id("implicit")
        self.assertEqual(dialog.port_entry.get_placeholder_text(), "990")

    def test_insecure_needs_a_confirmation_each_time(self) -> None:
        dialog = self.dialog()
        dialog.protocol_combo.set_active_id("ftps")
        page = dialog.transfer_page
        with mock.patch("ophion.gtkui.dialogs.transfer_page.confirm", return_value=False) as ask:
            page.tls_verify.set_active_id("insecure")
        self.assertEqual(page.tls_verify.get_active_id(), "system")
        with mock.patch("ophion.gtkui.dialogs.transfer_page.confirm", return_value=True):
            page.tls_verify.set_active_id("insecure")
        self.assertEqual(page.tls_verify.get_active_id(), "insecure")
        ask.assert_called_once()

    def test_anonymous_needs_no_credential(self) -> None:
        from ophion.credentials import Credential

        dialog = self.dialog(credentials=[Credential("c1", "Ops", "ops")], selected_credential_id="c1")
        dialog.protocol_combo.set_active_id("ftp")
        dialog.transfer_page.anonymous.set_active(True)
        self.assertFalse(dialog._credential_combo.get_sensitive())
        self.assertIsNone(dialog.selected_credential_id())
        dialog.protocol_combo.set_active_id("sftp")  # no anonymous SFTP
        self.assertEqual(dialog.selected_credential_id(), "c1")

    def test_settings_round_trip_through_build_host(self) -> None:
        host = HostEntry("files", {"hostname": "10.0.0.20"}, protocol="ftps",
                         metadata={"transfer": {"v": 1, "tls_mode": "implicit", "max_connections": 5,
                                                "future_key": [1, 2]}})
        dialog = self.dialog(host)
        page = dialog.transfer_page
        self.assertEqual((page.tls_mode_id(), int(page.max_connections.get_value())), ("implicit", 5))
        page.passive.set_active(False)
        page.remote_dir.set_text("/srv/data")
        built = dialog.build_host("Acme", "")
        saved = built.metadata["transfer"]
        self.assertEqual((saved["passive"], saved["initial_remote_dir"], saved["future_key"], saved["tls_mode"]),
                         (False, "/srv/data", [1, 2], "implicit"))
        self.assertNotIn("passive", built.options)  # never an ssh option

    def test_custom_ca_without_a_file_is_invalid(self) -> None:
        dialog = self.dialog()
        dialog.alias_entry.set_text("files")
        dialog.hostname_entry.set_text("10.0.0.20")
        dialog.protocol_combo.set_active_id("ftps")
        dialog.transfer_page.tls_verify.set_active_id("custom_ca")
        self.assertFalse(dialog.validate())
        self.assertEqual(dialog.layout.current_page, "transfer")
        dialog.transfer_page.tls_verify.set_active_id("system")
        self.assertTrue(dialog.validate())


@unittest.skipUnless(_display(), "needs a display")
@unittest.skipIf(os.environ.get("SSHM_SKIP_INTEGRATION") == "1", "integration tests disabled")
class TestConnectionTests(unittest.TestCase):
    def setUp(self) -> None:
        from tests.fake_ftp import FakeFtpServer, make_certificate

        self.tmp = tempfile.TemporaryDirectory()
        mock.patch.dict(os.environ, {"XDG_DATA_HOME": self.tmp.name}).start()
        self.addCleanup(mock.patch.stopall)
        root = Path(self.tmp.name) / "root"
        root.mkdir()
        self.cert = make_certificate(Path(self.tmp.name))
        self.server = FakeFtpServer(root, tls_mode="explicit", certificate=self.cert, require_session_reuse=True)
        self.server.__enter__()
        self.addCleanup(self.server.__exit__, None, None, None)
        self.addCleanup(self.tmp.cleanup)

    def dialog(self, password="s3cret pass"):
        from ophion.gtkui.dialogs.host_dialog import HostDialog
        from ophion.transfer.factory import make_backend

        dialog = HostDialog(None, None, transfer_backend_factory=lambda host, _cred: make_backend(
            host, "alice", password))
        self.addCleanup(dialog.destroy)
        dialog.alias_entry.set_text("files")
        dialog.hostname_entry.set_text("127.0.0.1")
        dialog.protocol_combo.set_active_id("ftps")
        dialog.port_entry.set_text(str(self.server.port))
        return dialog

    def run_test(self, dialog):
        page = dialog.transfer_page
        page.last_test = None
        dialog.transfer_page.test_button.clicked()
        self.assertTrue(_pump_until(lambda: page.last_test is not None))
        return page.last_test

    def test_untrusted_certificate_is_reported_and_never_pinned(self) -> None:
        from ophion.transfer.pins import PinStore

        ok, text = self.run_test(self.dialog())
        self.assertFalse(ok)
        self.assertIn("SHA-256:", text)
        self.assertIn("CN=localhost", text)
        self.assertFalse(PinStore().path.exists())  # a test never saves anything

    def test_success_reports_banner_tls_and_features(self) -> None:
        dialog = self.dialog()
        dialog.transfer_page.tls_verify.set_active_id("custom_ca")
        dialog.transfer_page.ca_file.set_filename(str(self.cert[0]))
        ok, text = self.run_test(dialog)
        self.assertTrue(ok, text)
        for expected in ("Connected.", "Acme test FTP", "TLS: TLSv1.", "Features:", "Connected in"):
            self.assertIn(expected, text)
        self.assertEqual(self.server.reuse_refusals, 0)

    def test_wrong_password_reports_without_the_password(self) -> None:
        dialog = self.dialog(password="hunter2-wrong")
        dialog.transfer_page.tls_verify.set_active_id("custom_ca")
        dialog.transfer_page.ca_file.set_filename(str(self.cert[0]))
        ok, text = self.run_test(dialog)
        self.assertFalse(ok)
        self.assertIn("rejected the user name or password", text)
        self.assertNotIn("hunter2", text)


if __name__ == "__main__":
    unittest.main()
