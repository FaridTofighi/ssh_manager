"""The per-user launcher, so the desktop shows Ophion's icon for its running windows (no GTK).

GNOME (and most docks / task switchers) take the icon of a running app from its
``.desktop`` file, matched by the window class (``StartupWMClass``) or the
application id — not from the icon a window sets on itself. A copy started from
source or from the AppImage has no such file installed, so at start-up
:func:`ensure_launcher` writes ``$XDG_DATA_HOME/applications/<APP_ID>.desktop``
(``Icon=ophion``, ``Exec`` = how this copy was started) and copies the icon
files to ``$XDG_DATA_HOME/icons/hicolor/<size>/apps/ophion.png``.

Only a launcher this module wrote (``X-Ophion-Generated=true``) is ever
rewritten — when it is missing or out of date (the checkout or the AppImage
moved); one the user made or edited is left alone. An installed package's own
launcher in ``/usr/share/applications`` is not duplicated. Failures are logged
and ignored: a missing launcher only costs the icon.
"""

from __future__ import annotations

import filecmp
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Mapping, Optional, Sequence

from .identity import APP_ID, APP_NAME, APP_SLUG, APP_TAGLINE

LOGGER = logging.getLogger(__name__)
MARKER = "X-Ophion-Generated=true"
PACKAGE_DIR = Path(__file__).resolve().parent
ICON_SOURCE = PACKAGE_DIR / "gtkui" / "assets" / "app-icon" / "hicolor"


def data_home(environ: Mapping[str, str]) -> Path:
    value = environ.get("XDG_DATA_HOME")
    return Path(value).expanduser() if value else Path(environ.get("HOME") or Path.home()) / ".local" / "share"


def system_launcher_exists(environ: Mapping[str, str]) -> bool:
    """An installed package already provides the launcher (``$XDG_DATA_DIRS/applications``)."""

    dirs = environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"
    appdir = environ.get("APPDIR") or environ.get("OPHION_APPDIR") or ""
    for folder in dirs.split(":"):
        if not folder or (appdir and folder.startswith(appdir)):
            continue  # the AppImage's own copy is not installed on the system
        if (Path(folder) / "applications" / f"{APP_ID}.desktop").is_file():
            return True
    return False


def _quote(argument: str) -> str:
    """One ``Exec`` argument (Desktop Entry spec: quoted when needed, ``%`` doubled)."""

    argument = argument.replace("%", "%%")
    if not any(ch in argument for ch in ' \t\n"\'\\><~|&;$*?#()`'):
        return argument
    return '"' + "".join("\\" + ch if ch in '"`$\\' else ch for ch in argument) + '"'


def launch_command(environ: Mapping[str, str], argv0: str = "", executable: str = "") -> Optional[List[str]]:
    """How to start this copy again: the AppImage file, else Python with ``main.py``; ``None`` if unknown."""

    appimage = environ.get("APPIMAGE", "")
    if appimage and os.path.isfile(appimage):
        return [appimage]
    main = PACKAGE_DIR.parent / "main.py"
    python = executable or sys.executable
    if main.is_file() and python:
        return [python, str(main)]
    return None


def desktop_entry(command: Sequence[str]) -> str:
    return "\n".join([
        "[Desktop Entry]",
        "Type=Application",
        f"Name={APP_NAME}",
        f"GenericName={APP_TAGLINE}",
        "Comment=Organise SSH hosts, open terminals, transfer files and connect to remote desktops",
        "Exec=" + " ".join(_quote(part) for part in command) + " %U",
        f"Icon={APP_SLUG}",
        "Terminal=false",
        "Categories=Network;RemoteAccess;",
        "Keywords=ssh;sftp;ftp;rdp;terminal;remote;",
        f"StartupWMClass={APP_ID}",
        "StartupNotify=true",
        MARKER,
        "",
    ])


def _install_icons(icons: Path) -> bool:
    changed = False
    for size_dir in sorted(ICON_SOURCE.glob("*x*")):
        source = size_dir / "apps" / f"{APP_SLUG}.png"
        target = icons / "hicolor" / size_dir.name / "apps" / f"{APP_SLUG}.png"
        if not source.is_file() or (target.is_file() and filecmp.cmp(source, target, shallow=False)):
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        changed = True
    if changed:
        hicolor = icons / "hicolor"
        os.utime(hicolor)  # icon caches compare the folder's time
        if (hicolor / "index.theme").is_file() and shutil.which("gtk-update-icon-cache"):
            subprocess.run(["gtk-update-icon-cache", "-f", "-t", "-q", str(hicolor)], check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
    return changed


def ensure_launcher(environ: Optional[Mapping[str, str]] = None, *, executable: str = "") -> str:
    """Write or refresh the per-user launcher and icons; returns what happened (for logs and tests).

    ``"installed"``, ``"updated"``, ``"current"`` (nothing to do), ``"user"`` (a launcher not written
    here: left alone), ``"system"`` (an installed package has one) or ``"skipped"`` (no way to start
    this copy, or an error).
    """

    environ = os.environ if environ is None else environ
    if system_launcher_exists(environ):
        return "system"
    command = launch_command(environ, executable=executable)
    if command is None:
        return "skipped"
    home = data_home(environ)
    path = home / "applications" / f"{APP_ID}.desktop"
    text = desktop_entry(command)
    try:
        icons_changed = _install_icons(home / "icons")
        if path.exists():
            current = path.read_text(encoding="utf-8", errors="replace")
            if MARKER not in current.splitlines():
                return "user"
            if current == text:
                return "current" if not icons_changed else "updated"
            status = "updated"
        else:
            status = "installed"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp")
        tmp.write_text(text, encoding="utf-8")
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
        if shutil.which("update-desktop-database"):
            subprocess.run(["update-desktop-database", "-q", str(path.parent)], check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
        LOGGER.info("launcher %s: %s", status, path)
        return status
    except (OSError, subprocess.SubprocessError) as exc:
        LOGGER.info("launcher not written (%s)", type(exc).__name__)
        return "skipped"


__all__ = ["ensure_launcher", "desktop_entry", "launch_command", "system_launcher_exists", "MARKER"]
