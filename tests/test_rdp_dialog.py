"""RDP in the host dialog, Preferences → Remote Desktop, Diagnostics and the window (skipped without a display)."""

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from ssh_manager.config import HostEntry
from ssh_manager.rdp import engines
from ssh_manager.rdp.engines import ClientBinary, EngineReport, Platform, build_engines, parse_help_options
from ssh_manager.rdp.settings import SharedFolder

FIXTURES = Path(__file__).parent / "fixtures" / "freerdp"


def _display() -> bool:
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return False
    try:
        import gi

        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk  # type: ignore

        return Gtk.init_check(None)[0]
    except Exception:
        return False


def pump_until(predicate, timeout=5.0) -> bool:
    from gi.repository import GLib  # type: ignore

    deadline = time.monotonic() + timeout
    context = GLib.MainContext.default()
    while time.monotonic() < deadline:
        while context.pending():
            context.iteration(False)
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def report(options=None, platform=Platform("wayland", False, "", "", False, "debian")) -> EngineReport:
    help_options = parse_help_options((FIXTURES / "help-3.15.txt").read_text())
    if options is not None:
        help_options = frozenset(options)
    clients = [ClientBinary("xfreerdp3", "/usr/bin/xfreerdp3", (3, 15, 0), "3.15.0", help_options)]
    built = build_engines(clients, platform)
    chosen, note = engines.choose_engine(built)
    return EngineReport(platform, clients, built, chosen, note)


def rdp_host(**rdp) -> HostEntry:
    return HostEntry("win-01", {"hostname": "10.0.0.50", "user": "CORP\\alice"}, protocol="rdp",
                     metadata={"rdp": {"v": 1, **rdp}})


@unittest.skipUnless(_display(), "needs a display")
class HostDialogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.addCleanup(engines.store_report, None)
        engines.store_report(None)

    def dialog(self, host=None, **kwargs):
        from ssh_manager.gtkui.dialogs.host_dialog import HostDialog

        dialog = HostDialog(None, host, **kwargs)
        self.addCleanup(dialog.destroy)
        return dialog

    def visible(self, dialog):
        return [p for p in ("connection", "transfer", "ssh_options", "rdp", "rdp_display", "rdp_devices",
                            "rdp_connection") if dialog.layout.page_visible(p)]

    def test_pages_follow_the_protocol(self) -> None:
        dialog = self.dialog()
        self.assertEqual(self.visible(dialog), ["connection", "ssh_options"])
        dialog.protocol_combo.set_active_id("rdp")
        self.assertEqual(self.visible(dialog), ["rdp", "rdp_display", "rdp_devices", "rdp_connection"])
        self.assertEqual(dialog.port_entry.get_placeholder_text(), "3389")

    def test_round_trip_keeps_unknown_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dialog = self.dialog(rdp_host(domain="CORP", future_key=[1], drives=[{"name": "work", "path": tmp}],
                                          gateway={"host": "gw.acme.test"}),
                                 rdp_ssh_hosts=[("ssh-1", "bastion (10.0.0.1)")])
            pages = dialog.rdp_pages
            pages.via_ssh.set_active_id("ssh-1")
            pages.keyboard.set_active_id("fa-IR")
            pages.add_drive(tmp)  # a second share of the same folder gets a unique name
            built = dialog.build_host("Acme", "")
            data = built.metadata["rdp"]
            self.assertEqual((built.protocol, data["domain"], data["future_key"], data["via_ssh"]),
                             ("rdp", "CORP", [1], "ssh-1"))
            self.assertEqual(data["keyboard_layout"], "fa-IR")
            self.assertEqual(data["gateway"]["host"], "gw.acme.test")
            self.assertEqual(len(data["drives"]), 2)
            self.assertEqual(len({d["name"] for d in data["drives"]}), 2)
            self.assertFalse(dialog.validate())  # a gateway and an SSH tunnel together are refused
            self.assertIn("either an RD Gateway or an SSH tunnel", dialog._rdp_error.get_text())
            pages.via_ssh.set_active_id("")
            self.assertTrue(dialog.validate())

    def test_validation_jumps_to_the_rdp_page_and_hides_secrets(self) -> None:
        dialog = self.dialog(rdp_host())
        dialog.rdp_pages.extra_args.set_text("/p:hunter2")
        self.assertFalse(dialog.validate())
        self.assertEqual(dialog.layout.current_page, "rdp")
        self.assertNotIn("hunter2", dialog._rdp_error.get_text())
        dialog.rdp_pages.extra_args.set_text("")
        dialog.rdp_pages.size_mode.set_active_id("fixed")
        dialog.rdp_pages.width.set_value(100)  # the spin clamps to 200
        self.assertTrue(dialog.validate())
        dialog.rdp_pages.drive_store.append(["bad name!", "relative/path", False, False])
        self.assertFalse(dialog.validate())

    def test_presets_and_custom(self) -> None:
        pages = self.dialog(rdp_host()).rdp_pages
        pages.preset.set_active_id("modem")
        self.assertFalse(pages.toggles["wallpaper"].get_active())
        pages.toggles["wallpaper"].set_active(True)
        self.assertEqual(pages.preset.get_active_id(), "custom")
        pages.preset.set_active_id("lan")
        self.assertTrue(all(check.get_active() for name, check in pages.toggles.items() if name != "compression"))

    def test_risky_choices_ask_first(self) -> None:
        pages = self.dialog(rdp_host()).rdp_pages
        with mock.patch("ssh_manager.gtkui.dialogs.rdp_page.confirm", return_value=False) as ask:
            pages.cert_policy.set_active_id("ignore")
            pages.security.set_active_id("rdp")
            pages.home_drive.set_active(True)
            pages.extra_unchecked.set_active(True)
        self.assertEqual(ask.call_count, 4)
        self.assertEqual((pages.cert_policy.get_active_id(), pages.security.get_active_id()), ("ask", "auto"))
        self.assertFalse(pages.home_drive.get_active() or pages.extra_unchecked.get_active())
        with mock.patch("ssh_manager.gtkui.dialogs.rdp_page.confirm", return_value=True):
            pages.cert_policy.set_active_id("ignore")
        self.assertEqual(pages.settings().cert_policy, "ignore")

    def test_engine_gating(self) -> None:
        engines.store_report(report(options=set(parse_help_options((FIXTURES / "help-3.15.txt").read_text()))
                                    - {"usb", "drive", "gateway"}))
        dialog = self.dialog(rdp_host())
        pages = dialog.rdp_pages
        self.assertFalse(pages.usb.get_sensitive())
        self.assertIn("not available", pages.usb.get_tooltip_text())
        self.assertFalse(pages.gw_host.get_sensitive())
        self.assertFalse(pages.add_drive_button.get_parent().get_parent().get_sensitive())
        self.assertTrue(pages.clipboard.get_sensitive())
        self.assertIn("External window", pages.engine_note.get_text())
        engines.store_report(report())
        dialog._apply_rdp_engine()
        self.assertTrue(pages.usb.get_sensitive())

    def test_no_engine_still_saves(self) -> None:
        engines.store_report(EngineReport(Platform(), [], build_engines([], Platform()), None, "none"))
        dialog = self.dialog(rdp_host())
        self.assertIn("No FreeRDP client", dialog.rdp_pages.engine_note.get_text())
        self.assertTrue(dialog.validate())

    def test_test_connection_uses_the_probe(self) -> None:
        if os.environ.get("SSHM_SKIP_INTEGRATION") == "1":
            self.skipTest("integration tests skipped")
        from ssh_manager.rdp.probe import PROTOCOL_HYBRID
        from tests.fake_rdp import FakeRdpServer, neg_rsp, write_certificate

        with tempfile.TemporaryDirectory() as tmp:
            server = FakeRdpServer([neg_rsp(PROTOCOL_HYBRID)], write_certificate(Path(tmp)))
            self.addCleanup(server.close)
            host = rdp_host()
            host.options.update(hostname="127.0.0.1", port=str(server.port))
            pages = self.dialog(host).rdp_pages
            pages.test_connection()
            self.assertTrue(pump_until(lambda: pages.last_test is not None))
        ok, text = pages.last_test
        self.assertTrue(ok, text)
        self.assertIn("Network Level Authentication", text)
        self.assertIn("SHA-256", text)
        empty = self.dialog(HostEntry("x", {}, protocol="rdp")).rdp_pages
        empty.test_connection()
        self.assertFalse(empty.last_test[0])

    def test_sensitive_folders_and_restricted_admin_ask_first(self) -> None:
        pages = self.dialog(rdp_host()).rdp_pages
        with tempfile.TemporaryDirectory() as home:
            secret = Path(home) / ".ssh"
            secret.mkdir()
            with mock.patch.dict(os.environ, {"HOME": home}):
                with mock.patch("ssh_manager.gtkui.dialogs.rdp_page.confirm", return_value=False) as ask:
                    pages.add_drive(str(secret))
                self.assertEqual(len(pages.drive_store), 0)
                self.assertIn("~/.ssh", ask.call_args.args[1])
                with mock.patch("ssh_manager.gtkui.dialogs.rdp_page.confirm", return_value=True):
                    pages.add_drive(str(secret))
                folder = pages.settings().drives[0]
                self.assertTrue(folder.allow_sensitive)
                self.assertEqual(pages.problems(), [])
        with mock.patch("ssh_manager.gtkui.dialogs.rdp_page.confirm", return_value=False) as ask:
            pages.restricted_admin.set_active(True)
        ask.assert_called_once()
        self.assertFalse(pages.restricted_admin.get_active())

    def test_gateway_credential_and_vm_id(self) -> None:
        from ssh_manager.credentials import Credential

        credentials = [Credential("c-gw", "Gateway", "bob")]
        dialog = self.dialog(rdp_host(gateway={"host": "gw.acme.test", "use_same_credentials": False}),
                             credentials=credentials)
        pages = dialog.rdp_pages
        self.assertTrue(pages.gw_credential.get_sensitive())
        pages.gw_credential.set_active_id("c-gw")
        pages.vm_id.set_text("1f3e7f8c-0d4b-4b63-9e57-2f0a5d6b8c11")
        data = dialog.build_host("Acme", "").metadata["rdp"]
        self.assertEqual((data["gateway"]["credential_id"], data["hyperv_vm_id"]),
                         ("c-gw", "1f3e7f8c-0d4b-4b63-9e57-2f0a5d6b8c11"))
        pages.gw_same.set_active(True)
        self.assertFalse(pages.gw_credential.get_sensitive())

    def test_switching_away_from_rdp_warns(self) -> None:
        dialog = self.dialog(rdp_host(domain="CORP"))
        with mock.patch("ssh_manager.gtkui.dialogs.host_dialog.confirm", return_value=False) as ask:
            dialog.protocol_combo.set_active_id("ssh")
        self.assertIn("remote desktop settings", ask.call_args.args[2])
        self.assertEqual(dialog.selected_protocol(), "rdp")


@unittest.skipUnless(_display(), "needs a display")
class PreferencesTests(unittest.TestCase):
    def test_remote_desktop_page_and_diagnostics(self) -> None:
        from ssh_manager.gtkui.dialogs.preferences import PreferencesDialog
        from ssh_manager.settings import AppSettings

        with tempfile.TemporaryDirectory() as tmp:
            settings = AppSettings(config_path=Path(tmp) / "settings.json")
            settings.remote_desktop.max_sessions = 4
            dialog = PreferencesDialog(None, False, 1.0, 11, settings)
            self.addCleanup(dialog.destroy)
            self.assertIn("remote_desktop", dialog.layout._pages)
            dialog._rdp_engine_combo.set_active_id("external")
            dialog._rdp_binary_entry.set_text(" /opt/freerdp/bin/xfreerdp3 ")
            values = dialog.values().remote_desktop
            self.assertEqual((values.preferred_engine, values.binary_path, values.max_sessions),
                             ("external", "/opt/freerdp/bin/xfreerdp3", 4))
            calls = []
            with mock.patch("ssh_manager.gtkui.dialogs.rdp_diagnostics.discover_async",
                            side_effect=lambda cb, **kw: (calls.append(kw), cb(report()))):
                diag = dialog.open_rdp_diagnostics()
            self.addCleanup(diag.destroy)
            self.assertEqual(calls[0], {"preference": "external", "override": "/opt/freerdp/bin/xfreerdp3"})
            self.assertTrue(pump_until(lambda: diag.report is not None))
            self.assertIn("External window", diag.summary.get_text())
            with mock.patch("gi.repository.Gtk.Clipboard.get") as clipboard:
                copied = diag.copy()
            clipboard.return_value.set_text.assert_called_once()
            self.assertIn("FreeRDP 3.15.0", copied)

    def test_settings_round_trip(self) -> None:
        from ssh_manager.settings import AppSettings, RemoteDesktopPreferences

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            settings = AppSettings(config_path=path)
            settings.remote_desktop = RemoteDesktopPreferences("embedded", "/usr/bin/xfreerdp", 3)
            settings.save()
            self.assertEqual(AppSettings.load(path).remote_desktop, settings.remote_desktop)
        junk = RemoteDesktopPreferences.from_dict({"preferred_engine": "magic", "binary_path": "a\nb",
                                                   "max_sessions": 999})
        self.assertEqual((junk.preferred_engine, junk.binary_path, junk.max_sessions), ("auto", "", 64))


@unittest.skipUnless(_display(), "needs a display")
class WindowTests(unittest.TestCase):
    def setUp(self) -> None:
        from tests.gtk_window_fixture import WindowSandbox

        self.sandbox = WindowSandbox(store=True)
        self.addCleanup(self.sandbox.close)
        self.win = self.sandbox.window
        self.win.sessions.rdp_discovery.join(10)  # its own discovery must finish before a test sets a report
        self.addCleanup(engines.store_report, None)
        self.notes = []
        patcher = mock.patch.object(self.win, "_notify", side_effect=lambda msg, *a, **k: self.notes.append(msg))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_opening_without_freerdp_explains(self) -> None:
        host = rdp_host()  # opening with a client is covered by tests/test_rdp_session.py (no network there)
        engines.store_report(EngineReport(Platform(distro="ubuntu"), [], build_engines([], Platform()), None))
        self.win.sessions.open_hosts([host])
        self.assertTrue(any("apt install" in note for note in self.notes), self.notes)
        self.assertEqual(self.win.terminal_tabs.tab_count(), 0)

    def test_tree_icon_and_tunnel_hosts(self) -> None:
        from ssh_manager.gtkui.constants import RowKind
        from ssh_manager.gtkui.tree_icons import tree_icon_for

        self.assertEqual(tree_icon_for(RowKind.HOST, protocol="rdp").name, "sshm-desktop")
        self.win.manager.add_host("staging", "hosts.conf", rdp_host())
        tunnel = self.win.hosts.tunnel_hosts()
        self.assertTrue(tunnel)
        self.assertNotIn("win-01", " ".join(label for _id, label in tunnel))  # only SSH hosts

    def test_shared_folder_name_generation(self) -> None:
        from ssh_manager.gtkui.dialogs.host_dialog import HostDialog

        dialog = HostDialog(None, rdp_host())
        self.addCleanup(dialog.destroy)
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "My Files (2024)"
            folder.mkdir()
            dialog.rdp_pages.add_drive(str(folder))
            name = dialog.rdp_pages.settings().drives[0].name
        self.assertRegex(name, r"^[A-Za-z0-9._-]+$")
        self.assertIsInstance(SharedFolder(name, str(folder)), SharedFolder)


if __name__ == "__main__":
    unittest.main()
