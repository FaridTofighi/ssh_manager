"""The product's names and ids, in one place (no GTK).

The app was called "SSH Manager" before; the ``LEGACY_*`` names are needed to
find and move its data (``legacy_paths.py``) and to recognise files it wrote.
Formats, crypto labels and keyring service names keep their old values on
purpose (see ``docs/rename-audit.md``): changing them would make existing data
unreadable. The Python package was ``ssh_manager`` and is ``ophion`` now (a tiny
``ssh_manager`` package only forwards ``python3 -m ssh_manager``).
"""

from __future__ import annotations

APP_NAME = "Ophion"  # window title, About, notifications, reports
APP_SLUG = "ophion"  # command, directories, icon name, file prefixes
# Reverse-DNS of github.com/FaridTofighi; the packaging files repeat it in their names (packaging/appimage).
APP_ID = "io.github.faridtofighi.Ophion"  # Gtk.Application id, desktop file, metainfo, WM class
APP_TAGLINE = "Connection manager for SSH, SFTP, FTP and RDP"
ENV_PREFIX = "OPHION_"  # environment variables (the legacy prefix is still read as a fallback)

LEGACY_APP_NAME = "SSH Manager"
LEGACY_SLUG = "ssh-manager"
LEGACY_APP_ID = "org.codex.SSHManager"
LEGACY_ENV_PREFIX = "SSH_MANAGER_"


def env(name: str, environ=None, default: str = "") -> str:
    """``OPHION_<name>``, else the legacy ``SSH_MANAGER_<name>``, else ``default``."""

    import os

    environ = os.environ if environ is None else environ
    for prefix in (ENV_PREFIX, LEGACY_ENV_PREFIX):
        value = environ.get(prefix + name)
        if value:
            return value
    return default


__all__ = ["APP_NAME", "APP_SLUG", "APP_ID", "APP_TAGLINE", "ENV_PREFIX", "LEGACY_APP_NAME", "LEGACY_SLUG",
           "LEGACY_APP_ID", "LEGACY_ENV_PREFIX", "env"]
