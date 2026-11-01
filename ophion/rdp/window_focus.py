"""Bring an external FreeRDP window to the front (X11 only; best effort, pure Python).

Wayland gives no program a way to raise another program's window, so there
the "Focus window" button is disabled with that reason. On X11 the window is
found by the client's process id with ``xdotool`` or ``wmctrl`` when one of
them is installed (they are optional).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import Callable, List, Optional, Sequence, Tuple

Run = Callable[[Sequence[str]], Tuple[int, str]]


def _run(argv: Sequence[str]) -> Tuple[int, str]:
    try:
        done = subprocess.run(list(argv), stdin=subprocess.DEVNULL, capture_output=True, timeout=3, check=False,
                              close_fds=True)
    except (OSError, subprocess.TimeoutExpired):
        return -1, ""
    return done.returncode, done.stdout.decode("utf-8", "replace")[:65536]


def focus_tool(session_type: str = "", which: Callable[[str], Optional[str]] = shutil.which) -> str:
    """``"xdotool"``, ``"wmctrl"`` or "" (unavailable: Wayland, or neither tool installed)."""

    session = session_type or os.environ.get("XDG_SESSION_TYPE", "")
    if session == "wayland" or not os.environ.get("DISPLAY"):
        return ""
    for tool in ("xdotool", "wmctrl"):
        if which(tool):
            return tool
    return ""


def focus_pid(pid: int, tool: str, run: Run = _run) -> bool:
    """Activate the window of ``pid``; True when a window was found."""

    if tool == "xdotool":
        code, out = run(["xdotool", "search", "--pid", str(int(pid))])
        ids: List[str] = [line.strip() for line in out.splitlines() if line.strip().isdigit()]
        if code != 0 or not ids:
            return False
        return run(["xdotool", "windowactivate", ids[-1]])[0] == 0
    if tool == "wmctrl":
        code, out = run(["wmctrl", "-lp"])
        for line in out.splitlines():
            parts = line.split(None, 3)
            if len(parts) >= 3 and parts[2] == str(int(pid)) and parts[0].startswith("0x"):
                return run(["wmctrl", "-ia", parts[0]])[0] == 0
        return False
    return False


__all__ = ["focus_tool", "focus_pid"]
