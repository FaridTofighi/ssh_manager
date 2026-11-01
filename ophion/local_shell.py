"""The local shell of a "New tab" terminal: which shell, where it starts, its environment (no GTK).

* :func:`resolve_shell` — the configured command, else ``$SHELL``, else the
  account's shell, else ``/bin/bash``, else ``/bin/sh``; always an absolute,
  executable path (VTE's ``spawn_async`` does not search ``PATH``).
* :func:`start_directory` — the current local tab's folder ("current") or the
  home folder; it must exist, else the home folder, else ``/``.
* :func:`local_shell_env` — the app's environment without its own secrets and
  internals. When the app runs from its AppImage (``OPHION_APPDIR``, formerly ``SSH_MANAGER_APPDIR``), the
  bundled folders are removed from ``PATH`` and the other search paths, so the
  user's shell and the programs it starts (git, python, ssh …) are the host's.
* :func:`terminal_env` — what every VTE child (the local shell, ssh) gets as
  its terminal: ``TERM=xterm-256color`` like gnome-terminal, whatever terminal
  the app itself was started from, and no tmux / screen variables. ssh sends
  ``TERM`` to the server, where e.g. ``tmux-256color`` or ``xterm-kitty`` is
  often unknown ("'tmux-256color': unknown terminal type").
"""

from __future__ import annotations

import os
import pwd
import shutil
from typing import Callable, Dict, List, Mapping, Optional

from .identity import env as env_value

FALLBACK_SHELLS = ("/bin/bash", "/bin/sh")
DEFAULT_TERM = "xterm-256color"
# Path lists the AppImage launcher prepends its own folders to (``packaging/appimage/AppRun``).
APPIMAGE_PATH_LISTS = ("PATH", "LD_LIBRARY_PATH", "XDG_DATA_DIRS", "GI_TYPELIB_PATH", "GSETTINGS_SCHEMA_DIR",
                       "GTK_PATH", "GIO_MODULE_DIR", "PYTHONHOME", "PYTHONPATH", "LTDL_LIBRARY_PATH",
                       "GTK_EXE_PREFIX", "GTK_DATA_PREFIX", "SSL_CERT_FILE")
# Set by the launcher (or the AppImage runtime) for the app itself: never for the user's shell.
# (The GTK module caches live in ~/.cache, outside the AppImage, but list its loaders.)
APPIMAGE_ONLY = ("PYTHONNOUSERSITE", "PYTHONDONTWRITEBYTECODE", "GDK_PIXBUF_MODULE_FILE", "GTK_IM_MODULE_FILE",
                 "APPDIR", "APPIMAGE", "ARGV0", "OWD")
SECRET_NAMES = ("SSHPASS",)
SECRET_PREFIXES = ("OPHION_", "SSH_MANAGER_", "SSHM_")  # the app's own variables, before and after the rename
# The terminal the app was started from (tmux, screen): not the terminal its tabs run in.
OUTER_TERMINAL_NAMES = ("TMUX", "TMUX_PANE", "STY", "WINDOW")


def _executable(path: str) -> bool:
    return bool(path) and os.path.isabs(path) and os.path.isfile(path) and os.access(path, os.X_OK)


def resolve_shell(command: str = "", environ: Optional[Mapping[str, str]] = None, *,
                  account_shell: Optional[Callable[[], str]] = None,
                  executable: Callable[[str], bool] = _executable) -> str:
    """The absolute path of the shell to start (see the module docstring for the order)."""

    environ = os.environ if environ is None else environ
    command = (command or "").strip()
    candidates: List[str] = []
    if command:
        if os.path.isabs(command):
            candidates.append(command)
        else:
            found = shutil.which(command, path=environ.get("PATH", os.defpath))
            if found:
                candidates.append(found)
    candidates.append(environ.get("SHELL", ""))
    try:
        candidates.append((account_shell or (lambda: pwd.getpwuid(os.getuid()).pw_shell))() or "")
    except (KeyError, OSError):
        pass
    candidates.extend(FALLBACK_SHELLS)
    for candidate in candidates:
        if executable(candidate):
            return candidate
    return FALLBACK_SHELLS[-1]


def shell_argv(shell: str, login: bool = False) -> List[str]:
    return [shell, "-l"] if login else [shell]


def home_directory(environ: Optional[Mapping[str, str]] = None) -> str:
    environ = os.environ if environ is None else environ
    home = environ.get("HOME", "")
    if home and os.path.isdir(home):
        return home
    try:
        home = pwd.getpwuid(os.getuid()).pw_dir
    except (KeyError, OSError):
        home = ""
    return home if home and os.path.isdir(home) else "/"


def start_directory(start_in: str = "current", current: Optional[str] = None,
                    environ: Optional[Mapping[str, str]] = None) -> str:
    """Where a new local shell starts: ``current`` (the folder of the current local tab) or ``home``."""

    if start_in == "current" and current and os.path.isdir(current):
        return current
    return home_directory(environ)


def process_cwd(pid: Optional[int]) -> Optional[str]:
    """The working folder of process ``pid`` (Linux ``/proc``), or ``None``."""

    if not pid:
        return None
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return None


def _inside(path: str, folder: str) -> bool:
    path, folder = os.path.normpath(path), os.path.normpath(folder)
    return path == folder or path.startswith(folder.rstrip(os.sep) + os.sep)


def local_shell_env(environ: Optional[Mapping[str, str]] = None) -> List[str]:
    """``NAME=value`` entries for the local shell (see the module docstring)."""

    environ = dict(os.environ if environ is None else environ)
    appdir = env_value("APPDIR", environ)  # OPHION_APPDIR, else the old SSH_MANAGER_APPDIR
    env: Dict[str, str] = {}
    for name, value in environ.items():
        if name in SECRET_NAMES or name.startswith(SECRET_PREFIXES):
            continue
        if appdir:
            if name in APPIMAGE_ONLY:
                continue
            if name in APPIMAGE_PATH_LISTS:
                kept = [part for part in value.split(":") if part and not _inside(part, appdir)]
                if not kept:
                    continue  # only bundled folders (PYTHONHOME, PYTHONPATH …): drop it
                value = ":".join(kept)
        env[name] = value
    return [f"{name}={value}" for name, value in terminal_env(env).items()]


def terminal_env(environ: Mapping[str, str]) -> Dict[str, str]:
    """``environ`` for a child of a VTE terminal: VTE's ``TERM`` / ``COLORTERM``, no outer tmux / screen."""

    env = {name: value for name, value in environ.items() if name not in OUTER_TERMINAL_NAMES}
    env["TERM"] = DEFAULT_TERM
    env["COLORTERM"] = "truecolor"
    return env


__all__ = ["resolve_shell", "shell_argv", "start_directory", "home_directory", "process_cwd", "local_shell_env",
           "terminal_env"]
