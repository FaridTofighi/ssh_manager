"""Port forwarding in the host dialog, with sessions and with the app; export / import keep the forwards."""

import unittest
from unittest import mock

from tests.gtk_window_fixture import display_available
from tests.test_local_terminal import pump
from tests.test_tunnels_ui import TunnelsUiCase


class ExportImportTests(unittest.TestCase):
    def test_native_export_and_import_keep_forwards(self) -> None:
        from ophion.exporters.native import ExportReport, audit_metadata
        from ophion.importers.native import read_native

        from tests.test_native_import import document, group, host, raw

        forwards = [{"id": "f1", "name": "db", "kind": "local", "bind_host": "127.0.0.1", "bind_port": 15432,
                     "dest_host": "db.internal", "dest_port": 5432}]
        kept = audit_metadata({"forwards": forwards, "notes": "n"}, "web", ExportReport(), notes=True,
                              tunnel_ref=lambda _id: None)
        self.assertEqual(kept["forwards"], forwards)
        doc = read_native(raw(document([group("G", [host("web", metadata={"forwards": forwards})])])))
        self.assertEqual(doc.hosts()[0].metadata["forwards"], forwards)


@unittest.skipUnless(display_available(), "needs a display")
class HostDialogPageTests(TunnelsUiCase):
    def host_dialog(self, host):
        dialog = self.win.hosts.host_dialog(host)
        self.addCleanup(dialog.destroy)
        return dialog

    def test_page_is_built_lazily_and_saves_forwards(self) -> None:
        from ophion.tunnels.model import forwards_from_metadata

        existing = self.forward(name="kept")
        self.controller.save(self.host, [existing])
        host = self.controller.host(self.controller.host_key(self.host))
        dialog = self.host_dialog(host)
        self.assertIsNone(dialog.forwards_page)  # not built while the dialog opens
        self.assertEqual(forwards_from_metadata(dialog.build_host("prod", "").metadata)[0].name, "kept")  # untouched page: kept
        dialog.layout.show_page("forwarding")
        self.assertIsNotNone(dialog.forwards_page)
        page = dialog.forwards_page
        self.assertEqual([f.name for f in page.forwards()], ["kept"])
        page.edit_forward = lambda forward: self.forward(name="added")
        page.add()
        self.assertEqual([f.name for f in forwards_from_metadata(dialog.build_host("prod", "").metadata)], ["kept", "added"])

    def test_page_only_for_ssh_hosts(self) -> None:
        dialog = self.host_dialog(self.host)
        self.assertTrue(dialog.layout.page_visible("forwarding"))
        dialog._protocol = "ftp"
        dialog._apply_protocol()
        self.assertFalse(dialog.layout.page_visible("forwarding"))

    def test_raw_ssh_options_are_shown_and_importable(self) -> None:
        from ophion.gtkui.dialogs.forwards_page import ForwardsPage

        options = {"LocalForward": "8080 db:5432", "DynamicForward": "garbage here"}
        page = ForwardsPage(None, [], host_id="h", ssh_options=lambda: options)
        self.addCleanup(page.destroy)
        self.assertEqual(len(page.raw_buttons), 1)  # the garbage one is shown, not importable
        page.edit_forward = lambda forward: forward  # the editor's OK
        page.raw_buttons[0].clicked()
        imported = page.forwards()[0]
        self.assertEqual((imported.bind_port, imported.dest_host, imported.dest_port), (8080, "db", 5432))
        self.assertEqual(options["LocalForward"], "8080 db:5432")  # the ssh option itself is left alone


@unittest.skipUnless(display_available(), "needs a display")
class SessionAndAppTests(TunnelsUiCase):
    def test_with_session_starts_beside_the_terminal_and_stops_with_the_last_tab(self) -> None:
        from ophion.gtk_terminal import TerminalTab

        forward = self.forward(auto_start_with_session=True)
        manual = self.forward(name="manual")
        self.controller.save(self.host, [forward, manual])
        host = self.controller.host(self.controller.host_key(self.host))
        with mock.patch.object(TerminalTab, "_begin_connect"):  # no real ssh for the terminal itself
            self.win.sessions.open_ssh_host(host)
            self.win.sessions.open_ssh_host(host)  # a second tab: started once
        self.assertTrue(pump(10, until=lambda: self.controller.status(host, forward).state == "active"))
        self.assertEqual(self.controller.status(host, manual).state, "stopped")
        tabs = [t for t in self.win.terminal_tabs.tabs() if getattr(t, "_host", None) is not None]
        self.assertEqual(len(tabs), 2)
        self.win.terminal_tabs.close_tab(tabs[0])
        pump(0.3)
        self.assertEqual(self.controller.status(host, forward).state, "active")  # one tab is still open
        self.win.terminal_tabs.close_tab(tabs[1])
        self.assertTrue(pump(5, until=lambda: self.controller.status(host, forward).state == "stopped"))

    def test_with_app_one_at_a_time_and_one_summary(self) -> None:
        import socket

        busy = socket.create_server(("127.0.0.1", 0))
        self.addCleanup(busy.close)
        good = self.forward(auto_start_with_app=True)
        bad = self.forward(name="bad", auto_start_with_app=True, bind_port=busy.getsockname()[1])
        self.controller.save(self.host, [good, bad])
        toasts = []
        self.win._notify = lambda text, level=None, **kw: toasts.append(text)
        queued = self.controller.start_with_app()
        self.assertEqual(len(queued), 2)
        self.assertTrue(pump(10, until=lambda: self.controller.status(self.host, good).state == "active"
                             and self.controller.status(self.host, bad).state == "failed"))
        pump(0.3)
        summaries = [t for t in toasts if "did not start" in t]
        self.assertEqual(summaries, ["1 tunnel did not start (web-01). Open Tunnels to retry."])


if __name__ == "__main__":
    unittest.main()
