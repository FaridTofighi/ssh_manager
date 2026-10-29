"""Phase F0: the protocol registry, schema v2 and everything that used to assume SSH."""

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ssh_manager import protocols
from ssh_manager.config import GroupConfig, HostEntry
from ssh_manager.host_utils import host_connection_params
from ssh_manager.inventory.manager import StoreConfigManager
from ssh_manager.inventory.openssh import non_ssh_hosts, render_export
from ssh_manager.inventory.store import (
    _SCHEMA_V1,
    SCHEMA_VERSION,
    InventoryStore,
    InventoryTooNew,
    NewHost,
    ValidationError,
    clean_protocol,
)
from ssh_manager.ssh_command import build_ssh_command


def ftp_host(alias="files", protocol="ftp", **options):
    opts = {"hostname": "10.0.0.20", **options}
    return HostEntry(alias, opts, protocol=protocol, parent_group="Acme", child_group="transfer")


class RegistryTests(unittest.TestCase):
    def test_built_in_kinds(self) -> None:
        self.assertEqual([k.id for k in protocols.all_kinds()][:4], ["ssh", "ftp", "ftps", "sftp"])
        self.assertEqual({k.id: k.default_port for k in protocols.all_kinds()}, {"ssh": 22, "ftp": 21, "ftps": 21,
                                                                                 "sftp": 22, "rdp": 3389})
        self.assertTrue(protocols.SSH.terminal and not protocols.SFTP.terminal)
        self.assertFalse(protocols.FTP.encrypted)
        self.assertEqual(protocols.FTP.icon, "sshm-transfer")

    def test_host_helpers(self) -> None:
        self.assertEqual(protocols.protocol_of(HostEntry("a")), "ssh")
        self.assertTrue(protocols.is_ssh(HostEntry("a")))
        self.assertEqual(protocols.badge_for(HostEntry("a")), "")  # SSH stays unmarked
        self.assertEqual(protocols.badge_for(ftp_host(protocol="sftp")), "SFTP")
        future = ftp_host(protocol="vnc")  # not built in (RDP is, since the RDP work)
        self.assertIsNone(protocols.kind_for(future))
        self.assertEqual(protocols.badge_for(future), "VNC")
        self.assertFalse(protocols.can_open(future))

    def test_validate(self) -> None:
        self.assertEqual(protocols.FTP.validate(ftp_host()), [])
        self.assertEqual(protocols.FTP.validate(HostEntry("x", {}, protocol="ftp")), ["FTP (unencrypted) needs a host name."])
        self.assertIn("between 1 and 65535", protocols.FTP.validate(ftp_host(port="70000"))[0])
        self.assertEqual(protocols.SSH.validate(HostEntry("alias-only")), [])

    def test_openers(self) -> None:
        # A protocol of its own: ftp / ftps / sftp have the file browser's opener (phase F2).
        kind = protocols.register(protocols.ConnectionKind("demo", "Demo", "DEMO", "sshm-monitor", 7))
        self.addCleanup(protocols._KINDS.pop, "demo", None)
        host = ftp_host(protocol="demo")
        self.assertIsNone(protocols.opener_for("demo"))
        self.assertFalse(protocols.can_open(host))
        protocols.set_opener("demo", lambda win, h: h.alias)
        self.addCleanup(protocols.set_opener, "demo", None)
        self.assertTrue(protocols.can_open(host))
        self.assertEqual(protocols.opener_for("DEMO")(None, host), "files")
        self.assertIs(protocols.get("demo"), kind)

    def test_a_new_protocol_needs_only_registration(self) -> None:
        kind = protocols.register(protocols.ConnectionKind("vnc", "VNC", "VNC", "sshm-monitor", 5900))
        self.addCleanup(protocols._KINDS.pop, "vnc", None)
        self.assertIs(protocols.get("vnc"), kind)
        self.assertEqual(protocols.default_port("vnc"), 5900)


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "inventory.db"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def make_v1(self) -> None:
        conn = sqlite3.connect(self.path)
        for statement in (part.strip() for part in _SCHEMA_V1.split(";")):
            if statement:
                conn.execute(statement)
        conn.execute("INSERT INTO groups VALUES('g1', NULL, 'Acme', 'acme', 0, '', 0, 0)")
        conn.execute("INSERT INTO hosts(id, group_id, alias, alias_key, hostname, port, user, created_at, updated_at) "
                     "VALUES('h1', 'g1', 'web', 'web', '10.0.0.5', 22, 'alice', 0, 0)")
        conn.execute("PRAGMA user_version=1")
        conn.commit()
        conn.close()

    def test_migration_v1_to_v2_backs_up_and_defaults_to_ssh(self) -> None:
        self.make_v1()
        store = InventoryStore(self.path)
        self.addCleanup(store.close)
        self.assertEqual((SCHEMA_VERSION, store.schema_version), (2, 2))
        self.assertEqual(store.get_host("h1").protocol, "ssh")
        self.assertTrue(any("schema-v1" in p.name for p in store.list_backups()))
        indexes = {row[1] for row in store.conn.execute("PRAGMA index_list(hosts)")}
        self.assertIn("hosts_protocol", indexes)

    def test_too_new_is_still_refused(self) -> None:
        conn = sqlite3.connect(self.path)
        conn.execute("PRAGMA user_version=3")
        conn.close()
        with self.assertRaises(InventoryTooNew):
            InventoryStore(self.path)

    def test_protocol_is_stored_updated_duplicated_and_moved(self) -> None:
        store = InventoryStore(self.path)
        self.addCleanup(store.close)
        acme = store.create_group(None, "Acme")
        other = store.create_group(None, "Other")
        row = store.add_host(NewHost(acme.id, "files", "10.0.0.20", protocol="FTP"))
        self.assertEqual(store.get_host(row.id).protocol, "ftp")
        store.update_host(row.id, protocol="sftp")
        self.assertEqual(store.get_host(row.id).protocol, "sftp")
        store.update_host(row.id, alias="files-2")  # protocol unchanged when not given
        self.assertEqual(store.get_host(row.id).protocol, "sftp")
        _copy, mapping = store.duplicate_group(acme.id, None, "Acme copy")
        self.assertEqual(store.get_host(mapping[row.id]).protocol, "sftp")
        store.move_hosts([row.id], other.id)
        self.assertEqual(store.get_host(row.id).protocol, "sftp")
        self.assertEqual(store.add_host(NewHost(acme.id, "plain", "h")).protocol, "ssh")

    def test_malformed_protocol_is_rejected(self) -> None:
        self.assertEqual((clean_protocol(None), clean_protocol("")), ("ssh", "ssh"))
        self.assertEqual(clean_protocol(" RDP "), "rdp")  # unknown ids are kept
        for bad in ("f t p", "ftp;drop", "x" * 17):
            with self.assertRaises(ValidationError):
                clean_protocol(bad)

    def test_manager_carries_protocol_through_hostentry(self) -> None:
        store = InventoryStore(self.path)
        self.addCleanup(store.close)
        manager = StoreConfigManager(store)
        manager.create_parent_group("Acme")
        host = HostEntry("files", {"hostname": "10.0.0.20", "port": "990"}, protocol="ftps")
        self.assertTrue(manager.add_host("Acme", "", host)[0])
        loaded = manager.tree["Acme"][""].hosts[0]
        self.assertEqual((loaded.protocol, loaded.options["port"]), ("ftps", "990"))
        loaded.protocol = "sftp"
        self.assertTrue(manager.update_host("Acme", "", "files", loaded)[0])
        self.assertEqual(manager.tree["Acme"][""].hosts[0].protocol, "sftp")
        self.assertNotIn("protocol", manager.tree["Acme"][""].hosts[0].options)  # never an ssh -o option


class SshAssumptionTests(unittest.TestCase):
    def test_openssh_export_skips_non_ssh_hosts(self) -> None:
        tree = {"Acme": {"": GroupConfig("Acme", "", Path(), [HostEntry("web", {"hostname": "10.0.0.5"}),
                                                              ftp_host("files")])}}
        text = render_export(tree)
        self.assertIn("Host web", text)
        self.assertNotIn("files", text)
        self.assertEqual(non_ssh_hosts(tree), ["Acme · files"])

    def test_build_ssh_command_refuses_other_protocols(self) -> None:
        with self.assertRaises(ValueError):
            build_ssh_command(ftp_host())
        self.assertEqual(build_ssh_command(HostEntry("web", {"hostname": "h"}))[0], "ssh")

    def test_default_port_follows_the_protocol(self) -> None:
        self.assertEqual(host_connection_params(ftp_host(user="bob"))[1], 21)
        self.assertEqual(host_connection_params(ftp_host(protocol="sftp"))[1], 22)
        self.assertEqual(host_connection_params(ftp_host(port="2121"))[1], 2121)
        self.assertEqual(host_connection_params(HostEntry("web", {"hostname": "h"}))[1], 22)


class FilterAndTextTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            from ssh_manager.gtkui.controllers import tree_controller  # noqa: F401
        except Exception as exc:  # pragma: no cover - no GTK
            raise unittest.SkipTest(str(exc))

    def test_proto_filter_terms(self) -> None:
        from ssh_manager.gtkui.controllers.tree_controller import filter_host, parse_filter

        terms, error = parse_filter("proto:ftp")
        self.assertEqual((terms, error), ([("proto", "ftp")], ""))
        self.assertTrue(filter_host(ftp_host(), terms))
        self.assertFalse(filter_host(ftp_host(protocol="sftp"), terms))
        self.assertTrue(filter_host(HostEntry("web"), parse_filter("protocol:ssh")[0]))

    def test_tooltip_and_search_text(self) -> None:
        from ssh_manager.gtkui.controllers.tree_controller import host_tooltip, search_text_for_host

        tooltip = host_tooltip(ftp_host(protocol="sftp", port="22"), "bob", "unknown")
        self.assertIn("bob@10.0.0.20\n", tooltip)  # default port not repeated
        self.assertIn("Protocol: SFTP (SSH file transfer)", tooltip)
        self.assertNotIn("Protocol", host_tooltip(HostEntry("web", {"hostname": "h"}), "", "unknown"))
        self.assertIn("ftps", search_text_for_host(ftp_host(protocol="ftps")))

    def test_copy_text_is_a_url_without_secrets(self) -> None:
        from ssh_manager.gtkui.host_facts import protocol_text, ssh_command_text

        self.assertEqual(ssh_command_text(ftp_host(), "bob"), "ftp://bob@10.0.0.20")
        self.assertEqual(ssh_command_text(ftp_host(protocol="sftp", port="2222"), "a b@c"),
                         "sftp://a%20b%40c@10.0.0.20:2222")
        self.assertEqual(ssh_command_text(HostEntry("web", {"hostname": "h", "port": "2222"}), "u"), "ssh u@h -p 2222")
        self.assertEqual(protocol_text(ftp_host(protocol="ftps")), "FTPS (FTP over TLS)")

    def test_transfer_hosts_get_the_transfer_icon(self) -> None:
        from ssh_manager.gtkui.constants import RowKind
        from ssh_manager.gtkui.tree_icons import tree_icon_for

        self.assertEqual(tree_icon_for(RowKind.HOST, protocol="sftp").name, "sshm-transfer")
        self.assertEqual(tree_icon_for(RowKind.HOST, protocol="ssh").name, "sshm-server")
        self.assertEqual(tree_icon_for(RowKind.HOST).name, "sshm-server")

    def test_transfer_icon_follows_the_icon_rules(self) -> None:
        svg = (Path(__file__).resolve().parents[1] / "ssh_manager/gtkui/assets/icons/sshm-transfer-symbolic.svg")
        text = svg.read_text()
        self.assertNotIn("stroke", text)
        self.assertIn('viewBox="0 0 24 24"', text)


def _display() -> bool:
    try:
        import gi

        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk  # type: ignore

        return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")) and Gtk.init_check(None)[0]
    except Exception:
        return False


@unittest.skipUnless(_display(), "needs a display")
class HostDialogTests(unittest.TestCase):
    def dialog(self, host=None, **kwargs):
        from ssh_manager.gtkui.dialogs.host_dialog import HostDialog

        dialog = HostDialog(None, host, **kwargs)
        self.addCleanup(dialog.destroy)
        return dialog

    def test_protocol_is_the_first_field_and_defaults_to_ssh(self) -> None:
        dialog = self.dialog()
        self.assertEqual(dialog.selected_protocol(), "ssh")
        self.assertIs(dialog._sections["general"].rows[0].control, dialog.protocol_combo)
        self.assertEqual(dialog.port_entry.get_placeholder_text(), "22")

    def test_switching_swaps_pages_and_the_default_port(self) -> None:
        dialog = self.dialog(HostEntry("web", {"hostname": "10.0.0.5", "port": "22"}))
        dialog.protocol_combo.set_active_id("ftp")
        self.assertEqual(dialog.port_entry.get_text(), "")  # was the old default, not typed
        self.assertEqual(dialog.port_entry.get_placeholder_text(), "21")
        self.assertFalse(dialog.layout.page_visible("connection"))
        self.assertFalse(dialog.layout.page_visible("ssh_options"))
        dialog.port_entry.set_text("2121")  # typed: kept on the next switch
        dialog.protocol_combo.set_active_id("sftp")
        self.assertEqual(dialog.port_entry.get_text(), "2121")
        self.assertTrue(dialog.layout.page_visible("connection"))
        built = dialog.build_host("Acme", "")
        self.assertEqual((built.protocol, built.options["port"]), ("sftp", "2121"))

    def test_ftp_drops_ssh_only_settings_after_confirmation(self) -> None:
        host = HostEntry("web", {"hostname": "10.0.0.5", "identityfile": "~/.ssh/id", "ServerAliveInterval": "30"})
        dialog = self.dialog(host)
        with mock.patch("ssh_manager.gtkui.dialogs.host_dialog.confirm", return_value=False) as ask:
            dialog.protocol_combo.set_active_id("ftp")
        self.assertIn("private key", ask.call_args[0][2])
        self.assertIn("SSH options", ask.call_args[0][2])
        self.assertEqual(dialog.selected_protocol(), "ssh")  # declined: unchanged
        with mock.patch("ssh_manager.gtkui.dialogs.host_dialog.confirm", return_value=True):
            dialog.protocol_combo.set_active_id("ftp")
        built = dialog.build_host("Acme", "")
        self.assertEqual(built.protocol, "ftp")
        self.assertNotIn("identityfile", built.options)
        self.assertNotIn("ServerAliveInterval", built.options)

    def test_new_hosts_switch_without_asking(self) -> None:
        dialog = self.dialog()
        dialog.identity_entry.set_text("~/.ssh/id")
        with mock.patch("ssh_manager.gtkui.dialogs.host_dialog.confirm") as ask:
            dialog.protocol_combo.set_active_id("ftps")
        ask.assert_not_called()

    def test_transfer_settings_are_kept_for_transfer_protocols(self) -> None:
        host = HostEntry("files", {"hostname": "10.0.0.20"}, metadata={"transfer": {"v": 1, "passive": True}},
                         protocol="ftp")
        dialog = self.dialog(host, metadata=host.metadata)
        saved = dialog.build_host("Acme", "").metadata["transfer"]  # normalized by the File transfer page
        self.assertEqual((saved["v"], saved["passive"]), (1, True))
        with mock.patch("ssh_manager.gtkui.dialogs.host_dialog.confirm", return_value=True) as ask:
            dialog.protocol_combo.set_active_id("ssh")
        self.assertIn("file-transfer settings", ask.call_args[0][2])
        self.assertNotIn("transfer", dialog.build_host("Acme", "").metadata)

    def test_hostname_is_required_for_transfer_protocols(self) -> None:
        dialog = self.dialog()
        dialog.alias_entry.set_text("files")
        dialog.protocol_combo.set_active_id("sftp")
        self.assertFalse(dialog.validate())
        dialog.hostname_entry.set_text("10.0.0.20")
        self.assertTrue(dialog.validate())

    def test_folder_inventories_are_ssh_only(self) -> None:
        dialog = self.dialog(protocols_enabled=False)
        self.assertFalse(dialog.protocol_combo.get_sensitive())
        self.assertIn("SSH only", dialog._protocol_row.help.get_text())


@unittest.skipUnless(_display(), "needs a display")
class WindowTests(unittest.TestCase):
    def setUp(self) -> None:
        from tests.gtk_window_fixture import WindowSandbox

        self.sandbox = WindowSandbox(store=True)
        self.addCleanup(self.sandbox.close)
        self.win = self.sandbox.window
        self.notes = []
        self.win._notify = lambda message, *a, **k: self.notes.append(message)
        self.win.manager.add_host("staging", "hosts.conf", ftp_host("files", protocol="sftp"))
        self.win.tree.rebuild(refresh=True)
        self.files = self.win.tree.find_host_by_alias("files")

    def test_dispatch_by_protocol(self) -> None:
        web = self.win.tree.find_host_by_alias("web-01")
        # Phase F2: SFTP hosts open a file browser (no network here: the tab class is replaced).
        with mock.patch.object(self.win.terminal_tabs, "new_terminal", return_value=mock.Mock()) as new_terminal, \
                mock.patch.object(self.win.terminal_tabs, "add_page") as add_page, \
                mock.patch("ssh_manager.gtkui.file_browser.tab.FileBrowserTab") as browser, \
                mock.patch.object(self.win.sessions, "bind_tab_status"):
            self.win.sessions.open_hosts([web, self.files])
        self.assertEqual(new_terminal.call_count, 1)  # only the SSH host got a terminal
        self.assertEqual(browser.call_args[0][0].alias, "files")
        add_page.assert_called_once_with(browser.return_value)
        self.assertFalse(any("cannot open" in note for note in self.notes), self.notes)

    def test_unknown_protocol_is_a_message_not_an_exception(self) -> None:
        future = ftp_host("future", protocol="vnc")
        self.win.sessions.open_hosts([future])
        self.assertTrue(any("cannot open" in note and "vnc" in note for note in self.notes), self.notes)

    def test_tree_row_shows_the_protocol_pill(self) -> None:
        view = self.win.tree_view
        path = view.expand_to_row("host", "staging", "hosts.conf", "files")
        renderer = view.row_renderer
        view._row_data(None, renderer, view.store, view.store.get_iter(path))
        self.assertEqual((renderer.props.badge, renderer.props.protocol), ("SFTP", "sftp"))
        web = view.expand_to_row("host", "prod", "web.conf", "web-01")
        view._row_data(None, renderer, view.store, view.store.get_iter(web))
        self.assertEqual(renderer.props.badge, "")

    def test_details_show_the_protocol(self) -> None:
        content = self.win.host_page._host_content(self.files, {}, 0.0)
        facts = {label: value for label, value, _mono in content.facts}
        self.assertEqual(facts["Protocol"], "SFTP (SSH file transfer)")
        self.assertEqual(content.icon, "sshm-transfer")
        self.assertTrue(content.copy_text.startswith("sftp://"))

    def test_proto_filter_in_the_tree(self) -> None:
        self.win.tree.on_filter_text_changed("proto:sftp")
        self.assertEqual(list(self.win.tree.host_lookup.values())[0].alias, "files")
        self.assertEqual(len(self.win.tree.host_lookup), 1)

    def test_export_reports_skipped_hosts(self) -> None:
        target = Path(self.sandbox.root) / "export.conf"
        self.win.store_actions.export_openssh(target=target, offer_include=False)
        self.assertNotIn("files", target.read_text())
        self.assertTrue(any("left out" in note for note in self.notes), self.notes)

    def test_audit_log_records_the_protocol(self) -> None:
        import time

        log = Path(self.sandbox.root) / "audit.log"
        self.win._settings.enable_audit_logging = True
        self.win._settings.audit_log_path = str(log)
        self.win.sessions.audit_log("connect", self.files)
        for _ in range(100):
            if log.exists() and log.read_text():
                break
            time.sleep(0.02)
        self.assertIn("protocol=sftp", log.read_text())


if __name__ == "__main__":
    unittest.main()
