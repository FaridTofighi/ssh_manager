#!/usr/bin/env python3
"""Run the test suite on a private X server with fixed GTK settings.

On a desktop session the compositor may never let an unseen test window draw a frame. GTK applies
CSS and lays widgets out in that frame step, so layout tests then measure stale sizes (after a
density switch, for one). Fonts, DPI and the user's own GTK settings also change every measurement.
This runner gives every run the same display:

* a private X server: Xvfb, else Xephyr (it opens a window on the current display), 1920x1200 at
  96 dpi, the resolution the snapshots and pixel goldens are taken at;
* GTK settings from a temporary ``XDG_CONFIG_HOME`` (``gtk-3.0/settings.ini``: Adwaita, Sans 10,
  96 dpi), so ``~/.config`` is neither read nor written;
* GSettings in memory, its own D-Bus session (``dbus-run-session`` when installed), no
  accessibility bridge; ``XDG_DATA_DIRS`` without Flatpak's exports, so the private bus cannot
  start the user's Flatpak apps (a sync client started there with no settings asks for a login);
* no real application is ever launched: links, files and folders open in a no-op handler (a
  temporary ``XDG_DATA_HOME`` with a ``mimeapps.list``; ``BROWSER=true``), and the runner lists
  what the tests asked to open.

Usage::

    tools/run_gtk_tests.py                      # the whole suite (pytest, else unittest)
    tools/run_gtk_tests.py tests/test_x.py -k y # arguments go to pytest
    tools/run_gtk_tests.py -- python3 -m unittest tests.test_x   # any command

The exit status is the command's; the server is always stopped.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import List, Optional

ROOT = Path(__file__).resolve().parents[1]
SCREEN = "1920x1200x24"
DPI = 96
SETTINGS = f"""[Settings]
gtk-theme-name=Adwaita
gtk-icon-theme-name=Adwaita
gtk-font-name=Sans 10
gtk-xft-dpi={DPI * 1024}
gtk-xft-antialias=1
gtk-xft-hinting=1
gtk-xft-hintstyle=hintslight
gtk-xft-rgba=none
gtk-application-prefer-dark-theme=0
"""
# Every URI a test opens goes here instead of a browser or file manager (see opened.log).
NOOP_DESKTOP = """[Desktop Entry]
Type=Application
Name=Test opener (does nothing)
Exec=/bin/sh -c 'printf "%%s\\n" "$*" >> "$OPHION_TEST_OPENED"' opener %U
NoDisplay=true
MimeType={types};
"""
OPENED_TYPES = ("x-scheme-handler/http", "x-scheme-handler/https", "x-scheme-handler/file", "inode/directory",
                "text/plain", "text/html", "text/markdown", "application/pdf", "application/octet-stream")


def server_command(display: str) -> Optional[List[str]]:
    if shutil.which("Xvfb"):
        return ["Xvfb", display, "-screen", "0", SCREEN, "-dpi", str(DPI), "-nolisten", "tcp"]
    if shutil.which("Xephyr") and os.environ.get("DISPLAY"):
        width, height, _depth = SCREEN.split("x")
        return ["Xephyr", display, "-screen", f"{width}x{height}", "-dpi", str(DPI), "-ac", "-br", "-nolisten", "tcp"]
    return None


def free_display() -> str:
    for number in range(80, 200):
        if not Path(f"/tmp/.X11-unix/X{number}").exists() and not Path(f"/tmp/.X{number}-lock").exists():
            return f":{number}"
    raise SystemExit("run_gtk_tests: no free X display number")


def wait_for(display: str, server: subprocess.Popen, seconds: float = 15.0) -> None:
    socket = Path(f"/tmp/.X11-unix/X{display.lstrip(':')}")
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if server.poll() is not None:
            raise SystemExit(f"run_gtk_tests: the X server exited ({server.returncode})")
        if socket.exists():
            if not shutil.which("xdpyinfo"):
                return
            probe = subprocess.run(["xdpyinfo", "-display", display], stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
            if probe.returncode == 0:
                return
        time.sleep(0.1)
    raise SystemExit("run_gtk_tests: the X server did not start")


def system_data_dirs() -> str:
    """``XDG_DATA_DIRS`` without Flatpak exports (their D-Bus services would start apps on the test bus)."""

    current = os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"
    kept = [d for d in current.split(":") if d and "/flatpak/" not in d]
    for needed in ("/usr/local/share", "/usr/share"):
        if needed not in kept:
            kept.append(needed)
    return ":".join(kept)


def default_command(args: List[str]) -> List[str]:
    try:
        import pytest  # noqa: F401
    except ImportError:
        return [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."] + args
    return [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"] + (args or ["tests"])


def main(argv: List[str]) -> int:
    if "--" in argv:
        command = argv[argv.index("--") + 1:]
    else:
        command = default_command(argv)
    display = free_display()
    server = server_command(display)
    if server is None:
        raise SystemExit("run_gtk_tests: needs Xvfb (or Xephyr inside a desktop session)")
    with tempfile.TemporaryDirectory(prefix="ophion-gtk-tests-") as tmp:
        config = Path(tmp) / "config"
        (config / "gtk-3.0").mkdir(parents=True)
        (config / "gtk-3.0" / "settings.ini").write_text(SETTINGS, encoding="utf-8")
        data = Path(tmp) / "data"
        (data / "applications").mkdir(parents=True)
        (data / "applications" / "ophion-test-opener.desktop").write_text(
            NOOP_DESKTOP.format(types=";".join(OPENED_TYPES)), encoding="utf-8")
        defaults = "\n".join(f"{kind}=ophion-test-opener.desktop" for kind in OPENED_TYPES)
        (config / "mimeapps.list").write_text(f"[Default Applications]\n{defaults}\n", encoding="utf-8")
        opened = Path(tmp) / "opened.log"
        log = open(Path(tmp) / "xserver.log", "w")
        process = subprocess.Popen(server, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            wait_for(display, process)
            env = dict(os.environ)
            for name in ("WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS", "GTK_THEME", "GDK_SCALE", "GDK_DPI_SCALE"):
                env.pop(name, None)
            env.update({
                "DISPLAY": display,
                "GDK_BACKEND": "x11",
                "XDG_CONFIG_HOME": str(config),
                "XDG_DATA_HOME": str(data),
                "XDG_DATA_DIRS": system_data_dirs(),
                "OPHION_TEST_OPENED": str(opened),
                "BROWSER": "true",
                "GSETTINGS_BACKEND": "memory",
                "NO_AT_BRIDGE": "1",
                "GIO_USE_VFS": "local",  # no gvfs daemon on the private bus
                "OPHION_TEST_DISPLAY": f"private-{DPI}dpi",  # tests with pixel goldens of this display check them
            })
            if shutil.which("dbus-run-session"):
                command = ["dbus-run-session", "--"] + command
            print(f"run_gtk_tests: {Path(server[0]).name} {display} {SCREEN} at {DPI} dpi", flush=True)
            status = subprocess.call(command, cwd=ROOT, env=env)
            if opened.exists() and opened.read_text().strip():
                print("run_gtk_tests: the tests asked to open (nothing was launched):", flush=True)
                for line in opened.read_text().splitlines():
                    print(f"    {line}", flush=True)
            return status
        except KeyboardInterrupt:
            return 130
        finally:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=5)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            log.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
