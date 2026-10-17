"""Reusable GTK password dialog for SSH authentication.

Implementation lives in :mod:`ssh_manager.gtkui.dialogs.password`; this module
re-exports it for existing imports.
"""

from __future__ import annotations

from ssh_manager.gtkui.dialogs.password import HostDetails, PasswordDialog

__all__ = ["HostDetails", "PasswordDialog"]
