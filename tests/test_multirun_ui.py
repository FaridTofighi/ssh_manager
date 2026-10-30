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


class ResultsTests(MultiRunUiCase):
    SCRIPT = 'case {alias} in web-02) echo "6.0"; exit 3;; *) echo "6.1"; echo "warn {alias}" >&2;; esac'

    def ran(self):
        dialog = self.dialog(self.keys())
        dialog.set_command(self.SCRIPT)
        dialog.start()
        self.finish(dialog)
        return dialog

    def test_filters_output_search_grouping_and_diff(self) -> None:
        dialog = self.ran()
        view = dialog.results
        total = len(view.store)
        view.set_filter("failed")
        self.assertEqual([row[3] for row in view.filtered], ["web-02"])
        self.assertEqual(view.filtered[0][2], "Failed (exit 3)")
        view.set_filter("succeeded")
        self.assertEqual(len(view.filtered), total - 1)
        view.set_filter("all")
        web01 = self.keys("web-01")[0]
        self.assertTrue(view.select(web01))
        self.assertEqual(view.stdout_view.get_buffer().props.text, "6.1\n")
        self.assertEqual(view.stderr_view.get_buffer().props.text, "warn web-01\n")
        view.output_search.set_text("WARN")
        self.assertEqual(view.highlight(), 1)
        groups = view.rebuild_groups()
        self.assertEqual([len(g.keys) for g in groups], [total - 1, 1])
        view.set_grouped(True)
        labels = [row[0] for row in view.groups]
        self.assertTrue(labels[0].startswith(f"Same output on {total - 1} hosts: 6.1"))
        shown = []
        view.show_diff = lambda title, text: shown.append((title, text))
        view.set_grouped(False)
        view.select(self.keys("web-02")[0])
        diff = view.differences()
        self.assertIn("-6.1", diff)
        self.assertIn("+6.0", diff)
        self.assertEqual(shown[0][0], "Differences: web-02")

    def test_copy_export_rerun_failed_and_sessions(self) -> None:
        import csv
        import os
        import stat
        from unittest import mock

        dialog = self.ran()
        tsv = dialog.copy_results()
        self.assertEqual(tsv.splitlines()[0], "host\tgroup\tstatus\texit\tduration\toutput")
        target = Path(self.tmp.name) / "out.csv"
        dialog.choose_export = lambda: str(target)
        self.assertEqual(dialog.export(), str(target))
        rows = list(csv.reader(target.open(encoding="utf-8")))
        self.assertEqual(rows[0][:3], ["host", "group", "status"])
        self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o600)
        json_target = Path(self.tmp.name) / "out.json"
        dialog.choose_export = lambda: str(json_target)
        dialog.export()
        self.assertIn('"host": "web-02"', json_target.read_text())
        self.assertTrue(dialog.result_buttons["rerun_failed"].get_sensitive())
        before = len(self.server.exec_commands)
        run = dialog.rerun_failed()
        self.assertEqual([t.alias for t in run.targets], ["web-02"])  # exactly the failed host
        self.finish(dialog)
        self.assertEqual(len(self.server.exec_commands) - before, 1)
        with mock.patch.object(self.controller, "open_sessions") as sessions:
            dialog.open_failed_sessions()
        sessions.assert_called_once_with(self.keys("web-02"))

    def test_results_arrive_in_batches(self) -> None:
        from ssh_manager.multirun import engine as R
        from ssh_manager.gtkui.dialogs import multirun as D

        dialog = self.dialog([])
        targets = [R.Target(key=f"k{i}", alias=f"h{i}", hostname="x") for i in range(1200)]
        run = R.MultiRun(targets, "true", connect=lambda *a: None)
        dialog.run = run
        started = time.perf_counter()
        dialog.results.reset(targets)
        for target in targets:
            run.events.put((target.key, R.HostResult(target.key, target.alias, status=R.SUCCESS, exit_code=0)))
        dialog._drain()
        elapsed = time.perf_counter() - started
        done = sum(1 for r in dialog.results.results.values() if r.status == R.SUCCESS)
        self.assertEqual(done, D.BATCH)  # one batch per tick, the rest next time
        self.assertLess(elapsed, 1.0)
        dialog._drain()
        dialog._drain()
        self.assertEqual(sum(1 for r in dialog.results.results.values() if r.status == R.SUCCESS), 1200)


class SavedCommandTests(MultiRunUiCase):
    SECRET = "Token-sentinel-in-a-command"

    def test_saved_and_history_with_a_keybox_are_encrypted(self) -> None:
        box = self.sandbox.use_keybox("pw")
        self.controller._store = None
        dialog = self.dialog(self.keys("web-01"))
        dialog.set_command(f"curl -H 'X-Token: {self.SECRET}' http://localhost/")
        dialog.ask_name = lambda: "token call"
        saved = dialog.save_current()
        self.assertEqual(saved.name, "token call")
        dialog.set_command("echo {alias}")
        dialog.start()
        self.finish(dialog)
        path = Path(self.win._settings.config_path).parent / "commands.enc"
        raw = path.read_bytes()
        self.assertNotIn(self.SECRET.encode(), raw)
        self.assertNotIn(b"echo {alias}", raw)
        self.controller._store = None  # read it again from the file
        second = self.dialog([])
        self.assertEqual(second.history.get_model()[1][1], "echo {alias}")
        commands = self.controller.store().commands()
        second.load_saved(commands[0])
        self.assertIn(self.SECRET, second.command_text)
        self.assertEqual(box.id, self.controller._store_key)
        second.clear_history()
        self.assertEqual(self.controller.store().history(), [])
        second._fill_saved_menu()
        labels = [item.get_label() for item in second.saved_menu.get_children() if hasattr(item, "get_label")]
        self.assertIn("token call", labels)

    def test_without_a_key_history_is_memory_only(self) -> None:
        self.controller._store = None
        dialog = self.dialog(self.keys("web-01"))
        dialog.set_command("uptime")
        dialog.start()
        self.finish(dialog)
        self.assertEqual(self.controller.store().history(), ["uptime"])
        self.assertFalse(self.controller.store().persistent)
        dialog.ask_name = lambda: "x"
        self.assertIsNone(dialog.save_current())
        config = Path(self.win._settings.config_path).parent
        self.assertEqual(list(config.glob("commands*")), [])


if __name__ == "__main__":
    unittest.main()
