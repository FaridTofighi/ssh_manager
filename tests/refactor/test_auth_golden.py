"""Characterization (§2.4.2): ssh output and sshpass exit codes through the auth / host-key code paths.

Part 1 (pure): every ``tests/fixtures/ssh_output`` capture and a few typical messages through
``classify_exit`` (exit codes 0, 5, 6, 255, killed), ``detect_changed`` / ``detect_unknown``,
``passphrase_prompt`` and ``ends_with_password_prompt``.

Part 2 (a real ``TerminalTab``, VTE's spawn mocked as in ``tests/test_auth_flow.py``): a password session
is spawned, the output is fed into the terminal and the child exits with each code. The golden records
the status line, the log, whether a password retry was scheduled, the host-key bar calls and the
``session-closed`` emissions. The password dialogs are never shown.
"""

import dataclasses
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from ophion.config import HostEntry
from tests.gtk_window_fixture import display_available
from tests.refactor import golden

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "ssh_output"
PASSWORD = "flow-test-password"
MESSAGES = {
    "permission_denied_retry": "deploy@web.internal's password: \r\nPermission denied, please try again.\r\n",
    "publickey_only": "deploy@web.internal: Permission denied (publickey).\r\n",
    "connection_refused": "ssh: connect to host web.internal port 22: Connection refused\r\n",
    "otp_prompt": "Verification code: ",
    "too_many_failures": "Received disconnect from 10.0.0.5 port 22:2: Too many authentication failures\r\n",
    "empty": "",
}
CODES = {"ok": 0, "sshpass_wrong_password": 5, "sshpass_host_key": 6, "ssh_error": 255}


def captures() -> dict:
    # Bytes, decoded: these are raw terminal captures (read_text() would translate their line ends).
    texts = {path.stem: path.read_bytes().decode("utf-8") for path in sorted(FIXTURES.glob("*.txt"))}
    texts.update(MESSAGES)
    return texts


def plain(value):
    if value is None:
        return None
    if dataclasses.is_dataclass(value):
        return {key: plain(item) for key, item in dataclasses.asdict(value).items()}
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value]
    return value if isinstance(value, (str, int, float, bool)) else str(value)


class ClassificationTests(unittest.TestCase):
    def test_outputs_and_exit_codes(self) -> None:
        from ophion.auth_flow import classify_exit, ends_with_password_prompt, passphrase_prompt
        from ophion.host_key_output import detect_changed, detect_unknown

        result = {}
        for name, text in captures().items():
            result[name] = {
                "classify_exit": {code: classify_exit(status, text).value
                                  for code, status in {**CODES, "killed": -1}.items()},
                "changed_after_exit": plain(detect_changed(text, exited=True)),
                "changed_while_running": plain(detect_changed(text, exited=False)),
                "unknown": plain(detect_unknown(text)),
                "passphrase_prompt": passphrase_prompt(text),
                "ends_with_password_prompt": ends_with_password_prompt(text),
            }
        golden.check(self, "auth_classification", result)


@unittest.skipUnless(display_available(), "needs a display")
class SessionExitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for patch in (
            mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": self.tmp.name}),
            mock.patch("ophion.gtk_terminal.shutil.which", return_value="/usr/bin/sshpass"),
            mock.patch("ophion.gtk_terminal.os.kill"),
            mock.patch("ophion.gtk_terminal.TerminalTab._preflight_then_start",
                       lambda tab, argv, envv: tab._start_child(argv, envv)),
            mock.patch("ophion.gtk_terminal.PasswordRequestDialog"),
            mock.patch("ophion.gtk_terminal.SavePasswordDialog"),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def pump(self, seconds: float) -> None:
        from gi.repository import Gtk

        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            while Gtk.events_pending():
                Gtk.main_iteration_do(False)
            time.sleep(0.01)

    def run_exit(self, text: str, code: int) -> dict:
        from ophion.auth_flow import PasswordResolution
        from ophion.gtk_terminal import TerminalTab

        logs, closed = [], []
        tab = TerminalTab(HostEntry("web", {"hostname": "web.internal", "user": "deploy"}),
                          lambda message, level=None: logs.append([message, getattr(level, "value", level)]),
                          autostart=False, password_provider=lambda: PasswordResolution(PASSWORD, "credential", "c1"))
        self.addCleanup(tab.destroy)
        tab._terminal.spawn_async = mock.Mock()
        tab.connect("session-closed", lambda _tab, alias, ok: closed.append([alias, ok]))
        bar_calls = []
        show_bar = tab._show_host_key_bar
        tab._show_host_key_bar = lambda kind, markup: bar_calls.append([kind, markup]) or show_bar(kind, markup)
        tab._spawn()
        call = tab._terminal.spawn_async.call_args[0]
        call[9](tab._terminal, 4242, None, None)  # the spawn callback: the child runs
        tab._terminal.feed(text.encode("utf-8"))  # as the pty delivered it
        self.pump(0.3)  # VTE takes the text in on a timer
        tab._on_child_exited(tab._terminal, code << 8)
        self.pump(0.05)
        record = {
            "status": tab._statusbar.status_label.get_text(),
            "banner": tab._status_label.get_text(),
            "logs": logs,
            "retry_scheduled": bool(tab._retry_timeout_id),
            "retry_pending": tab._auth_retry_pending,
            "host_key_bar": bar_calls,
            "session_closed": closed,
        }
        return golden.mask(record, (PASSWORD,))

    def test_exit_matrix(self) -> None:
        result = {}
        for name, text in captures().items():
            for label, code in CODES.items():
                with self.subTest(output=name, exit=label):
                    result[f"{name}/{label}"] = self.run_exit(text, code)
        self.assertFalse(any(PASSWORD in str(value) for value in result.values()))  # R8: never in a log
        golden.check(self, "auth_session_exits", result)


if __name__ == "__main__":
    unittest.main()
