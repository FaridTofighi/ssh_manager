"""Smoke start for tools/refactor_smoke.sh: the real main window with a sample inventory, shown ~5 s, closed.

Uses the tests' WindowSandbox (temporary HOME / XDG folders, the sample inventory in the app store): the
window is built as the app builds it, without the start-up dialogs. Exit 0 = the window was mapped and closed.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import gi  # noqa: E402

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402

from tests.gtk_window_fixture import WindowSandbox  # noqa: E402


def main() -> int:
    sandbox = WindowSandbox(store=True)
    window = sandbox.window
    window.resize(1200, 800)
    window.show_all()
    Gtk.Window.show(window)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        time.sleep(0.02)
    mapped = window.get_mapped()
    hosts = sum(len(group.hosts) for groups in window.manager.tree.values() for group in groups.values())
    sandbox.close()
    print(f"smoke: main window mapped={mapped}, {hosts} hosts in the tree")
    return 0 if mapped and hosts else 1


if __name__ == "__main__":
    sys.exit(main())
