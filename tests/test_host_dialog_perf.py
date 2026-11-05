"""The host dialog on a large inventory: RDP pages built on demand, a cheap SSH-tunnel list, a time budget.

Measured before the change (3,996 hosts, tools/bench_large_inventory.py): host_dialog() took 3.9 s for any
host, 3.6 s of it in RdpPages._build_connection filling a Gtk.ComboBoxText with every SSH host.
"""

import time
import unittest

from ophion.config import HostEntry
from tests.gtk_window_fixture import display_available


def rdp_host(**rdp) -> HostEntry:
    return HostEntry("win-01", {"hostname": "10.0.0.50"}, protocol="rdp", metadata={"rdp": {"v": 1, **rdp}})


@unittest.skipUnless(display_available(), "needs a display")
class LazyRdpPagesTests(unittest.TestCase):
    def dialog(self, host=None, **kwargs):
        from ophion.gtkui.dialogs.host_dialog import HostDialog

        dialog = HostDialog(None, host, **kwargs)
        self.addCleanup(dialog.destroy)
        return dialog

    def test_an_ssh_host_builds_no_rdp_widgets_and_reads_no_tunnel_hosts(self) -> None:
        asked = []
        dialog = self.dialog(rdp_ssh_hosts=lambda: asked.append(1) or [("ssh-1", "bastion")])
        self.assertIsNone(dialog.rdp_pages)
        self.assertEqual(asked, [])
        for page_id in ("rdp", "rdp_display", "rdp_devices", "rdp_connection"):  # the pages exist, hidden
            self.assertFalse(dialog.layout.page_visible(page_id))

        dialog.protocol_combo.set_active_id("rdp")  # chosen: built once, with the tunnel hosts
        pages = dialog.rdp_pages
        self.assertIsNotNone(pages)
        self.assertEqual(asked, [1])
        dialog.protocol_combo.set_active_id("ssh")
        dialog.protocol_combo.set_active_id("rdp")
        self.assertIs(dialog.rdp_pages, pages)
        self.assertEqual(asked, [1])
        dialog.alias_entry.set_text("win-02")
        dialog.hostname_entry.set_text("10.0.0.51")
        pages.domain.set_text("CORP")
        built = dialog.build_host("Acme", "")
        self.assertEqual((built.protocol, built.metadata["rdp"]["domain"]), ("rdp", "CORP"))

    def test_transfer_page_and_other_forwards_are_built_on_demand(self) -> None:
        asked = []
        dialog = self.dialog(forwards_others=lambda: asked.append(1) or [])
        self.assertIsNone(dialog._transfer_page)
        dialog.protocol_combo.set_active_id("sftp")
        self.assertIsNotNone(dialog._transfer_page)
        self.assertIs(dialog.transfer_page, dialog._transfer_page)
        dialog.protocol_combo.set_active_id("ssh")
        self.assertEqual(asked, [])
        dialog.layout.show_page("forwarding")  # the Port Forwarding page asks for the other hosts' forwards
        self.assertEqual(asked, [1])

    def test_an_rdp_host_shows_its_settings_and_tunnel(self) -> None:
        dialog = self.dialog(rdp_host(domain="CORP", via_ssh="ssh-1"), rdp_ssh_hosts=[("ssh-1", "bastion")])
        self.assertEqual(dialog.rdp_pages.domain.get_text(), "CORP")
        self.assertEqual(dialog.rdp_pages.selected_via_ssh(), "ssh-1")
        self.assertEqual(dialog.build_host("Acme", "").metadata["rdp"]["via_ssh"], "ssh-1")

    def test_an_ssh_host_keeps_stored_rdp_settings_untouched(self) -> None:
        stored = {"v": 1, "domain": "CORP", "future_key": [1]}
        host = HostEntry("web-01", {"hostname": "10.0.0.5"}, protocol="ssh", metadata={"rdp": stored})
        dialog = self.dialog(host)
        self.assertIsNone(dialog.rdp_pages)
        self.assertEqual(dialog.build_host("Acme", "").metadata["rdp"], stored)


@unittest.skipUnless(display_available(), "needs a display")
class TunnelPickerTests(unittest.TestCase):
    def pages(self, hosts, **rdp):
        from ophion.gtkui.dialogs.host_dialog import HostDialog

        dialog = HostDialog(None, rdp_host(**rdp), rdp_ssh_hosts=hosts)
        self.addCleanup(dialog.destroy)
        return dialog.rdp_pages

    def test_up_to_30_hosts_keep_the_combo(self) -> None:
        from gi.repository import Gtk

        pages = self.pages([(f"id-{n}", f"host-{n}") for n in range(30)], via_ssh="id-7")
        self.assertIsInstance(pages.via_ssh, Gtk.ComboBoxText)
        self.assertEqual(pages.selected_via_ssh(), "id-7")

    def test_more_hosts_get_a_searchable_list(self) -> None:
        from gi.repository import Gtk

        from ophion.gtkui.widgets.host_picker import HostPicker

        hosts = [(f"id-{n}", f"host-{n} (10.0.0.{n})") for n in range(40)]
        pages = self.pages(hosts)
        picker = pages.via_ssh
        self.assertIsInstance(picker, HostPicker)
        self.assertNotIsInstance(picker, Gtk.ComboBox)
        self.assertEqual((pages.selected_via_ssh(), picker.label.get_text()), ("", "Connect directly"))
        values = picker.list.visible_values()
        self.assertEqual(values[0], "")  # "Connect directly" first
        self.assertEqual(len(values), 41)
        picker.list.entry.set_text("host-1")  # host-1, host-10 … host-19, and "Connect directly"
        self.assertEqual(picker.list.visible_values(), [""] + [f"id-{n}" for n in [1] + list(range(10, 20))])
        self.assertEqual(picker.list.count.get_text(), "11 of 40 hosts")
        picker.list.select_value("id-12")
        picker.list.view.row_activated(picker.list.view.get_selection().get_selected_rows()[1][0],
                                       picker.list.view.get_column(0))
        self.assertEqual(pages.selected_via_ssh(), "id-12")
        self.assertEqual(picker.label.get_text(), "host-12 (10.0.0.12)")
        self.assertTrue(picker.set_active_id(""))
        self.assertFalse(picker.set_active_id("nope"))

    def test_a_deleted_tunnel_host_is_kept_and_named(self) -> None:
        pages = self.pages([(f"id-{n}", f"host-{n}") for n in range(40)], via_ssh="gone")
        self.assertEqual(pages.selected_via_ssh(), "gone")
        self.assertEqual(pages.via_ssh.label.get_text(), "(a host that no longer exists)")
        self.assertEqual(pages.settings().via_ssh, "gone")

    def test_twenty_thousand_rows_build_fast(self) -> None:
        from ophion.gtkui.widgets.host_picker import HostPicker

        hosts = [(f"id-{n}", f"host-{n} (10.{n // 65536}.{n // 256 % 256}.{n % 256})") for n in range(20000)]
        started = time.perf_counter()
        picker = HostPicker(hosts, first=("", "Connect directly"))
        took = time.perf_counter() - started
        picker.destroy()
        self.assertLess(took, 0.15, f"{took:.3f} s")


class TunnelHostLabelsTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile
        from pathlib import Path

        from ophion.inventory.store import InventoryStore, NewHost

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = InventoryStore(Path(self.tmp.name) / "inventory.db")
        self.addCleanup(self.store.close)
        group = self.store.create_group(None, "G")
        rows = self.store.add_hosts_bulk([
            NewHost(group.id, "web", "10.0.0.1"), NewHost(group.id, "Bastion", "bastion.example"),
            NewHost(group.id, "alpha", ""), NewHost(group.id, "win", "10.0.0.9", protocol="rdp"),
            NewHost(group.id, "files", "ftp.example", protocol="ftp")])
        self.ids = {row.alias: row.id for row in rows}

    def test_ssh_hosts_only_sorted_in_one_query(self) -> None:
        statements = []
        self.store.conn.set_trace_callback(statements.append)
        labels = self.store.list_ssh_host_labels(exclude_id=self.ids["web"])
        self.store.conn.set_trace_callback(None)
        self.assertEqual(labels, [(self.ids["alpha"], "alpha", ""), (self.ids["Bastion"], "Bastion", "bastion.example")])
        self.assertEqual(len(statements), 1, statements)
        self.assertEqual([row[1] for row in self.store.list_ssh_host_labels()], ["alpha", "Bastion", "web"])


@unittest.skipUnless(display_available(), "needs a display")
class HostDialogBudgetTests(unittest.TestCase):
    """New SSH host < 0.5 s, RDP host < 1 s on 3,996 hosts (or < 3x the 10-host time, whichever is larger)."""

    @classmethod
    def setUpClass(cls) -> None:
        from tests.gtk_window_fixture import WindowSandbox
        from tests.large_inventory import populate

        cls.sandbox = WindowSandbox(store=True)
        cls.hosts = cls.sandbox.window.hosts
        cls.win_host = HostEntry("win-ref", {"hostname": "10.0.0.50"}, protocol="rdp", metadata={"rdp": {"v": 1}})
        cls.small = {"ssh": cls.best(), "rdp": cls.best(cls.win_host)}
        populate(cls.sandbox.store)
        cls.sandbox.window.manager.refresh()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.sandbox.close()

    @classmethod
    def best(cls, host=None) -> float:
        times = []
        for _ in range(2):
            started = time.perf_counter()
            cls.hosts.host_dialog(host).destroy()
            times.append(time.perf_counter() - started)
        return min(times)

    def test_new_ssh_host(self) -> None:
        self.assertGreater(len(self.hosts.tunnel_hosts()), 3000)
        took, small = self.best(), self.small["ssh"]
        self.assertLess(took, max(0.5, 3 * small), f"New SSH host: {took:.2f} s (10 hosts: {small:.2f} s)")

    def test_rdp_host(self) -> None:
        took, small = self.best(self.win_host), self.small["rdp"]
        self.assertLess(took, max(1.0, 3 * small), f"RDP host: {took:.2f} s (10 hosts: {small:.2f} s)")


if __name__ == "__main__":
    unittest.main()
