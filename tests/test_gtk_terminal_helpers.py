import threading
import time
import unittest

from ophion.config import HostEntry
from ophion.password_change import PasswordChangeTracker
from ophion.gtk_terminal import TerminalTab
from ophion.gtk_terminal import (
    auth_failure_output_detected,
    broadcast_target_tabs,
    build_remote_stats_command,
    parse_remote_stats_output,
    session_tile_position,
    terminal_key_event_payload,
    terminal_text,
)
from gi.repository import Gdk, Vte  # type: ignore
from ophion.text_utils import strip_ansi


class GtkTerminalHelperTests(unittest.TestCase):
    def test_terminal_text_passes_null_attributes_argument(self) -> None:
        class FakeTerminal:
            def __init__(self) -> None:
                self.args = None

            def get_text(self, *args):
                self.args = args
                return ("terminal output", object())

        term = FakeTerminal()

        self.assertEqual(terminal_text(term), "terminal output")
        self.assertEqual(len(term.args), 3)
        self.assertIsNone(term.args[2])

    def test_terminal_text_reads_whole_scrollback_with_range_format(self) -> None:
        class FakeAdjustment:
            def get_lower(self) -> float:
                return 3.0

            def get_upper(self) -> float:
                return 203.0

        class FakeTerminal:
            def __init__(self) -> None:
                self.args = None

            def get_vadjustment(self):
                return FakeAdjustment()

            def get_column_count(self) -> int:
                return 80

            def get_text_range_format(self, *args):
                self.args = args
                return ("whole scrollback", 16)

            def get_text(self, *args):  # deprecated path must not be used
                raise AssertionError("get_text called")

        term = FakeTerminal()

        self.assertEqual(terminal_text(term), "whole scrollback")
        self.assertEqual(term.args, (Vte.Format.TEXT, 3, 0, 202, 79))

    def test_focus_retry_gives_up_on_a_terminal_that_is_never_shown(self) -> None:
        from unittest import mock

        from ophion import gtk_terminal

        class HiddenTerminal:
            grabbed = False

            def get_realized(self):
                return False

            def get_mapped(self):
                return False

            def grab_focus(self):
                self.grabbed = True

        term = HiddenTerminal()
        dummy = type("DummyTab", (), {"_active_terminal": lambda self: term})()
        scheduled = []
        with mock.patch.object(gtk_terminal.GLib, "timeout_add", side_effect=lambda ms, cb: scheduled.append((ms, cb))), \
                mock.patch.object(gtk_terminal.GLib, "idle_add", side_effect=AssertionError("busy idle retry")):
            TerminalTab.focus_terminal(dummy)
        self.assertEqual(len(scheduled), 1)
        interval, retry = scheduled[0]
        self.assertGreaterEqual(interval, 20)  # a timer, not a busy idle loop
        calls = 1
        while retry():
            calls += 1
            self.assertLess(calls, 1000, "the retry never gives up")
        self.assertEqual(calls, gtk_terminal.FOCUS_RETRY_LIMIT - 1)
        self.assertFalse(term.grabbed)

    def test_banner_is_left_alone_after_destroy(self) -> None:
        from gi.repository import Gtk  # type: ignore

        calls = []

        class RecordingBar(Gtk.InfoBar):
            def set_revealed(self, revealed: bool) -> None:
                calls.append(revealed)
                Gtk.InfoBar.set_revealed(self, revealed)

        dummy = type("DummyTab", (), {"_banner": RecordingBar(), "_banner_alive": True})()
        dummy._banner.connect("destroy", lambda bar: TerminalTab._on_banner_destroy(dummy, bar))

        TerminalTab._reveal_banner(dummy, True)
        dummy._banner.destroy()
        TerminalTab._reveal_banner(dummy, False)  # child exited after the tab closed

        self.assertEqual(calls, [True])
        self.assertFalse(dummy._banner_alive)

    def test_older_vte_text_is_read_through_write_contents(self) -> None:
        # VTE < 0.72 has no get_text_range_format; get_text / get_text_range print two VTE-WARNINGs per
        # call there (ignored callback, deprecated attributes array), so the contents are written to a
        # memory stream instead.
        class OldTerminal:
            def __init__(self) -> None:
                self.flags = None

            def get_cursor_position(self):
                return 9, 3

            def get_column_count(self) -> int:
                return 80

            def write_contents_sync(self, stream, flags, cancellable):
                self.flags = flags
                stream.write_all("first line\nPassword: \n\n\n".encode(), None)
                return True

            def get_text(self, *args):
                raise AssertionError("get_text called")

            def get_text_range(self, *args):
                raise AssertionError("get_text_range called")

        term = OldTerminal()
        self.assertEqual(terminal_text(term), "first line\nPassword: \n\n\n")
        self.assertEqual(term.flags, Vte.WriteFlags.DEFAULT)
        self.assertEqual(TerminalTab._terminal_tail(object(), term), "first line\nPassword: ")
        self.assertEqual(TerminalTab._terminal_tail(object(), term, limit=10), "Password: ")

    def test_write_contents_reads_a_real_terminal(self) -> None:
        from ophion.gtk_terminal import _terminal_contents

        import time

        from gi.repository import Gtk  # type: ignore

        term = Vte.Terminal()
        term.feed(b"hello marker\r\nsecond line\r\n")
        deadline = time.monotonic() + 5
        while "second line" not in (_terminal_contents(term) or "") and time.monotonic() < deadline:
            while Gtk.events_pending():  # VTE processes fed data from the main loop
                Gtk.main_iteration_do(False)
            time.sleep(0.01)
        self.assertIn("hello marker\nsecond line", _terminal_contents(term) or "")

    def test_terminal_text_real_vte_returns_scrollback(self) -> None:
        term = Vte.Terminal()
        term.feed(b"hello marker\r\nsecond line\r\n")
        text = terminal_text(term)
        self.assertIsInstance(text, str)

    def test_terminal_tail_uses_range_format_api(self) -> None:
        class FakeTerminal:
            def __init__(self) -> None:
                self.range_format_args = None

            def get_cursor_position(self):
                return 12, 20

            def get_column_count(self) -> int:
                return 80

            def get_text_range_format(self, *args):
                self.range_format_args = args
                return ("New password:", object())

        term = FakeTerminal()

        self.assertEqual(TerminalTab._terminal_tail(object(), term), "New password:")
        self.assertEqual(term.range_format_args[0], Vte.Format.TEXT)
        self.assertEqual(len(term.range_format_args), 5)

    def test_observe_terminal_input_stages_confirmed_password_change(self) -> None:
        staged = []
        tails = iter(["New password:", "Retype new password:"])

        dummy = type(
            "DummyTab",
            (),
            {
                "_password_change_tracker": PasswordChangeTracker(),
                "_terminal_tail": lambda self, term: next(tails),
                "_stage_detected_password_change": lambda self, password, term: staged.append((password, term)),
            },
        )()
        term = object()

        TerminalTab._observe_terminal_input(dummy, term, "new-secret\r")
        TerminalTab._observe_terminal_input(dummy, term, "new-secret\r")

        self.assertEqual(staged, [("new-secret", term)])

    def test_password_change_poll_persists_success_output(self) -> None:
        persisted = []
        tracker = PasswordChangeTracker()
        tracker.observe_input("New password:", "new-secret\r")
        tracker.observe_input("Retype new password:", "new-secret\r")
        dummy = type(
            "DummyTab",
            (),
            {
                "_password_change_tracker": tracker,
                "_terminal_tail": lambda self, term: "passwd: all authentication tokens updated successfully.",
                "_persist_detected_password_change": lambda self, password: persisted.append(password),
                "_authenticated": True,
                "_password_prompt_poll_source_id": 99,
                "_password_change_terminal": None,
                "_focused_terminal": object(),
                "_terminal": object(),
            },
        )()

        result = TerminalTab._poll_password_change_output(dummy)

        self.assertFalse(result)
        self.assertEqual(persisted, ["new-secret"])
        self.assertEqual(dummy._password_prompt_poll_source_id, 0)

    def test_auth_failure_output_detects_permission_denied_retry_prompt(self) -> None:
        self.assertTrue(auth_failure_output_detected("Permission denied, please try again."))
        self.assertTrue(auth_failure_output_detected("alice@example.com's password:\nPermission denied (password)."))
        self.assertTrue(auth_failure_output_detected("Permission denied (publickey,gssapi-keyex,gssapi-with-mic,password)."))
        self.assertFalse(auth_failure_output_detected("Last login: today"))

    def test_password_poll_handles_auth_failure_output(self) -> None:
        calls = []
        dummy = type(
            "DummyTab",
            (),
            {
                "_password_change_terminal": None,
                "_focused_terminal": object(),
                "_terminal": object(),
                "_terminal_tail": lambda self, term: "Permission denied, please try again.",
                "_handle_auth_failure_retry": lambda self: calls.append("retry") or True,
                "_password_prompt_poll_source_id": 99,
                "_authenticated": False,  # before login: the text is ssh's own
            },
        )()

        result = TerminalTab._poll_password_change_output(dummy)

        self.assertFalse(result)
        self.assertEqual(calls, ["retry"])
        self.assertEqual(dummy._password_prompt_poll_source_id, 0)

    def test_auth_failure_retry_never_deletes_and_schedules_prompt(self) -> None:
        # Changed deliberately (Phase 1): a rejected password may be a shared credential; it is never deleted.
        deleted = []
        actions = []

        class Store:
            def delete(self, host, port, username) -> None:
                deleted.append((host, port, username))

        dummy = type(
            "DummyTab",
            (),
            {
                "_auth_retry_pending": False,
                "_password_source": "credential",
                "_PASSWORD_SOURCES": TerminalTab._PASSWORD_SOURCES,
                "_session_password": None,
                "_secure_store": Store(),
                "_credential_components": lambda self: ("example.com", 22, "alice"),
                "_terminate_current_child": lambda self: actions.append("terminated"),
                "_set_status": lambda self, message, connected: actions.append((message, connected)),
                "_log": lambda self, message, success: actions.append((message, success)),
                "_host": HostEntry(alias="app"),
                "_schedule_retry_prompt": lambda self: actions.append("prompt"),
            },
        )()

        handled = TerminalTab._handle_auth_failure_retry(dummy)

        self.assertTrue(handled)
        self.assertTrue(dummy._auth_retry_pending)
        self.assertEqual(deleted, [])
        self.assertIn("terminated", actions)
        self.assertIn("prompt", actions)

    def test_child_exit_handles_auth_failure_output_even_after_local_spawn_connected(self) -> None:
        calls = []
        dummy = type(
            "DummyTab",
            (),
            {
                "_child_pid": 123,
                "_auth_failure_output": lambda self, term: "Permission denied (publickey,password).",
                "_handle_auth_failure_retry": lambda self: calls.append("retry") or True,
                "_set_status": lambda self, message, connected: calls.append(("status", message, connected)),
                "_auth_retry_pending": False,
                "_host": HostEntry(alias="app"),
                "_authenticated": False,
                "_check_marker_now": lambda self: False,
                "_stop_auth_sources": lambda self, keep_retry=False: None,
            },
        )()

        TerminalTab._on_child_exited(dummy, object(), 0)

        self.assertIsNone(dummy._child_pid)
        self.assertEqual(calls, ["retry"])

    def test_terminal_commit_skips_recent_key_duplicate(self) -> None:
        observed = []
        term = object()
        dummy = type(
            "DummyTab",
            (),
            {
                "_last_key_input": (term, "n", time.monotonic()),
                "_observe_terminal_input": lambda self, observed_term, text: observed.append((observed_term, text)),
            },
        )()

        TerminalTab._on_terminal_commit(dummy, term, "n", 1)

        self.assertEqual(observed, [])

    def test_terminal_key_press_observes_input_even_with_commit_handler(self) -> None:
        observed = []
        term = object()
        event = type("Event", (), {"keyval": Gdk.KEY_n, "state": 0, "string": "n"})()
        dummy = type(
            "DummyTab",
            (),
            {
                "_session_active": True,
                "_input_commit_handlers": {term: 1},
                "_input_observer": None,
                "_observe_terminal_input": lambda self, observed_term, text: observed.append((observed_term, text)),
            },
        )()

        handled = TerminalTab._on_terminal_key_press(dummy, term, event)

        self.assertFalse(handled)
        self.assertEqual(observed, [(term, "n")])
        self.assertEqual(dummy._last_key_input[0], term)
        self.assertEqual(dummy._last_key_input[1], "n")

    def test_strip_ansi_removes_csi_osc_and_bell(self) -> None:
        text = "\x1b[31mred\x1b[0m\x1b]0;title\x07bell\x07\x1b]2;tab\x1b\\done"
        self.assertEqual(strip_ansi(text), "redbelldone")

    def test_apply_stats_result_ignores_wrong_value_types(self) -> None:
        class DummySpinner:
            def stop(self) -> None:
                pass

            def hide(self) -> None:
                pass

        class DummyLabel:
            def __init__(self) -> None:
                self.text = ""

            def set_text(self, text: str) -> None:
                self.text = text

        dummy = type(
            "DummyTab",
            (),
            {
                "_info_spinner": DummySpinner(),
                "_stats_lock": threading.Lock(),
                "_stats_updating": True,
                "_cpu_label": DummyLabel(),
                "_mem_label": DummyLabel(),
                "_disk_label": DummyLabel(),
            },
        )()

        result = TerminalTab._apply_stats_result(
            dummy,
            {
                "cpu": "fast",
                "mem_used": True,
                "mem_total": 1024,
                "disk_used": 1.5,
                "disk_total": "large",
            },
        )

        self.assertFalse(result)
        self.assertEqual(dummy._cpu_label.text, "CPU: N/A")
        self.assertEqual(dummy._mem_label.text, "Mem: N/A")
        self.assertEqual(dummy._disk_label.text, "Disk: N/A")

    def test_parse_remote_stats_output_requires_json_object(self) -> None:
        self.assertEqual(parse_remote_stats_output('{"cpu": 1.5}'), {"cpu": 1.5})
        self.assertIsNone(parse_remote_stats_output(""))
        self.assertIsNone(parse_remote_stats_output("[]"))
        self.assertIsNone(parse_remote_stats_output("not json"))

    def test_build_remote_stats_command_uses_sshpass_when_password_available(self) -> None:
        command, env = build_remote_stats_command(
            ["ssh", "app.internal"],
            password="secret",
            sshpass_path="/usr/bin/sshpass",
        )

        self.assertEqual(command[:2], ["/usr/bin/sshpass", "-e"])
        self.assertIn("ConnectTimeout=6", command)
        self.assertNotIn("BatchMode=yes", command)
        self.assertEqual(command[-2:], ["python3", "-"])
        self.assertIsNotNone(env)
        assert env is not None
        self.assertEqual(env["SSHPASS"], "secret")

    def test_build_remote_stats_command_uses_batch_mode_without_password(self) -> None:
        command, env = build_remote_stats_command(["ssh", "app.internal"])

        self.assertEqual(command[:5], ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6"])
        self.assertEqual(command[-2:], ["python3", "-"])
        self.assertIsNone(env)

    def test_fetch_stats_via_paramiko_uses_existing_session(self) -> None:
        class DummyChannel:
            def shutdown_write(self) -> None:
                pass

            def recv_exit_status(self) -> int:
                return 0

        class DummyStdin:
            def __init__(self) -> None:
                self.channel = DummyChannel()
                self.payload = ""

            def write(self, data: str) -> None:
                self.payload += data

            def flush(self) -> None:
                pass

            def close(self) -> None:
                pass

        class DummyStdout:
            channel = DummyChannel()

            def read(self) -> bytes:
                return b'{"cpu": 5, "mem_used": 1024, "mem_total": 2048, "disk_used": 1, "disk_total": 2}'

        class DummyClient:
            def __init__(self) -> None:
                self.stdin = DummyStdin()
                self.commands = []

            def exec_command(self, command: str, timeout: int):
                self.commands.append((command, timeout))
                return self.stdin, DummyStdout(), None

        client = DummyClient()
        dummy = type(
            "DummyTab",
            (),
            {
                "_session_client": client,
                "_host": HostEntry(alias="app"),
            },
        )()

        data = TerminalTab._fetch_stats_via_paramiko(dummy)

        self.assertEqual(client.commands, [("python3 -", 9)])
        self.assertIn("cpu_percent", client.stdin.payload)
        self.assertEqual(data["cpu"], 5)

    def test_session_tile_position_balances_grid(self) -> None:
        self.assertEqual([session_tile_position(i, 4) for i in range(4)], [(0, 0), (0, 1), (1, 0), (1, 1)])
        self.assertEqual([session_tile_position(i, 5) for i in range(5)], [(0, 0), (0, 1), (0, 2), (1, 0), (1, 1)])
        with self.assertRaises(ValueError):
            session_tile_position(-1, 2)

    def test_broadcast_target_tabs_respects_inclusion_and_connection_state(self) -> None:
        source = object()
        included_target = object()
        excluded_target = object()
        disconnected_target = object()

        targets = broadcast_target_tabs(
            [source, included_target, excluded_target, disconnected_target],
            source,
            {source: True, included_target: True, excluded_target: False, disconnected_target: True},
            {source: True, included_target: True, excluded_target: True, disconnected_target: False},
        )

        self.assertEqual(targets, [included_target])
        self.assertEqual(
            broadcast_target_tabs([source, included_target], source, {source: False, included_target: True}, {source: True, included_target: True}),
            [],
        )

    def test_terminal_key_event_payload_maps_command_keys(self) -> None:
        event = type("Event", (), {"keyval": Gdk.KEY_Return, "state": 0, "string": ""})()
        self.assertEqual(terminal_key_event_payload(event), "\r")

        event.keyval = Gdk.KEY_BackSpace
        self.assertEqual(terminal_key_event_payload(event), "\x7f")

        event.keyval = Gdk.KEY_Up
        self.assertEqual(terminal_key_event_payload(event), "\x1b[A")

        event.keyval = Gdk.KEY_a
        event.string = "a"
        self.assertEqual(terminal_key_event_payload(event), "a")

        event.state = Gdk.ModifierType.MOD1_MASK
        self.assertIsNone(terminal_key_event_payload(event))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
