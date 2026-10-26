"""Toasts take input only on themselves: real X clicks (XTest) in the full main window after a failed session.

In-process synthesized events skip GDK's own picking, so this runs the
window in a child process on a private X server (Xvfb, else Xephyr) and
clicks through the X server like ``xdotool``. Skipped when neither server
nor libXtst is installed.
"""

import ctypes.util
import os
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _server_command(display: str):
    if shutil.which("Xvfb"):
        return ["Xvfb", display, "-screen", "0", "1600x1000x24", "-nolisten", "tcp"]
    if shutil.which("Xephyr"):
        return ["Xephyr", display, "-screen", "1600x1000", "-ac", "-br", "-nolisten", "tcp"]
    return None


def _free_display() -> str:
    for number in range(73, 99):
        if not Path(f"/tmp/.X11-unix/X{number}").exists() and not Path(f"/tmp/.X{number}-lock").exists():
            return f":{number}"
    raise unittest.SkipTest("no free X display number")


@unittest.skipUnless(_server_command(":0") and ctypes.util.find_library("Xtst"), "needs Xvfb or Xephyr and libXtst")
class RealClickTests(unittest.TestCase):
    """One probe run (about half a minute); the assertions read its report."""

    report = None

    @classmethod
    def setUpClass(cls) -> None:
        display = _free_display()
        server = subprocess.Popen(_server_command(display), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            socket = Path(f"/tmp/.X11-unix/X{display[1:]}")
            deadline = time.monotonic() + 10
            while not socket.exists() and time.monotonic() < deadline:
                time.sleep(0.1)
            if not socket.exists():
                raise unittest.SkipTest("the private X server did not start")
            env = dict(os.environ, DISPLAY=display, PYTHONPATH=str(ROOT))
            env.pop("WAYLAND_DISPLAY", None)
            proc = subprocess.run([sys.executable, "-m", "tests.toast_freeze_probe", "--map", "--xtest"], cwd=ROOT,
                                  env=env, capture_output=True, text=True, timeout=300)
            cls.output = proc.stdout + proc.stderr
            cls.report = {}
            cls.points = []
            for line in proc.stdout.splitlines():
                key, _sep, value = line.partition(": ")
                if key == "xtest_point":
                    cls.points.append(value)
                elif _sep:
                    cls.report[key] = value
        finally:
            server.terminate()
            try:
                server.wait(5)
            except subprocess.TimeoutExpired:
                server.kill()

    def test_the_failed_session_shows_the_error_toast(self) -> None:
        self.assertTrue(self.report.get("toast_shown", "").startswith("True"), self.output[-3000:])
        self.assertIn("Host key verification failed", self.report.get("toasts_visible", ""))

    def test_the_dismiss_button_dismisses_with_a_real_click(self) -> None:
        self.assertEqual(self.report.get("xtest_dismiss_removed_one"), "True", self.report.get("xtest_dismiss_press_log"))

    def test_a_click_on_a_toasts_text_stays_on_the_toast(self) -> None:
        log = self.report.get("xtest_toast_body_press_log", "")
        self.assertIn("button-press->", log)
        self.assertIn("Toast", log.split("button-press->", 1)[1].split(" grab=")[0])

    def test_clicks_beside_the_toasts_reach_the_widgets_below(self) -> None:
        self.assertGreaterEqual(len(self.points), 20, self.output[-3000:])
        swallowed = [p for p in self.points
                     if p.split(" ", 1)[1].startswith(("SSHManagerWindow", "no press", "ToastOverlay", "Toast",
                                                       "Revealer"))]
        self.assertEqual(swallowed, [])

    def test_no_grab_and_no_extra_window_after_the_failure(self) -> None:
        # Audited right after the failed session, before any click (a click on Reconnect opens a prompt).
        self.assertEqual(self.report.get("gtk_grab"), "None")
        self.assertEqual(self.report.get("visible_toplevels"),
                         "['SSHManagerWindow(modal=False,transient=False,type=toplevel)']")
        self.assertEqual(self.report.get("open_popovers_or_menus"), "[]")


if __name__ == "__main__":
    unittest.main()
