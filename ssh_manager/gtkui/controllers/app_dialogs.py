"""Application-level dialogs: settings, credentials, vault, config file, about, shortcuts."""

from __future__ import annotations

import datetime
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Optional

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import GLib, Gtk  # type: ignore  # noqa: E402

from ...master_auth import hash_master_password, verify_master_password  # noqa: E402
from ...persistence import write_json_atomic  # noqa: E402
from ..dialogs.base import is_accept  # noqa: E402
from ..dialogs.config_viewer import ConfigViewerDialog  # noqa: E402
from ..dialogs.credentials import CredentialManagerDialog  # noqa: E402
from ..dialogs.files import choose_save_file  # noqa: E402
from ..dialogs.preferences import PreferencesDialog, PreferencesValues  # noqa: E402
from ..dialogs.secrets import NewSecretDialog  # noqa: E402
from ..dialogs.vault import VaultDialog  # noqa: E402
from ..i18n import _  # noqa: E402
from ..theme.tokens import Level  # noqa: E402
from .shortcuts import build_shortcuts_window  # noqa: E402

if TYPE_CHECKING:  # pragma: no cover
    from ...gtk_app import SSHManagerWindow


LOGGER = logging.getLogger(__name__)


class AppDialogs:
    """Application-level dialogs: settings, credentials, vault, config file, about, shortcuts."""

    def __init__(self, win: "SSHManagerWindow") -> None:
        self.win = win
        self._shortcuts_window: Optional[Gtk.ShortcutsWindow] = None

    def open_settings(self) -> None:
        dialog = PreferencesDialog(
            self.win,
            self.win._theme_mode,
            self.win.terminal_tabs.default_font_scale,
            self.win._ui_font_size,
            self.win._settings,
            on_reset=self.change_master_password,
            on_export=self.export_settings_snapshot,
            latency_probe=self.win._ui.latency_probe,
            density=self.win.density,
            tab_label_mode=self.win._ui.tab_label_mode,
            inventory=self.inventory_info(),
        )
        try:
            values = dialog.values() if is_accept(dialog.run()) else None
        finally:
            dialog.destroy()
        if values is not None:
            self.apply_preferences(values)

    def inventory_info(self):
        """Preferences → Inventory (app store only)."""

        manager = self.win.manager
        if getattr(manager, "store", None) is None:
            return None
        actions = self.win.store_actions
        return {
            "location": str(manager.base_dir),
            "detail": manager.read_only_reason if manager.read_only else "",
            "backup": None if manager.read_only else actions.backup_now,
            "reveal": actions.reveal_inventory,
            "export": lambda: actions.export_openssh(),
        }

    def apply_preferences(self, values: PreferencesValues) -> None:
        self.win._apply_theme_mode(values.theme_mode)
        self.win.terminal_tabs.set_default_font_scale(values.terminal_font_scale)
        self.win.terminal_tabs.apply_font_scale_to_all(values.terminal_font_scale)
        self.win._apply_ui_font(values.ui_font_size)
        self.win.set_density(values.density)
        self.win._ui.tab_label_mode = values.tab_label_mode.value
        self.win.terminal_tabs.set_label_mode(values.tab_label_mode)
        self.win._ui.theme_mode = values.theme_mode.value
        self.win._ui.font_scale = values.terminal_font_scale
        self.win._ui.ui_font_size = self.win._ui_font_size
        self.win._ui.latency_probe = values.latency_probe
        self.win.host_page.set_latency_enabled(values.latency_probe)
        settings = self.win._settings
        master_toggled = values.master_password_disabled != settings.master_password_disabled
        settings.host_key_policy = values.host_key_policy
        settings.connect_timeout = values.connect_timeout
        settings.auth_timeout = values.auth_timeout
        settings.banner_timeout = values.banner_timeout
        settings.use_paramiko_vte = values.use_paramiko_vte
        settings.master_password_disabled = values.master_password_disabled
        settings.allowed_ciphers = values.allowed_ciphers
        settings.allowed_kex = values.allowed_kex
        settings.secrets_policy = values.secrets_policy
        settings.enable_audit_logging = values.enable_audit_logging
        settings.audit_log_path = values.audit_log_path
        settings.git_watch_enabled = values.git_watch_enabled
        settings.git_watch_path = values.git_watch_path
        settings.git_watch_interval = values.git_watch_interval
        settings.terminal_appearance = values.terminal_appearance
        if values.file_transfer is not None:
            settings.file_transfer = values.file_transfer
        if values.remote_desktop is not None:
            changed = values.remote_desktop != settings.remote_desktop
            settings.remote_desktop = values.remote_desktop
            if changed:
                self.win.sessions.refresh_rdp_engines()
        settings.save()
        self.win._save_ui_state()
        if master_toggled:
            if values.master_password_disabled:
                self.win.creds.rekey_password_file(None)
            elif self.win._master_password:
                self.win.creds.rekey_password_file(self.win._master_password)
            elif self.win._secure_store.uses_file_backend:
                self.win._notify_info(_("Saved passwords will be encrypted after the next master password unlock."))
        self.win.terminal_tabs.set_appearance(values.terminal_appearance)
        self.win.inventory.refresh_git_watch()
        self.win._notify(_("Settings saved."), Level.SUCCESS)

    def export_settings_snapshot(self) -> None:
        timestamp = datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z"
        default_name = f"settings_export_{time.strftime('%Y%m%d_%H%M')}.json"
        target = choose_save_file(self.win, _("Export Settings & Policies"), current_name=default_name)
        if not target:
            return

        payload: Dict[str, Any] = {
            "version": 1,
            "generated_at": timestamp,
            "app": {"name": "SSH Manager", "version": ""},
            "security_policy": {
                "host_key_checking": self.win._settings.host_key_policy,
                "allowed_ciphers": self.win._settings.allowed_ciphers,
                "allowed_kex_algorithms": self.win._settings.allowed_kex,
            },
            "secrets_policy": self.win._settings.secrets_policy_with_defaults(),
            "rbac": {
                "has_per_host_permissions": True,
            },
            "audit_logging": {
                "enabled": self.win._settings.enable_audit_logging,
                "path": self.win._settings.audit_log_path if self.win._settings.enable_audit_logging else "",
            },
            "git_sync": {
                "enabled": self.win._settings.git_watch_enabled,
                "repo_paths": [self.win._settings.git_watch_path] if self.win._settings.git_watch_path else [],
                "auto_fetch_interval_minutes": int(self.win._settings.git_watch_interval // 60),
            },
            "shared_inventory": {
                "path": str(self.win.inventory.shared_path) if self.win.inventory.shared_path else "",
                "auto_reload_enabled": bool(self.win.inventory.shared_poll_id),
            },
        }
        try:
            write_json_atomic(Path(target).expanduser(), payload, mode=0o600)
            self.win._notify(_("Settings & policies exported successfully."), Level.SUCCESS)
        except Exception as exc:
            self.win._notify(_("Export failed: {error}").format(error=exc), Level.WARNING)

    def open_credentials(self) -> None:
        CredentialManagerDialog(self.win, self.win._credential_store).run_and_destroy()
        self.win.tree.update_details()

    def open_config_file(self) -> None:
        path = self.win.inventory.resolve_config_view_path()
        ConfigViewerDialog(self.win, path).run_and_destroy()

    def open_vault(self) -> None:
        VaultDialog(self.win, self.win._vault).run_and_destroy()
        self.win.tree.update_details()

    def show_shortcuts(self) -> None:
        # One window per main window, hidden on close (see build_shortcuts_window).
        window = self._shortcuts_window
        if window is None:
            window = self._shortcuts_window = build_shortcuts_window(self.win)
        window.show_all()
        window.present()

    def open_documentation(self) -> None:
        readme = Path(__file__).resolve().parents[3] / "README.md"
        if not readme.exists():
            self.win._notify_info(_("The documentation (README.md) is not installed."))
            return
        try:
            Gtk.show_uri_on_window(self.win, readme.as_uri(), Gtk.get_current_event_time())
        except GLib.Error as exc:
            self.win._notify_error(_("Unable to open the documentation: {error}").format(error=exc.message))

    def show_about(self) -> None:
        about = Gtk.AboutDialog(transient_for=self.win, modal=True)
        about.set_program_name(_("SSH Manager"))
        about.set_logo_icon_name("utilities-terminal")
        about.set_comments(_("Browse grouped OpenSSH configs, manage credentials and run embedded SSH sessions."))
        about.run()
        about.destroy()

    def change_master_password(self) -> None:
        stored = self.win._settings.master_password_hash
        if not stored:
            return
        dialog = NewSecretDialog(
            self.win,
            _("Change Master Password"),
            _("Choose a new master password"),
            _("Confirm the current password first. Passwords stored on this computer are re-encrypted."),
            accept_label=_("_Change"),
            verify_current=lambda text: verify_master_password(text, stored),
        )
        new_password = dialog.ask()
        if new_password is None:
            return
        self.win._settings.master_password_hash = hash_master_password(new_password)
        self.win._settings.save()
        self.win._master_password = new_password
        self.win.creds.rekey_password_file(None if self.win._settings.master_password_disabled else new_password)
        self.win._notify(_("Master password updated."), Level.SUCCESS)
