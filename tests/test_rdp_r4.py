"""RDP integrations: Quick Connect and command line, .rdp import/export in the window, Wake and Connect."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ssh_manager.config import HostEntry
from ssh_manager.gtk_main import parse_args, startup_target
from ssh_manager.protocols.urls import cli_rdp_target, parse_connection_url
from ssh_manager.rdp.rdpfile import encode_rdp, import_rdp


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


class UrlAndCliTests(unittest.TestCase):
    def test_rdp_urls(self) -> None:
        url, reason = parse_connection_url("rdp://CORP%5Calice@10.0.0.50:3390/ignored")
        self.assertEqual((url.protocol, url.user, url.port, url.path, reason), ("rdp", "CORP\\alice", 3390, "", ""))
        self.assertEqual(parse_connection_url("rdp://h")[0].effective_port, 3389)
        self.assertIn("password", parse_connection_url("rdp://alice:pw@h")[1])
        self.assertIsNone(parse_connection_url("sftp://CORP%5Calice@h")[0])  # backslashes only for RDP users

    def test_cli(self) -> None:
        url, _reason = cli_rdp_target("10.0.0.50:3390", "alice", "CORP")
        self.assertEqual((url.user, url.port), ("CORP\\alice", 3390))
        self.assertEqual(cli_rdp_target("h", "alice@corp.example", "CORP")[0].user, "alice@corp.example")
        self.assertIsNone(cli_rdp_target("h", "u:secret")[0])
        self.assertIsNone(cli_rdp_target("ftp://h")[0])
        args, extras = parse_args(["--rdp", "win-01.acme.test", "--user", "bob"])
        host, error = startup_target(args, extras)
        self.assertEqual((host.protocol, host.option("hostname"), host.option("user"), host.id, error),
                         ("rdp", "win-01.acme.test", "bob", "", ""))
        args, extras = parse_args(["rdp://bob@10.0.0.51", "--some-gtk-option"])
        self.assertEqual(startup_target(args, extras)[0].option("hostname"), "10.0.0.51")
        self.assertEqual(startup_target(*parse_args([])), (None, ""))
        self.assertTrue(startup_target(*parse_args(["--rdp=-oProxy"]))[1])

    def test_main_refuses_bad_targets_before_starting_gtk(self) -> None:
        from ssh_manager import gtk_main

        with mock.patch.object(gtk_main, "SSHManagerApplication") as app:
            self.assertEqual(gtk_main.main(["--rdp", "bad host"]), 2)
        app.assert_not_called()


class WakeOnLanSettingsTests(unittest.TestCase):
    def test_problems(self) -> None:
        from ssh_manager.rdp.settings import RdpSettings

        self.assertEqual(RdpSettings().problems(), [])  # off by default
        self.assertEqual(RdpSettings(wol_mac="AA:BB:CC:DD:EE:01", wol_broadcast="10.0.0.255").problems(), [])
        self.assertIn("MAC address", " ".join(RdpSettings(wol_mac="AA:BB").problems()))
        self.assertIn("broadcast", " ".join(RdpSettings(wol_mac="AA:BB:CC:DD:EE:01",
                                                        wol_broadcast="93.184.216.34").problems()))
        loaded, warnings = RdpSettings.from_json({"v": 1, "wol_wait_s": 99999})
        self.assertEqual(loaded.wol_wait_s, 30)  # out of range: the default, with a warning
        self.assertIn("wol_wait_s", " ".join(warnings))


@unittest.skipUnless(_display(), "needs a display")
class WakeOnLanDialogTests(unittest.TestCase):
    def test_fields_round_trip_and_validate(self) -> None:
        from ssh_manager.gtkui.dialogs.host_dialog import HostDialog

        host = HostEntry("win", {"hostname": "10.0.0.80"}, protocol="rdp", metadata={"rdp": {"v": 1}})
        dialog = HostDialog(None, host)
        self.addCleanup(dialog.destroy)
        pages = dialog.rdp_pages
        pages.wol_mac.set_text("aa-bb-cc-dd-ee-80")
        pages.wol_broadcast.set_text("10.0.0.255")
        pages.wol_wait.set_value(5)
        self.assertTrue(dialog.validate())
        data = dialog.build_host("Acme", "").metadata["rdp"]
        self.assertEqual((data["wol_mac"], data["wol_broadcast"], data["wol_wait_s"]),
                         ("aa-bb-cc-dd-ee-80", "10.0.0.255", 5))
        pages.wol_mac.set_text("nonsense")
        self.assertFalse(dialog.validate())
        self.assertIn("MAC address", dialog._rdp_error.get_text())


@unittest.skipUnless(_display(), "needs a display")
class QuickConnectTests(unittest.TestCase):
    def test_quick_connect_rdp(self) -> None:
        from ssh_manager.gtkui.controllers.session_controller import quick_connect_host

        host = quick_connect_host("rdp://CORP%5Calice@10.0.0.50")
        self.assertEqual((host.protocol, host.option("user"), host.metadata), ("rdp", "CORP\\alice", {"rdp": {"v": 1}}))
        by_port = quick_connect_host("alice@10.0.0.50:3389")
        self.assertEqual((by_port.protocol, by_port.option("port")), ("rdp", None))
        self.assertEqual(quick_connect_host("alice@10.0.0.50:22").protocol, "ssh")


@unittest.skipUnless(_display(), "needs a display")
class WindowTests(unittest.TestCase):
    def setUp(self) -> None:
        from tests.gtk_window_fixture import WindowSandbox

        self.sandbox = WindowSandbox(store=True)
        self.addCleanup(self.sandbox.close)
        self.win = self.sandbox.window
        self.win.sessions.rdp_discovery.join(10)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.notes = []
        patcher = mock.patch.object(self.win, "_notify", side_effect=lambda msg, *a, **k: self.notes.append(msg))
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, name: str, text: str) -> str:
        path = Path(self.tmp.name) / name
        path.write_bytes(encode_rdp(text))
        return str(path)

    def test_import_files_into_a_group(self) -> None:
        good = self.write("Win 01.rdp", "full address:s:10.0.0.70\r\nusername:s:CORP\\alice\r\nsession bpp:i:16\r\n"
                                        "password 51:b:01000000AA\r\n")
        twin = self.write("Win 01 copy.rdp", "full address:s:10.0.0.71\r\n")
        bad = self.write("broken.rdp", "username:s:alice\r\n")
        with mock.patch.object(self.win.hosts, "pick_group", return_value=("staging", "hosts.conf", False)):
            report = self.win.rdp_files.import_files([good, twin, bad])
        hosts = {h.alias: h for h in self.win.manager.tree["staging"]["hosts.conf"].hosts}
        self.assertEqual(hosts["Win 01"].protocol, "rdp")
        self.assertEqual(hosts["Win 01"].metadata["rdp"]["color_depth"], 16)
        self.assertEqual(hosts["Win 01"].metadata["imported_from"], "rdp-file")
        self.assertIn("Win 01 copy", hosts)
        text = "\n".join(report)
        self.assertIn("broken.rdp", text)
        self.assertIn("password 51", text)
        self.assertNotIn("01000000AA", text + repr(hosts["Win 01"].metadata))
        self.assertIn("Imported 2 RDP hosts.", self.notes)

    def test_export_selected(self) -> None:
        host = HostEntry("win-02", {"hostname": "10.0.0.72", "user": "bob", "port": "3390"}, protocol="rdp",
                         metadata={"rdp": {"v": 1, "domain": "CORP", "color_depth": 24}})
        self.win.manager.add_host("staging", "hosts.conf", host)
        self.win.tree.rebuild(refresh=True)
        stored = self.win.tree.find_host_by_alias("win-02")
        self.win.tree.select_host(stored.parent_group, stored.child_group, stored.alias)
        target = Path(self.tmp.name) / "out.rdp"
        self.assertEqual(self.win.rdp_files.export_selected(str(target)), str(target))
        back = import_rdp(target.read_bytes())
        self.assertEqual((back.hostname, back.options["port"], back.options["user"], back.settings.domain,
                          back.settings.color_depth), ("10.0.0.72", "3390", "bob", "CORP", 24))
        labels = [item.label for section in self.win.tree.context_menu_spec() for item in section.items
                  if getattr(item, "visible", True)]
        self.assertIn("Export as ._rdp File…", labels)
        self.assertNotIn("_Wake and Connect", labels)  # no MAC address

    def test_wake_and_connect(self) -> None:
        host = HostEntry("win-03", {"hostname": "10.0.0.73"}, protocol="rdp",
                         metadata={"rdp": {"v": 1, "wol_mac": "AA:BB:CC:DD:EE:03", "wol_broadcast": "10.0.0.255",
                                           "wol_wait_s": 0}})
        self.win.manager.add_host("staging", "hosts.conf", host)
        self.win.tree.rebuild(refresh=True)
        stored = self.win.tree.find_host_by_alias("win-03")
        self.win.tree.select_host(stored.parent_group, stored.child_group, stored.alias)
        labels = [item.label for section in self.win.tree.context_menu_spec() for item in section.items
                  if getattr(item, "visible", True)]
        self.assertIn("_Wake and Connect", labels)
        with mock.patch("ssh_manager.gtkui.controllers.rdp_files.wake") as wake, \
                mock.patch("ssh_manager.gtkui.controllers.rdp_files.GLib.timeout_add_seconds") as later:
            self.assertTrue(self.win.rdp_files.wake_and_connect())
        wake.assert_called_once_with("AA:BB:CC:DD:EE:03", "10.0.0.255")
        self.assertEqual(later.call_args.args[0], 0)
        with mock.patch.object(self.win.sessions, "open_hosts") as open_hosts:
            self.assertFalse(later.call_args.args[1](*later.call_args.args[2:]))  # runs once
        self.assertEqual([h.alias for h in open_hosts.call_args.args[0]], ["win-03"])
        self.assertTrue(any("Wake-up packet sent" in note for note in self.notes))


if __name__ == "__main__":
    unittest.main()
