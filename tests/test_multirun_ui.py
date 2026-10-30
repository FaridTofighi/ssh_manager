"""Run Command on Hosts, the dialog: targets, a real run against the in-process server, cancel, locking."""

import tempfile
import time
import unittest
from pathlib import Path

from tests.gtk_window_fixture import display_available
from tests.test_local_terminal import pump
from tests.test_multirun_engine import PASSWORD


@unittest.skipUnless(display_available(), "needs a display")
class MultiRunUiCase(unittest.TestCase):
    def setUp(self) -> None:
        from tests.fake_sftp import FakeSftpServer
        from tests.gtk_window_fixture import WindowSandbox

        from ssh_manager.transfer.settings import TransferSettings

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.server = FakeSftpServer(Path(self.tmp.name), users={"alice": PASSWORD})
        self.server.__enter__()
        self.addCleanup(self.server.__exit__)
        known = Path(self.tmp.name) / "known_hosts"
        key = self.server.host_key
        known.write_text(f"[127.0.0.1]:{self.server.port} {key.get_name()} {key.get_base64()}\n")
        self.sandbox = WindowSandbox(store=True)
        self.addCleanup(self.sandbox.close)
        self.win = self.sandbox.window
        self.controller = self.win.multirun

        def connect(target, timeout):
            from ssh_manager.transfer.sftp import SftpBackend

            return SftpBackend("127.0.0.1", self.server.port, "alice", PASSWORD, known_hosts=known,
                               allow_agent=False, settings=TransferSettings(timeout_connect=timeout)
                               ).open_transport("Run command")

        self.controller.connect = connect

    def keys(self, *aliases):
        hosts = self.controller.host_by_key()
        return [key for key, host in hosts.items() if not aliases or host.alias in aliases]

    def dialog(self, keys):
        from ssh_manager.gtkui.dialogs.multirun import MultiRunDialog

        dialog = MultiRunDialog(None, self.controller, keys)
        self.addCleanup(dialog.destroy)
        return dialog

    def finish(self, dialog, seconds=30):
        self.assertTrue(pump(seconds, until=lambda: dialog.run is not None and dialog.run.done
                             and dialog._drain_id == 0), "the run did not finish")


class DialogTests(MultiRunUiCase):
    def test_twelve_hundred_hosts_open_quickly_and_show_a_count(self) -> None:
        from ssh_manager.inventory.store import NewHost

        store = self.sandbox.store
        group = store.create_group(None, "Many")
        for index in range(1200):
            store.add_host(NewHost(group.id, f"many-{index}", f"m{index}.example", 22, "u"))
        self.win.manager.refresh()
        keys = self.keys()
        self.assertGreaterEqual(len(keys), 1200)
        started = time.perf_counter()
        dialog = self.dialog(keys)
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 0.3, f"{elapsed:.3f} s")
        self.assertRegex(dialog.targets_label.get_text(), r"^\d+ hosts selected")
        self.assertNotIn("many-1", dialog.targets_label.get_text())  # never a list of names
        started = time.perf_counter()
        from ssh_manager.gtkui.dialogs.multirun_targets import TargetChooserDialog

        chooser = TargetChooserDialog(None, self.win.manager.tree, keys[:10], key=self.win.creds.credential_key)
        self.addCleanup(chooser.destroy)
        print(f"\n[multirun] dialog {elapsed * 1000:.0f} ms, chooser {1000 * (time.perf_counter() - started):.0f} ms "
              f"for {len(keys)} hosts")

    def test_target_chooser_search_and_select_matches(self) -> None:
        from ssh_manager.gtkui.dialogs.multirun_targets import TargetChooserDialog

        chooser = TargetChooserDialog(None, self.win.manager.tree, [], key=self.win.creds.credential_key)
        self.addCleanup(chooser.destroy)
        chooser.search.set_text("web-0")
        chooser.refilter()
        self.assertEqual(sorted(self.controller.host_by_key()[k].alias for k in chooser.matches()),
                         ["web-01", "web-02"])
        chooser.select_matches(True)
        self.assertEqual(len(chooser.selected), 2)
        self.assertEqual(chooser.count.get_text(), "2 hosts selected")
        chooser.search.set_text("")
        chooser.refilter()
        db = self.keys("db-primary")[0]
        chooser.toggle_key(db)
        self.assertEqual(len(chooser.selected), 3)

    def test_a_run_fills_the_table_and_skips_non_ssh_hosts(self) -> None:
        from ssh_manager.inventory.store import NewHost

        store = self.sandbox.store
        group = store.create_group(None, "Desktops")
        store.add_host(NewHost(group.id, "desk", "desk.example", 3389, "u", protocol="rdp"))
        self.win.manager.refresh()
        keys = self.keys("web-01", "web-02", "db-primary", "desk")
        dialog = self.dialog(keys)
        self.assertIn("1 is not an SSH host and is skipped", dialog.targets_label.get_text())
        dialog.set_command('echo "hello {alias}"')
        logged = []
        self.controller.log = logged.append
        self.assertIsNotNone(dialog.start())
        self.assertFalse(dialog.command.get_sensitive())  # locked while running
        self.finish(dialog)
        statuses = {r.alias: r.status for r in dialog.results.results.values()}
        self.assertEqual(statuses, {"web-01": "success", "web-02": "success", "db-primary": "success",
                                    "desk": "skipped"})
        self.assertEqual(dialog.results.results[self.keys("web-02")[0]].stdout, "hello web-02\n")
        self.assertEqual(dialog.progress.get_text(), "4 / 4 done")
        self.assertTrue(dialog.command.get_sensitive())
        self.assertEqual(logged, [logged[0]])
        self.assertRegex(logged[0], r"^multirun: 4 hosts, 3 ok, 0 failed, [\d.]+ s$")
        rows = [(row[3], row[2]) for row in dialog.results.store]
        self.assertIn(("desk", "Skipped"), rows)

    def test_cancel_and_closing_while_running(self) -> None:
        dialog = self.dialog(self.keys())
        dialog.concurrency.set_value(1)
        dialog.set_command("sleep 5")
        dialog.start()
        self.assertTrue(pump(5, until=lambda: any(r.status == "running" for r in dialog.results.results.values())))
        asked = []
        dialog.confirm_cancel = lambda: asked.append(1) or False
        self.assertFalse(dialog.may_close())  # "Cancel the running command?" → no
        self.assertTrue(dialog.running)
        dialog.confirm_cancel = lambda: asked.append(2) or True
        started = time.monotonic()
        self.assertTrue(dialog.may_close())
        self.finish(dialog, 10)
        self.assertLess(time.monotonic() - started, 4)
        self.assertEqual(asked, [1, 2])
        self.assertTrue(all(r.status == "cancelled" for r in dialog.results.results.values()))


if __name__ == "__main__":
    unittest.main()
