"""The Tunnels manager, the forward editor and quitting with tunnels running (real engine, in-process server)."""

import socket
import tempfile
import time
import unittest
from pathlib import Path

from tests.gtk_window_fixture import display_available
from tests.test_local_terminal import pump
from tests.test_tunnels_engine import PASSWORD, EchoServer, free_port, roundtrip


@unittest.skipUnless(display_available(), "needs a display")
class TunnelsUiCase(unittest.TestCase):
    def setUp(self) -> None:
        from tests.fake_sftp import FakeSftpServer
        from tests.gtk_window_fixture import WindowSandbox

        from ophion.gtkui.controllers.tunnels import TunnelController, _idle
        from ophion.transfer.settings import TransferSettings
        from ophion.tunnels.engine import TunnelManager

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.server = FakeSftpServer(Path(self.tmp.name), users={"alice": PASSWORD})
        self.server.__enter__()
        self.addCleanup(self.server.__exit__)
        self.echo = EchoServer()
        self.addCleanup(self.echo.close)
        known = Path(self.tmp.name) / "known_hosts"
        key = self.server.host_key
        known.write_text(f"[127.0.0.1]:{self.server.port} {key.get_name()} {key.get_base64()}\n")

        def connect(_host_key):
            from ophion.transfer.sftp import SftpBackend

            return SftpBackend("127.0.0.1", self.server.port, "alice", PASSWORD, known_hosts=known, allow_agent=False,
                               settings=TransferSettings(timeout_connect=5)).open_transport("Port forwarding")

        self.sandbox = WindowSandbox(store=True)
        self.addCleanup(self.sandbox.close)
        self.win = self.sandbox.window
        self.controller = TunnelController(self.win, manager=TunnelManager(connect, dispatch=_idle))
        self.win.tunnels = self.controller
        self.addCleanup(self.controller.shutdown)
        self.host = next(h for h in self.controller.hosts() if h.alias == "web-01")

    def forward(self, **kw):
        from ophion.tunnels.model import LOCAL, Forward

        base = dict(name="echo", kind=LOCAL, bind_port=free_port(), dest_host="127.0.0.1", dest_port=self.echo.port)
        base.update(kw)
        return Forward(**base)

    def dialog(self):
        from ophion.gtkui.dialogs.tunnels import TunnelsDialog

        dialog = TunnelsDialog(None, self.controller)
        self.addCleanup(dialog.destroy)
        return dialog

    def status_of(self, dialog, forward):
        for parent in dialog.store:
            for row in parent.iterchildren():
                if row[10] == forward.id:
                    return row[5]
        return None


class ManagerTests(TunnelsUiCase):
    def test_lists_starts_and_stops(self) -> None:
        forward = self.forward()
        self.assertTrue(self.controller.save(self.host, [forward]))
        dialog = self.dialog()
        self.assertEqual([row[0] for row in dialog.store], ["web-01"])  # only hosts with forwards
        self.assertEqual(self.status_of(dialog, forward), "Stopped")
        self.assertTrue(dialog.select(self.controller.host_key(self.host), forward.id))
        dialog.toggle()
        self.assertTrue(pump(10, until=lambda: self.status_of(dialog, forward) == "Active"))
        self.assertEqual(dialog.buttons["toggle"].get_label(), "_Stop")
        self.assertEqual(roundtrip(forward.bind_port), b"hello tunnel")
        self.assertEqual(dialog.copy_address(), forward.listen_address)
        dialog.toggle()
        self.assertTrue(pump(5, until=lambda: self.status_of(dialog, forward) == "Stopped"))
        with self.assertRaises(OSError):
            roundtrip(forward.bind_port)

    def test_add_edit_delete(self) -> None:
        from ophion.tunnels.model import DYNAMIC, forwards_from_metadata

        dialog = self.dialog()
        key = self.controller.host_key(self.host)
        new = self.forward(name="added")
        dialog.pick_host = lambda: key
        dialog.edit_forward = lambda forward, host: new
        self.assertIs(dialog.add(), new)
        host = self.controller.host(key)
        self.assertEqual([f.name for f in forwards_from_metadata(host.metadata)], ["added"])
        dialog.edit_forward = lambda forward, host: type(forward)(**{**forward.__dict__, "name": "renamed",
                                                                     "kind": DYNAMIC, "dest_host": "",
                                                                     "dest_port": 0})
        self.assertEqual(dialog.edit().name, "renamed")
        self.assertEqual(forwards_from_metadata(self.controller.host(key).metadata)[0].kind, DYNAMIC)
        dialog.confirm = lambda *a: False
        self.assertFalse(dialog.delete())
        dialog.confirm = lambda *a: True
        self.assertTrue(dialog.delete())
        self.assertEqual(forwards_from_metadata(self.controller.host(key).metadata), [])
        self.assertTrue(dialog.empty.get_visible())

    def test_failure_is_worded_with_its_reason(self) -> None:
        busy = socket.create_server(("127.0.0.1", 0))
        self.addCleanup(busy.close)
        forward = self.forward(bind_port=busy.getsockname()[1])
        self.controller.save(self.host, [forward])
        dialog = self.dialog()
        dialog.select(self.controller.host_key(self.host), forward.id)
        dialog.toggle()
        self.assertTrue(pump(10, until=lambda: self.status_of(dialog, forward) == "Failed"))
        row = next(r for p in dialog.store for r in p.iterchildren() if r[10] == forward.id)
        self.assertIn("already used by another program", row[8])
        self.assertEqual(row[4], "dialog-error-symbolic")  # an icon and a word

    def test_twelve_hundred_hosts_stay_quick(self) -> None:
        from ophion.inventory.store import NewHost

        store = self.sandbox.store
        group = store.create_group(None, "Many")
        for index in range(1200):
            store.add_host(NewHost(group.id, f"many-{index}", f"m{index}.example", 22, "u"))
        self.win.manager.refresh()
        self.controller.save(self.host, [self.forward()])
        started = time.perf_counter()
        dialog = self.dialog()
        dialog.refresh()
        self.assertLess(time.perf_counter() - started, 2.0)
        self.assertEqual(len(dialog.store), 1)


class EditorTests(TunnelsUiCase):
    def test_validation_and_the_bind_flag(self) -> None:
        from ophion.gtkui.dialogs.tunnels import ForwardDialog

        other = self.forward(name="taken")
        dialog = ForwardDialog(None, None, host_id="h1", others=[("h2", other)])
        self.addCleanup(dialog.destroy)
        dialog.dest_host.set_text("db.internal")
        dialog.dest_port.set_value(5432)
        dialog.bind_port.set_value(15432)
        self.assertTrue(dialog.primary_button.get_sensitive())
        dialog.bind_port.set_value(other.bind_port)
        self.assertFalse(dialog.primary_button.get_sensitive())
        self.assertIn("taken", dialog.messages.get_text())
        dialog.bind_port.set_value(15432)
        dialog.bind_host.set_text("0.0.0.0")
        self.assertFalse(dialog.primary_button.get_sensitive())
        dialog.allow_remote.set_active(True)
        self.assertTrue(dialog.primary_button.get_sensitive())
        self.assertIn("Other computers", dialog.messages.get_text())
        dialog.kind_buttons["dynamic"].set_active(True)
        self.assertFalse(dialog.dest_host.get_sensitive())
        self.assertIn("SOCKS5", dialog.explain.get_text())
        self.assertEqual(dialog.forward().dest_port, 0)
        self.assertFalse(dialog.stop_last.get_active())
        dialog.with_session.set_active(True)
        self.assertTrue(dialog.stop_last.get_active())  # follows "with a session" until the user changes it

    def test_host_picker_and_editor_answer_after_closing(self) -> None:
        from gi.repository import Gtk

        from ophion.gtkui.dialogs.tunnels import ForwardDialog, HostPickerDialog

        def picker(choose, response=Gtk.ResponseType.OK):
            dialog = HostPickerDialog(None, [("web-01", "key-1"), ("web-02", "key-2")])  # (alias, key)
            self.assertEqual([row[0] for row in dialog.list.filter], ["web-01", "web-02"])  # names shown, not keys
            if choose:
                dialog.list.select_value(choose)
            dialog.run = lambda: response  # what the user answered; ask() then destroys the dialog
            return dialog.ask()

        self.assertEqual(picker("key-2"), "key-2")  # crashed: the selection was read after destroy
        self.assertIsNone(picker(""))  # nothing chosen
        self.assertIsNone(picker("key-1", Gtk.ResponseType.CANCEL))
        editor = ForwardDialog(None, None, host_id="key-1", others=[], host_label="web-01")
        editor.dest_host.set_text("db.internal")
        editor.run = lambda: Gtk.ResponseType.OK
        self.assertEqual(editor.ask().dest_host, "db.internal")

    def test_quit_with_running_tunnels_asks(self) -> None:
        forward = self.forward()
        self.controller.save(self.host, [forward])
        self.controller.start(self.host, forward)
        self.assertTrue(pump(10, until=lambda: self.controller.active_count() == 1))
        asked = []
        self.assertFalse(self.controller.confirm_quit(lambda title, text: asked.append(title) or False))
        self.assertEqual(asked, ["1 tunnel is running. Stop it and quit?"])
        self.assertEqual(self.controller.active_count(), 1)
        self.assertTrue(self.controller.confirm_quit(lambda title, text: True))
        self.assertEqual(self.controller.running_count(), 0)
        self.assertTrue(self.controller.confirm_quit(lambda title, text: self.fail("no tunnels: no question")))


if __name__ == "__main__":
    unittest.main()
