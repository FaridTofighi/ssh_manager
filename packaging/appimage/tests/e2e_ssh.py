"""End-to-end password login through the app's own terminal path (VTE + bundled sshpass + bundled ssh).

    OPHION_APPIMAGE_EXEC=1 ./Ophion.AppImage python3 e2e_ssh.py HOST PORT USER PASSWORD

Run by tests/smoke.sh under Xvfb against a throw-away sshd whose host key is already in
~/.ssh/known_hosts. Passes when the session's LocalCommand sentinel reports the login
(``TerminalTab.authenticated``) and the processes that ran are the bundled sshpass / ssh.
"""

import os
import sys
import time

APPDIR = os.environ["OPHION_APPDIR"]
sys.path.insert(0, os.path.join(APPDIR, "usr/share/ophion"))

import gi  # noqa: E402

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # type: ignore  # noqa: E402

from ssh_manager.auth_flow import PasswordResolution  # noqa: E402
from ssh_manager.config import HostEntry  # noqa: E402
from ssh_manager.gtk_terminal import TerminalTab  # noqa: E402


def pump(seconds, until):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        if until():
            return True
        time.sleep(0.02)
    return until()


def exe_tree(pid):
    """(exe, ...) of ``pid`` and its descendants, read from /proc."""

    found = []
    try:
        found.append(os.path.realpath(f"/proc/{pid}/exe"))
        for task in os.listdir(f"/proc/{pid}/task"):
            with open(f"/proc/{pid}/task/{task}/children") as handle:
                for child in handle.read().split():
                    found.extend(exe_tree(int(child)))
    except OSError:
        pass
    return found


def main() -> int:
    host, port, user, password = sys.argv[1:5]
    assert Gtk.init_check(sys.argv)[0], "no display"
    logs = []
    entry = HostEntry("e2e", {"hostname": host, "port": port, "user": user})
    window = Gtk.OffscreenWindow()
    tab = TerminalTab(entry, lambda message, level: logs.append(str(message)), autostart=True,
                      password_provider=lambda: PasswordResolution(password, "credential", None))
    window.add(tab)
    window.show_all()
    pump(10, lambda: tab._child_pid is not None)
    exes = exe_tree(tab._child_pid) if tab._child_pid else []
    ok = pump(30, lambda: tab.authenticated)
    exes += exe_tree(tab._child_pid) if tab._child_pid else []
    print(f"authenticated: {ok}")
    print(f"processes: {sorted(set(exes))}")
    print(f"log: {logs[-5:]}")
    bundled = {os.path.basename(p) for p in exes if p.startswith(os.path.realpath(APPDIR) + os.sep)}
    tab.destroy()
    window.destroy()
    if not ok:
        print("FAIL: the login was not confirmed by the sentinel")
        return 1
    if not {"sshpass", "ssh"} <= bundled:
        print(f"FAIL: sshpass / ssh were not the bundled ones: {sorted(set(exes))}")
        return 1
    print("PASS end-to-end password login (bundled sshpass + ssh, sentinel confirmed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
