from __future__ import annotations

import os
import json
import logging
import shlex
import time
import datetime
import threading
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Vte", "2.91")

from gi.repository import Gdk, Gio, GLib, Gtk, Pango, GObject, GdkPixbuf, Vte  # type: ignore  # noqa: E402

from ssh_manager.config import ConfigManager, HostEntry
from ssh_manager.settings import AppSettings, TerminalAppearanceSettings
from ssh_manager.secure_store import FileKeyring, KeyringError, SecureStore, SecureStoreError
from ssh_manager.password_dialog import PasswordDialog
from ssh_manager.connector import Connector
from ssh_manager.connection_state import ConnectionStateMachine
from ssh_manager.gtk_terminal import TerminalNotebook
from ssh_manager.credentials import CredentialStore, Credential
from ssh_manager.config_viewer import ConfigViewerDialog
from ssh_manager.vault import CredentialVault, VaultEntry, VaultError, default_vault_path
from ssh_manager.smart_groups import SmartGroup, load_smart_groups, save_smart_groups
from ssh_manager.templates import built_in_templates, HostTemplate
from ssh_manager.profiles import built_in_profiles, SessionProfile
from ssh_manager.host_utils import build_scp_command, host_config_key, host_connection_params, host_option, with_user
from ssh_manager.master_auth import (
    hash_master_password,
    master_hash_needs_upgrade,
    verify_master_password,
)
from ssh_manager.persistence import append_text, write_json_atomic

UI_STATE_FILE = ".ui_state.json"
DND_TARGET = "ssh-manager/tree-row"

LOGGER = logging.getLogger(__name__)


def ui_state_path_for_base(base_dir: Path) -> Path:
    return (base_dir.parent if base_dir.is_file() else base_dir) / UI_STATE_FILE


def audit_log_key(value: object) -> str:
    text = str(value).strip().lower()
    cleaned = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in text)
    return cleaned.strip("_") or "field"


def audit_log_value(value: object, *, default: str = "") -> str:
    text = " ".join(str(value).split())
    return text.replace(" ", "_") or default


APP_CSS_DARK = """
/* GTK polish for SSH Manager.
 * - Styles are scoped to named widgets/classes to avoid fighting the system theme.
 * - Keeps a dark-leaning look while remaining readable in light themes by avoiding global overrides.
 * - Spacing uses a small, consistent set of values (6/12/18) and keeps focus outlines visible.
 * - Colors should remain compatible with GNOME/Adwaita; tweak cautiously. */
@define-color accent #d1d5db;
@define-color accent-strong #a4acb8;
@define-color accent-soft rgba(209, 213, 219, 0.22);
@define-color surface rgb(37, 42, 44);
@define-color surface-alt rgb(37, 42, 44);
@define-color surface-elevated rgb(37, 42, 44);
@define-color sidebar-bg rgb(37, 42, 44);
@define-color panel-border rgba(180, 188, 202, 0.26);
@define-color text-primary #f6f7f9;
@define-color text-muted #c2c7d1;
@define-color success-text #4ade80;
@define-color error-text #fca5a5;

headerbar.toolbar-header,
GtkHeaderBar.toolbar-header {
    padding-left: 12px;
    padding-right: 12px;
    border-bottom: 1px solid rgba(88, 96, 110, 0.45);
}

headerbar.toolbar-header button,
GtkHeaderBar.toolbar-header GtkButton {
    border-radius: 9px;
    padding: 6px 12px;
    transition: all 120ms ease;
}

headerbar.toolbar-header button:hover,
GtkHeaderBar.toolbar-header GtkButton:hover {
    background-color: rgba(255, 255, 255, 0.05);
}

headerbar.toolbar-header button:focus,
GtkHeaderBar.toolbar-header GtkButton:focus {
    box-shadow: 0 0 0 2px rgba(185, 196, 207, 0.45);
}

.terminal-info-bar {
    background-color: rgb(37, 42, 44);
    border-radius: 14px;
    border: 1px solid rgba(209, 213, 219, 0.18);
    padding: 6px 12px;
    box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.05);
}

.terminal-info-bar GtkLabel {
    color: @text-primary;
}

.sidebar-toggle {
    background-color: rgb(37, 42, 44);
    border: 1px solid rgba(209, 213, 219, 0.28);
    border-radius: 0 12px 12px 0;
    padding: 6px 6px;
}

.sidebar-toggle:hover {
    background-color: rgb(37, 42, 44);
}

.sidebar-toggle:active {
    background-color: rgb(37, 42, 44);
}

#root {
    background-color: rgb(37, 42, 44);
}

#sidebar {
    background-color: rgb(37, 42, 44);
    border-radius: 18px;
    padding: 16px;
    border: 1px solid @panel-border;
    box-shadow: 0 22px 44px rgba(11, 12, 16, 0.32);
}

#content {
    background-color: rgb(37, 42, 44);
    border-radius: 18px;
    border: 1px solid @panel-border;
    padding: 16px;
    box-shadow: 0 26px 52px rgba(9, 9, 11, 0.28);
}

GtkScrolledWindow {
    border-radius: 12px;
    background-color: rgb(37, 42, 44);
}

GtkViewport {
    background-color: rgb(37, 42, 44);
}

GtkTreeView {
    background-color: rgb(37, 42, 44);
    color: @text-primary;
    border-radius: 12px;
}

GtkTreeView row {
    background-color: rgb(37, 42, 44);
}

GtkTreeView row:selected,
GtkTreeView row:selected:hover {
    background-color: rgb(37, 42, 44);
    border-radius: 10px;
}

GtkTreeView row:hover {
    background-color: rgb(37, 42, 44);
}

#detail-frame {
    border-radius: 18px;
    border: 1px solid rgba(209, 213, 219, 0.28);
    background-color: rgb(37, 42, 44);
    box-shadow: 0 26px 60px rgba(8, 8, 10, 0.4);
    padding: 0;
}

#detail-box {
    background-color: rgb(37, 42, 44);
    padding: 18px;
}

#detail-box GtkLabel {
    color: @text-primary;
}

#detail-box GtkLabel.detail-subtitle {
    color: @text-muted;
    font-size: 13px;
    letter-spacing: 0.2px;
}

#detail-box GtkLabel.detail-title {
    font-weight: 600;
    font-size: 18px;
    letter-spacing: 0.25px;
}

#terminal-area {
    border-radius: 14px;
    border: 1px solid @panel-border;
    background-color: rgb(37, 42, 44);
}

#status-banner {
    background-color: rgb(37, 42, 44);
    border-radius: 24px;
    padding: 14px 24px;
    border: 1px solid rgba(209, 213, 219, 0.32);
    box-shadow: 0 14px 36px rgba(9, 9, 11, 0.32);
}

GtkButton {
    border-radius: 9px;
    background-color: rgb(37, 42, 44);
    color: @text-primary;
    padding: 7px 12px;
    transition: all 120ms ease;
    border: 1px solid transparent;
}

GtkButton:hover {
    background-color: rgb(37, 42, 44);
    border-color: rgba(185, 196, 207, 0.2);
}

GtkButton:checked,
GtkButton:active {
    background-color: rgb(37, 42, 44);
    border-color: rgba(185, 196, 207, 0.3);
}

GtkLabel.dim-label {
    color: @text-muted;
}

.tag {
    background-color: rgb(37, 42, 44);
    color: @text-primary;
    border-radius: 999px;
    padding: 4px 12px;
    margin: 2px;
}

.tag GtkLabel {
    color: @text-primary;
}

.info {
    background-color: rgb(37, 42, 44);
    border-radius: 12px;
    border: 1px solid rgba(132, 144, 160, 0.24);
}

.success {
    color: @success-text;
}

.error {
    color: @error-text;
}

GtkEntry,
GtkSearchEntry {
    background-color: rgb(37, 42, 44);
    color: @text-primary;
    border-radius: 10px;
    padding: 6px 10px;
    border: 1px solid rgba(209, 213, 219, 0.28);
    box-shadow: 0 22px 44px rgba(11, 12, 16, 0.32);
}

GtkEntry:focus,
GtkSearchEntry:focus {
    border-color: rgba(185, 196, 207, 0.6);
    box-shadow: 0 0 0 2px rgba(185, 196, 207, 0.24);
}

.search-card {
    background-color: rgba(30, 30, 30, 0.92);
    border-radius: 12px;
    border: 1px solid rgba(255, 255, 255, 0.08);
    padding: 10px 12px;
    box-shadow: 0 18px 40px rgba(0, 0, 0, 0.35);
}

.search-card GtkFrame,
.search-card GtkFrame > border {
    border: none;
}

.search-input {
    transition: border-color 120ms cubic-bezier(0.33, 1, 0.68, 1),
                box-shadow 120ms cubic-bezier(0.33, 1, 0.68, 1);
}

.search-input:focus {
    border-color: #5294e2;
    box-shadow: 0 0 0 2px rgba(82, 148, 226, 0.45);
}

.search-toggle-button {
    border-radius: 999px;
    padding: 4px;
    border: 1px solid transparent;
    background-color: transparent;
    transition: background-color 120ms ease, border-color 120ms ease;
}

.search-toggle-button:hover,
.search-toggle-button:focus {
    background-color: rgba(255, 255, 255, 0.06);
    border-color: rgba(255, 255, 255, 0.12);
}

.search-toggle-icon {
    transition: -gtk-icon-transform 150ms cubic-bezier(0.33, 1, 0.68, 1);
}

.search-toggle-icon.open {
    -gtk-icon-transform: rotate(90deg);
}

.search-card-actions {
    padding-top: 6px;
    border-top: 1px solid rgba(255, 255, 255, 0.08);
}

.host-section-card {
    background-color: rgba(30, 30, 30, 0.92);
    border-radius: 12px;
    border: 1px solid rgba(255, 255, 255, 0.08);
    box-shadow: 0 16px 40px rgba(0, 0, 0, 0.35);
    margin-bottom: 12px;
}

.host-section-card .section-title {
    color: @text-primary;
    font-size: 14px;
}

.section-sidebar-wrapper {
    background-color: rgba(30, 30, 30, 0.78);
    border-radius: 16px;
    border: 1px solid rgba(255, 255, 255, 0.08);
}

.section-sidebar {
    background-color: transparent;
}

.section-sidebar GtkListBoxRow {
    border-radius: 10px;
    padding: 10px 12px;
    margin-bottom: 4px;
}

.section-sidebar GtkListBoxRow:selected {
    background-color: rgba(82, 148, 226, 0.25);
}

.section-nav-label {
    color: @text-muted;
}

.section-sidebar GtkListBoxRow:selected .section-nav-label {
    color: @text-primary;
    font-weight: 600;
}

GtkMenu {
    background-color: rgb(37, 42, 44);
    color: @text-primary;
    border-radius: 12px;
    border: 1px solid @panel-border;
    padding: 4px;
}

GtkMenuItem {
    border-radius: 8px;
    padding: 6px 12px;
}

GtkMenuItem:hover {
    background-color: rgb(37, 42, 44);
}

GtkSeparator {
    background-color: rgb(37, 42, 44);
}
"""

APP_CSS_LIGHT = """
/* Light-friendly variant to respect system light themes. */
@define-color app-base    #f7f8fa;
@define-color app-panel   #ffffff;
@define-color app-border  #d0d4d9;
@define-color app-text    #202124;
@define-color app-muted   #5f6368;

#root,
#content,
GtkScrolledWindow,
GtkViewport {
    background-color: @app-base;
    color: @app-text;
}

#sidebar {
    background-color: @app-panel;
    border: 1px solid @app-border;
}

#detail-frame,
#detail-box,
#terminal-area,
#status-banner {
    background-color: @app-panel;
    color: @app-text;
    border: 1px solid @app-border;
}

GtkTreeView {
    background-color: @app-panel;
    color: @app-text;
}

GtkTreeView row:selected,
GtkTreeView row:selected:hover {
    background-color: rgba(0, 0, 0, 0.08);
}

GtkEntry,
GtkSearchEntry {
    background-color: @app-panel;
    color: @app-text;
    border: 1px solid @app-border;
    border-radius: 6px;
}

GtkMenu {
    background-color: @app-panel;
    color: @app-text;
}

GtkButton {
    padding: 6px 10px;
}

GtkLabel.dim-label {
    color: @app-muted;
}

.tag {
    background-color: #e9ecf1;
    color: @app-text;
}

.host-section-card {
    background-color: rgba(255, 255, 255, 0.98);
    border-radius: 12px;
    border: 1px solid rgba(0, 0, 0, 0.08);
    box-shadow: 0 16px 40px rgba(15, 23, 42, 0.12);
    margin-bottom: 12px;
}

.host-section-card .section-title {
    color: @app-text;
    font-size: 14px;
}

.section-sidebar-wrapper {
    background-color: rgba(255, 255, 255, 0.95);
    border-radius: 16px;
    border: 1px solid rgba(0, 0, 0, 0.08);
}

.section-sidebar {
    background-color: transparent;
}

.section-sidebar GtkListBoxRow {
    border-radius: 10px;
    padding: 10px 12px;
    margin-bottom: 4px;
}

.section-sidebar GtkListBoxRow:selected {
    background-color: rgba(74, 144, 226, 0.18);
}

.section-nav-label {
    color: @app-muted;
}

.section-sidebar GtkListBoxRow:selected .section-nav-label {
    color: @app-text;
    font-weight: 600;
}

.search-card {
    background-color: rgba(245, 245, 245, 0.95);
    border-radius: 12px;
    border: 1px solid rgba(0, 0, 0, 0.08);
    padding: 10px 12px;
    box-shadow: 0 18px 40px rgba(15, 23, 42, 0.12);
}

.search-card GtkFrame,
.search-card GtkFrame > border {
    border: none;
}

.search-input {
    transition: border-color 120ms cubic-bezier(0.33, 1, 0.68, 1),
                box-shadow 120ms cubic-bezier(0.33, 1, 0.68, 1);
}

.search-input:focus {
    border-color: #4a90e2;
    box-shadow: 0 0 0 2px rgba(74, 144, 226, 0.35);
}

.search-toggle-button {
    border-radius: 999px;
    padding: 4px;
    border: 1px solid transparent;
    background-color: transparent;
    transition: background-color 120ms ease, border-color 120ms ease;
}

.search-toggle-button:hover,
.search-toggle-button:focus {
    background-color: rgba(74, 144, 226, 0.1);
    border-color: rgba(74, 144, 226, 0.4);
}

.search-toggle-icon {
    transition: -gtk-icon-transform 150ms cubic-bezier(0.33, 1, 0.68, 1);
}

.search-toggle-icon.open {
    -gtk-icon-transform: rotate(90deg);
}

.search-card-actions {
    padding-top: 6px;
    border-top: 1px solid rgba(0, 0, 0, 0.08);
}

.host-status-dot.connected { background-color: #4caf50; }
.host-status-dot.error { background-color: #e53935; }
.host-status-dot.unknown { background-color: #9e9e9e; }
"""


@dataclass
class UIHostConfig:
    host: str
    port: int
    username: str
    timeout: float
    remember_password_default: bool
    parent_window: Gtk.Window
    options: Dict[str, str] = field(default_factory=dict)


class HostDialog(Gtk.Dialog):
    """Dialog for creating or editing a host entry."""

    def __init__(
        self,
        parent: Gtk.Window,
        host: Optional[HostEntry] = None,
        *,
        credentials: Optional[List[Credential]] = None,
        selected_credential_id: Optional[str] = None,
        metadata: Optional[Dict[str, str]] = None,
        vault_entries: Optional[List[VaultEntry]] = None,
        vault_unlocked: bool = False,
        template_defaults: Optional[Dict[str, str]] = None,
        profiles: Optional[Dict[str, SessionProfile]] = None,
    ) -> None:
        Gtk.Dialog.__init__(self, title="Connection Details", transient_for=parent, modal=True)
        self.add_button("_Cancel", Gtk.ResponseType.CANCEL)
        self.add_button("_Save", Gtk.ResponseType.OK)
        self.set_default_size(900, 650)
        self.set_resizable(True)
        self._credential_choices: List[Credential] = []
        self._selected_credential_id: Optional[str] = selected_credential_id
        if credentials:
            self._credential_choices = list(credentials)
        self._metadata = dict(metadata or {})
        self._selected_vault_id: Optional[str] = self._metadata.get("vault_id")
        self._vault_entries = list(vault_entries or [])
        self._vault_unlocked = vault_unlocked
        self._template_defaults = template_defaults or {}
        self._profiles = profiles or {}
        self._selected_profile_id: Optional[str] = self._metadata.get("default_session_profile_id")
        perms = self._metadata.get("permissions", {})
        self._perm_view = bool(perms.get("can_view", True)) if isinstance(perms, dict) else True
        self._perm_connect = bool(perms.get("can_connect", True)) if isinstance(perms, dict) else True
        self._perm_edit = bool(perms.get("can_edit", True)) if isinstance(perms, dict) else True

        self.alias_entry = Gtk.Entry()
        self.alias_entry.set_placeholder_text("Alias (required)")

        self.hostname_entry = Gtk.Entry()
        self.hostname_entry.set_placeholder_text("example.com")

        self.user_entry = Gtk.Entry()
        self.user_entry.set_placeholder_text("ubuntu")

        self.port_entry = Gtk.Entry()
        self.port_entry.set_placeholder_text("22")

        self.identity_entry = Gtk.Entry()
        self.identity_entry.set_placeholder_text("~/.ssh/id_ed25519")

        self.proxy_entry = Gtk.Entry()
        self.proxy_entry.set_placeholder_text("jump-host or user@jump")

        self.tags_entry = Gtk.Entry()
        self.tags_entry.set_placeholder_text("production, web, eu")

        self.environment_entry = Gtk.Entry()
        self.environment_entry.set_placeholder_text("prod / staging")

        self.owner_entry = Gtk.Entry()
        self.owner_entry.set_placeholder_text("team / owner")

        self.service_entry = Gtk.Entry()
        self.service_entry.set_placeholder_text("service/component")

        self.notes_entry = Gtk.Entry()
        self.notes_entry.set_placeholder_text("Notes")

        self.extras_view = Gtk.TextView()
        self.extras_view.set_wrap_mode(Gtk.WrapMode.WORD)
        self.extras_view.set_monospace(True)

        self._credential_combo = Gtk.ComboBoxText()
        self._credential_combo.append_text("None")
        for cred in self._credential_choices:
            self._credential_combo.append(cred.id, f"{cred.name} ({cred.username})")
        if self._selected_credential_id:
            try:
                self._credential_combo.set_active_id(self._selected_credential_id)
            except Exception:
                self._selected_credential_id = None
                self._credential_combo.set_active(0)
        if self._credential_combo.get_active() < 0:
            self._selected_credential_id = None
            self._credential_combo.set_active(0)
        self._credential_combo.connect("changed", self._on_credential_changed)

        self._vault_combo = Gtk.ComboBoxText()
        self._vault_combo.append_text("None")
        for entry in self._vault_entries:
            self._vault_combo.append(entry.id, f"{entry.label} ({entry.username})")
        if self._selected_vault_id:
            try:
                self._vault_combo.set_active_id(self._selected_vault_id)
            except Exception:
                self._vault_combo.set_active(0)
        else:
            self._vault_combo.set_active(0)
        self._vault_combo.set_sensitive(self._vault_unlocked and bool(self._vault_entries))
        self._vault_combo.connect("changed", self._on_vault_changed)

        self._profile_combo = Gtk.ComboBoxText()
        self._profile_combo.append_text("None")
        for pid, prof in self._profiles.items():
            self._profile_combo.append(pid, prof.name)
        if self._selected_profile_id:
            try:
                self._profile_combo.set_active_id(self._selected_profile_id)
            except Exception:
                self._profile_combo.set_active(0)
        else:
            self._profile_combo.set_active(0)
        self._profile_combo.connect("changed", self._on_profile_changed)

        self._perm_view_check = Gtk.CheckButton(label="Allow viewing details")
        self._perm_connect_check = Gtk.CheckButton(label="Allow connecting")
        self._perm_edit_check = Gtk.CheckButton(label="Allow editing")
        self._perm_view_check.set_active(self._perm_view)
        self._perm_connect_check.set_active(self._perm_connect)
        self._perm_edit_check.set_active(self._perm_edit)

        content = self.get_content_area()
        content.set_border_width(16)
        content.set_spacing(0)

        root = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        content.add(root)

        self._section_stack = Gtk.Stack()
        self._section_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self._section_stack.set_transition_duration(200)

        nav = Gtk.ListBox()
        nav.set_selection_mode(Gtk.SelectionMode.SINGLE)
        nav.connect("row-selected", self._on_section_selected)
        nav.get_style_context().add_class("section-sidebar")
        nav.set_activate_on_single_click(True)

        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        sidebar.set_border_width(10)
        sidebar.get_style_context().add_class("section-sidebar-wrapper")
        sidebar.pack_start(nav, True, True, 0)
        sidebar.set_size_request(220, -1)
        root.pack_start(sidebar, False, False, 0)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.add(self._section_stack)
        root.pack_start(scroller, True, True, 0)

        sections: List[Tuple[str, str, Callable[[], Gtk.Widget]]] = [
            ("general", "General", self._build_general_section),
            ("connection", "Connection", self._build_connection_section),
            ("ssh_options", "SSH Options", self._build_ssh_options_section),
            ("ports", "Ports & Forwarding", self._build_ports_section),
            ("credentials", "Credentials", self._build_credentials_section),
            ("tags", "Tags / Groups", self._build_tags_section),
            ("metadata", "Metadata / Notes", self._build_metadata_section),
            ("advanced", "Advanced", self._build_advanced_section),
        ]

        for sec_id, label, builder in sections:
            page = builder()
            self._section_stack.add_titled(page, sec_id, label)
            row = self._make_section_row(label, sec_id)
            nav.add(row)

        first_row = nav.get_row_at_index(0)
        if first_row:
            nav.select_row(first_row)

        self.show_all()

        if host:
            self.alias_entry.set_text(host.alias)
            self.hostname_entry.set_text(host_option(host, "hostname") or "")
            self.user_entry.set_text(host_option(host, "user") or "")
            self.port_entry.set_text(host_option(host, "port") or "")
            self.identity_entry.set_text(host_option(host, "identityfile") or "")
            self.proxy_entry.set_text(
                host_option(host, "proxyjump")
                or host_option(host, "jump_host")
                or host_option(host, "proxycommand")
                or ""
            )
            self.tags_entry.set_text(host_option(host, "tags") or "")
            self.environment_entry.set_text(host.metadata.get("environment", ""))
            self.owner_entry.set_text(host.metadata.get("owner", ""))
            self.service_entry.set_text(host.metadata.get("service", ""))
            self.notes_entry.set_text(host.metadata.get("notes", ""))
            vault_id = host.metadata.get("vault_id", "")
            if vault_id:
                self._selected_vault_id = vault_id
            prof_id = host.metadata.get("default_session_profile_id", "")
            if prof_id:
                self._selected_profile_id = prof_id
            perms = host.metadata.get("permissions", {})
            if isinstance(perms, dict):
                self._perm_view_check.set_active(bool(perms.get("can_view", True)))
                self._perm_connect_check.set_active(bool(perms.get("can_connect", True)))
                self._perm_edit_check.set_active(bool(perms.get("can_edit", True)))
            buf = self.extras_view.get_buffer()
            extras: List[str] = []
            for key, value in host.options.items():
                lowered = str(key).lower()
                if lowered in {
                    "hostname",
                    "user",
                    "port",
                    "identityfile",
                    "proxyjump",
                    "jump_host",
                    "proxycommand",
                    "tags",
                }:
                    continue
                extras.append(f"{key}={value}")
            buf.set_text("\n".join(extras))
        elif self._template_defaults:
            self.hostname_entry.set_text(self._template_defaults.get("hostname", ""))
            self.user_entry.set_text(self._template_defaults.get("user", ""))
            self.port_entry.set_text(self._template_defaults.get("port", ""))
            self.identity_entry.set_text(self._template_defaults.get("identityfile", ""))
            self.proxy_entry.set_text(self._template_defaults.get("proxyjump", ""))
            self.tags_entry.set_text(self._template_defaults.get("tags", ""))
            self.environment_entry.set_text(self._template_defaults.get("environment", ""))
            self.owner_entry.set_text(self._template_defaults.get("owner", ""))
            self.service_entry.set_text(self._template_defaults.get("service", ""))
            self.notes_entry.set_text(self._template_defaults.get("notes", ""))
            buf = self.extras_view.get_buffer()
            buf.set_text("")
        self._apply_selected_credential()
        self._apply_selected_profile()

    def set_alias(self, alias: str) -> None:
        self.alias_entry.set_text(alias)

    def _apply_selected_credential(self) -> None:
        cred_id = self._selected_credential_id
        if cred_id:
            cred = next((c for c in self._credential_choices if c.id == cred_id), None)
            if cred:
                self.user_entry.set_text(cred.username)
                self.user_entry.set_sensitive(False)
                return
        self.user_entry.set_sensitive(True)

    def _on_credential_changed(self, combo: Gtk.ComboBoxText) -> None:
        self._selected_credential_id = combo.get_active_id() or None
        self._apply_selected_credential()

    def _on_vault_changed(self, combo: Gtk.ComboBoxText) -> None:
        self._selected_vault_id = combo.get_active_id() or None
        entry = next((e for e in self._vault_entries if e.id == self._selected_vault_id), None)
        if entry:
            self.user_entry.set_text(entry.username)
            self.user_entry.set_sensitive(False)
        else:
            self.user_entry.set_sensitive(True)

    def _apply_selected_profile(self) -> None:
        if self._selected_profile_id:
            try:
                self._profile_combo.set_active_id(self._selected_profile_id)
            except Exception:
                self._profile_combo.set_active(0)

    def _on_profile_changed(self, combo: Gtk.ComboBoxText) -> None:
        self._selected_profile_id = combo.get_active_id() or None

    def _make_section_row(self, label: str, sec_id: str) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        setattr(row, "section_id", sec_id)
        row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row_box.set_border_width(6)
        lbl = Gtk.Label(label=label)
        lbl.set_xalign(0.0)
        lbl.get_style_context().add_class("section-nav-label")
        row_box.pack_start(lbl, True, True, 0)
        row.add(row_box)
        return row

    def _on_section_selected(self, listbox: Gtk.ListBox, row: Optional[Gtk.ListBoxRow]) -> None:
        if not row:
            return
        sec_id = getattr(row, "section_id", None)
        if sec_id:
            self._section_stack.set_visible_child_name(sec_id)

    def _section_page(self) -> Gtk.Box:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        page.set_border_width(16)
        return page

    def _section_card(self, title: str) -> Tuple[Gtk.Frame, Gtk.Box]:
        frame = Gtk.Frame()
        frame.set_shadow_type(Gtk.ShadowType.NONE)
        frame.get_style_context().add_class("host-section-card")
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        inner.set_border_width(14)
        frame.add(inner)
        heading = Gtk.Label()
        heading.set_xalign(0.0)
        heading.set_use_markup(True)
        heading.set_markup(f"<b>{GLib.markup_escape_text(title)}</b>")
        heading.get_style_context().add_class("section-title")
        inner.pack_start(heading, False, False, 0)
        return frame, inner

    def _append_form_row(self, container: Gtk.Box, label: str, widget: Gtk.Widget) -> None:
        row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        lbl = Gtk.Label(label=label)
        lbl.set_xalign(0.0)
        lbl.get_style_context().add_class("dim-label")
        row.pack_start(lbl, False, False, 0)
        widget.set_hexpand(True)
        row.pack_start(widget, False, False, 0)
        container.pack_start(row, False, False, 0)

    def _build_general_section(self) -> Gtk.Widget:
        page = self._section_page()
        card, body = self._section_card("General Details")
        self._append_form_row(body, "Alias", self.alias_entry)
        self._append_form_row(body, "Host name", self.hostname_entry)
        page.pack_start(card, False, False, 0)
        return page

    def _build_connection_section(self) -> Gtk.Widget:
        page = self._section_page()
        card, body = self._section_card("Connection & SSH")
        self._append_form_row(body, "User", self.user_entry)
        self._append_form_row(body, "Port", self.port_entry)
        self._append_form_row(body, "Private key", self.identity_entry)
        self._append_form_row(body, "Jump host (-J)", self.proxy_entry)
        page.pack_start(card, False, False, 0)
        return page

    def _build_ssh_options_section(self) -> Gtk.Widget:
        page = self._section_page()
        card, body = self._section_card("Custom SSH Options")
        hint = Gtk.Label(label="Enter additional SSH options as key=value pairs (one per line).")
        hint.set_xalign(0.0)
        hint.set_line_wrap(True)
        hint.get_style_context().add_class("dim-label")
        body.pack_start(hint, False, False, 0)
        expander = Gtk.Expander(label="Advanced options editor")
        expander.set_expanded(True)
        expander_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        expander_box.set_border_width(6)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.set_min_content_height(160)
        scroller.add(self.extras_view)
        expander_box.pack_start(scroller, True, True, 0)
        expander.add(expander_box)
        body.pack_start(expander, True, True, 0)
        page.pack_start(card, True, True, 0)
        return page

    def _build_ports_section(self) -> Gtk.Widget:
        page = self._section_page()
        card, body = self._section_card("Port & Forwarding Guidance")
        text = (
            "Configure LocalForward/RemoteForward/DynamicForward directives via the "
            "custom SSH options section to mirror ssh_config behavior."
        )
        label = Gtk.Label(label=text)
        label.set_xalign(0.0)
        label.set_line_wrap(True)
        body.pack_start(label, False, False, 0)
        page.pack_start(card, False, False, 0)
        return page

    def _build_credentials_section(self) -> Gtk.Widget:
        page = self._section_page()
        card, body = self._section_card("Credentials & Profiles")
        self._append_form_row(body, "Credential", self._credential_combo)
        self._append_form_row(body, "Vault credential", self._vault_combo)
        self._append_form_row(body, "Default Session Profile", self._profile_combo)
        page.pack_start(card, False, False, 0)
        return page

    def _build_tags_section(self) -> Gtk.Widget:
        page = self._section_page()
        card, body = self._section_card("Tags & Grouping")
        info = Gtk.Label(
            label="Use comma-separated tags to classify hosts for filtering and smart groups."
        )
        info.set_xalign(0.0)
        info.set_line_wrap(True)
        info.get_style_context().add_class("dim-label")
        body.pack_start(info, False, False, 0)
        self._append_form_row(body, "Tags", self.tags_entry)
        page.pack_start(card, False, False, 0)
        return page

    def _build_metadata_section(self) -> Gtk.Widget:
        page = self._section_page()
        card, body = self._section_card("Metadata & Notes")
        self._append_form_row(body, "Environment", self.environment_entry)
        self._append_form_row(body, "Owner", self.owner_entry)
        self._append_form_row(body, "Service", self.service_entry)
        self._append_form_row(body, "Notes", self.notes_entry)
        page.pack_start(card, False, False, 0)
        return page

    def _build_advanced_section(self) -> Gtk.Widget:
        page = self._section_page()
        card, body = self._section_card("Local Permissions (advisory)")
        body.pack_start(self._perm_view_check, False, False, 0)
        body.pack_start(self._perm_connect_check, False, False, 0)
        body.pack_start(self._perm_edit_check, False, False, 0)
        page.pack_start(card, False, False, 0)
        return page

    def build_host(self, parent: str, child: str) -> Optional[HostEntry]:
        alias = self.alias_entry.get_text().strip()
        if not alias:
            return None

        options: Dict[str, str] = {}
        hostname = self.hostname_entry.get_text().strip()
        user = self.user_entry.get_text().strip()
        port = self.port_entry.get_text().strip()
        identity = self.identity_entry.get_text().strip()
        jump = self.proxy_entry.get_text().strip()
        tags = self.tags_entry.get_text().strip()

        if hostname:
            options["hostname"] = hostname
        if user:
            options["user"] = user
        if port:
            options["port"] = port
        if identity:
            options["identityfile"] = identity
        if jump:
            options["proxyjump"] = jump
        if tags:
            options["tags"] = ",".join(part.strip() for part in tags.split(",") if part.strip())

        cred_id = self._selected_credential_id
        if cred_id:
            cred = next((c for c in self._credential_choices if c.id == cred_id), None)
            if cred:
                options["user"] = cred.username

        buf = self.extras_view.get_buffer()
        start_iter = buf.get_start_iter()
        end_iter = buf.get_end_iter()
        for line in buf.get_text(start_iter, end_iter, False).splitlines():
            line = line.strip()
            if not line or "=" not in line:
                continue
            key, value = line.split("=", 1)
            options[key.strip()] = value.strip()

        metadata = {
            "environment": self.environment_entry.get_text().strip(),
            "owner": self.owner_entry.get_text().strip(),
            "service": self.service_entry.get_text().strip(),
            "notes": self.notes_entry.get_text().strip(),
        }
        if self._selected_vault_id:
            metadata["vault_id"] = self._selected_vault_id
        if self._selected_profile_id:
            metadata["default_session_profile_id"] = self._selected_profile_id
        perms = {
            "can_view": self._perm_view_check.get_active(),
            "can_connect": self._perm_connect_check.get_active(),
            "can_edit": self._perm_edit_check.get_active(),
        }
        if not all(perms.values()):
            metadata["permissions"] = perms

        return HostEntry(
            alias=alias,
            options=options,
            metadata={k: v for k, v in metadata.items() if v},
            parent_group=parent,
            child_group=child,
        )

    def selected_credential_id(self) -> Optional[str]:
        return self._selected_credential_id


class GroupPickerDialog(Gtk.Dialog):
    """Dialog for choosing or creating a parent/child group destination."""

    def __init__(
        self,
        parent: Gtk.Window,
        manager: ConfigManager,
        title: str,
        *,
        allow_new_child: bool = False,
        initial_parent: Optional[str] = None,
        initial_child: Optional[str] = None,
    ) -> None:
        Gtk.Dialog.__init__(self, title=title, transient_for=parent, modal=True)
        self.add_button("_Cancel", Gtk.ResponseType.CANCEL)
        self.add_button("_Apply", Gtk.ResponseType.OK)
        self.set_default_size(420, 0)

        self._allow_new_child = allow_new_child

        box = self.get_content_area()
        box.set_border_width(16)
        box.set_spacing(12)

        self._parent_combo = Gtk.ComboBoxText()
        parents = sorted(manager.tree.keys(), key=str.lower)
        for name in parents:
            self._parent_combo.append_text(name)
        if initial_parent and initial_parent in parents:
            self._parent_combo.set_active(parents.index(initial_parent))
        elif parents:
            self._parent_combo.set_active(0)

        parent_label = Gtk.Label(label="Parent group")
        parent_label.set_xalign(0.0)
        parent_label.get_style_context().add_class("dim-label")
        box.pack_start(parent_label, False, False, 0)
        box.pack_start(self._parent_combo, False, False, 0)

        self._child_combo = Gtk.ComboBoxText()
        box.pack_start(self._child_combo, False, False, 0)

        self._new_child_entry = Gtk.Entry()
        self._new_child_entry.set_placeholder_text("New child group name")
        if allow_new_child:
            hint = Gtk.Label(label="Leave blank to use existing child group")
            hint.set_xalign(0.0)
            hint.get_style_context().add_class("dim-label")
            box.pack_start(hint, False, False, 0)
            box.pack_start(self._new_child_entry, False, False, 0)

        self._manager = manager
        self._parent_combo.connect("changed", self._on_parent_changed)
        self._refresh_child_combo(initial_child)
        self.show_all()

    def selected(self) -> Optional[Tuple[str, str, bool]]:
        parent = self._parent_combo.get_active_text()
        if not parent:
            return None
        new_child = self._new_child_entry.get_text().strip() if self._allow_new_child else ""
        if new_child:
            return parent, new_child, True
        child = self._child_combo.get_active_text()
        if not child:
            return None
        return parent, child, False

    def _on_parent_changed(self, *_args) -> None:
        self._refresh_child_combo(None)

    def _refresh_child_combo(self, initial_child: Optional[str]) -> None:
        self._child_combo.remove_all()
        parent = self._parent_combo.get_active_text()
        if not parent:
            return
        names = sorted(self._manager.tree.get(parent, {}).keys(), key=str.lower)
        for name in names:
            self._child_combo.append_text(name)
        if initial_child and initial_child in names:
            self._child_combo.set_active(names.index(initial_child))
        elif names:
            self._child_combo.set_active(0)


class PreferencesDialog(Gtk.Dialog):
    """Settings for theme and font defaults."""

    def __init__(
        self,
        parent: Gtk.Window,
        prefer_dark: bool,
        terminal_font_scale: float,
        ui_font_size: int,
        settings: AppSettings,
        *,
        on_reset: Optional[Callable[[], None]] = None,
        on_export: Optional[Callable[[], None]] = None,
    ) -> None:
        Gtk.Dialog.__init__(self, title="Settings", transient_for=parent, modal=True)
        self.add_button("_Cancel", Gtk.ResponseType.CANCEL)
        self.add_button("_Apply", Gtk.ResponseType.OK)
        self._on_reset = on_reset
        self._on_export = on_export
        self._appearance = settings.terminal_appearance or TerminalAppearanceSettings()

        content = self.get_content_area()
        content.set_border_width(12)
        root_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        content.add(root_box)

        stack = Gtk.Stack()
        stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        stack.set_transition_duration(200)

        sidebar = Gtk.StackSidebar()
        sidebar.set_stack(stack)
        root_box.pack_start(sidebar, False, False, 0)
        root_box.pack_start(stack, True, True, 0)

        def page_box() -> Gtk.Box:
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
            box.set_border_width(12)
            return box

        general_page = page_box()
        passwords_page = page_box()
        secrets_page = page_box()
        terminal_page = page_box()
        connection_page = page_box()
        ssh_page = page_box()
        git_page = page_box()
        audit_page = page_box()
        master_page = page_box()

        stack.add_titled(general_page, "general", "General")
        stack.add_titled(passwords_page, "passwords", "Passwords")
        stack.add_titled(secrets_page, "secrets", "Secrets Policy")
        stack.add_titled(terminal_page, "terminal", "Terminal")
        stack.add_titled(connection_page, "connection", "Connection")
        stack.add_titled(ssh_page, "ssh_security", "SSH Security")
        stack.add_titled(git_page, "git", "Git Sync")
        stack.add_titled(audit_page, "audit", "Audit Logging")
        stack.add_titled(master_page, "master_password", "Master Password")

        theme_box = Gtk.Box(spacing=8)
        label = Gtk.Label(label="Prefer dark theme")
        label.set_xalign(0.0)
        theme_box.pack_start(label, True, True, 0)
        self._dark_switch = Gtk.Switch()
        self._dark_switch.set_active(prefer_dark)
        theme_box.pack_start(self._dark_switch, False, False, 0)
        general_page.pack_start(theme_box, False, False, 0)

        ui_font_box = Gtk.Box(spacing=8)
        ui_font_label = Gtk.Label(label="Application font size")
        ui_font_label.set_xalign(0.0)
        ui_font_box.pack_start(ui_font_label, True, True, 0)
        self._ui_font_combo = Gtk.ComboBoxText()
        self._ui_font_sizes = [10, 12, 14, 16, 18, 20, 24, 28, 32, 36, 40, 44, 48]
        for size in self._ui_font_sizes:
            self._ui_font_combo.append_text(str(size))
        nearest = min(self._ui_font_sizes, key=lambda val: abs(val - ui_font_size))
        self._ui_font_combo.set_active(self._ui_font_sizes.index(nearest))
        ui_font_box.pack_start(self._ui_font_combo, False, False, 0)
        general_page.pack_start(ui_font_box, False, False, 0)

        font_box = Gtk.Box(spacing=8)
        font_label = Gtk.Label(label="Terminal font scale")
        font_label.set_xalign(0.0)
        font_box.pack_start(font_label, True, True, 0)
        self._font_spin = Gtk.SpinButton.new_with_range(0.6, 2.5, 0.1)
        self._font_spin.set_value(terminal_font_scale)
        font_box.pack_start(self._font_spin, False, False, 0)
        general_page.pack_start(font_box, False, False, 0)

        remember_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        remember_label = Gtk.Label(label="Passwords")
        remember_label.set_xalign(0.0)
        remember_box.pack_start(remember_label, False, False, 0)
        self._remember_check = Gtk.CheckButton(label="Remember passwords by default")
        self._remember_check.set_active(settings.remember_password_default)
        remember_box.pack_start(self._remember_check, False, False, 0)
        passwords_page.pack_start(remember_box, False, False, 0)

        policy = settings.secrets_policy_with_defaults() if hasattr(settings, "secrets_policy_with_defaults") else {}
        secrets_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        secrets_label = Gtk.Label(label="Secrets policy")
        secrets_label.set_xalign(0.0)
        secrets_box.pack_start(secrets_label, False, False, 0)
        self._allow_plaintext_check = Gtk.CheckButton(label="Allow plain-text passwords on hosts")
        self._allow_plaintext_check.set_active(bool(policy.get("allow_plaintext_host_passwords", True)))
        secrets_box.pack_start(self._allow_plaintext_check, False, False, 0)
        self._warn_plaintext_check = Gtk.CheckButton(label="Warn when saving plain-text passwords")
        self._warn_plaintext_check.set_active(bool(policy.get("warn_on_plaintext_host_passwords", True)))
        secrets_box.pack_start(self._warn_plaintext_check, False, False, 0)
        self._prefer_vault_check = Gtk.CheckButton(label="Prefer using the credential vault for passwords")
        self._prefer_vault_check.set_active(bool(policy.get("prefer_vault_for_passwords", True)))
        secrets_box.pack_start(self._prefer_vault_check, False, False, 0)
        rotation_row = Gtk.Box(spacing=8)
        rotation_label = Gtk.Label(label="Suggest rotation after (days)")
        rotation_label.set_xalign(0.0)
        rotation_row.pack_start(rotation_label, True, True, 0)
        self._rotation_spin = Gtk.SpinButton.new_with_range(1, 3650, 1)
        self._rotation_spin.set_value(float(policy.get("rotation_hint_days", 90)))
        rotation_row.pack_start(self._rotation_spin, False, False, 0)
        secrets_box.pack_start(rotation_row, False, False, 0)
        secrets_page.pack_start(secrets_box, False, False, 0)

        audit_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        audit_label = Gtk.Label(label="Audit logging")
        audit_label.set_xalign(0.0)
        audit_box.pack_start(audit_label, False, False, 0)
        self._audit_enable = Gtk.CheckButton(label="Enable audit logging")
        self._audit_enable.set_active(bool(getattr(settings, "enable_audit_logging", False)))
        audit_box.pack_start(self._audit_enable, False, False, 0)
        path_row = Gtk.Box(spacing=8)
        path_row.set_sensitive(self._audit_enable.get_active())
        path_label = Gtk.Label(label="Audit log file")
        path_label.set_xalign(0.0)
        path_row.pack_start(path_label, False, False, 0)
        self._audit_path_entry = Gtk.Entry()
        self._audit_path_entry.set_hexpand(True)
        self._audit_path_entry.set_text(str(getattr(settings, "audit_log_path", "")))
        self._audit_path_entry.set_editable(False)
        path_row.pack_start(self._audit_path_entry, True, True, 0)
        browse = Gtk.Button(label="Change…")
        browse.connect("clicked", self._choose_audit_path)
        path_row.pack_start(browse, False, False, 0)
        audit_box.pack_start(path_row, False, False, 0)
        self._audit_enable.connect("toggled", lambda btn: path_row.set_sensitive(btn.get_active()))
        audit_page.pack_start(audit_box, False, False, 0)

        git_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        git_label = Gtk.Label(label="Git update notifications")
        git_label.set_xalign(0.0)
        git_box.pack_start(git_label, False, False, 0)
        self._git_watch_enable = Gtk.CheckButton(label="Enable Git remote update checks")
        self._git_watch_enable.set_active(bool(getattr(settings, "git_watch_enabled", False)))
        git_box.pack_start(self._git_watch_enable, False, False, 0)
        git_path_row = Gtk.Box(spacing=8)
        git_path_row.set_sensitive(self._git_watch_enable.get_active())
        git_path_label = Gtk.Label(label="Git directory")
        git_path_label.set_xalign(0.0)
        git_path_row.pack_start(git_path_label, False, False, 0)
        self._git_path_entry = Gtk.Entry()
        self._git_path_entry.set_hexpand(True)
        self._git_path_entry.set_text(str(getattr(settings, "git_watch_path", "")))
        self._git_path_entry.set_editable(False)
        git_path_row.pack_start(self._git_path_entry, True, True, 0)
        git_browse = Gtk.Button(label="Change…")
        git_browse.connect("clicked", self._choose_git_path)
        git_path_row.pack_start(git_browse, False, False, 0)
        git_box.pack_start(git_path_row, False, False, 0)
        self._git_watch_enable.connect("toggled", lambda btn: git_path_row.set_sensitive(btn.get_active()))
        interval_row = Gtk.Box(spacing=8)
        interval_label = Gtk.Label(label="Check interval (seconds)")
        interval_label.set_xalign(0.0)
        interval_row.pack_start(interval_label, False, False, 0)
        self._git_interval_spin = Gtk.SpinButton.new_with_range(60, 3600, 30)
        self._git_interval_spin.set_value(float(getattr(settings, "git_watch_interval", 600)))
        interval_row.pack_start(self._git_interval_spin, False, False, 0)
        git_box.pack_start(interval_row, False, False, 0)
        git_page.pack_start(git_box, False, False, 0)

        term_label = Gtk.Label(label="Terminal appearance")
        term_label.set_xalign(0.0)
        term_label.get_style_context().add_class("dim-label")
        terminal_page.pack_start(term_label, False, False, 0)

        term_list = Gtk.ListBox()
        term_list.set_selection_mode(Gtk.SelectionMode.NONE)
        terminal_page.pack_start(term_list, False, False, 0)

        # Font row
        font_row = Gtk.ListBoxRow()
        font_row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        font_label = Gtk.Label(label="Font")
        font_label.set_xalign(0.0)
        font_label.set_valign(Gtk.Align.CENTER)
        font_row_box.pack_start(font_label, False, False, 0)
        font_controls = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self._use_system_font = Gtk.CheckButton(label="Use system fixed-width font")
        self._use_system_font.set_active(self._appearance.use_system_mono_font)
        self._use_system_font.connect("toggled", lambda *_: self._update_font_controls())
        font_controls.pack_start(self._use_system_font, False, False, 0)
        custom_font_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._font_button = Gtk.Button(label="Custom font…")
        self._font_button.connect("clicked", self._choose_font)
        custom_font_box.pack_start(self._font_button, False, False, 0)
        self._font_size_spin = Gtk.SpinButton.new_with_range(8.0, 36.0, 0.5)
        if self._appearance.font_size:
            self._font_size_spin.set_value(self._appearance.font_size)
        custom_font_box.pack_start(Gtk.Label(label="Size"), False, False, 0)
        custom_font_box.pack_start(self._font_size_spin, False, False, 0)
        font_controls.pack_start(custom_font_box, False, False, 0)
        font_row_box.pack_start(font_controls, True, True, 0)
        font_row.add(font_row_box)
        term_list.add(font_row)

        # Color row
        color_row = Gtk.ListBoxRow()
        color_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        color_label = Gtk.Label(label="Colors")
        color_label.set_xalign(0.0)
        color_label.set_valign(Gtk.Align.CENTER)
        color_box.pack_start(color_label, False, False, 0)
        color_controls = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self._scheme_combo = Gtk.ComboBoxText()
        schemes = [
            ("default", "Default"),
            ("dark", "Dark"),
            ("light", "Light"),
            ("solarized-dark", "Solarized Dark"),
            ("solarized-light", "Solarized Light"),
        ]
        for _key, label_text in schemes:
            self._scheme_combo.append_text(label_text)
        scheme_keys = [s[0] for s in schemes]
        if self._appearance.color_scheme in scheme_keys:
            self._scheme_combo.set_active(scheme_keys.index(self._appearance.color_scheme))
        else:
            self._scheme_combo.set_active(0)
        self._scheme_combo.connect("changed", lambda *_: self._update_preview_terminal())
        color_controls.pack_start(self._scheme_combo, False, False, 0)
        self._custom_colors_check = Gtk.CheckButton(label="Use custom colors")
        self._custom_colors_check.set_active(self._appearance.use_custom_colors)
        self._custom_colors_check.connect("toggled", lambda *_: self._update_color_controls())
        color_controls.pack_start(self._custom_colors_check, False, False, 0)
        fg_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        fg_row.pack_start(Gtk.Label(label="Foreground"), False, False, 0)
        self._fg_button = Gtk.ColorButton()
        if self._appearance.fg_color:
            rgba = Gdk.RGBA()
            if rgba.parse(self._appearance.fg_color):
                self._fg_button.set_rgba(rgba)
        self._fg_button.connect("color-set", lambda *_: self._update_preview_terminal())
        fg_row.pack_start(self._fg_button, False, False, 0)
        color_controls.pack_start(fg_row, False, False, 0)
        bg_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        bg_row.pack_start(Gtk.Label(label="Background"), False, False, 0)
        self._bg_button = Gtk.ColorButton()
        if self._appearance.bg_color:
            rgba = Gdk.RGBA()
            if rgba.parse(self._appearance.bg_color):
                self._bg_button.set_rgba(rgba)
        self._bg_button.connect("color-set", lambda *_: self._update_preview_terminal())
        bg_row.pack_start(self._bg_button, False, False, 0)
        color_controls.pack_start(bg_row, False, False, 0)
        color_box.pack_start(color_controls, True, True, 0)
        color_row.add(color_box)
        term_list.add(color_row)

        # Cursor row
        cursor_row = Gtk.ListBoxRow()
        cursor_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        cursor_label = Gtk.Label(label="Cursor & rendering")
        cursor_label.set_xalign(0.0)
        cursor_label.set_valign(Gtk.Align.CENTER)
        cursor_box.pack_start(cursor_label, False, False, 0)
        cursor_controls = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self._cursor_combo = Gtk.ComboBoxText()
        shapes = [("block", "Block"), ("ibeam", "I‑beam"), ("underline", "Underline")]
        for key, text in shapes:
            self._cursor_combo.append_text(text)
        shape_keys = [s[0] for s in shapes]
        try:
            self._cursor_combo.set_active(shape_keys.index(self._appearance.cursor_shape))
        except ValueError:
            self._cursor_combo.set_active(0)
        self._cursor_combo.connect("changed", lambda *_: self._update_preview_terminal())
        cursor_controls.pack_start(self._cursor_combo, False, False, 0)
        self._cursor_blink_check = Gtk.CheckButton(label="Cursor blinks")
        self._cursor_blink_check.set_active(self._appearance.cursor_blink)
        self._cursor_blink_check.connect("toggled", lambda *_: self._update_preview_terminal())
        cursor_controls.pack_start(self._cursor_blink_check, False, False, 0)
        self._bold_is_bright_check = Gtk.CheckButton(label="Bold text is bright")
        self._bold_is_bright_check.set_active(self._appearance.bold_is_bright)
        self._bold_is_bright_check.connect("toggled", lambda *_: self._update_preview_terminal())
        cursor_controls.pack_start(self._bold_is_bright_check, False, False, 0)
        cursor_box.pack_start(cursor_controls, True, True, 0)
        cursor_row.add(cursor_box)
        term_list.add(cursor_row)

        preview_label = Gtk.Label(label="Preview")
        preview_label.set_xalign(0.0)
        preview_label.get_style_context().add_class("dim-label")
        terminal_page.pack_start(preview_label, False, False, 0)
        self._preview_term = Vte.Terminal()
        self._preview_term.set_size_request(-1, 140)
        self._preview_term.set_scrollback_lines(500)
        self._preview_term.spawn_async(
            Vte.PtyFlags.DEFAULT,
            os.environ.get("HOME", "/"),
            ["/bin/sh", "-c", "printf 'ssh-manager terminal preview\\n$ echo example\\n'"],
            [],
            GLib.SpawnFlags.DEFAULT,
            None,
            None,
            -1,
            None,
            None,
        )
        preview_frame = Gtk.Frame()
        preview_frame.set_shadow_type(Gtk.ShadowType.IN)
        preview_frame.add(self._preview_term)
        terminal_page.pack_start(preview_frame, False, False, 0)

        policy_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        policy_label = Gtk.Label(label="Host key policy")
        policy_label.set_xalign(0.0)
        policy_box.pack_start(policy_label, False, False, 0)
        self._policy_strict = Gtk.RadioButton.new_with_label(None, "Strict (reject unknown hosts)")
        self._policy_auto = Gtk.RadioButton.new_with_label_from_widget(
            self._policy_strict,
            "Auto-add new hosts",
        )
        if settings.host_key_policy == "auto-add":
            self._policy_auto.set_active(True)
        else:
            self._policy_strict.set_active(True)
        policy_box.pack_start(self._policy_strict, False, False, 0)
        policy_box.pack_start(self._policy_auto, False, False, 0)
        ssh_page.pack_start(policy_box, False, False, 0)

        cipher_label = Gtk.Label(label="Allowed ciphers (comma-separated, optional)")
        cipher_label.set_xalign(0.0)
        ssh_page.pack_start(cipher_label, False, False, 0)
        self._cipher_entry = Gtk.Entry()
        self._cipher_entry.set_text(", ".join(settings.allowed_ciphers))
        ssh_page.pack_start(self._cipher_entry, False, False, 0)

        kex_label = Gtk.Label(label="Allowed KEX algorithms (comma-separated, optional)")
        kex_label.set_xalign(0.0)
        ssh_page.pack_start(kex_label, False, False, 0)
        self._kex_entry = Gtk.Entry()
        self._kex_entry.set_text(", ".join(settings.allowed_kex))
        ssh_page.pack_start(self._kex_entry, False, False, 0)

        timeout_label = Gtk.Label(label="Timeouts (seconds)")
        timeout_label.set_xalign(0.0)
        connection_page.pack_start(timeout_label, False, False, 0)

        timeout_grid = Gtk.Grid(column_spacing=12, row_spacing=6)
        connection_page.pack_start(timeout_grid, False, False, 0)

        def timeout_row(row: int, text: str, value: float) -> Gtk.SpinButton:
            lbl = Gtk.Label(label=text)
            lbl.set_xalign(0.0)
            timeout_grid.attach(lbl, 0, row, 1, 1)
            spin = Gtk.SpinButton.new_with_range(1.0, 600.0, 1.0)
            spin.set_digits(1)
            spin.set_value(value)
            timeout_grid.attach(spin, 1, row, 1, 1)
            return spin

        self._connect_timeout_spin = timeout_row(0, "Connection timeout", settings.connect_timeout)
        self._auth_timeout_spin = timeout_row(1, "Authentication timeout", settings.auth_timeout)
        self._banner_timeout_spin = timeout_row(2, "Banner timeout", settings.banner_timeout)

        self._paramiko_check = Gtk.CheckButton(label="Enable Paramiko + VTE bridge (experimental)")
        self._paramiko_check.set_active(settings.use_paramiko_vte)
        connection_page.pack_start(self._paramiko_check, False, False, 0)

        disable_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        disable_label = Gtk.Label(label="Master password")
        disable_label.set_xalign(0.0)
        disable_box.pack_start(disable_label, False, False, 0)
        self._disable_master_check = Gtk.CheckButton(label="Disable master password prompt on startup")
        self._disable_master_check.set_active(settings.master_password_disabled)
        disable_box.pack_start(self._disable_master_check, False, False, 0)
        master_page.pack_start(disable_box, False, False, 0)

        reset_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        reset_label = Gtk.Label(label="Master password")
        reset_label.set_xalign(0.0)
        reset_row.pack_start(reset_label, True, True, 0)
        reset_btn = Gtk.Button(label="Reset…")
        reset_btn.get_style_context().add_class("destructive-action")
        reset_btn.connect("clicked", self._on_reset_clicked)
        reset_row.pack_start(reset_btn, False, False, 0)
        master_page.pack_start(reset_row, False, False, 0)

        export_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        export_label = Gtk.Label(label="Export settings & policies")
        export_label.set_xalign(0.0)
        export_row.pack_start(export_label, True, True, 0)
        export_btn = Gtk.Button(label="Export…")
        export_btn.connect("clicked", self._on_export_clicked)
        export_row.pack_start(export_btn, False, False, 0)
        master_page.pack_start(export_row, False, False, 0)

        self._update_font_controls()
        self._update_color_controls()
        self._update_preview_terminal()

        self.show_all()

    def values(
        self,
    ) -> Tuple[
        bool,
        float,
        int,
        bool,
        str,
        float,
        float,
        float,
        bool,
        bool,
        list[str],
        list[str],
        Dict[str, Any],
        bool,
        str,
        bool,
        str,
        int,
        TerminalAppearanceSettings,
    ]:
        ui_font_text = self._ui_font_combo.get_active_text()
        ui_font_size = int(ui_font_text) if ui_font_text else 14
        return (
            self._dark_switch.get_active(),
            float(self._font_spin.get_value()),
            ui_font_size,
            self._remember_check.get_active(),
            "auto-add" if self._policy_auto.get_active() else "reject",
            float(self._connect_timeout_spin.get_value()),
            float(self._auth_timeout_spin.get_value()),
            float(self._banner_timeout_spin.get_value()),
            self._paramiko_check.get_active(),
            self._disable_master_check.get_active(),
            [val.strip() for val in self._cipher_entry.get_text().split(",") if val.strip()],
            [val.strip() for val in self._kex_entry.get_text().split(",") if val.strip()],
            {
                "allow_plaintext_host_passwords": self._allow_plaintext_check.get_active(),
                "warn_on_plaintext_host_passwords": self._warn_plaintext_check.get_active(),
                "prefer_vault_for_passwords": self._prefer_vault_check.get_active(),
                "rotation_hint_days": int(self._rotation_spin.get_value()),
            },
            self._audit_enable.get_active(),
            self._audit_path_entry.get_text().strip(),
            self._git_watch_enable.get_active(),
            self._git_path_entry.get_text().strip(),
            int(self._git_interval_spin.get_value()),
            self._appearance_from_controls(),
        )

    def _appearance_from_controls(self) -> TerminalAppearanceSettings:
        fg = self._color_to_hex(self._fg_button.get_rgba()) if self._custom_colors_check.get_active() else None
        bg = self._color_to_hex(self._bg_button.get_rgba()) if self._custom_colors_check.get_active() else None
        scheme_keys = ["default", "dark", "light", "solarized-dark", "solarized-light"]
        scheme_idx = self._scheme_combo.get_active()
        scheme = scheme_keys[scheme_idx] if 0 <= scheme_idx < len(scheme_keys) else "default"
        shape_idx = self._cursor_combo.get_active()
        shape_keys = ["block", "ibeam", "underline"]
        cursor_shape = shape_keys[shape_idx] if 0 <= shape_idx < len(shape_keys) else "block"
        custom_font = self._appearance.custom_font
        return TerminalAppearanceSettings(
            use_system_mono_font=self._use_system_font.get_active(),
            custom_font=None if self._use_system_font.get_active() else custom_font,
            font_size=self._font_size_spin.get_value() if not self._use_system_font.get_active() else None,
            color_scheme=scheme,
            use_custom_colors=self._custom_colors_check.get_active(),
            fg_color=fg,
            bg_color=bg,
            palette=self._appearance.palette,
            cursor_shape=cursor_shape,
            cursor_blink=self._cursor_blink_check.get_active(),
            bold_is_bright=self._bold_is_bright_check.get_active(),
        )

    def _color_to_hex(self, rgba: Gdk.RGBA) -> str:
        r = int(rgba.red * 255 + 0.5)
        g = int(rgba.green * 255 + 0.5)
        b = int(rgba.blue * 255 + 0.5)
        return f"#{r:02x}{g:02x}{b:02x}"

    def _update_font_controls(self) -> None:
        custom_enabled = not self._use_system_font.get_active()
        self._font_button.set_sensitive(custom_enabled)
        self._font_size_spin.set_sensitive(custom_enabled)
        self._update_preview_terminal()

    def _update_color_controls(self) -> None:
        enabled = self._custom_colors_check.get_active()
        self._fg_button.set_sensitive(enabled)
        self._bg_button.set_sensitive(enabled)
        self._scheme_combo.set_sensitive(not enabled)
        self._update_preview_terminal()

    def _choose_font(self, _button: Gtk.Button) -> None:
        chooser = Gtk.FontChooserDialog(title="Select font", transient_for=self, modal=True)
        if self._appearance.custom_font:
            chooser.set_font(self._appearance.custom_font)
        resp = chooser.run()
        font_name = chooser.get_font() if resp == Gtk.ResponseType.OK else None
        chooser.destroy()
        if font_name:
            self._appearance.custom_font = font_name
            self._update_preview_terminal()

    def _preview_colors(self, appearance: TerminalAppearanceSettings) -> Tuple[Gdk.RGBA, Gdk.RGBA, Gdk.RGBA, Gdk.RGBA, list[Gdk.RGBA]]:
        def rgba(hex_color: str) -> Gdk.RGBA:
            color = Gdk.RGBA()
            color.parse(hex_color)
            return color

        background_hex = "#1f2329"
        foreground_hex = "#f5f6f8"
        cursor_hex = "#b9c4cf"
        highlight_hex = "#2f333a"
        palette_hex = [
            "#1f2329",
            "#ef9a9a",
            "#a5d6a7",
            "#ffe0b2",
            "#90caf9",
            "#c7a4ff",
            "#80cbc4",
            "#f5f6f8",
            "#394049",
            "#f28b82",
            "#81c995",
            "#fdd663",
            "#8ab4f8",
            "#d7aefb",
            "#78d9ec",
            "#ffffff",
        ]

        scheme = appearance.color_scheme or "default"
        if appearance.use_custom_colors and appearance.fg_color and appearance.bg_color:
            foreground_hex = appearance.fg_color
            background_hex = appearance.bg_color
        else:
            if scheme == "dark":
                background_hex = "#1f2329"
                foreground_hex = "#f5f6f8"
            elif scheme == "light":
                background_hex = "#f8fafc"
                foreground_hex = "#0f172a"
            elif scheme == "solarized-dark":
                background_hex = "#002b36"
                foreground_hex = "#93a1a1"
                palette_hex = [
                    "#073642",
                    "#dc322f",
                    "#859900",
                    "#b58900",
                    "#268bd2",
                    "#d33682",
                    "#2aa198",
                    "#eee8d5",
                    "#002b36",
                    "#cb4b16",
                    "#586e75",
                    "#657b83",
                    "#839496",
                    "#6c71c4",
                    "#93a1a1",
                    "#fdf6e3",
                ]
            elif scheme == "solarized-light":
                background_hex = "#fdf6e3"
                foreground_hex = "#586e75"
                palette_hex = [
                    "#073642",
                    "#dc322f",
                    "#859900",
                    "#b58900",
                    "#268bd2",
                    "#d33682",
                    "#2aa198",
                    "#eee8d5",
                    "#002b36",
                    "#cb4b16",
                    "#586e75",
                    "#657b83",
                    "#839496",
                    "#6c71c4",
                    "#93a1a1",
                    "#fdf6e3",
                ]
        if appearance.palette:
            palette_hex = appearance.palette

        background = rgba(background_hex)
        foreground = rgba(foreground_hex)
        cursor = rgba(cursor_hex)
        highlight = rgba(highlight_hex)
        palette = [rgba(color) for color in palette_hex]
        return foreground, background, cursor, highlight, palette

    def _apply_appearance_to_terminal(self, term: Vte.Terminal, appearance: TerminalAppearanceSettings) -> None:
        desc = None
        if not appearance.use_system_mono_font:
            desc = Pango.FontDescription(appearance.custom_font or "Monospace")
            if appearance.font_size:
                desc.set_size(int(appearance.font_size * Pango.SCALE))
        if desc:
            term.set_font(desc)
        fg, bg, cursor, highlight, palette = self._preview_colors(appearance)
        term.set_colors(fg, bg, palette)
        term.set_color_background(bg)
        term.set_color_foreground(fg)
        term.set_color_cursor(cursor)
        term.set_color_highlight(highlight)
        term.set_bold_is_bright(appearance.bold_is_bright)
        shape = appearance.cursor_shape
        if shape == "ibeam":
            term.set_cursor_shape(Vte.CursorShape.IBEAM)
        elif shape == "underline":
            term.set_cursor_shape(Vte.CursorShape.UNDERLINE)
        else:
            term.set_cursor_shape(Vte.CursorShape.BLOCK)
        term.set_cursor_blink_mode(Vte.CursorBlinkMode.ON if appearance.cursor_blink else Vte.CursorBlinkMode.OFF)

    def _update_preview_terminal(self) -> None:
        appearance = self._appearance_from_controls()
        self._apply_appearance_to_terminal(self._preview_term, appearance)

    def _on_reset_clicked(self, _button: Gtk.Button) -> None:
        if self._on_reset:
            self._on_reset()

    def _on_export_clicked(self, _button: Gtk.Button) -> None:
        if self._on_export:
            self._on_export()

    def _choose_audit_path(self, _button: Gtk.Button) -> None:
        chooser = Gtk.FileChooserDialog(
            title="Select audit log file",
            parent=self,
            action=Gtk.FileChooserAction.SAVE,
            buttons=(
                Gtk.STOCK_CANCEL,
                Gtk.ResponseType.CANCEL,
                Gtk.STOCK_SAVE,
                Gtk.ResponseType.OK,
            ),
        )
        current = self._audit_path_entry.get_text()
        if current:
            chooser.set_filename(current)
        response = chooser.run()
        filename = chooser.get_filename() if response == Gtk.ResponseType.OK else None
        chooser.destroy()
        if filename:
            self._audit_path_entry.set_text(filename)

    def _choose_git_path(self, _button: Gtk.Button) -> None:
        chooser = Gtk.FileChooserDialog(
            title="Select Git directory",
            parent=self,
            action=Gtk.FileChooserAction.SELECT_FOLDER,
            buttons=(
                Gtk.STOCK_CANCEL,
                Gtk.ResponseType.CANCEL,
                Gtk.STOCK_OPEN,
                Gtk.ResponseType.OK,
            ),
        )
        current = self._git_path_entry.get_text()
        if current:
            chooser.set_current_folder(current)
        resp = chooser.run()
        folder = chooser.get_filename() if resp == Gtk.ResponseType.OK else None
        chooser.destroy()
        if folder:
            self._git_path_entry.set_text(folder)


class CredentialEditDialog(Gtk.Dialog):
    """Dialog for adding or editing a stored credential."""

    def __init__(self, parent: Gtk.Window, credential: Optional[Credential] = None) -> None:
        title = "Edit Credential" if credential else "New Credential"
        Gtk.Dialog.__init__(self, title=title, transient_for=parent, modal=True)
        self.add_button("_Cancel", Gtk.ResponseType.CANCEL)
        save_btn = self.add_button("_Save", Gtk.ResponseType.OK)
        save_btn.get_style_context().add_class("suggested-action")
        self.set_default_response(Gtk.ResponseType.OK)

        box = self.get_content_area()
        box.set_border_width(16)
        box.set_spacing(10)

        self.name_entry = Gtk.Entry()
        self.name_entry.set_placeholder_text("Label (e.g. Production)")
        box.pack_start(self._row("Name", self.name_entry), False, False, 0)

        self.user_entry = Gtk.Entry()
        self.user_entry.set_placeholder_text("Username")
        box.pack_start(self._row("Username", self.user_entry), False, False, 0)

        self.pass_entry = Gtk.Entry()
        self.pass_entry.set_visibility(False)
        self.pass_entry.set_invisible_char("•")
        box.pack_start(self._row("Password", self.pass_entry), False, False, 0)

        self._error_label = Gtk.Label()
        self._error_label.get_style_context().add_class("error")
        self._error_label.set_xalign(0.0)
        self._error_label.set_no_show_all(True)
        box.pack_start(self._error_label, False, False, 0)

        if credential:
            self.name_entry.set_text(credential.name)
            self.user_entry.set_text(credential.username)
            hint = Gtk.Label(label="Leave password blank to keep existing.")
            hint.get_style_context().add_class("dim-label")
            hint.set_xalign(0.0)
            box.pack_start(hint, False, False, 0)

        self.show_all()
        self.name_entry.grab_focus()

    def _row(self, label: str, widget: Gtk.Widget) -> Gtk.Box:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        lbl = Gtk.Label(label=label)
        lbl.set_xalign(0.0)
        lbl.get_style_context().add_class("dim-label")
        row.pack_start(lbl, False, False, 0)
        row.pack_start(widget, True, True, 0)
        return row

    def values(self) -> Optional[Tuple[str, str, Optional[str]]]:
        name = self.name_entry.get_text().strip()
        user = self.user_entry.get_text().strip()
        password = self.pass_entry.get_text()
        if not name or not user:
            self._error_label.set_text("Name and username are required.")
            self._error_label.show()
            return None
        self._error_label.hide()
        return name, user, password or None


class CredentialManagerDialog(Gtk.Dialog):
    """Simple CRUD list for saved credentials."""

    def __init__(self, parent: Gtk.Window, store: CredentialStore) -> None:
        Gtk.Dialog.__init__(self, title="Credentials", transient_for=parent, modal=True)
        self.add_button("_Close", Gtk.ResponseType.CLOSE)
        self.set_default_size(520, 360)
        self._store = store

        box = self.get_content_area()
        box.set_border_width(12)
        box.set_spacing(8)

        self._list_store = Gtk.ListStore(str, str, str)  # id, name, username
        self._tree = Gtk.TreeView(model=self._list_store)
        self._tree.set_headers_visible(True)
        for idx, (title, col_id) in enumerate([("Name", 1), ("Username", 2)]):
            renderer = Gtk.CellRendererText()
            column = Gtk.TreeViewColumn(title, renderer, text=col_id)
            column.set_resizable(True)
            self._tree.append_column(column)
        self._tree.connect("row-activated", lambda *_: self._edit())
        selection = self._tree.get_selection()
        selection.connect("changed", lambda *_: self._update_buttons())
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.add(self._tree)
        box.pack_start(scroller, True, True, 0)

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        add_btn = Gtk.Button(label="Add")
        add_btn.connect("clicked", lambda *_: self._add())
        edit_btn = Gtk.Button(label="Edit")
        edit_btn.connect("clicked", lambda *_: self._edit())
        del_btn = Gtk.Button(label="Delete")
        del_btn.connect("clicked", lambda *_: self._delete())
        self._edit_btn = edit_btn
        self._delete_btn = del_btn
        for btn in (add_btn, edit_btn, del_btn):
            btn_row.pack_start(btn, False, False, 0)
        box.pack_start(btn_row, False, False, 0)

        hint = Gtk.Label(label="Passwords are stored encrypted using the master password.")
        hint.get_style_context().add_class("dim-label")
        hint.set_xalign(0.0)
        box.pack_start(hint, False, False, 0)

        self._refresh()
        self._update_buttons()
        self.show_all()

    def _refresh(self) -> None:
        self._list_store.clear()
        for cred in self._store.list():
            self._list_store.append([cred.id, cred.name, cred.username])

    def _current_id(self) -> Optional[str]:
        selection = self._tree.get_selection()
        model, itr = selection.get_selected()
        if not model or not itr:
            return None
        return model.get_value(itr, 0)

    def _update_buttons(self) -> None:
        has_sel = bool(self._current_id())
        if hasattr(self, "_edit_btn"):
            self._edit_btn.set_sensitive(has_sel)
        if hasattr(self, "_delete_btn"):
            self._delete_btn.set_sensitive(has_sel)

    def _show_store_error(self, message: str, exc: SecureStoreError) -> None:
        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            buttons=Gtk.ButtonsType.OK,
            message_type=Gtk.MessageType.ERROR,
            text=message,
        )
        dialog.format_secondary_text(str(exc))
        dialog.run()
        dialog.destroy()

    def _add(self) -> None:
        dialog = CredentialEditDialog(self)
        while True:
            response = dialog.run()
            if response != Gtk.ResponseType.OK:
                break
            values = dialog.values()
            if values:
                name, user, password = values
                if name and user and password is not None:
                    try:
                        self._store.add(name, user, password)
                        self._refresh()
                    except SecureStoreError as exc:
                        self._show_store_error("Could not save credential.", exc)
                break
        dialog.destroy()

    def _edit(self) -> None:
        cred_id = self._current_id()
        if not cred_id:
            return
        cred = self._store.get(cred_id)
        if not cred:
            return
        dialog = CredentialEditDialog(self, cred)
        while True:
            response = dialog.run()
            if response != Gtk.ResponseType.OK:
                break
            values = dialog.values()
            if values:
                name, user, password = values
                if name and user:
                    try:
                        self._store.update(cred.id, name, user, password=password)
                        self._refresh()
                    except SecureStoreError as exc:
                        self._show_store_error("Could not update credential.", exc)
                break
        dialog.destroy()

    def _delete(self) -> None:
        cred_id = self._current_id()
        if not cred_id:
            return
        cred = self._store.get(cred_id)
        if not cred:
            self._refresh()
            return
        confirm = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            buttons=Gtk.ButtonsType.OK_CANCEL,
            message_type=Gtk.MessageType.QUESTION,
            text=f"Delete credential '{cred.name}'?",
        )
        confirm.format_secondary_text("This will remove the stored username/password and unbind any hosts using it.")
        response = confirm.run()
        confirm.destroy()
        if response == Gtk.ResponseType.OK:
            try:
                self._store.delete(cred_id)
                self._refresh()
            except SecureStoreError as exc:
                self._show_store_error("Could not delete credential.", exc)


class VaultDialog(Gtk.Dialog):
    """Manage encrypted vault (optional, additive)."""

    def __init__(self, parent: Gtk.Window, vault: CredentialVault) -> None:
        Gtk.Dialog.__init__(self, title="Credential Vault", transient_for=parent, modal=True)
        self.add_button("_Close", Gtk.ResponseType.CLOSE)
        self.set_default_size(520, 420)
        self._vault = vault

        box = self.get_content_area()
        box.set_border_width(12)
        box.set_spacing(10)

        if not vault.available:
            label = Gtk.Label(
                label="Encryption support is unavailable (missing 'cryptography' package).\nExisting flows are unchanged."
            )
            label.set_line_wrap(True)
            box.pack_start(label, True, True, 0)
            self.show_all()
            return

        self._status_label = Gtk.Label()
        self._status_label.set_xalign(0.0)
        box.pack_start(self._status_label, False, False, 0)

        action_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._unlock_entry = Gtk.Entry()
        self._unlock_entry.set_visibility(False)
        self._unlock_entry.set_invisible_char("•")
        self._unlock_entry.set_placeholder_text("Master password")
        action_row.pack_start(self._unlock_entry, True, True, 0)
        unlock_btn = Gtk.Button(label="Unlock")
        unlock_btn.connect("clicked", lambda *_: self._unlock())
        action_row.pack_start(unlock_btn, False, False, 0)
        box.pack_start(action_row, False, False, 0)

        create_btn = Gtk.Button(label="Create/Reset Vault")
        create_btn.connect("clicked", lambda *_: self._create_master())
        box.pack_start(create_btn, False, False, 0)

        change_btn = Gtk.Button(label="Change Master Password")
        change_btn.connect("clicked", lambda *_: self._change_master())
        box.pack_start(change_btn, False, False, 0)

        self._list_store = Gtk.ListStore(str, str, str)  # id, label, username
        self._tree = Gtk.TreeView(model=self._list_store)
        for idx, (title, col_idx) in enumerate([("Label", 1), ("Username", 2)]):
            renderer = Gtk.CellRendererText()
            column = Gtk.TreeViewColumn(title, renderer, text=col_idx)
            column.set_resizable(True)
            self._tree.append_column(column)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.add(self._tree)
        box.pack_start(scroller, True, True, 0)

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        add_btn = Gtk.Button(label="Add")
        add_btn.connect("clicked", lambda *_: self._add())
        edit_btn = Gtk.Button(label="Edit")
        edit_btn.connect("clicked", lambda *_: self._edit())
        del_btn = Gtk.Button(label="Delete")
        del_btn.connect("clicked", lambda *_: self._delete())
        export_btn = Gtk.Button(label="Export…")
        export_btn.connect("clicked", lambda *_: self._export())
        import_btn = Gtk.Button(label="Import…")
        import_btn.connect("clicked", lambda *_: self._import())
        for btn in (add_btn, edit_btn, del_btn, export_btn, import_btn):
            btn_row.pack_start(btn, False, False, 0)
        box.pack_start(btn_row, False, False, 0)

        self._refresh()
        self.show_all()

    # -------------------------------------------------------------- helpers
    def _set_status(self, text: str) -> None:
        if self._status_label:
            self._status_label.set_text(text)

    def _current_id(self) -> Optional[str]:
        selection = self._tree.get_selection()
        model, itr = selection.get_selected()
        if not model or not itr:
            return None
        return model.get_value(itr, 0)

    def _refresh(self) -> None:
        self._list_store.clear()
        if not self._vault.unlocked:
            self._set_status("Locked")
            return
        for entry in self._vault.entries():
            self._list_store.append([entry.id, entry.label, entry.username])
        self._set_status("Unlocked")

    def _prompt_text(self, title: str, placeholder: str, secret: bool = False) -> Optional[str]:
        dialog = Gtk.Dialog(title=title, transient_for=self, modal=True)
        dialog.add_button("_Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("_OK", Gtk.ResponseType.OK)
        entry = Gtk.Entry()
        entry.set_placeholder_text(placeholder)
        if secret:
            entry.set_visibility(False)
            entry.set_invisible_char("•")
        box = dialog.get_content_area()
        box.set_border_width(12)
        box.pack_start(entry, False, False, 0)
        dialog.show_all()
        response = dialog.run()
        text = entry.get_text()
        dialog.destroy()
        if response == Gtk.ResponseType.OK and text:
            return text
        return None

    # -------------------------------------------------------------- actions
    def _unlock(self) -> None:
        password = self._unlock_entry.get_text()
        if not password:
            self._set_status("Enter master password to unlock.")
            return
        ok = False
        try:
            ok = self._vault.unlock(password)
        except VaultError as exc:
            self._set_status(str(exc))
            return
        if ok:
            self._set_status("Unlocked")
            self._refresh()
        else:
            self._set_status("Incorrect master password.")

    def _create_master(self) -> None:
        password = self._prompt_text("Set Master Password", "New master password", secret=True)
        confirm = self._prompt_text("Confirm Master Password", "Confirm", secret=True) if password else None
        if not password or password != confirm:
            self._set_status("Master password not set (empty or mismatch).")
            return
        try:
            self._vault.create_master(password)
            self._set_status("Vault created and unlocked.")
            self._refresh()
        except VaultError as exc:
            self._set_status(str(exc))

    def _change_master(self) -> None:
        old_pw = self._prompt_text("Current Master Password", "Current password", secret=True)
        new_pw = self._prompt_text("New Master Password", "New password", secret=True) if old_pw else None
        confirm = self._prompt_text("Confirm New Master Password", "Confirm", secret=True) if new_pw else None
        if not old_pw or not new_pw or new_pw != confirm:
            self._set_status("Master password change cancelled or mismatch.")
            return
        try:
            if self._vault.change_master(old_pw, new_pw):
                self._set_status("Master password updated.")
                self._refresh()
            else:
                self._set_status("Incorrect current master password.")
        except VaultError as exc:
            self._set_status(str(exc))

    def _add(self) -> None:
        if not self._vault.unlocked:
            self._set_status("Unlock vault first.")
            return
        label = self._prompt_text("Credential Label", "Label")
        username = self._prompt_text("Username", "Username")
        password = self._prompt_text("Password", "Password", secret=True)
        if not label or not username or password is None:
            return
        import uuid
        entry = VaultEntry(uuid.uuid4().hex, label, username, password)
        try:
            self._vault.add_entry(entry)
            self._refresh()
        except VaultError as exc:
            self._set_status(str(exc))

    def _edit(self) -> None:
        vid = self._current_id()
        if not vid:
            return
        entry = self._vault.get_entry(vid)
        if not entry:
            return
        label = self._prompt_text("Credential Label", "Label") or entry.label
        username = self._prompt_text("Username", "Username") or entry.username
        password = self._prompt_text("Password (blank to keep)", "Password", secret=True)
        new_entry = VaultEntry(entry.id, label, username, entry.password if password is None else password)
        try:
            self._vault.update_entry(new_entry)
            self._refresh()
        except VaultError as exc:
            self._set_status(str(exc))

    def _delete(self) -> None:
        vid = self._current_id()
        if not vid:
            return
        try:
            self._vault.delete_entry(vid)
            self._refresh()
        except VaultError as exc:
            self._set_status(str(exc))

    def _export(self) -> None:
        if not self._vault.unlocked:
            self._set_status("Unlock vault first.")
            return
        chooser = Gtk.FileChooserDialog(
            title="Export Vault",
            parent=self,
            action=Gtk.FileChooserAction.SAVE,
            buttons=(
                Gtk.STOCK_CANCEL,
                Gtk.ResponseType.CANCEL,
                Gtk.STOCK_SAVE,
                Gtk.ResponseType.OK,
            ),
        )
        response = chooser.run()
        target = chooser.get_filename() if response == Gtk.ResponseType.OK else None
        chooser.destroy()
        if not target:
            return
        pw = self._prompt_text("Export Password", "Password", secret=True)
        if not pw:
            return
        try:
            self._vault.export_encrypted(Path(target), pw)
            self._set_status("Exported vault.")
        except VaultError as exc:
            self._set_status(str(exc))

    def _import(self) -> None:
        chooser = Gtk.FileChooserDialog(
            title="Import Vault",
            parent=self,
            action=Gtk.FileChooserAction.OPEN,
            buttons=(
                Gtk.STOCK_CANCEL,
                Gtk.ResponseType.CANCEL,
                Gtk.STOCK_OPEN,
                Gtk.ResponseType.OK,
            ),
        )
        response = chooser.run()
        src = chooser.get_filename() if response == Gtk.ResponseType.OK else None
        chooser.destroy()
        if not src:
            return
        pw = self._prompt_text("Import Password", "Password", secret=True)
        if not pw:
            return
        try:
            ok = self._vault.import_encrypted(Path(src), pw)
            self._set_status("Imported entries." if ok else "Import failed.")
            if ok:
                self._refresh()
        except VaultError as exc:
            self._set_status(str(exc))


class SSHManagerApplication(Gtk.Application):
    """GTK application entry point."""

    def __init__(self, base_dir: Path) -> None:
        Gtk.Application.__init__(self, application_id="org.codex.SSHManager")
        self._base_dir = base_dir
        self._window: Optional[SSHManagerWindow] = None
        self._css_provider: Optional[Gtk.CssProvider] = None
        self._settings = AppSettings.load()
        self._master_password: Optional[str] = None

    def do_startup(self) -> None:  # type: ignore[override]
        Gtk.Application.do_startup(self)
        self._install_css(False)
        self._create_actions()

    def do_activate(self) -> None:  # type: ignore[override]
        if not self._window:
            if not self._ensure_master_password():
                self.quit()
                return
            self._window = SSHManagerWindow(
                self,
                self._base_dir,
                self._settings,
                master_password=self._master_password,
            )
        self._window.present()

    def _ensure_master_password(self) -> bool:
        if self._settings.master_password_disabled:
            return True
        if not self._settings.master_password_hash:
            return self._prompt_set_master_password()
        return self._prompt_master_auth()

    def _prompt_master_auth(self) -> bool:
        dialog = Gtk.Dialog(modal=True)
        dialog.set_destroy_with_parent(True)
        dialog.set_position(Gtk.WindowPosition.CENTER)
        dialog.add_button("_Cancel", Gtk.ResponseType.CANCEL)
        unlock_btn = dialog.add_button("_Unlock", Gtk.ResponseType.OK)
        unlock_btn.get_style_context().add_class("suggested-action")
        dialog.set_default_response(Gtk.ResponseType.OK)

        header = Gtk.HeaderBar(title="Unlock SSH Manager")
        header.set_show_close_button(True)
        dialog.set_titlebar(header)

        content = dialog.get_content_area()
        content.set_border_width(18)
        content.set_spacing(14)

        intro = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        icon = Gtk.Image.new_from_icon_name("changes-prevent-symbolic", Gtk.IconSize.DIALOG)
        intro.pack_start(icon, False, False, 0)
        label = Gtk.Label(label="<b>Enter your master password</b>\nThis protects saved SSH credentials.", use_markup=True)
        label.set_xalign(0.0)
        label.set_line_wrap(True)
        intro.pack_start(label, True, True, 0)
        content.pack_start(intro, False, False, 0)

        entry = Gtk.Entry()
        entry.set_visibility(False)
        entry.set_invisible_char("•")
        entry.set_activates_default(True)
        entry.set_placeholder_text("Master password")
        entry.set_margin_top(4)
        content.pack_start(entry, False, False, 0)

        show_toggle = Gtk.CheckButton(label="Show password")
        show_toggle.connect("toggled", lambda btn: entry.set_visibility(btn.get_active()))
        show_toggle.set_halign(Gtk.Align.START)
        content.pack_start(show_toggle, False, False, 0)

        error_label = Gtk.Label()
        error_label.get_style_context().add_class("error")
        error_label.set_no_show_all(True)
        error_label.set_xalign(0.0)
        content.pack_start(error_label, False, False, 0)

        def show_error(message: str) -> None:
            error_label.set_text(message)
            error_label.show()

        dialog.show_all()
        entry.grab_focus()

        while True:
            response = dialog.run()
            text = entry.get_text().strip()
            if response != Gtk.ResponseType.OK:
                dialog.destroy()
                return False
            if text:
                if verify_master_password(text, self._settings.master_password_hash):
                    if master_hash_needs_upgrade(self._settings.master_password_hash):
                        self._settings.master_password_hash = hash_master_password(text)
                        self._settings.save()
                    self._master_password = text
                    dialog.destroy()
                    return True
            entry.set_text("")
            show_error("Incorrect master password. Please try again.")

    def _prompt_set_master_password(self) -> bool:
        dialog = Gtk.Dialog(modal=True)
        dialog.set_destroy_with_parent(True)
        dialog.set_position(Gtk.WindowPosition.CENTER)
        dialog.add_button("_Cancel", Gtk.ResponseType.CANCEL)
        save_btn = dialog.add_button("_Save", Gtk.ResponseType.OK)
        save_btn.get_style_context().add_class("suggested-action")
        dialog.set_default_response(Gtk.ResponseType.OK)

        header = Gtk.HeaderBar(title="Set Master Password")
        header.set_show_close_button(True)
        dialog.set_titlebar(header)

        content = dialog.get_content_area()
        content.set_border_width(18)
        content.set_spacing(14)

        intro = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        icon = Gtk.Image.new_from_icon_name("emblem-readonly", Gtk.IconSize.DIALOG)
        intro.pack_start(icon, False, False, 0)
        label = Gtk.Label(label="<b>Create a master password</b>\nIt secures stored SSH passwords.", use_markup=True)
        label.set_xalign(0.0)
        label.set_line_wrap(True)
        intro.pack_start(label, True, True, 0)
        content.pack_start(intro, False, False, 0)

        entry = Gtk.Entry()
        entry.set_visibility(False)
        entry.set_invisible_char("•")
        entry.set_activates_default(True)
        entry.set_placeholder_text("New master password")
        entry.set_margin_top(4)
        content.pack_start(entry, False, False, 0)

        confirm = Gtk.Entry()
        confirm.set_visibility(False)
        confirm.set_invisible_char("•")
        confirm.set_activates_default(True)
        confirm.set_placeholder_text("Confirm password")
        content.pack_start(confirm, False, False, 0)

        error_label = Gtk.Label()
        error_label.get_style_context().add_class("error")
        error_label.set_no_show_all(True)
        error_label.set_xalign(0.0)
        content.pack_start(error_label, False, False, 0)

        def show_error(message: str) -> None:
            error_label.set_text(message)
            error_label.show()

        dialog.show_all()
        entry.grab_focus()
        while True:
            response = dialog.run()
            text = entry.get_text().strip()
            confirm_text = confirm.get_text().strip()
            if response != Gtk.ResponseType.OK:
                dialog.destroy()
                return False
            if not text:
                show_error("Password cannot be empty.")
                continue
            if text != confirm_text:
                show_error("Passwords do not match.")
                continue
            self._settings.master_password_hash = hash_master_password(text)
            self._settings.save()
            self._master_password = text
            dialog.destroy()
            return True

    def _install_css(self, prefer_dark: bool = True) -> None:
        screen = Gdk.Screen.get_default()
        if not screen:
            return
        if self._css_provider is not None:
            try:
                Gtk.StyleContext.remove_provider_for_screen(screen, self._css_provider)
            except Exception:
                pass
            self._css_provider = None
        if not prefer_dark:
            return
        provider = Gtk.CssProvider()
        try:
            provider.load_from_data(APP_CSS_DARK.encode("utf-8"))
        except GLib.Error:
            return
        Gtk.StyleContext.add_provider_for_screen(
            screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        self._css_provider = provider

    def _create_actions(self) -> None:
        mapping = {
            "new-host": lambda: self._with_window(lambda win: win.prompt_new_host()),
            "new-tab": lambda: self._with_window(lambda win: win.open_selected_host()),
            "close-tab": lambda: self._with_window(lambda win: win.terminal_tabs.close_current()),
            "copy": lambda: self._with_window(lambda win: win.terminal_tabs.copy_current()),
            "paste": lambda: self._with_window(lambda win: win.terminal_tabs.paste_current()),
            "interrupt": lambda: self._with_window(lambda win: win.terminal_tabs.interrupt_current()),
            "focus-search": lambda: self._with_window(lambda win: win.focus_search()),
            "font-increase": lambda: self._with_window(lambda win: win.terminal_tabs.increase_font()),
            "font-decrease": lambda: self._with_window(lambda win: win.terminal_tabs.decrease_font()),
            "font-reset": lambda: self._with_window(lambda win: win.terminal_tabs.reset_font()),
        }
        for name, callback in mapping.items():
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda _act, _param, cb=callback: cb())
            self.add_action(action)

        self.set_accels_for_action("app.new-host", ("<Primary>n",))
        self.set_accels_for_action("app.new-tab", ("<Primary>t",))
        self.set_accels_for_action("app.close-tab", ("<Primary>w",))
        self.set_accels_for_action("app.copy", ("<Primary><Shift>c",))
        self.set_accels_for_action("app.paste", ("<Primary><Shift>v",))
        self.set_accels_for_action("app.interrupt", ("<Primary><Shift>k",))
        self.set_accels_for_action("app.focus-search", ("<Primary>f",))
        self.set_accels_for_action("app.font-increase", ("<Primary>plus", "<Primary>equal", "<Primary>KP_Add"))
        self.set_accels_for_action("app.font-decrease", ("<Primary>minus", "<Primary>KP_Subtract"))
        self.set_accels_for_action("app.font-reset", ("<Primary>0", "<Primary>KP_0"))

    def _with_window(self, func) -> None:
        if self._window:
            func(self._window)


class SSHManagerWindow(Gtk.ApplicationWindow):
    """Main window for the SSH manager."""

    COLUMN_ICON = 0
    COLUMN_TEXT = 1
    COLUMN_KIND = 2
    COLUMN_PARENT = 3
    COLUMN_CHILD = 4
    COLUMN_ALIAS = 5
    COLUMN_HOST = 6
    COLUMN_SEARCH = 7
    COLUMN_COLOR = 8
    COLUMN_STATUS_COLOR = 9
    ICON_GROUP = "ssh-manager-group"
    ICON_SUBGROUP = "ssh-manager-subgroup"
    ICON_HOST = "ssh-manager-host"

    def __init__(
        self,
        app: Gtk.Application,
        base_dir: Path,
        settings: AppSettings,
        *,
        master_password: Optional[str] = None,
    ) -> None:
        Gtk.ApplicationWindow.__init__(self, application=app)
        self.set_title("SSH Manager")
        self.set_default_size(1280, 820)
        self.set_size_request(960, 640)
        self.set_name("ssh-manager-window")

        self.manager = ConfigManager(base_dir)
        self._settings = settings
        self._master_password = master_password
        self._secure_store = SecureStore.with_default_backend(master_password=master_password)
        self._credential_store = CredentialStore(self._secure_store)
        self._vault = CredentialVault(default_vault_path())
        self._audit_lock = threading.Lock()
        self._password_dialog = PasswordDialog()
        self._paramiko_connectors: Dict[object, Connector] = {}
        self._ui_state_path = ui_state_path_for_base(base_dir)
        self._ui_state = self._load_ui_state()
        self._profiles: Dict[str, SessionProfile] = {p.id: p for p in built_in_profiles()}
        self._shared_manager: Optional[ConfigManager] = None
        self._shared_path: Optional[Path] = None
        self._read_only_keys: set[str] = set()
        self._shared_poll_id: int = 0
        self._shared_mtime: Optional[float] = None
        self._shared_missing_warned = False
        colors = self._ui_state.get("group_colors", {})
        self._group_colors: Dict[str, str] = (
            {str(k): str(v) for k, v in colors.items()} if isinstance(colors, dict) else {}
        )
        host_colors = self._ui_state.get("host_colors", {})
        self._host_colors: Dict[str, str] = (
            {str(k): str(v) for k, v in host_colors.items()} if isinstance(host_colors, dict) else {}
        )
        self._color_pixbuf_cache: Dict[str, GdkPixbuf.Pixbuf] = {}

        self._prefer_dark = bool(self._ui_state.get("prefer_dark", True))
        self._ui_font_size = int(self._ui_state.get("ui_font_size", 14))
        settings = Gtk.Settings.get_default()
        self._base_font_family = "Sans"
        if settings:
            font_name = settings.get_property("gtk-font-name") or ""
            if font_name:
                desc = Pango.FontDescription(font_name)
                family = desc.get_family()
                if family:
                    self._base_font_family = family
        self._apply_ui_font(self._ui_font_size)
        self._host_lookup: Dict[str, HostEntry] = {}
        self._host_status: Dict[str, str] = {}
        self._host_iters: Dict[str, Gtk.TreeIter] = {}
        self._filter_query: str = ""
        self._filter_error: str = ""
        self._smart_groups: List[SmartGroup] = load_smart_groups()
        self._status_timeout_id = 0
        self._git_watch_thread: Optional[threading.Thread] = None
        self._git_watch_stop = threading.Event()
        self._git_updates_available = False
        self._git_watch_path: Optional[Path] = None
        self._sidebar_visible = bool(self._ui_state.get("sidebar_visible", True))
        self._sidebar_width_saved = int(self._ui_state.get("sidebar_width", 320))
        self._sidebar_container: Optional[Gtk.Widget] = None
        self._sidebar_toggle_button: Optional[Gtk.Button] = None
        self._sidebar_toggle_icon: Optional[Gtk.Image] = None
        self._sidebar_visible_before_max = self._sidebar_visible
        self._terminal_maximized = False
        self._terminal_toggle_button: Optional[Gtk.Button] = None
        self._detail_revealer: Optional[Gtk.Revealer] = None
        self._status_visible_before_max = False
        self.search_revealer: Optional[Gtk.Revealer] = None
        self.search_entry: Optional[Gtk.SearchEntry] = None
        self.filter_entry: Optional[Gtk.Entry] = None
        self._search_inputs_box: Optional[Gtk.Box] = None
        self._search_toggle_button: Optional[Gtk.Button] = None
        self._search_toggle_icon: Optional[Gtk.Image] = None
        self._search_inputs_orientation = Gtk.Orientation.VERTICAL
        self._search_panel_escape_ready = False

        self.tree_store = Gtk.TreeStore(str, str, str, str, str, str, object, str, str, str)

        self._build_ui()
        self._register_actions()
        self._apply_ui_state()
        self._rebuild_tree(refresh=True)
        self._refresh_git_watch()
        backend = self._secure_store.backend
        if isinstance(backend, FileKeyring) and backend.locked:
            self._notify_warning(
                "Saved passwords are encrypted but could not be unlocked; enable the master password to use them."
            )

        self.connect("delete-event", self._on_delete_event)
        self.connect("destroy", self._on_destroy)

    # ------------------------------------------------------------------ setup UI
    def _build_ui(self) -> None:
        header = Gtk.HeaderBar(title="SSH Manager")
        header.set_show_close_button(True)
        header.get_style_context().add_class("toolbar-header")
        header.get_style_context().add_class("toolbar-header")

        end_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        header.pack_end(end_box)

        self._credentials_button = self._toolbar_button(
            "dialog-password-symbolic",
            "Credentials",
            self._on_credentials_clicked,
        )
        self._credentials_button.set_tooltip_text("Manage saved credentials (username/password).")
        end_box.pack_start(self._credentials_button, False, False, 0)

        self._settings_button = self._toolbar_button("emblem-system-symbolic", "Settings", self._on_settings_clicked)
        self._settings_button.set_tooltip_text("Open application settings and policies.")
        end_box.pack_start(self._settings_button, False, False, 0)

        self._config_view_button = self._toolbar_button(
            "text-x-generic-symbolic",
            "Config",
            self._on_config_view_clicked,
        )
        self._config_view_button.set_tooltip_text("View or edit SSH config files.")
        end_box.pack_start(self._config_view_button, False, False, 0)

        self._vault_button = self._toolbar_button(
            "dialog-password-symbolic",
            "Vault",
            self._on_vault_clicked,
        )
        self._vault_button.set_tooltip_text("Open the encrypted credential vault.")
        end_box.pack_start(self._vault_button, False, False, 0)

        self._shared_button = self._toolbar_button(
            "folder-remote-symbolic",
            "Open Shared",
            self._on_open_shared_clicked,
        )
        self._shared_button.set_tooltip_text("Load an additional read-only shared inventory.")
        end_box.pack_start(self._shared_button, False, False, 0)

        self._git_update_button = self._toolbar_button(
            "software-update-available-symbolic",
            "Git updates",
            self._on_git_updates_clicked,
        )
        self._git_update_button.set_tooltip_text("New commits available in remote repository.")
        self._git_update_button.hide()
        end_box.pack_start(self._git_update_button, False, False, 0)

        self._terminal_toggle_button = self._toolbar_button(
            "view-fullscreen-symbolic",
            "Maximize",
            self._toggle_terminal_maximized,
        )
        end_box.pack_start(self._terminal_toggle_button, False, False, 0)

        self.set_titlebar(header)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        root.set_name("root")
        self.add(root)

        self._status_revealer = Gtk.Revealer()
        self._status_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_UP)
        self._status_revealer.set_transition_duration(220)
        self._status_revealer.set_reveal_child(False)

        self._bulk_revealer = Gtk.Revealer()
        self._bulk_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self._bulk_revealer.set_transition_duration(180)
        self._bulk_revealer.set_reveal_child(False)

        self._status_label = Gtk.Label()
        self._status_label.set_xalign(0.0)
        status_box = Gtk.Box(spacing=6)
        status_box.set_border_width(8)
        status_box.set_name("status-banner")
        status_box.pack_start(self._status_label, True, True, 0)

        bulk_box = Gtk.Box(spacing=6)
        bulk_box.set_border_width(8)
        bulk_box.set_name("status-banner")
        bulk_label = Gtk.Label(label="Bulk actions:")
        bulk_label.set_xalign(0.0)
        bulk_box.pack_start(bulk_label, False, False, 0)
        bulk_connect_btn = Gtk.Button(label="Connect Selected")
        bulk_connect_btn.connect("clicked", lambda *_: self._bulk_connect_selected())
        bulk_box.pack_start(bulk_connect_btn, False, False, 0)
        bulk_move_btn = Gtk.Button(label="Move Selected…")
        bulk_move_btn.connect("clicked", lambda *_: self._bulk_move_selected())
        bulk_box.pack_start(bulk_move_btn, False, False, 0)
        bulk_tag_btn = Gtk.Button(label="Tag/Color Selected…")
        bulk_tag_btn.connect("clicked", lambda *_: self._bulk_tag_selected())
        bulk_box.pack_start(bulk_tag_btn, False, False, 0)
        self._bulk_revealer.add(bulk_box)

        self._status_revealer.add(status_box)
        self._status_revealer.connect("notify::child-revealed", self._on_status_revealer_revealed)
        if not self._status_revealer.get_child_revealed():
            self._status_revealer.hide()

        main_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        root.pack_start(main_box, True, True, 0)

        toggle_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        toggle_box.set_border_width(0)
        toggle_box.set_vexpand(True)
        main_box.pack_start(toggle_box, False, False, 0)

        self._sidebar_toggle_button = Gtk.Button()
        self._sidebar_toggle_button.set_relief(Gtk.ReliefStyle.NONE)
        self._sidebar_toggle_button.get_style_context().add_class("sidebar-toggle")
        self._sidebar_toggle_button.set_size_request(28, -1)
        self._sidebar_toggle_icon = Gtk.Image.new_from_icon_name("go-previous-symbolic", Gtk.IconSize.BUTTON)
        self._sidebar_toggle_button.add(self._sidebar_toggle_icon)
        self._sidebar_toggle_button.connect("clicked", lambda _btn: self._toggle_sidebar())
        toggle_box.pack_start(self._sidebar_toggle_button, False, False, 6)
        toggle_box.pack_start(Gtk.Box(), True, True, 0)

        self._paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        main_box.pack_start(self._paned, True, True, 0)

        sidebar_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        sidebar_box.set_border_width(12)
        sidebar_box.set_name("sidebar")
        self._sidebar_container = sidebar_box

        search_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        search_label = Gtk.Label(label="Search & Filter")
        search_label.set_xalign(0.0)
        search_label.get_style_context().add_class("dim-label")
        search_header.pack_start(search_label, True, True, 0)
        self._search_toggle_button = Gtk.Button()
        self._search_toggle_button.set_relief(Gtk.ReliefStyle.NONE)
        self._search_toggle_button.get_style_context().add_class("search-toggle-button")
        self._search_toggle_button.set_tooltip_text("Show search panel")
        self._search_toggle_icon = Gtk.Image.new_from_icon_name("system-search-symbolic", Gtk.IconSize.BUTTON)
        self._search_toggle_icon.get_style_context().add_class("search-toggle-icon")
        self._search_toggle_button.add(self._search_toggle_icon)
        self._search_toggle_button.connect("clicked", lambda _btn: self._toggle_search_panel())
        search_header.pack_start(self._search_toggle_button, False, False, 0)
        sidebar_box.pack_start(search_header, False, False, 0)

        self.search_revealer = Gtk.Revealer()
        self.search_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self.search_revealer.set_transition_duration(150)
        self.search_revealer.set_reveal_child(False)
        self.search_revealer.connect("notify::child-revealed", self._on_search_panel_reveal)
        sidebar_box.pack_start(self.search_revealer, False, False, 0)

        search_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        search_card.set_hexpand(True)
        search_card.set_margin_bottom(6)
        search_card.get_style_context().add_class("search-card")
        self._search_inputs_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        search_card.pack_start(self._search_inputs_box, False, False, 0)

        self._search_entry = Gtk.SearchEntry()
        self._search_entry.set_placeholder_text("Search hosts, users, tags…")
        self._search_entry.connect("search-changed", self._on_search_changed)
        self._search_entry.connect("key-press-event", self._on_search_key_press)
        self._search_entry.get_style_context().add_class("search-input")
        self._search_entry.set_hexpand(True)
        self._search_inputs_box.pack_start(self._search_entry, True, True, 0)
        self._search_escape_time = 0.0
        self.search_entry = self._search_entry

        self._filter_entry = Gtk.Entry()
        self._filter_entry.set_placeholder_text("Filter (e.g. tag:prod AND owner:alice)")
        self._filter_entry.connect("changed", self._on_filter_changed)
        self._filter_entry.connect("key-press-event", self._on_filter_key_press)
        self._filter_entry.get_style_context().add_class("search-input")
        self._filter_entry.set_hexpand(True)
        self._search_inputs_box.pack_start(self._filter_entry, True, True, 0)
        self.filter_entry = self._filter_entry

        self._filter_error_label = Gtk.Label()
        self._filter_error_label.set_xalign(0.0)
        self._filter_error_label.get_style_context().add_class("error")
        self._filter_error_label.set_no_show_all(True)
        search_card.pack_start(self._filter_error_label, False, False, 0)

        self._smart_group_list = Gtk.ListBox()
        self._smart_group_list.connect("row-activated", self._on_smart_group_activated)
        smart_frame = Gtk.Frame(label="Smart Groups")
        smart_frame.get_style_context().add_class("dim-label")
        smart_frame.add(self._smart_group_list)
        search_card.pack_start(smart_frame, False, False, 0)
        self._refresh_smart_group_list()

        sg_btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        sg_btn_row.get_style_context().add_class("search-card-actions")
        add_sg_btn = Gtk.Button(label="Save Filter")
        add_sg_btn.connect("clicked", lambda *_: self._save_current_filter_as_group())
        del_sg_btn = Gtk.Button(label="Delete")
        del_sg_btn.connect("clicked", lambda *_: self._delete_selected_smart_group())
        for btn in (add_sg_btn, del_sg_btn):
            sg_btn_row.pack_start(btn, False, False, 0)
        search_card.pack_start(sg_btn_row, False, False, 0)

        self.search_revealer.add(search_card)
        if not self.search_revealer.get_child_revealed():
            self.search_revealer.hide()
        self._update_search_toggle_state()

        self.tree_view = Gtk.TreeView(model=self.tree_store)
        self.tree_view.set_headers_visible(False)
        self.tree_view.set_enable_tree_lines(True)
        self.tree_view.connect("row-activated", self._on_row_activated)
        self.tree_view.connect("button-press-event", self._on_tree_button_press)
        self.tree_view.connect("key-press-event", self._on_tree_key_press)
        selection = self.tree_view.get_selection()
        selection.set_mode(Gtk.SelectionMode.MULTIPLE)
        selection.connect("changed", self._on_selection_changed)
        self._configure_drag_and_drop()

        icon_renderer = Gtk.CellRendererPixbuf()
        text_renderer = Gtk.CellRendererText()
        text_renderer.set_property("ellipsize", Pango.EllipsizeMode.END)

        column = Gtk.TreeViewColumn()
        column.pack_start(icon_renderer, False)
        column.pack_start(text_renderer, True)
        column.set_cell_data_func(icon_renderer, self._render_icon)
        column.set_cell_data_func(text_renderer, self._render_text)
        self.tree_view.append_column(column)

        tree_scroller = Gtk.ScrolledWindow()
        tree_scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        tree_scroller.add(self.tree_view)
        sidebar_box.pack_start(tree_scroller, True, True, 0)

        self._paned.pack1(sidebar_box, resize=True, shrink=False)

        right_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        right_box.set_border_width(12)
        right_box.set_name("content")

        self._detail_frame = Gtk.Frame()
        self._detail_frame.set_shadow_type(Gtk.ShadowType.NONE)
        self._detail_frame.set_name("detail-frame")
        self._detail_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self._detail_box.set_name("detail-box")
        self._detail_frame.add(self._detail_box)
        self._detail_revealer = Gtk.Revealer()
        self._detail_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_UP)
        self._detail_revealer.set_transition_duration(250)
        self._detail_revealer.set_reveal_child(True)
        self._detail_revealer.add(self._detail_frame)
        self._detail_revealer.connect("notify::child-revealed", self._on_detail_revealer_revealed)
        if not self._detail_revealer.get_child_revealed():
            self._detail_revealer.hide()
        right_box.pack_start(self._detail_revealer, False, False, 0)

        self._detail_title = Gtk.Label()
        self._detail_title.set_xalign(0.0)
        self._detail_title.get_style_context().add_class("detail-title")
        self._detail_box.pack_start(self._detail_title, False, False, 0)

        self._detail_subtitle = Gtk.Label()
        self._detail_subtitle.set_xalign(0.0)
        self._detail_subtitle.get_style_context().add_class("detail-subtitle")
        self._detail_box.pack_start(self._detail_subtitle, False, False, 0)

        self._tag_flow = Gtk.FlowBox()
        self._tag_flow.set_selection_mode(Gtk.SelectionMode.NONE)
        self._tag_flow.set_column_spacing(4)
        self._tag_flow.set_row_spacing(4)
        self._detail_box.pack_start(self._tag_flow, False, False, 0)

        self._detail_grid = Gtk.Grid(column_spacing=12, row_spacing=6)
        self._detail_box.pack_start(self._detail_grid, False, False, 0)

        default_font = float(self._ui_state.get("font_scale", 1.0))
        self.terminal_tabs = TerminalNotebook(
            self._log_event,
            default_font_scale=default_font,
            password_store=self._secure_store,
            password_update_handler=self._save_detected_password_change,
            appearance=self._settings.terminal_appearance,
        )
        self.terminal_tabs.set_name("terminal-area")
        right_box.pack_start(self.terminal_tabs, True, True, 0)

        right_box.pack_start(self._status_revealer, False, False, 0)
        right_box.pack_start(self._bulk_revealer, False, False, 0)

        self._paned.pack2(right_box, resize=True, shrink=False)

        self._update_sidebar_toggle_ui()
        self._update_terminal_toggle_ui()
        self._status_visible_before_max = self._status_revealer.get_reveal_child()
        self._update_toolbar_state()
        self._apply_dark_theme(self._prefer_dark)
        app = self.get_application()
        if app and hasattr(app, "_install_css"):
            app._install_css(self._prefer_dark)
        self.show_all()
        self._status_revealer.set_reveal_child(False)
        self._bulk_revealer.set_reveal_child(False)
        self.connect("size-allocate", self._on_window_size_allocate)

    def _toolbar_button(self, icon_name: str, label: str, callback) -> Gtk.Button:
        button = Gtk.Button()
        button.set_relief(Gtk.ReliefStyle.NONE)
        image = Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.BUTTON)
        button.set_image(image)
        button.set_always_show_image(True)
        button.set_label(label)
        button.connect("clicked", lambda _btn: callback())
        return button

    def _update_sidebar_toggle_ui(self) -> None:
        if not self._sidebar_toggle_icon or not self._sidebar_toggle_button:
            return
        if self._sidebar_visible:
            icon_name = "go-previous-symbolic"
            tooltip = "Hide sidebar"
        else:
            icon_name = "go-next-symbolic"
            tooltip = "Show sidebar"
        if self._terminal_maximized:
            self._sidebar_toggle_button.set_sensitive(False)
            tooltip = "Restore terminals to adjust sidebar"
        else:
            self._sidebar_toggle_button.set_sensitive(True)
        self._sidebar_toggle_icon.set_from_icon_name(icon_name, Gtk.IconSize.BUTTON)
        self._sidebar_toggle_button.set_tooltip_text(tooltip)

    def _toggle_sidebar(self) -> None:
        if self._terminal_maximized:
            return
        self._set_sidebar_visible(not self._sidebar_visible)

    def _set_sidebar_visible(self, visible: bool, *, initial: bool = False, persist: bool = True) -> None:
        if self._sidebar_container is None:
            self._sidebar_visible = visible
            return
        if visible == self._sidebar_visible and not initial:
            self._update_sidebar_toggle_ui()
            return
        if visible:
            self._sidebar_container.show_all()
            width = max(220, self._sidebar_width_saved)
            GLib.idle_add(self._paned.set_position, width)
        else:
            if self._sidebar_visible:
                self._sidebar_width_saved = max(220, self._paned.get_position())
            self._sidebar_container.hide()
            self._paned.set_position(0)
        self._sidebar_visible = visible
        if not self._terminal_maximized and persist:
            self._sidebar_visible_before_max = visible
        self._update_sidebar_toggle_ui()
        if not initial and persist:
            self._save_ui_state()

    def _update_terminal_toggle_ui(self) -> None:
        if not self._terminal_toggle_button:
            return
        tooltip = "Restore panels" if self._terminal_maximized else "Maximize terminals"
        image = Gtk.Image.new_from_icon_name(
            "view-restore-symbolic" if self._terminal_maximized else "view-fullscreen-symbolic",
            Gtk.IconSize.BUTTON,
        )
        self._terminal_toggle_button.set_image(image)
        self._terminal_toggle_button.set_label("")
        self._terminal_toggle_button.set_tooltip_text(tooltip)

    def _toggle_terminal_maximized(self) -> None:
        self._set_terminal_maximized(not self._terminal_maximized)

    def _set_terminal_maximized(self, maximized: bool, *, initial: bool = False) -> None:
        if maximized == self._terminal_maximized and not initial:
            self._update_terminal_toggle_ui()
            return
        self._terminal_maximized = maximized
        if maximized:
            self._sidebar_visible_before_max = self._sidebar_visible
            self._status_visible_before_max = self._status_revealer.get_reveal_child()
            if self._detail_revealer:
                self._detail_revealer.show()
                self._detail_revealer.set_reveal_child(False)
            self._status_revealer.show()
            self._status_revealer.set_reveal_child(False)
            if self._sidebar_visible_before_max:
                self._set_sidebar_visible(False, persist=False)
        else:
            if self._detail_revealer:
                self._detail_revealer.show()
                self._detail_revealer.set_reveal_child(True)
            self._status_revealer.show()
            self._status_revealer.set_reveal_child(self._status_visible_before_max)
            if self._sidebar_visible_before_max:
                self._set_sidebar_visible(True, persist=False)
        self._update_terminal_toggle_ui()
        self._update_sidebar_toggle_ui()
        if not initial:
            self._save_ui_state()

    def _on_detail_revealer_revealed(self, revealer: Gtk.Revealer, _pspec: GObject.ParamSpec) -> None:
        if revealer.get_child_revealed():
            revealer.show()
        else:
            revealer.hide()

    def _on_status_revealer_revealed(self, revealer: Gtk.Revealer, _pspec: GObject.ParamSpec) -> None:
        if revealer.get_child_revealed():
            revealer.show()
        else:
            revealer.hide()

    def _update_bulk_bar(self) -> None:
        count = len(self._selected_hosts())
        self._bulk_revealer.set_reveal_child(count >= 2)

    def _bulk_connect_selected(self) -> None:
        hosts = self._selected_hosts()
        if len(hosts) < 2:
            return
        for host in hosts:
            self._open_hosts([host])

    def _bulk_move_selected(self) -> None:
        hosts = self._selected_hosts()
        if len(hosts) < 2:
            return
        picker = GroupPickerDialog(
            self,
            self.manager,
            "Move selected hosts",
            allow_new_child=True,
        )
        response = picker.run()
        selection = picker.selected() if response == Gtk.ResponseType.OK else None
        picker.destroy()
        if not selection:
            return
        target_parent, target_child, create_child = selection
        key_list = [(h.parent_group, h.child_group, h.alias) for h in hosts]
        ok, msg = self.manager.move_hosts(key_list, target_parent, target_child, create_child=create_child)
        self._log_event(msg, ok)
        if ok:
            for parent, child, alias in key_list:
                self._rebind_credential_key(
                    f"{parent}::{child}::{alias}",
                    f"{target_parent}::{target_child}::{alias}",
                )
            self._rebuild_tree(refresh=True)

    def _bulk_tag_selected(self) -> None:
        hosts = self._selected_hosts()
        if len(hosts) < 2:
            return
        dialog = Gtk.Dialog(title="Tag/Color Selected Hosts", transient_for=self, modal=True)
        dialog.add_button("_Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("_Apply", Gtk.ResponseType.OK)
        box = dialog.get_content_area()
        box.set_border_width(12)
        box.set_spacing(8)

        color_entry = Gtk.Entry()
        color_entry.set_placeholder_text("Color (e.g., #ff0000 or leave empty)")
        box.pack_start(Gtk.Label(label="Color"), False, False, 0)
        box.pack_start(color_entry, False, False, 0)

        tag_entry = Gtk.Entry()
        tag_entry.set_placeholder_text("Tag to add (comma-separated accepted)")
        box.pack_start(Gtk.Label(label="Tag"), False, False, 0)
        box.pack_start(tag_entry, False, False, 0)

        dialog.show_all()
        response = dialog.run()
        color_val = color_entry.get_text().strip()
        tag_val = tag_entry.get_text().strip()
        dialog.destroy()
        if response != Gtk.ResponseType.OK:
            return

        if color_val:
            for h in hosts:
                self._set_host_color(h.parent_group, h.child_group, h.alias, color_val)

        # Apply tags to each host options (append if necessary) and persist
        if tag_val:
            for h in hosts:
                tags = [t.strip() for t in (host_option(h, "tags") or "").split(",") if t.strip()]
                new_tags = [t for t in tags]
                for part in tag_val.split(","):
                    t = part.strip()
                    if t and t not in new_tags:
                        new_tags.append(t)
                h.options["tags"] = ",".join(new_tags)
                ok, msg = self.manager.update_host(h.parent_group, h.child_group, h.alias, h)
                if not ok:
                    self._log_event(msg, False)

        self._rebuild_tree(refresh=True)
        tree_view = getattr(self, "tree_view", None)
        if tree_view:
            tree_view.queue_draw()

    # ------------------------------------------------------------- tree helpers
    def _render_icon(self, _column, cell: Gtk.CellRendererPixbuf, model, iter_, _data) -> None:
        icon_name = model.get_value(iter_, self.COLUMN_ICON)
        color = model.get_value(iter_, self.COLUMN_COLOR)
        cell.set_property("visible", True)
        if icon_name:
            pix = self._tree_icon_pixbuf(icon_name, color)
            if pix:
                cell.set_property("pixbuf", pix)
                cell.set_property("icon-name", None)
                return
        cell.set_property("pixbuf", None)
        cell.set_property("icon-name", icon_name)

    def _render_text(self, _column, cell: Gtk.CellRendererText, model, iter_, _data) -> None:
        cell.set_property("text", model.get_value(iter_, self.COLUMN_TEXT))
        status_color = model.get_value(iter_, self.COLUMN_STATUS_COLOR)
        if status_color:
            cell.set_property("foreground", status_color)
            cell.set_property("foreground-set", True)
        else:
            cell.set_property("foreground-set", False)

    def _tree_icon_pixbuf(self, icon_name: str, color: str = "") -> Optional[GdkPixbuf.Pixbuf]:
        icon_color = color or self._default_tree_icon_color(icon_name)
        cache_key = f"icon::{icon_name}::{icon_color}"
        cached = self._color_pixbuf_cache.get(cache_key)
        if cached:
            return cached
        rgba = Gdk.RGBA()
        if not rgba.parse(icon_color):
            return None
        pix = self._draw_colored_tree_icon_pixbuf(icon_name, rgba)
        if pix and self._pixbuf_has_visible_pixels(pix):
            self._color_pixbuf_cache[cache_key] = pix
            return pix
        return None

    def _default_tree_icon_color(self, icon_name: str) -> str:
        if icon_name == self.ICON_GROUP:
            return "#f59e0b"
        if icon_name == self.ICON_SUBGROUP:
            return "#38bdf8"
        if icon_name == self.ICON_HOST:
            return "#94a3b8"
        return "#9ca3af"

    def _draw_colored_tree_icon_pixbuf(self, icon_name: str, rgba: Gdk.RGBA) -> GdkPixbuf.Pixbuf:
        size = 18
        rowstride = size * 4
        pixels = bytearray(rowstride * size)
        r = int(rgba.red * 255 + 0.5)
        g = int(rgba.green * 255 + 0.5)
        b = int(rgba.blue * 255 + 0.5)
        a = int(rgba.alpha * 255 + 0.5)
        fill = (r, g, b, a)
        outline = (*self._scale_rgb(r, g, b, 0.58), a)
        light = (*self._blend_rgb((r, g, b), (255, 255, 255), 0.28), a)
        detail = (*self._blend_rgb((r, g, b), (255, 255, 255), 0.7), min(230, a))
        if icon_name == self.ICON_GROUP:
            self._paint_rect(pixels, size, 2, 5, 14, 15, outline)
            self._paint_rect(pixels, size, 3, 6, 13, 14, fill)
            self._paint_rect(pixels, size, 5, 3, 16, 13, outline)
            self._paint_rect(pixels, size, 6, 4, 15, 12, light)
            self._paint_rect(pixels, size, 7, 6, 14, 7, detail)
            self._paint_rect(pixels, size, 7, 9, 14, 10, detail)
        elif icon_name == self.ICON_SUBGROUP:
            self._paint_rect(pixels, size, 3, 3, 10, 7, outline)
            self._paint_rect(pixels, size, 4, 4, 10, 6, light)
            self._paint_rect(pixels, size, 1, 6, 17, 16, outline)
            self._paint_rect(pixels, size, 2, 7, 16, 15, fill)
            self._paint_rect(pixels, size, 3, 8, 15, 9, light)
            self._paint_rect(pixels, size, 12, 11, 14, 13, detail)
        elif icon_name == self.ICON_HOST:
            self._paint_rect(pixels, size, 2, 3, 16, 16, outline)
            self._paint_rect(pixels, size, 3, 4, 15, 15, fill)
            self._paint_rect(pixels, size, 4, 6, 14, 7, detail)
            self._paint_rect(pixels, size, 4, 10, 14, 11, detail)
            self._paint_rect(pixels, size, 12, 13, 14, 14, detail)
        else:
            self._paint_rect(pixels, size, 3, 3, 15, 15, outline)
            self._paint_rect(pixels, size, 4, 4, 14, 14, fill)
        data = GLib.Bytes.new(bytes(pixels))
        return GdkPixbuf.Pixbuf.new_from_bytes(
            data,
            GdkPixbuf.Colorspace.RGB,
            True,
            8,
            size,
            size,
            rowstride,
        )

    def _paint_rect(
        self,
        pixels: bytearray,
        size: int,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        color: Tuple[int, int, int, int],
    ) -> None:
        r, g, b, a = color
        rowstride = size * 4
        for y in range(max(0, y1), min(size, y2)):
            row = y * rowstride
            for x in range(max(0, x1), min(size, x2)):
                idx = row + x * 4
                pixels[idx] = r
                pixels[idx + 1] = g
                pixels[idx + 2] = b
                pixels[idx + 3] = a

    def _scale_rgb(self, r: int, g: int, b: int, factor: float) -> Tuple[int, int, int]:
        return (
            max(0, min(255, int(r * factor + 0.5))),
            max(0, min(255, int(g * factor + 0.5))),
            max(0, min(255, int(b * factor + 0.5))),
        )

    def _blend_rgb(
        self,
        source: Tuple[int, int, int],
        target: Tuple[int, int, int],
        amount: float,
    ) -> Tuple[int, int, int]:
        return (
            max(0, min(255, int(source[0] + (target[0] - source[0]) * amount + 0.5))),
            max(0, min(255, int(source[1] + (target[1] - source[1]) * amount + 0.5))),
            max(0, min(255, int(source[2] + (target[2] - source[2]) * amount + 0.5))),
        )

    def _pixbuf_has_visible_pixels(self, pixbuf: GdkPixbuf.Pixbuf) -> bool:
        channels = pixbuf.get_n_channels()
        if channels < 3:
            return False
        if not pixbuf.get_has_alpha() or channels < 4:
            return True
        width = pixbuf.get_width()
        height = pixbuf.get_height()
        rowstride = pixbuf.get_rowstride()
        pixels = bytes(pixbuf.get_pixels())
        for y in range(height):
            row = y * rowstride
            for x in range(width):
                idx = row + (x * channels) + 3
                if idx < len(pixels) and pixels[idx] > 0:
                    return True
        return False

    def _group_color_for(self, parent: str, child: str) -> str:
        key = parent if not child else f"{parent}/{child}"
        return self._group_colors.get(key, "")

    def _resolved_group_color_for(self, parent: str, child: str) -> str:
        if child:
            color = self._group_color_for(parent, child)
            if color:
                return color
        return self._group_color_for(parent, "")

    def _set_group_color(self, parent: str, child: str, color: Optional[str]) -> None:
        key = parent if not child else f"{parent}/{child}"
        if color:
            self._group_colors[key] = color
        else:
            self._group_colors.pop(key, None)
        self._save_ui_state()

    def _host_color_for(self, parent: str, child: str, alias: str) -> str:
        return self._host_colors.get(self._make_host_key(parent, child, alias), "")

    def _set_host_color(self, parent: str, child: str, alias: str, color: Optional[str]) -> None:
        key = self._make_host_key(parent, child, alias)
        if color:
            self._host_colors[key] = color
        else:
            self._host_colors.pop(key, None)
        self._save_ui_state()

    def _item_color_for(self, parent: str, child: str, alias: str = "") -> str:
        if alias:
            return self._host_color_for(parent, child, alias) or self._resolved_group_color_for(parent, child)
        return self._resolved_group_color_for(parent, child)

    def _rgba_to_hex(self, rgba: Gdk.RGBA) -> str:
        r = int(rgba.red * 255 + 0.5)
        g = int(rgba.green * 255 + 0.5)
        b = int(rgba.blue * 255 + 0.5)
        return f"#{r:02x}{g:02x}{b:02x}"

    def _rebuild_tree(self, *, refresh: bool = False) -> None:
        if refresh:
            self.manager.refresh()
        query = self._search_entry.get_text().strip().lower()
        filter_query = self._filter_query.strip()
        filter_terms = self._parse_filter(filter_query) if filter_query else None
        if filter_query and filter_terms is None:
            self._filter_error_label.set_text(self._filter_error or "Invalid filter.")
            self._filter_error_label.show()
            filter_terms = None
        else:
            self._filter_error_label.hide()

        self.tree_store.clear()
        self._host_lookup.clear()
        self._host_iters = {}
        self._read_only_keys = set()

        def walk_inventory(tree: Dict[str, Dict[str, GroupConfig]], shared: bool = False) -> None:
            for parent_name, children in sorted(tree.items(), key=lambda kv: kv[0].lower()):
                label_parent = f"[Shared] {parent_name}" if shared else parent_name
                parent_iter: Optional[Gtk.TreeIter] = None
                parent_matches = not query or query in label_parent.lower()
                folder_cache: Dict[Tuple[str, str], Gtk.TreeIter] = {}

                def ensure_parent_iter() -> Gtk.TreeIter:
                    nonlocal parent_iter
                    if parent_iter is None:
                        color = self._item_color_for(label_parent, "")
                        parent_iter = self.tree_store.append(
                            None,
                            [
                                self.ICON_GROUP,
                                label_parent,
                                "parent",
                                label_parent,
                                "",
                                "",
                                None,
                                label_parent.lower(),
                                color,
                                "",
                            ],
                        )
                    return parent_iter

                for child_name, group in sorted(children.items(), key=lambda kv: kv[0].lower()):
                    child_matches = parent_matches or (query and query in child_name.lower())
                    include_all_hosts = child_matches and bool(query)

                    segments = child_name.split("/")
                    current_iter = None
                    if child_matches or include_all_hosts or not query:
                        current_iter = ensure_parent_iter()
                        path_parts: List[str] = []
                        folder_iter = current_iter
                        for segment in segments[:-1]:
                            path_parts.append(segment)
                            folder_path = "/".join(path_parts)
                            cache_key = (label_parent, folder_path)
                            folder_iter = folder_cache.get(cache_key)
                            if folder_iter is None:
                                color = self._item_color_for(label_parent, folder_path)
                                folder_iter = self.tree_store.append(
                                    current_iter,
                                    [
                                        self.ICON_SUBGROUP,
                                        segment,
                                        "child-folder",
                                        label_parent,
                                        folder_path,
                                        "",
                                        None,
                                        f"{label_parent.lower()} {folder_path.lower()}",
                                        color,
                                        "",
                                    ],
                                )
                                folder_cache[cache_key] = folder_iter
                        current_iter = folder_iter

                    child_iter: Optional[Gtk.TreeIter] = None
                    if current_iter is not None:
                        color = self._item_color_for(label_parent, child_name)
                        child_iter = self.tree_store.append(
                            current_iter,
                            [
                                self.ICON_SUBGROUP,
                                segments[-1],
                                "child",
                                label_parent,
                                child_name,
                                "",
                                None,
                                f"{label_parent.lower()} {child_name.lower()}",
                                color,
                                "",
                            ],
                        )

                    for host in sorted(group.hosts, key=lambda h: h.alias.lower()):
                        if query and not (include_all_hosts or self._host_matches(host, query)):
                            continue
                        if filter_terms and not self._filter_host(host, filter_terms):
                            continue
                        child_iter = child_iter or ensure_parent_iter()
                        if current_iter is not None and child_iter is None:
                            color = self._item_color_for(label_parent, child_name)
                            child_iter = self.tree_store.append(
                                current_iter,
                                [
                                    self.ICON_SUBGROUP,
                                    segments[-1],
                                    "child",
                                    label_parent,
                                    child_name,
                                    "",
                                    None,
                                    f"{label_parent.lower()} {child_name.lower()}",
                                    color,
                                    "",
                                ],
                            )
                        icon = self.ICON_HOST
                        display = host.alias
                        key = self._make_host_key(label_parent, child_name, host.alias)
                        state = self._host_status.get(key, "unknown")
                        if key not in self._host_status:
                            self._host_status[key] = state
                        color_value = self._item_color_for(label_parent, child_name, host.alias)
                        status_color = "" if state == "unknown" else self._status_color(state)
                        host_iter = self.tree_store.append(
                            child_iter,
                            [
                                icon,
                                display,
                                "host",
                                label_parent,
                                child_name,
                                host.alias,
                                host,
                                self._search_text_for_host(host),
                                color_value,
                                status_color,
                            ],
                        )
                        self._host_lookup[key] = host
                        self._host_iters[key] = host_iter
                        if shared:
                            self._read_only_keys.add(key)

                if parent_iter is None and parent_matches and not query:
                    ensure_parent_iter()

        walk_inventory(self.manager.tree, shared=False)
        if self._shared_manager:
            walk_inventory(self._shared_manager.tree, shared=True)

        self.tree_view.expand_all()
        self._update_details()
        self._update_toolbar_state()

    def _host_matches(self, host: HostEntry, query: str) -> bool:
        return query in self._search_text_for_host(host)

    def _parse_filter(self, text: str) -> Optional[List[Tuple[str, str]]]:
        terms: List[Tuple[str, str]] = []
        if not text.strip():
            return terms
        self._filter_error = ""
        chunks = [part.strip() for part in text.split("AND")]
        for chunk in chunks:
            if not chunk:
                continue
            if ":" not in chunk:
                self._filter_error = f"Invalid term '{chunk}'. Use key:value."
                return None
            key, value = chunk.split(":", 1)
            key = key.strip().lower()
            value = value.strip().lower()
            if not key or not value:
                self._filter_error = f"Invalid term '{chunk}'."
                return None
            terms.append((key, value))
        return terms

    def _filter_host(self, host: HostEntry, terms: List[Tuple[str, str]]) -> bool:
        for key, val in terms:
            matched = False
            if key in {"tag", "tags"}:
                tags = [t.strip().lower() for t in (host_option(host, "tags") or "").split(",") if t.strip()]
                matched = val in tags
            elif key == "environment":
                matched = host.metadata.get("environment", "").lower() == val
            elif key == "owner":
                matched = host.metadata.get("owner", "").lower() == val
            elif key == "service":
                matched = host.metadata.get("service", "").lower() == val
            else:
                matched = (
                    (host_option(host, key) or "").lower() == val
                    or host.metadata.get(key, "").lower() == val
                )
            if not matched:
                return False
        return True

    def _host_has_plaintext_password(self, host: HostEntry) -> bool:
        for key, value in host.options.items():
            if value and "password" in key.lower():
                return True
        return False

    def _confirm_policy_override(self, message: str) -> bool:
        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.OK_CANCEL,
            text=message,
        )
        dialog.set_default_response(Gtk.ResponseType.CANCEL)
        response = dialog.run()
        dialog.destroy()
        return response == Gtk.ResponseType.OK

    def _check_secrets_policy(self, host: HostEntry) -> bool:
        policy = (
            self._settings.secrets_policy_with_defaults()
            if hasattr(self._settings, "secrets_policy_with_defaults")
            else {
                "allow_plaintext_host_passwords": True,
                "warn_on_plaintext_host_passwords": True,
                "prefer_vault_for_passwords": True,
            }
        )
        if not self._host_has_plaintext_password(host):
            return True
        if host.metadata.get("vault_id"):
            return True
        allow_plain = bool(policy.get("allow_plaintext_host_passwords", True))
        warn_plain = bool(policy.get("warn_on_plaintext_host_passwords", True))
        prefer_vault = bool(policy.get("prefer_vault_for_passwords", True))

        if allow_plain:
            if warn_plain:
                message = (
                    "This host stores a password in plain text. Consider using the credential vault instead."
                    if prefer_vault
                    else "This host stores a password in plain text."
                )
                self._notify_warning(message)
            return True

        message = (
            "Plain-text passwords are disallowed by the current secrets policy. "
            "Use the credential vault instead or confirm to bypass."
        )
        if self._confirm_policy_override(message):
            return True
        self._notify_warning("Host save cancelled by secrets policy.")
        return False

    def _search_text_for_host(self, host: HostEntry) -> str:
        display_user = self._display_username_for_host(host).lower()
        parts = [
            host.alias.lower(),
            (host_option(host, "hostname") or "").lower(),
            display_user,
            (host_option(host, "tags") or "").lower(),
            (host_option(host, "proxyjump") or "").lower(),
        ]
        return " ".join(filter(None, parts))

    def _refresh_smart_group_list(self) -> None:
        if not hasattr(self, "_smart_group_list") or self._smart_group_list is None:
            return
        for row in list(self._smart_group_list.get_children()):
            self._smart_group_list.remove(row)
        for grp in self._smart_groups:
            row = Gtk.ListBoxRow()
            label = Gtk.Label(label=grp.name)
            label.set_xalign(0.0)
            row.add(label)
            row.show_all()
            self._smart_group_list.add(row)
        self._smart_group_list.show_all()

    def _on_smart_group_activated(self, listbox: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
        idx = row.get_index()
        if idx < 0 or idx >= len(self._smart_groups):
            return
        grp = self._smart_groups[idx]
        self._filter_entry.set_text(grp.query)
        self._filter_query = grp.query
        self._rebuild_tree(refresh=False)

    def _save_current_filter_as_group(self) -> None:
        if not self._filter_query:
            self._filter_error_label.set_text("Enter a filter to save.")
            self._filter_error_label.show()
            return
        name = self._prompt_text("Save Smart Group", "Name")
        if not name:
            return
        self._smart_groups.append(SmartGroup(name=name, query=self._filter_query))
        if not save_smart_groups(self._smart_groups):
            self._smart_groups.pop()
            self._filter_error_label.set_text("Could not save smart group.")
            self._filter_error_label.show()
            return
        self._refresh_smart_group_list()

    def _delete_selected_smart_group(self) -> None:
        sel = self._smart_group_list.get_selected_row()
        if not sel:
            return
        idx = sel.get_index()
        if idx < 0 or idx >= len(self._smart_groups):
            return
        removed = self._smart_groups.pop(idx)
        if not save_smart_groups(self._smart_groups):
            self._smart_groups.insert(idx, removed)
            self._filter_error_label.set_text("Could not delete smart group.")
            self._filter_error_label.show()
            return
        self._refresh_smart_group_list()

    def _get_selected_iter(self) -> Optional[Gtk.TreeIter]:
        selection = self.tree_view.get_selection()
        model_rows, paths = selection.get_selected_rows()
        if model_rows and paths:
            first_path = paths[0]
            return model_rows.get_iter(first_path)
        return None

    def _coerce_host(self, obj) -> Optional[HostEntry]:
        if isinstance(obj, HostEntry):
            return obj
        if isinstance(obj, (list, tuple)):
            for item in obj:
                if isinstance(item, HostEntry):
                    return item
        return None

    def _current_item(self) -> Tuple[str, Dict[str, str]]:
        tree_iter = self._get_selected_iter()
        if not tree_iter:
            return "", {}
        model = self.tree_store
        kind = model.get_value(tree_iter, self.COLUMN_KIND)
        data = {
            "parent": model.get_value(tree_iter, self.COLUMN_PARENT),
            "child": model.get_value(tree_iter, self.COLUMN_CHILD),
            "alias": model.get_value(tree_iter, self.COLUMN_ALIAS),
        }
        return kind, data

    def _current_item_is_read_only(self) -> bool:
        kind, data = self._current_item()
        if kind != "host":
            return False
        key = self._make_host_key(data.get("parent", ""), data.get("child", ""), data.get("alias", ""))
        if key in self._read_only_keys:
            return True
        host = self._host_lookup.get(key)
        if host:
            perms = self._permissions_for_host(host)
            return not perms.get("can_edit", True)
        return False

    def _selected_hosts(self) -> List[HostEntry]:
        selection = self.tree_view.get_selection()
        model, paths = selection.get_selected_rows()
        hosts: List[HostEntry] = []
        if not model:
            return hosts
        for path in paths:
            tree_iter = model.get_iter(path)
            host = model.get_value(tree_iter, self.COLUMN_HOST)
            coerced = self._coerce_host(host)
            if coerced:
                hosts.append(coerced)
        return hosts

    def _selected_host(self) -> Optional[HostEntry]:
        tree_iter = self._get_selected_iter()
        if not tree_iter:
            return None
        host_obj = self.tree_store.get_value(tree_iter, self.COLUMN_HOST)
        return self._coerce_host(host_obj)

    def _permissions_for_host(self, host: HostEntry) -> Dict[str, bool]:
        perms = host.metadata.get("permissions")
        if isinstance(perms, dict):
            return {
                "can_view": bool(perms.get("can_view", True)),
                "can_connect": bool(perms.get("can_connect", True)),
                "can_edit": bool(perms.get("can_edit", True)),
            }
        return {"can_view": True, "can_connect": True, "can_edit": True}

    def _host_is_read_only(self, host: HostEntry) -> bool:
        key = self._make_host_key(host.parent_group, host.child_group, host.alias)
        perms = self._permissions_for_host(host)
        return key in self._read_only_keys or not perms.get("can_edit", True)

    def _render_permissions(self, host: HostEntry) -> str:
        perms = self._permissions_for_host(host)
        parts = []
        if not perms.get("can_view", True):
            parts.append("view locked")
        if not perms.get("can_connect", True):
            parts.append("connect locked")
        if not perms.get("can_edit", True):
            parts.append("edit locked")
        return ", ".join(parts)

    def _host_key(self, host: HostEntry) -> str:
        return host_config_key(host)

    def _bind_credential_for_key(self, host_key: str, cred_id: Optional[str]) -> bool:
        try:
            self._credential_store.bind_host(host_key, cred_id)
            return True
        except SecureStoreError as exc:
            self._log_event("Could not save credential binding. See logs for details.", False)
            LOGGER.warning("credential bind failed: %s", exc)
            return False

    def _rebind_credential_key(self, old_key: str, new_key: str) -> bool:
        try:
            self._credential_store.rebind_host(old_key, new_key)
            return True
        except SecureStoreError as exc:
            self._log_event("Could not update credential binding. See logs for details.", False)
            LOGGER.warning("credential rebind failed: %s", exc)
            return False

    def _remap_credential_prefix(self, old_prefix: str, new_prefix: str) -> bool:
        try:
            self._credential_store.remap_bindings_prefix(old_prefix, new_prefix)
            return True
        except SecureStoreError as exc:
            self._log_event("Could not update credential bindings. See logs for details.", False)
            LOGGER.warning("credential prefix remap failed: %s", exc)
            return False

    def _unbind_credential_prefix(self, prefix: str) -> bool:
        try:
            self._credential_store.unbind_prefix(prefix)
            return True
        except SecureStoreError as exc:
            self._log_event("Could not remove credential bindings. See logs for details.", False)
            LOGGER.warning("credential unbind failed: %s", exc)
            return False

    def _credential_for_host(self, host: HostEntry) -> Optional[Credential]:
        return self._credential_store.credential_for_host(self._host_key(host))

    def _vault_entry_for_host(self, host: HostEntry) -> Optional[VaultEntry]:
        vault_id = host.metadata.get("vault_id")
        if not vault_id or not getattr(self, "_vault", None) or not self._vault.unlocked:
            return None
        return self._vault.get_entry(vault_id)

    def _profile_for_host(self, host: HostEntry) -> Optional[SessionProfile]:
        pid = host.metadata.get("default_session_profile_id")
        if not pid:
            return None
        return self._profiles.get(pid)

    def _host_connection_params(self, host: HostEntry) -> Tuple[str, int, str]:
        return host_connection_params(host)

    def _seed_password_for_host(self, host: HostEntry, password: str) -> None:
        hostname, port, user = self._host_connection_params(host)
        if not hostname:
            return
        try:
            self._secure_store.set(hostname, port, user, password)
        except SecureStoreError:
            pass

    def _save_detected_password_change(self, host: HostEntry, password: str) -> bool:
        """Persist a password changed inside an active terminal session."""

        resolved = self._host_with_resolved_identity(host)
        hostname, port, user = self._host_connection_params(resolved)
        if not hostname:
            return False

        saved_any = False
        required_sources_ok = True
        try:
            self._secure_store.set(hostname, port, user, password)
            saved_any = True
        except SecureStoreError as exc:
            LOGGER.warning("per-host password update failed: %s", exc)

        cred_id = self._credential_store.binding_for_host(self._host_key(host))
        if cred_id and self._credential_store.get(cred_id):
            required_sources_ok = False
            try:
                self._credential_store.set_password(cred_id, password)
                saved_any = True
                required_sources_ok = True
            except SecureStoreError as exc:
                LOGGER.warning("credential password update failed: %s", exc)

        vault_entry = self._vault_entry_for_host(host)
        if vault_entry:
            required_sources_ok = False
            try:
                self._vault.update_entry(
                    VaultEntry(
                        vault_entry.id,
                        vault_entry.label,
                        vault_entry.username,
                        password,
                    )
                )
                saved_any = True
                required_sources_ok = True
            except VaultError as exc:
                LOGGER.warning("vault password update failed: %s", exc)

        return saved_any and required_sources_ok

    def _host_with_resolved_identity(self, host: HostEntry) -> HostEntry:
        cred = self._credential_for_host(host)
        vault_entry = self._vault_entry_for_host(host)
        if not cred and not vault_entry:
            return host

        if vault_entry:
            return with_user(host, vault_entry.username)
        if cred:
            return with_user(host, cred.username)
        return host

    def _credential_password_for_host(self, host: HostEntry) -> Optional[str]:
        cred = self._credential_for_host(host)
        vault_entry = self._vault_entry_for_host(host)
        if vault_entry:
            return vault_entry.password
        if cred:
            try:
                return self._credential_store.get_password(cred.id)
            except SecureStoreError as exc:
                self._log_event(f"Credential '{cred.name}' is unavailable: {exc}", False)
        return None

    def _resolve_host_credentials(self, host: HostEntry) -> HostEntry:
        resolved = self._host_with_resolved_identity(host)
        password = self._credential_password_for_host(host)
        if password:
            self._seed_password_for_host(resolved, password)
        return resolved

    def _display_username_for_host(self, host: HostEntry) -> str:
        cred = self._credential_for_host(host)
        if cred:
            return cred.username
        vault_entry = self._vault_entry_for_host(host)
        if vault_entry:
            return vault_entry.username
        return host_option(host, "user") or ""

    def _status_color(self, state: str) -> str:
        return {
            "connected": "#22c55e",
            "failed": "#ef4444",
            "unknown": "#9ca3af",
        }.get(state, "#9ca3af")

    def _set_host_status_for_key(self, key: str, state: str) -> None:
        state = state if state in {"connected", "failed", "unknown"} else "unknown"
        self._host_status[key] = state
        color = "" if state == "unknown" else self._status_color(state)
        tree_iter = self._host_iters.get(key)
        if tree_iter:
            try:
                self.tree_store.set_value(tree_iter, self.COLUMN_STATUS_COLOR, color)
            except Exception:
                pass

    def _set_host_status(self, host: HostEntry, state: str) -> None:
        self._set_host_status_for_key(self._host_key(host), state)

    def _bind_tab_status(self, tab, host: HostEntry) -> None:
        host_key = self._host_key(host)
        notified = {"connected": False}

        def on_status(_tab, _alias, connected: bool) -> None:
            self._set_host_status_for_key(host_key, "connected" if connected else "unknown")
            if connected and not notified["connected"]:
                notified["connected"] = True
                self._notify_connection_success(host)

        def on_closed(_tab, _alias, exited_normally: bool) -> None:
            self._set_host_status_for_key(host_key, "unknown" if exited_normally else "failed")

        def on_destroy(*_args) -> None:
            if self._host_status.get(host_key) != "failed":
                self._set_host_status_for_key(host_key, "unknown")

        try:
            tab.connect("status-changed", on_status)
            tab.connect("session-closed", on_closed)
            tab.connect("destroy", on_destroy)
        except Exception:
            pass

    def focus_search(self) -> None:
        self.show_search_panel()

    def _open_hosts(self, hosts: List[HostEntry], source: Optional[str] = None) -> None:
        if not hosts:
            if source:
                self._log_event(f"No hosts found in {source}.", False)
            else:
                self._log_event("No hosts to connect.", False)
            return

        opened: List[str] = []
        for host in hosts:
            host_obj = self._coerce_host(host)
            if not host_obj:
                self._log_event("Selected item is not a host; skipping.", False)
                continue
            self._audit_log("connect", host_obj)
            perms = self._permissions_for_host(host_obj)
            if not perms.get("can_connect", True):
                dialog = Gtk.MessageDialog(
                    transient_for=self,
                    modal=True,
                    buttons=Gtk.ButtonsType.OK_CANCEL,
                    message_type=Gtk.MessageType.QUESTION,
                    text="Connection is locally disabled for this host. Connect anyway?",
                )
                response = dialog.run()
                dialog.destroy()
                if response != Gtk.ResponseType.OK:
                    continue
            resolved = self._resolve_host_credentials(host_obj)
            if not isinstance(resolved, HostEntry):
                self._log_event("Unable to resolve host entry; skipping.", False)
                continue
            profile = self._profile_for_host(resolved)
            if self._settings.use_paramiko_vte:
                self._open_host_paramiko(resolved, profile=profile)
            else:
                self._log_event(f"Opening {resolved.alias}", True)
                tab = self.terminal_tabs.new_terminal(resolved, profile=profile)
                self._bind_tab_status(tab, resolved)
            opened.append(resolved.alias)

        if not opened:
            return
        if len(opened) == 1:
            self._log_event(f"Opened {opened[0]}.", True)
        else:
            suffix = f" from {source}" if source else ""
            self._log_event(f"Opened {len(opened)} connections{suffix}.", True)

    def _open_host_paramiko(self, host: HostEntry, profile: Optional[SessionProfile] = None) -> None:
        tab = self.terminal_tabs.new_terminal(host, autostart=False, profile=profile)
        self._bind_tab_status(tab, host)
        host_cfg = self._build_host_config(host)
        state_machine = ConnectionStateMachine()
        connector = Connector(
            self._password_dialog,
            self._secure_store,
            self._settings,
            state_machine,
        )
        self._paramiko_connectors[tab] = connector
        connector.add_connect_success_callback(lambda session, tab=tab: self._on_paramiko_success(tab, session))
        connector.add_aborted_callback(lambda reason, tab=tab: self._on_paramiko_aborted(tab, reason))
        connector.add_error_callback(lambda message, tab=tab: self._notify_error(message))
        connector.add_warning_callback(lambda message, tab=tab: self._notify_warning(message))
        tab.connect("destroy", lambda *_: self._cleanup_paramiko_connector(tab))
        connector.connect_to_host(host_cfg)

    def _cleanup_paramiko_connector(self, tab) -> None:
        self._paramiko_connectors.pop(tab, None)

    def _on_paramiko_success(self, tab, session) -> None:
        self._cleanup_paramiko_connector(tab)
        if not tab.get_parent():
            try:
                session.close()
            except Exception:
                pass
            return
        tab.attach_session(session)
        try:
            self._set_host_status(tab.host, "connected")
        except Exception:
            pass

    def _on_paramiko_aborted(self, tab, reason: str) -> None:
        self._cleanup_paramiko_connector(tab)
        self._log_event(reason or "Authentication cancelled.", False)
        try:
            self._set_host_status(tab.host, "failed")
        except Exception:
            pass
        self.terminal_tabs.close_tab(tab)

    def _build_host_config(self, host: HostEntry) -> UIHostConfig:
        hostname, port, username = self._host_connection_params(host)
        return UIHostConfig(
            host=hostname,
            port=port,
            username=username,
            timeout=self._settings.connect_timeout,
            remember_password_default=self._settings.remember_password_default,
            parent_window=self,
            options=dict(host.options),
        )

    def _notify_info(self, message: str) -> None:
        self._log_event(message, True)

    def _notify_connection_success(self, host: HostEntry) -> None:
        message = f"Connected to {host.alias}."
        app = self.get_application()
        if app and hasattr(app, "send_notification"):
            notification = Gio.Notification.new("SSH connected")
            notification.set_body(message)
            app.send_notification(f"ssh-manager-connected-{self._host_key(host)}", notification)
            return
        self._log_event(message, True)

    def _notify_warning(self, message: str) -> None:
        self._log_event(message, False)

    def _notify_error(self, message: str) -> None:
        self._log_event(message, False)

    def _audit_log(self, action: str, host: HostEntry, *, extra: Optional[Dict[str, str]] = None) -> None:
        if not getattr(self._settings, "enable_audit_logging", False):
            return
        path_text = getattr(self._settings, "audit_log_path", "")
        if not path_text:
            return
        timestamp = datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z"
        host_display = audit_log_value(host.alias or host_option(host, "hostname") or "", default="unknown")
        username = audit_log_value(host_option(host, "user") or "")
        fields: List[str] = [f"{timestamp} [{action}] host={host_display}"]
        if username:
            fields.append(f"user={username}")
        if extra:
            for k, v in extra.items():
                if v:
                    fields.append(f"{audit_log_key(k)}={audit_log_value(v)}")
        line = " ".join(fields) + "\n"

        def _write_line() -> None:
            try:
                path = Path(path_text).expanduser()
                with self._audit_lock:
                    append_text(path, line, mode=0o600)
            except Exception:
                pass

        threading.Thread(target=_write_line, daemon=True).start()

    def open_selected_host(self) -> None:
        hosts = self._selected_hosts()
        if not hosts:
            host = self._selected_host()
            if not host:
                self._log_event("Select a host to connect.", False)
                return
            hosts = [host]
        self._open_hosts(hosts)

    def _collect_hosts_for_child_group(self, parent: str, child: str) -> List[HostEntry]:
        groups = self.manager.tree.get(parent, {})
        group = groups.get(child)
        if group:
            return list(group.hosts)
        prefix = f"{child}/" if child else ""
        hosts: List[HostEntry] = []
        if prefix:
            for name, grp in groups.items():
                if name.startswith(prefix):
                    hosts.extend(grp.hosts)
        return hosts

    def _collect_hosts_for_parent_group(self, parent: str) -> List[HostEntry]:
        groups = self.manager.tree.get(parent, {})
        hosts: List[HostEntry] = []
        for group in groups.values():
            hosts.extend(group.hosts)
        return list(hosts)

    def _prompt_child_destination(self, parent: str) -> Optional[Tuple[str, bool]]:
        children = sorted(self.manager.tree.get(parent, {}).keys(), key=lambda name: name.lower())
        if children:
            listing = ", ".join(children)
            message = f"Child filename under {parent} (existing: {listing})"
            default = children[0]
        else:
            message = f"Create a child filename under {parent}"
            default = ""
        name = self._prompt_text("Move to child group", message, default)
        if not name:
            return None
        child = self._normalize_group_filename(name)
        if not child:
            return None
        create_child = child not in children
        return child, create_child

    # --------------------------------------------------------- sidebar handlers
    def _on_search_changed(self, _entry: Gtk.SearchEntry) -> None:
        self._rebuild_tree(refresh=False)

    def _on_search_key_press(self, _entry: Gtk.SearchEntry, event: Gdk.EventKey) -> bool:
        if event.keyval == Gdk.KEY_Escape:
            self._search_escape_time = time.monotonic()
            self.clear_focus()
            self._search_panel_escape_ready = True
            return True
        self._search_panel_escape_ready = False
        return False

    def _on_filter_changed(self, entry: Gtk.Entry) -> None:
        self._filter_query = entry.get_text().strip()
        self._rebuild_tree(refresh=False)

    def _on_filter_key_press(self, _entry: Gtk.Entry, event: Gdk.EventKey) -> bool:
        if event.keyval == Gdk.KEY_Escape:
            self.clear_focus()
            self._search_panel_escape_ready = True
            return True
        self._search_panel_escape_ready = False
        return False

    def _on_tree_key_press(self, _widget: Gtk.TreeView, event: Gdk.EventKey) -> bool:
        if event.keyval == Gdk.KEY_Escape:
            now = time.monotonic()
            if (
                self._search_panel_escape_ready
                and self.search_revealer
                and self.search_revealer.get_reveal_child()
            ):
                self.hide_search_panel()
                self._search_panel_escape_ready = False
                self._search_escape_time = 0.0
                return True
            if self._search_escape_time and now - self._search_escape_time < 0.5:
                self._search_entry.set_text("")
                self._search_escape_time = 0.0
                self.tree_view.get_selection().unselect_all()
                return True
            self._search_escape_time = now
        else:
            self._search_escape_time = 0.0
            self._search_panel_escape_ready = False
        return False

    def _on_selection_changed(self, *_args) -> None:
        self._update_details()
        self._update_toolbar_state()
        self._update_bulk_bar()

    def _on_row_activated(self, view: Gtk.TreeView, path, _column) -> None:
        # Double-click connects the activated host, even if it was not part of the selection.
        selection = view.get_selection()
        if selection:
            selection.unselect_all()
            selection.select_path(path)
        tree_iter = self.tree_store.get_iter(path)
        if not tree_iter:
            return
        host_obj = self.tree_store.get_value(tree_iter, self.COLUMN_HOST)
        host = self._coerce_host(host_obj)
        if host:
            self._open_hosts([host])

    def _on_tree_button_press(self, widget: Gtk.TreeView, event: Gdk.EventButton) -> bool:
        if event.button != 3:
            # Clear selection if clicking on empty area
            if event.button == 1:
                pos = widget.get_path_at_pos(int(event.x), int(event.y))
                if not pos:
                    widget.get_selection().unselect_all()
                    return False
            return False
        pos = widget.get_path_at_pos(int(event.x), int(event.y))
        if not pos:
            self._show_blank_context_menu(event)
            return True
        path, column, x, y = pos
        widget.grab_focus()
        selection = widget.get_selection()
        # Preserve multi-selection when right-clicking on already-selected rows.
        if not (selection and selection.path_is_selected(path)):
            widget.set_cursor(path, column, False)
        tree_iter = self.tree_store.get_iter(path)
        if not tree_iter:
            self._show_blank_context_menu(event)
            return True
        kind = self.tree_store.get_value(tree_iter, self.COLUMN_KIND)
        menu = Gtk.Menu()
        actions: list[Tuple[str, Callable[[], None]]] = []
        host_for_menu: Optional[HostEntry] = None
        perms_for_menu = {"can_view": True, "can_connect": True, "can_edit": True}
        if kind == "host":
            host_for_menu = self._coerce_host(self.tree_store.get_value(tree_iter, self.COLUMN_HOST))
            if host_for_menu:
                perms_for_menu = self._permissions_for_host(host_for_menu)

        if kind == "host":
            actions = [
                ("Connect", self._connect_selected),
                ("Reconnect", self._connect_selected),
                ("Copy SCP Command…", self._copy_scp_command),
                ("Edit", self._edit_selected),
                ("Duplicate", self._duplicate_selected),
                ("Move to Group…", self._move_selected),
                ("Change Color…", self._change_group_color),
                ("Clear Color", self._clear_group_color),
                ("Forget Password", self._forget_password_for_current_host),
                ("Delete", self._delete_selected),
            ]
        elif kind == "child":
            actions = [
                ("Connect All", self._connect_child_group),
                ("New Subgroup…", self._create_child_group_for_current),
                ("New Host…", self._create_host_in_current_group),
                ("Edit", self._rename_child_group),
                ("Duplicate", self._duplicate_child_group),
                ("Change Color…", self._change_group_color),
                ("Clear Color", self._clear_group_color),
                ("Delete", self._delete_child_group),
            ]
        elif kind == "child-folder":
            actions = [
                ("Connect All", self._connect_child_group),
                ("New Subgroup…", self._create_child_group_for_current),
                ("New Host…", self._create_host_in_current_group),
                ("Change Color…", self._change_group_color),
                ("Clear Color", self._clear_group_color),
            ]
        elif kind == "parent":
            actions = [
                ("Connect All", self._connect_parent_group),
                ("New Subgroup…", self._create_child_group_for_current),
                ("New Host…", self._create_host_in_current_group),
                ("Edit", self._rename_parent_group),
                ("Duplicate", self._duplicate_parent_group),
                ("Change Color…", self._change_group_color),
                ("Clear Color", self._clear_group_color),
                ("Delete", self._delete_parent_group),
            ]
        else:
            return False

        for label, handler in actions:
            item = Gtk.MenuItem(label=label)
            item.connect("activate", lambda _item, cb=handler: cb())
            if host_for_menu:
                if label in {"Edit", "Duplicate", "Move to Group…", "Delete"} and not perms_for_menu.get("can_edit", True):
                    item.set_sensitive(False)
            menu.append(item)
        toplevel = widget.get_toplevel()
        if isinstance(toplevel, Gtk.Window) and hasattr(menu, "set_transient_for"):
            menu.set_transient_for(toplevel)
        menu.attach_to_widget(widget, None)
        menu.show_all()
        if hasattr(menu, "popup_at_pointer"):
            menu.popup_at_pointer(event)
        else:
            menu.popup(None, None, None, None, event.button, event.time)
        return True

    def _show_blank_context_menu(self, event: Gdk.EventButton) -> None:
        menu = Gtk.Menu()
        for label, handler in [
            ("New Host", self._on_new_host_clicked),
            ("New Group", self._create_parent_group),
        ]:
            item = Gtk.MenuItem(label=label)
            item.connect("activate", lambda _item, cb=handler: cb())
            menu.append(item)
        menu.show_all()
        if hasattr(menu, "popup_at_pointer"):
            menu.popup_at_pointer(event)
        else:
            menu.popup(None, None, None, None, event.button, event.time)

    def _change_group_color(self) -> None:
        kind, data = self._current_item()
        if kind not in {"parent", "child", "child-folder", "host"}:
            return
        parent = data.get("parent") or ""
        child = data.get("child") or ""
        alias = data.get("alias") or ""
        title = "Choose Host Color" if kind == "host" else "Choose Group Color"
        dialog = Gtk.ColorChooserDialog(title=title, transient_for=self)
        current = self._item_color_for(parent, child, alias) if kind == "host" else self._group_color_for(parent, child)
        if current:
            rgba = Gdk.RGBA()
            if rgba.parse(current):
                dialog.set_rgba(rgba)
        response = dialog.run()
        if response == Gtk.ResponseType.OK:
            rgba = dialog.get_rgba()
            color = self._rgba_to_hex(rgba)
            if kind == "host":
                self._set_host_color(parent, child, alias, color)
            else:
                self._set_group_color(parent, child, color)
            self._color_pixbuf_cache.clear()
            self._rebuild_tree(refresh=False)
            tree_view = getattr(self, "tree_view", None)
            if tree_view:
                tree_view.queue_draw()
        dialog.destroy()

    def _clear_group_color(self) -> None:
        kind, data = self._current_item()
        if kind not in {"parent", "child", "child-folder", "host"}:
            return
        parent = data.get("parent") or ""
        child = data.get("child") or ""
        alias = data.get("alias") or ""
        if kind == "host":
            self._set_host_color(parent, child, alias, None)
        else:
            self._set_group_color(parent, child, None)
        self._color_pixbuf_cache.clear()
        self._rebuild_tree(refresh=False)
        tree_view = getattr(self, "tree_view", None)
        if tree_view:
            tree_view.queue_draw()

    # -------------------------------------------------------------- drag & drop
    def _configure_drag_and_drop(self) -> None:
        self._drag_dest_info: Optional[Tuple[Gtk.TreePath, Gtk.TreeViewDropPosition]] = None
        self._dnd_targets = [Gtk.TargetEntry.new(DND_TARGET, Gtk.TargetFlags.SAME_APP, 0)]
        self.tree_view.enable_model_drag_source(
            Gdk.ModifierType.BUTTON1_MASK, self._dnd_targets, Gdk.DragAction.MOVE
        )
        self.tree_view.enable_model_drag_dest(self._dnd_targets, Gdk.DragAction.MOVE)
        self.tree_view.connect("drag-data-get", self._on_drag_data_get)
        self.tree_view.connect("drag-data-received", self._on_drag_data_received)
        self.tree_view.connect("drag-motion", self._on_drag_motion)
        self.tree_view.connect("drag-drop", self._on_drag_drop)

    def _on_drag_motion(
        self,
        widget: Gtk.TreeView,
        context: Gdk.DragContext,
        x: int,
        y: int,
        time: int,
    ) -> bool:
        target = widget.get_dest_row_at_pos(x, y)
        if target:
            path, position = target
            widget.set_drag_dest_row(path, position)
            self._drag_dest_info = (path, position)
        else:
            self._drag_dest_info = None
        context.drag_status(Gdk.DragAction.MOVE, time)
        return True

    def _on_drag_drop(
        self,
        widget: Gtk.TreeView,
        context: Gdk.DragContext,
        x: int,
        y: int,
        time: int,
    ) -> bool:
        target = widget.get_dest_row_at_pos(x, y)
        if target:
            self._drag_dest_info = target
        atom_list = context.list_targets()
        if not atom_list:
            return False
        widget.drag_get_data(context, atom_list[0], time)
        return True

    def _on_drag_data_get(
        self,
        _widget: Gtk.TreeView,
        _context: Gdk.DragContext,
        selection: Gtk.SelectionData,
        _info: int,
        _time: int,
    ) -> None:
        payload = self._serialize_drag_payload()
        if not payload:
            selection.set(selection.get_target(), 8, b"")
            return
        data = json.dumps(payload).encode("utf-8")
        selection.set(selection.get_target(), 8, data)

    def _serialize_drag_payload(self) -> Optional[Dict[str, object]]:
        model, paths = self.tree_view.get_selection().get_selected_rows()
        if not model or not paths:
            return None
        items: List[Dict[str, str]] = []
        payload_kind: Optional[str] = None
        for path in paths:
            tree_iter = model.get_iter(path)
            if not tree_iter:
                continue
            kind = model.get_value(tree_iter, self.COLUMN_KIND)
            if kind not in {"host", "child"}:
                return None
            if payload_kind and kind != payload_kind:
                return None
            payload_kind = kind
            if kind == "host":
                items.append(
                    {
                        "kind": "host",
                        "parent": model.get_value(tree_iter, self.COLUMN_PARENT),
                        "child": model.get_value(tree_iter, self.COLUMN_CHILD),
                        "alias": model.get_value(tree_iter, self.COLUMN_ALIAS),
                    }
                )
            elif kind == "child":
                items.append(
                    {
                        "kind": "child",
                        "parent": model.get_value(tree_iter, self.COLUMN_PARENT),
                        "child": model.get_value(tree_iter, self.COLUMN_CHILD),
                    }
                )
        if not items or not payload_kind:
            return None
        return {"kind": payload_kind, "items": items}

    def _resolve_drop_target(
        self, path: Gtk.TreePath, position: Gtk.TreeViewDropPosition
    ) -> Optional[Dict[str, str]]:
        tree_iter = self.tree_store.get_iter(path)
        if not tree_iter:
            return None
        kind = self.tree_store.get_value(tree_iter, self.COLUMN_KIND)
        if kind == "host":
            return {
                "type": "child",
                "parent": self.tree_store.get_value(tree_iter, self.COLUMN_PARENT),
                "child": self.tree_store.get_value(tree_iter, self.COLUMN_CHILD),
            }
        if kind == "child":
            return {
                "type": "child",
                "parent": self.tree_store.get_value(tree_iter, self.COLUMN_PARENT),
                "child": self.tree_store.get_value(tree_iter, self.COLUMN_CHILD),
            }
        if kind == "parent":
            return {
                "type": "parent",
                "parent": self.tree_store.get_value(tree_iter, self.COLUMN_PARENT),
            }
        return None

    def _on_drag_data_received(
        self,
        widget: Gtk.TreeView,
        context: Gdk.DragContext,
        _x: int,
        _y: int,
        selection: Gtk.SelectionData,
        _info: int,
        time: int,
    ) -> None:
        data = selection.get_data()
        success = False
        if data:
            try:
                payload = json.loads(data.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                payload = None
            if isinstance(payload, dict) and self._drag_dest_info:
                path, position = self._drag_dest_info
                target = self._resolve_drop_target(path, position)
                if target:
                    success = self._handle_drop(payload, target)
        context.finish(success, False, time)
        self._drag_dest_info = None

    def _handle_drop(self, payload: Dict[str, object], target: Dict[str, str]) -> bool:
        kind = payload.get("kind")
        items = payload.get("items")
        if not isinstance(items, list) or not items:
            return False
        if kind == "host":
            if target.get("type") == "child":
                target_parent = target["parent"]
                target_child = target["child"]
                create_child = False
            elif target.get("type") == "parent":
                target_parent = target["parent"]
                choice = self._prompt_child_destination(target_parent)
                if not choice:
                    return False
                target_child, create_child = choice
            else:
                self._log_event("Drop hosts onto a group row.", False)
                return False
            sources: List[Tuple[str, str, str]] = []
            for item in items:
                if not isinstance(item, dict):
                    return False
                parent = item.get("parent")
                child = item.get("child")
                alias = item.get("alias")
                if not all(isinstance(val, str) and val for val in (parent, child, alias)):
                    return False
                if parent == target_parent and child == target_child:
                    continue
                sources.append((parent, child, alias))
            if not sources:
                self._log_event("Hosts already in target group.", False)
                return False
            success, msg = self.manager.move_hosts(
                sources, target_parent, target_child, create_child=create_child
            )
            self._log_event(msg, success)
            if success:
                for source_parent, source_child, alias in sources:
                    self._rebind_credential_key(
                        f"{source_parent}::{source_child}::{alias}",
                        f"{target_parent}::{target_child}::{alias}",
                    )
                self._rebuild_tree(refresh=True)
                self._select_child_group(target_parent, target_child)
            return success
        if kind == "child":
            if len(items) != 1:
                self._log_event("Drag one child group at a time.", False)
                return False
            source = items[0]
            if not isinstance(source, dict):
                return False
            source_parent = source.get("parent")
            source_child = source.get("child")
            if not isinstance(source_parent, str) or not isinstance(source_child, str):
                return False
            if target.get("type") == "child":
                target_parent = target["parent"]
            elif target.get("type") == "parent":
                target_parent = target["parent"]
            else:
                return False
            if source_parent == target_parent:
                self._log_event("Child group already in target parent.", False)
                return False
            success, msg = self.manager.move_child_group(source_parent, source_child, target_parent)
            self._log_event(msg, success)
            if success:
                self._remap_credential_prefix(
                    f"{source_parent}::{source_child}::",
                    f"{target_parent}::{source_child}::",
                )
                self._rebuild_tree(refresh=True)
                self._select_child_group(target_parent, source_child)
            return success
        return False

    # --------------------------------------------------------------- detail view
    def _update_details(self) -> None:
        for child in list(self._tag_flow.get_children()):
            self._tag_flow.remove(child)
        for child in list(self._detail_grid.get_children()):
            self._detail_grid.remove(child)

        hosts = self._selected_hosts()
        if len(hosts) > 1:
            self._detail_title.set_text(f"{len(hosts)} hosts selected")
            summary = ", ".join(host.alias for host in hosts[:3])
            if len(hosts) > 3:
                summary += "…"
            self._detail_subtitle.set_text(summary or "")
            self._detail_box.show_all()
            return

        kind, data = self._current_item()
        if kind == "host":
            host = self._selected_host()
            if not host:
                return
            perms = self._permissions_for_host(host)
            if not perms.get("can_view", True):
                self._detail_title.set_text("Viewing disabled")
                self._detail_subtitle.set_text("Viewing details is disabled for this host.")
                self._detail_box.show_all()
                return
            self._detail_title.set_text(host.alias)
            subtitle = host_option(host, "hostname") or host.alias
            display_user = self._display_username_for_host(host)
            if display_user:
                subtitle = f"{display_user}@{subtitle}"
            self._detail_subtitle.set_text(subtitle)

            tags = [tag.strip() for tag in (host_option(host, "tags") or "").split(",") if tag.strip()]
            for tag in tags:
                frame = Gtk.EventBox()
                label = Gtk.Label(label=tag)
                frame.add(label)
                frame.get_style_context().add_class("tag")
                self._tag_flow.add(frame)

            rows = [
                ("Group", f"{host.parent_group}/{host.child_group}"),
                ("Host name", host_option(host, "hostname") or host.alias),
                ("User", display_user),
                ("Port", host_option(host, "port") or ""),
                (
                    "Jump host",
                    host_option(host, "proxyjump")
                    or host_option(host, "proxycommand")
                    or "",
                ),
                ("Identity file", host_option(host, "identityfile") or ""),
                ("Environment", host.metadata.get("environment", "")),
                ("Owner", host.metadata.get("owner", "")),
                ("Service", host.metadata.get("service", "")),
                ("Notes", host.metadata.get("notes", "")),
                ("Default Profile", self._profiles.get(host.metadata.get("default_session_profile_id", ""), None).name if host.metadata.get("default_session_profile_id", "") in self._profiles else ""),
                ("Permissions", self._render_permissions(host)),
            ]
            row_idx = 0
            for label_text, value in rows:
                if not value:
                    continue
                key_label = Gtk.Label(label=label_text)
                key_label.set_xalign(0.0)
                key_label.get_style_context().add_class("dim-label")
                self._detail_grid.attach(key_label, 0, row_idx, 1, 1)

                value_label = Gtk.Label(label=value)
                value_label.set_xalign(0.0)
                value_label.set_selectable(True)
                self._detail_grid.attach(value_label, 1, row_idx, 1, 1)
                row_idx += 1
        elif kind == "child":
            parent = data["parent"]
            child = data["child"]
            self._detail_title.set_text(f"{parent}/{child}")
            group = self.manager.tree.get(parent, {}).get(child)
            count = len(group.hosts) if group else 0
            self._detail_subtitle.set_text(f"{count} host(s)")
        elif kind == "parent":
            parent = data["parent"]
            self._detail_title.set_text(parent)
            children = self.manager.tree.get(parent, {})
            host_count = sum(len(group.hosts) for group in children.values())
            self._detail_subtitle.set_text(f"{len(children)} child group(s) · {host_count} host(s)")
        else:
            self._detail_title.set_text("Select a host or group")
            self._detail_subtitle.set_text("Connections open in tabs on the right.")

        self._detail_box.show_all()

    # ------------------------------------------------------------ toolbar actions
    def _on_refresh_clicked(self) -> None:
        self._rebuild_tree(refresh=True)

    def _on_new_host_clicked(self) -> None:
        menu = Gtk.Menu()

        direct_item = Gtk.MenuItem(label="New Host…")
        direct_item.connect("activate", lambda *_: self.prompt_new_host())
        menu.append(direct_item)

        tmpl_root = Gtk.MenuItem(label="New from Template")
        tmpl_menu = Gtk.Menu()
        for tmpl in built_in_templates():
            item = Gtk.MenuItem(label=f"{tmpl.label}")
            item.connect("activate", lambda _m, t=tmpl: self.prompt_new_host(template=t))
            tmpl_menu.append(item)
        tmpl_menu.show_all()
        tmpl_root.set_submenu(tmpl_menu)
        menu.append(tmpl_root)

        menu.show_all()
        self._popup_menu(menu, getattr(self, "_new_host_button", None))

    def _popup_menu(self, menu: Gtk.Menu, anchor: Optional[Gtk.Widget]) -> None:
        if anchor is not None and hasattr(menu, "popup_at_widget"):
            menu.popup_at_widget(anchor, Gdk.Gravity.SOUTH, Gdk.Gravity.NORTH, None)
        elif hasattr(menu, "popup_at_pointer"):
            menu.popup_at_pointer(Gtk.get_current_event())
        else:
            menu.popup(None, None, None, None, 0, Gtk.get_current_event_time())

    def _on_new_folder_clicked(self) -> None:
        menu = Gtk.Menu()
        parent_item = Gtk.MenuItem(label="Parent Group")
        parent_item.connect("activate", lambda *_: self._create_parent_group())
        child_item = Gtk.MenuItem(label="Child Group")
        child_item.connect("activate", lambda *_: self._create_child_group())
        menu.append(parent_item)
        menu.append(child_item)
        menu.show_all()
        self._popup_menu(menu, getattr(self, "_new_folder_button", None))

    def _on_edit_clicked(self) -> None:
        kind, _ = self._current_item()
        if kind == "host":
            self._edit_selected()
        elif kind == "child":
            self._rename_child_group()
        elif kind == "parent":
            self._rename_parent_group()

    def _on_delete_clicked(self) -> None:
        kind, _ = self._current_item()
        if kind == "host":
            self._delete_selected()
        elif kind == "child":
            self._delete_child_group()
        elif kind == "parent":
            self._delete_parent_group()

    def _forget_password_for_current_host(self) -> None:
        host = self._selected_host()
        if not host:
            self._log_event("Select a host to remove stored password.", False)
            return
        resolved = self._host_with_resolved_identity(host)
        hostname, port, username = self._host_connection_params(resolved)
        try:
            self._secure_store.delete(hostname, port, username)
            binding = self._credential_store.binding_for_host(self._host_key(host))
            if binding:
                self._bind_credential_for_key(self._host_key(host), None)
            self._notify_info(f"Password removed for {host.alias}. You will be prompted next time.")
        except SecureStoreError as exc:
            self._notify_warning("Could not remove the stored password. See logs for details.")
            LOGGER.warning("forget password failed: %s", exc)

    def _on_connect_clicked(self) -> None:
        kind, _ = self._current_item()
        if kind == "child":
            self._connect_child_group()
        elif kind == "parent":
            self._connect_parent_group()
        else:
            self.open_selected_host()

    def _on_settings_clicked(self) -> None:
        dialog = PreferencesDialog(
            self,
            self._prefer_dark,
            self.terminal_tabs.default_font_scale,
            self._ui_font_size,
            self._settings,
            on_reset=self._reset_master_password_flow,
            on_export=self._export_settings_snapshot,
        )
        response = dialog.run()
        if response == Gtk.ResponseType.OK:
            (
                dark,
                font_scale,
                ui_font_size,
                remember_default,
                host_key_policy,
                connect_timeout,
                auth_timeout,
                banner_timeout,
                use_paramiko_vte,
                disable_master_password,
                allowed_ciphers,
                allowed_kex,
                secrets_policy,
                enable_audit,
                audit_path,
                git_watch_enabled,
                git_watch_path,
                git_watch_interval,
                term_appearance,
            ) = dialog.values()
            self._apply_dark_theme(dark)
            app = self.get_application()
            if app and hasattr(app, "_install_css"):
                app._install_css(dark)
            self.terminal_tabs.set_default_font_scale(font_scale)
            self.terminal_tabs.apply_font_scale_to_all(font_scale)
            self._apply_ui_font(ui_font_size)
            self._ui_state["prefer_dark"] = dark
            self._ui_state["font_scale"] = font_scale
            self._ui_state["ui_font_size"] = self._ui_font_size
            self._save_ui_state()
            self._settings.remember_password_default = remember_default
            self._settings.host_key_policy = host_key_policy
            self._settings.connect_timeout = connect_timeout
            self._settings.auth_timeout = auth_timeout
            self._settings.banner_timeout = banner_timeout
            self._settings.use_paramiko_vte = use_paramiko_vte
            master_toggled = disable_master_password != self._settings.master_password_disabled
            self._settings.master_password_disabled = disable_master_password
            self._settings.allowed_ciphers = allowed_ciphers
            self._settings.allowed_kex = allowed_kex
            self._settings.secrets_policy = secrets_policy
            self._settings.enable_audit_logging = enable_audit
            self._settings.audit_log_path = audit_path
            self._settings.git_watch_enabled = git_watch_enabled
            self._settings.git_watch_path = git_watch_path
            self._settings.git_watch_interval = git_watch_interval
            self._settings.terminal_appearance = term_appearance
            self._settings.save()
            if master_toggled:
                if disable_master_password:
                    self._rekey_password_file(None)
                elif self._master_password:
                    self._rekey_password_file(self._master_password)
                elif self._secure_store.uses_file_backend:
                    self._notify_info("Saved passwords will be encrypted after the next master password unlock.")
            try:
                self.terminal_tabs.set_appearance(term_appearance)
            except Exception:
                pass
            self._refresh_git_watch()
        dialog.destroy()

    def _export_settings_snapshot(self) -> None:
        timestamp = datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z"
        chooser = Gtk.FileChooserDialog(
            title="Export Settings & Policies",
            parent=self,
            action=Gtk.FileChooserAction.SAVE,
            buttons=(
                Gtk.STOCK_CANCEL,
                Gtk.ResponseType.CANCEL,
                Gtk.STOCK_SAVE,
                Gtk.ResponseType.OK,
            ),
        )
        default_name = f"settings_export_{time.strftime('%Y%m%d_%H%M')}.json"
        chooser.set_current_name(default_name)
        response = chooser.run()
        target = chooser.get_filename() if response == Gtk.ResponseType.OK else None
        chooser.destroy()
        if not target:
            return

        payload: Dict[str, Any] = {
            "version": 1,
            "generated_at": timestamp,
            "app": {"name": "SSH Manager", "version": ""},
            "security_policy": {
                "host_key_checking": self._settings.host_key_policy,
                "allowed_ciphers": self._settings.allowed_ciphers,
                "allowed_kex_algorithms": self._settings.allowed_kex,
            },
            "secrets_policy": self._settings.secrets_policy_with_defaults(),
            "rbac": {
                "has_per_host_permissions": True,
            },
            "audit_logging": {
                "enabled": self._settings.enable_audit_logging,
                "path": self._settings.audit_log_path if self._settings.enable_audit_logging else "",
            },
            "git_sync": {
                "enabled": self._settings.git_watch_enabled,
                "repo_paths": [self._settings.git_watch_path] if self._settings.git_watch_path else [],
                "auto_fetch_interval_minutes": int(self._settings.git_watch_interval // 60),
            },
            "shared_inventory": {
                "path": str(self._shared_path) if self._shared_path else "",
                "auto_reload_enabled": bool(self._shared_poll_id),
            },
        }
        try:
            write_json_atomic(Path(target).expanduser(), payload, mode=0o600)
            self._notify_info("Settings & policies exported successfully.")
        except Exception as exc:
            self._notify_warning(f"Export failed: {exc}")

    def _on_credentials_clicked(self) -> None:
        dialog = CredentialManagerDialog(self, self._credential_store)
        dialog.run()
        dialog.destroy()

    def _resolve_config_view_path(self) -> Path:
        # Prefer explicit config file if base_dir is a file
        if self.manager.base_dir.is_file():
            return self.manager.base_dir
        candidates = [
            Path("~/.ssh/config").expanduser(),
            self.manager.base_dir / "config",
        ]
        for cand in candidates:
            if cand.exists() and cand.is_file():
                return cand
        # Fallback to default even if missing (viewer will show message)
        return candidates[0]

    def _on_config_view_clicked(self) -> None:
        path = self._resolve_config_view_path()
        dialog = ConfigViewerDialog(self, path)
        dialog.run()
        dialog.destroy()

    def _set_git_updates(self, available: bool) -> None:
        self._git_updates_available = available
        if available:
            self._git_update_button.show()
        else:
            self._git_update_button.hide()

    def _git_watch_loop(self, interval: int) -> None:
        while not self._git_watch_stop.is_set():
            self._check_git_repo_for_updates()
            self._git_watch_stop.wait(interval)

    def _stop_git_watch_thread(self) -> None:
        if self._git_watch_thread and self._git_watch_thread.is_alive():
            self._git_watch_stop.set()
            self._git_watch_thread.join(timeout=1.0)
        self._git_watch_thread = None
        self._git_watch_stop.clear()

    def _refresh_git_watch(self) -> None:
        self._stop_git_watch_thread()
        self._set_git_updates(False)
        if not getattr(self._settings, "git_watch_enabled", False):
            return
        watch_target = self._settings.git_watch_path or (str(self._shared_path) if self._shared_path else "")
        if not watch_target:
            return
        path = Path(watch_target).expanduser()
        if not (path / ".git").exists():
            return
        self._git_watch_path = path
        interval = max(60, int(getattr(self._settings, "git_watch_interval", 600) or 600))
        self._git_watch_stop.clear()
        self._git_watch_thread = threading.Thread(target=self._git_watch_loop, args=(interval,), daemon=True)
        self._git_watch_thread.start()

    def _check_git_repo_for_updates(self) -> None:
        path = self._git_watch_path
        if not path or not (path / ".git").exists():
            return
        devnull = subprocess.DEVNULL
        try:
            subprocess.run(
                ["git", "-C", str(path), "fetch", "--all", "--prune"],
                stdout=devnull,
                stderr=devnull,
                check=False,
                timeout=30,
            )
        except Exception:
            return
        try:
            head = subprocess.run(
                ["git", "-C", str(path), "rev-parse", "HEAD"],
                stdout=subprocess.PIPE,
                stderr=devnull,
                check=True,
                text=True,
                timeout=10,
            ).stdout.strip()
            upstream = subprocess.run(
                ["git", "-C", str(path), "rev-parse", "@{u}"],
                stdout=subprocess.PIPE,
                stderr=devnull,
                check=True,
                text=True,
                timeout=10,
            ).stdout.strip()
            behind = subprocess.run(
                ["git", "-C", str(path), "rev-list", "--count", f"{head}..{upstream}"],
                stdout=subprocess.PIPE,
                stderr=devnull,
                check=True,
                text=True,
                timeout=10,
            ).stdout.strip()
            available = behind.isdigit() and int(behind) > 0
        except Exception:
            return
        GLib.idle_add(self._set_git_updates, available, priority=GLib.PRIORITY_DEFAULT)

    def _on_vault_clicked(self) -> None:
        dialog = VaultDialog(self, self._vault)
        dialog.run()
        dialog.destroy()

    def _on_git_updates_clicked(self) -> None:
        path_text = str(self._git_watch_path) if self._git_watch_path else ""
        message = "Remote updates are available. Run 'git pull' in the tracked directory to refresh."
        if path_text:
            message += f"\nDirectory: {path_text}"
        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            buttons=Gtk.ButtonsType.OK,
            message_type=Gtk.MessageType.INFO,
            text="Git updates detected",
        )
        dialog.format_secondary_text(message)
        dialog.run()
        dialog.destroy()

    def _start_shared_poll(self) -> None:
        if self._shared_poll_id:
            GLib.source_remove(self._shared_poll_id)
            self._shared_poll_id = 0
        if not self._shared_path:
            return
        self._shared_poll_id = GLib.timeout_add_seconds(5, self._check_shared_inventory)

    def _check_shared_inventory(self) -> bool:
        if not self._shared_path:
            return False
        try:
            stat_result = self._shared_path.stat()
        except FileNotFoundError:
            if not self._shared_missing_warned:
                self._log_event("Shared inventory file not found.", False)
                self._shared_missing_warned = True
            self._shared_mtime = None
            return True
        except OSError as exc:
            if not self._shared_missing_warned:
                self._log_event(f"Unable to read shared inventory: {exc}", False)
                self._shared_missing_warned = True
            return True

        self._shared_missing_warned = False
        mtime = stat_result.st_mtime
        if self._shared_mtime is not None and mtime <= self._shared_mtime:
            return True

        try:
            manager = ConfigManager(self._shared_path)
        except Exception as exc:
            self._log_event(f"Failed to reload shared inventory: {exc}", False)
            return True
        if not manager.tree:
            self._log_event("Failed to reload shared inventory — invalid format.", False)
            return True

        self._shared_manager = manager
        self._shared_mtime = mtime
        self._log_event("Reloaded shared inventory.", True)
        self._rebuild_tree(refresh=False)
        return True

    def _on_open_shared_clicked(self) -> None:
        chooser = Gtk.FileChooserDialog(
            title="Open Shared Inventory",
            parent=self,
            action=Gtk.FileChooserAction.OPEN,
            buttons=(
                Gtk.STOCK_CANCEL,
                Gtk.ResponseType.CANCEL,
                Gtk.STOCK_OPEN,
                Gtk.ResponseType.OK,
            ),
        )
        response = chooser.run()
        filename = chooser.get_filename() if response == Gtk.ResponseType.OK else None
        chooser.destroy()
        if not filename:
            return
        path = Path(filename)
        manager = ConfigManager(path)
        if not manager.tree:
            self._log_event("Unable to load shared inventory.", False)
            return
        self._shared_manager = manager
        self._shared_path = path
        try:
            self._shared_mtime = path.stat().st_mtime
        except OSError:
            self._shared_mtime = None
        self._shared_missing_warned = False
        self._log_event(f"Loaded shared inventory from {path}.", True)
        self._rebuild_tree(refresh=True)
        self._start_shared_poll()
        self._refresh_git_watch()

    def _prompt_master_reset(self) -> Optional[str]:
        dialog = Gtk.Dialog(modal=True)
        dialog.set_destroy_with_parent(True)
        dialog.set_position(Gtk.WindowPosition.CENTER)
        dialog.add_button("_Cancel", Gtk.ResponseType.CANCEL)
        next_btn = dialog.add_button("_Next", Gtk.ResponseType.OK)
        next_btn.get_style_context().add_class("suggested-action")
        dialog.set_default_response(Gtk.ResponseType.OK)

        header = Gtk.HeaderBar(title="Reset Master Password")
        header.set_show_close_button(True)
        dialog.set_titlebar(header)

        content = dialog.get_content_area()
        content.set_border_width(18)
        content.set_spacing(12)

        intro = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        icon = Gtk.Image.new_from_icon_name("dialog-password-symbolic", Gtk.IconSize.DIALOG)
        intro.pack_start(icon, False, False, 0)
        label = Gtk.Label(
            label="<b>Verify current master password</b>\nYou must confirm before changing it.",
            use_markup=True,
        )
        label.set_xalign(0.0)
        label.set_line_wrap(True)
        intro.pack_start(label, True, True, 0)
        content.pack_start(intro, False, False, 0)

        current = Gtk.Entry()
        current.set_visibility(False)
        current.set_invisible_char("•")
        current.set_activates_default(True)
        current.set_placeholder_text("Current master password")
        content.pack_start(current, False, False, 0)

        error_label = Gtk.Label()
        error_label.get_style_context().add_class("error")
        error_label.set_no_show_all(True)
        error_label.set_xalign(0.0)
        content.pack_start(error_label, False, False, 0)

        def show_error(msg: str) -> None:
            error_label.set_text(msg)
            error_label.show()

        dialog.show_all()
        current.grab_focus()
        while True:
            response = dialog.run()
            text = current.get_text().strip()
            if response != Gtk.ResponseType.OK:
                dialog.destroy()
                return None
            if not text:
                show_error("Password cannot be empty.")
                continue
            if verify_master_password(text, self._settings.master_password_hash):
                if master_hash_needs_upgrade(self._settings.master_password_hash):
                    self._settings.master_password_hash = hash_master_password(text)
                    self._settings.save()
                dialog.destroy()
                return text
            show_error("Incorrect master password. Try again.")

    def _prompt_master_reset_new(self) -> Optional[str]:
        dialog = Gtk.Dialog(modal=True)
        dialog.set_destroy_with_parent(True)
        dialog.set_position(Gtk.WindowPosition.CENTER)
        dialog.add_button("_Cancel", Gtk.ResponseType.CANCEL)
        save_btn = dialog.add_button("_Save", Gtk.ResponseType.OK)
        save_btn.get_style_context().add_class("suggested-action")
        dialog.set_default_response(Gtk.ResponseType.OK)

        header = Gtk.HeaderBar(title="Set New Master Password")
        header.set_show_close_button(True)
        dialog.set_titlebar(header)

        content = dialog.get_content_area()
        content.set_border_width(18)
        content.set_spacing(12)

        intro = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        icon = Gtk.Image.new_from_icon_name("emblem-readonly", Gtk.IconSize.DIALOG)
        intro.pack_start(icon, False, False, 0)
        label = Gtk.Label(
            label="<b>Choose a new master password</b>\nConfirm to apply the change.",
            use_markup=True,
        )
        label.set_xalign(0.0)
        label.set_line_wrap(True)
        intro.pack_start(label, True, True, 0)
        content.pack_start(intro, False, False, 0)

        entry = Gtk.Entry()
        entry.set_visibility(False)
        entry.set_invisible_char("•")
        entry.set_activates_default(True)
        entry.set_placeholder_text("New master password")
        content.pack_start(entry, False, False, 0)

        confirm = Gtk.Entry()
        confirm.set_visibility(False)
        confirm.set_invisible_char("•")
        confirm.set_activates_default(True)
        confirm.set_placeholder_text("Confirm password")
        content.pack_start(confirm, False, False, 0)

        error_label = Gtk.Label()
        error_label.get_style_context().add_class("error")
        error_label.set_no_show_all(True)
        error_label.set_xalign(0.0)
        content.pack_start(error_label, False, False, 0)

        def show_error(msg: str) -> None:
            error_label.set_text(msg)
            error_label.show()

        dialog.show_all()
        entry.grab_focus()
        while True:
            response = dialog.run()
            text = entry.get_text().strip()
            confirm_text = confirm.get_text().strip()
            if response != Gtk.ResponseType.OK:
                dialog.destroy()
                return None
            if not text:
                show_error("Password cannot be empty.")
                continue
            if text != confirm_text:
                show_error("Passwords do not match.")
                continue
            dialog.destroy()
            return text

    def _reset_master_password_flow(self) -> None:
        if not self._settings.master_password_hash:
            return
        current = self._prompt_master_reset()
        if current is None:
            return
        new_password = self._prompt_master_reset_new()
        if new_password is None:
            return

        self._settings.master_password_hash = hash_master_password(new_password)
        self._settings.save()
        self._master_password = new_password
        self._rekey_password_file(None if self._settings.master_password_disabled else new_password)
        self._log_event("Master password updated.", True)

    def _rekey_password_file(self, master_password: Optional[str]) -> None:
        """Re-encrypt the local password file (if used) under a new master password."""

        backend = self._secure_store.backend
        if not isinstance(backend, FileKeyring):
            return
        try:
            backend.rekey(master_password)
        except KeyringError as exc:
            LOGGER.warning("password file rekey failed: %s", exc)
            self._notify_warning("Could not re-encrypt saved passwords. See logs for details.")
            return
        if master_password is None:
            self._notify_warning(
                "Saved passwords are now stored unencrypted because the master password is disabled."
            )

    # ----------------------------------------------------------- host operations
    def prompt_new_host(self, template: Optional[HostTemplate] = None) -> None:
        parent, child = self._infer_target_group()
        create_child = False
        if not parent or not child:
            picker = GroupPickerDialog(
                self,
                self.manager,
                "Select host destination",
                allow_new_child=True,
                initial_parent=parent,
                initial_child=child,
            )
            response = picker.run()
            selection = picker.selected() if response == Gtk.ResponseType.OK else None
            picker.destroy()
            if not selection:
                return
            parent, child, create_child = selection

        dialog = HostDialog(
            self,
            credentials=self._credential_store.list(),
            selected_credential_id=None,
            metadata={},
            vault_entries=self._vault.entries() if self._vault.unlocked else [],
            vault_unlocked=self._vault.unlocked,
            template_defaults=template.defaults if template else None,
            profiles=self._profiles,
        )
        response = dialog.run()
        host = dialog.build_host(parent, child) if response == Gtk.ResponseType.OK else None
        cred_id = dialog.selected_credential_id() if response == Gtk.ResponseType.OK else None
        dialog.destroy()
        if not host:
            return
        if create_child:
            ok, msg = self.manager.create_child_group(parent, child)
            self._log_event(msg, ok)
            if not ok:
                return
        success, msg = self.manager.add_host(parent, child, host)
        self._log_event(msg, success)
        if success:
            self._bind_credential_for_key(self._host_key(host), cred_id)
            self._rebuild_tree(refresh=True)
            self._select_host(parent, child, host.alias)

    def _connect_selected(self) -> None:
        if self._current_item_is_read_only():
            self._log_event("Shared hosts are read-only; connect is allowed.", True)
        self.open_selected_host()

    def _connect_child_group(self) -> None:
        kind, data = self._current_item()
        if kind not in {"child", "child-folder"}:
            return
        parent = data["parent"]
        child = data["child"]
        hosts = self._collect_hosts_for_child_group(parent, child)
        self._open_hosts(hosts, f"group {parent}/{child}")

    def _connect_parent_group(self) -> None:
        kind, data = self._current_item()
        if kind != "parent":
            return
        parent = data["parent"]
        hosts = self._collect_hosts_for_parent_group(parent)
        self._open_hosts(hosts, f"parent {parent}")

    def _copy_scp_command(self) -> None:
        host = self._selected_host()
        if not host:
            self._log_event("Select a host to copy SCP command.", False)
            return
        command = shlex.join(build_scp_command(host))
        try:
            clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
            clipboard.set_text(command, -1)
            self._log_event("SCP command copied to clipboard.", True)
        except Exception as exc:
            self._log_event(f"Unable to copy command: {exc}", False)

    def _edit_selected(self) -> None:
        if self._current_item_is_read_only():
            self._log_event("Editing this host is disabled by local permissions.", False)
            return
        hosts = self._selected_hosts()
        if len(hosts) > 1:
            self._log_event("Select a single host to edit.", False)
            return
        host = self._selected_host()
        if not host:
            return
        current_binding = self._credential_store.binding_for_host(self._host_key(host))
        dialog = HostDialog(
            self,
            host,
            credentials=self._credential_store.list(),
            selected_credential_id=current_binding,
            metadata=host.metadata,
            vault_entries=self._vault.entries() if self._vault.unlocked else [],
            vault_unlocked=self._vault.unlocked,
            profiles=self._profiles,
        )
        response = dialog.run()
        updated = dialog.build_host(host.parent_group, host.child_group) if response == Gtk.ResponseType.OK else None
        cred_id = dialog.selected_credential_id() if response == Gtk.ResponseType.OK else None
        dialog.destroy()
        if not updated:
            return
        if not self._check_secrets_policy(updated):
            return
        changed: List[str] = []
        for key in {"alias", "hostname", "user", "port", "tags"}:
            old_value = host.alias if key == "alias" else (host_option(host, key) or "")
            new_value = updated.alias if key == "alias" else (host_option(updated, key) or "")
            if old_value != new_value and "password" not in key:
                changed.append(key)
        success, msg = self.manager.update_host(host.parent_group, host.child_group, host.alias, updated)
        self._log_event(msg, success)
        if success:
            self._bind_credential_for_key(self._host_key(updated), cred_id)
            if self._host_key(updated) != self._host_key(host):
                self._bind_credential_for_key(self._host_key(host), None)
            if changed:
                self._audit_log("edit", updated, extra={"fields": ",".join(changed)})
            self._rebuild_tree(refresh=True)
            self._select_host(updated.parent_group, updated.child_group, updated.alias)

    def _duplicate_selected(self) -> None:
        if self._current_item_is_read_only():
            self._log_event("Editing this host is disabled by local permissions.", False)
            return
        hosts = self._selected_hosts()
        if len(hosts) > 1:
            self._log_event("Select a single host to duplicate.", False)
            return
        host = self._selected_host()
        if not host:
            return
        current_binding = self._credential_store.binding_for_host(self._host_key(host))
        dialog = HostDialog(
            self,
            host,
            credentials=self._credential_store.list(),
            selected_credential_id=current_binding,
            metadata=host.metadata,
            vault_entries=self._vault.entries() if self._vault.unlocked else [],
            vault_unlocked=self._vault.unlocked,
            profiles=self._profiles,
        )
        dialog.set_alias(self._generate_duplicate_alias(host))
        response = dialog.run()
        clone = dialog.build_host(host.parent_group, host.child_group) if response == Gtk.ResponseType.OK else None
        cred_id = dialog.selected_credential_id() if response == Gtk.ResponseType.OK else None
        dialog.destroy()
        if not clone:
            return
        if not self._check_secrets_policy(clone):
            return
        success, msg = self.manager.add_host(host.parent_group, host.child_group, clone)
        self._log_event(msg, success)
        if success:
            self._bind_credential_for_key(self._host_key(clone), cred_id)
            self._audit_log("edit", clone, extra={"fields": "duplicate"})
            self._rebuild_tree(refresh=True)
            self._select_host(clone.parent_group, clone.child_group, clone.alias)

    def _move_selected(self) -> None:
        if self._current_item_is_read_only():
            self._log_event("Editing this host is disabled by local permissions.", False)
            return
        hosts = self._selected_hosts()
        if len(hosts) > 1:
            self._log_event("Select a single host to move.", False)
            return
        host = self._selected_host()
        if not host:
            return
        picker = GroupPickerDialog(
            self,
            self.manager,
            "Move host",
            allow_new_child=True,
            initial_parent=host.parent_group,
            initial_child=host.child_group,
        )
        response = picker.run()
        selection = picker.selected() if response == Gtk.ResponseType.OK else None
        picker.destroy()
        if not selection:
            return
        target_parent, target_child, create_child = selection
        binding = self._credential_store.binding_for_host(self._host_key(host))
        success, msg = self.manager.move_hosts(
            [(host.parent_group, host.child_group, host.alias)],
            target_parent,
            target_child,
            create_child=create_child,
        )
        self._log_event(msg, success)
        if success:
            if binding:
                self._bind_credential_for_key(
                    f"{target_parent}::{target_child}::{host.alias}",
                    binding,
                )
                self._bind_credential_for_key(self._host_key(host), None)
            self._rebuild_tree(refresh=True)
            self._select_host(target_parent, target_child, host.alias)

    def _delete_selected(self) -> None:
        if self._current_item_is_read_only():
            self._log_event("Editing this host is disabled by local permissions.", False)
            return
        hosts = self._selected_hosts()
        if len(hosts) > 1:
            self._log_event("Select a single item to delete.", False)
            return
        host = self._selected_host()
        if not host:
            return
        if not self._confirm(f"Delete host '{host.alias}'?"):
            return
        success, msg = self.manager.delete_host(host.parent_group, host.child_group, host.alias)
        self._log_event(msg, success)
        if success:
            self._bind_credential_for_key(self._host_key(host), None)
            self._audit_log("delete", host)
            self._rebuild_tree(refresh=True)

    def _create_parent_group(self) -> None:
        name = self._prompt_text("Create parent group", "Folder name:")
        if not name:
            return
        success, msg = self.manager.create_parent_group(name)
        self._log_event(msg, success)
        if success:
            self._rebuild_tree(refresh=True)

    def _create_child_group_for_current(self) -> None:
        kind, data = self._current_item()
        if kind not in {"parent", "child", "child-folder"}:
            return
        parent = data.get("parent", "")
        if not parent:
            return
        if kind == "child":
            base_child = data.get("child", "")
            default_leaf = base_child.split("/")[-1].rsplit(".", 1)[0] + "-sub.conf"
        elif kind == "child-folder":
            base_child = data.get("child", "")
            default_leaf = base_child.split("/")[-1] + "-sub.conf"
        else:
            base_child = ""
            default_leaf = "new-group.conf"
        path_display = f"{parent}/{base_child.rsplit('.', 1)[0]}" if base_child else parent
        name = self._prompt_text(
            "New subgroup",
            f"Filename under {path_display}:",
            default_leaf,
        )
        if not name:
            return
        leaf = self._normalize_group_filename(name)
        if base_child:
            base_prefix = base_child.rsplit(".", 1)[0]
            child_path = f"{base_prefix}/{leaf}" if base_prefix else leaf
        else:
            child_path = leaf
        success, msg = self.manager.create_child_group(parent, child_path)
        self._log_event(msg, success)
        if success:
            self._rebuild_tree(refresh=True)
            self._select_child_group(parent, child_path)

    def _create_child_group(self) -> None:
        parent, _child = self._infer_target_group()
        picker = GroupPickerDialog(
            self,
            self.manager,
            "Create child group",
            allow_new_child=True,
            initial_parent=parent,
            initial_child=None,
        )
        response = picker.run()
        selection = picker.selected() if response == Gtk.ResponseType.OK else None
        picker.destroy()
        if not selection:
            return
        parent_name, child_name, create_child = selection
        child_name = self._normalize_group_filename(child_name)
        if not create_child:
            self._log_event("Enter a new child group name.", False)
            return
        success, msg = self.manager.create_child_group(parent_name, child_name)
        self._log_event(msg, success)
        if success:
            self._rebuild_tree(refresh=True)

    def _create_host_in_current_group(self) -> None:
        kind, data = self._current_item()
        parent = data.get("parent", "")
        if not parent:
            return
        if kind == "child":
            child = data.get("child", "")
            if not child:
                return
            self._create_host_in_group(parent, child)
        elif kind == "child-folder":
            folder = data.get("child", "") or ""
            name = self._prompt_text(
                "Select subgroup",
                f"Subgroup under {parent}/{folder}:",
                "",
            )
            if not name:
                return
            leaf = self._normalize_group_filename(name)
            child = f"{folder}/{leaf}" if folder else leaf
            create_child = child not in self.manager.tree.get(parent, {})
            if create_child:
                ok, msg = self.manager.create_child_group(parent, child)
                self._log_event(msg, ok)
                if not ok:
                    return
            self._create_host_in_group(parent, child)
        elif kind == "parent":
            choice = self._prompt_child_destination(parent)
            if not choice:
                return
            child, create_child = choice
            if create_child:
                ok, msg = self.manager.create_child_group(parent, child)
                self._log_event(msg, ok)
                if not ok:
                    return
            self._create_host_in_group(parent, child)

    def _create_host_in_group(self, parent: str, child: str) -> None:
        dialog = HostDialog(
            self,
            credentials=self._credential_store.list(),
            selected_credential_id=None,
            metadata={},
            vault_entries=self._vault.entries() if self._vault.unlocked else [],
            vault_unlocked=self._vault.unlocked,
            profiles=self._profiles,
        )
        response = dialog.run()
        host = dialog.build_host(parent, child) if response == Gtk.ResponseType.OK else None
        cred_id = dialog.selected_credential_id() if response == Gtk.ResponseType.OK else None
        dialog.destroy()
        if not host:
            return
        if not self._check_secrets_policy(host):
            return
        success, msg = self.manager.add_host(parent, child, host)
        self._log_event(msg, success)
        if success:
            self._bind_credential_for_key(self._host_key(host), cred_id)
            self._audit_log("edit", host, extra={"fields": "new"})
            self._rebuild_tree(refresh=True)
            self._select_host(parent, child, host.alias)

    def _normalize_group_filename(self, name: str) -> str:
        slug = name.strip()
        if not slug:
            return slug
        return slug if slug.lower().endswith(".conf") else f"{slug}.conf"

    def _rename_parent_group(self) -> None:
        kind, data = self._current_item()
        if kind != "parent":
            return
        current = data["parent"]
        new_name = self._prompt_text("Rename parent group", "New name:", current)
        if not new_name:
            return
        success, msg = self.manager.rename_parent_group(current, new_name)
        self._log_event(msg, success)
        if success:
            self._remap_credential_prefix(f"{current}::", f"{new_name}::")
            self._rebuild_tree(refresh=True)
            self._select_parent_group(new_name)

    def _duplicate_parent_group(self) -> None:
        kind, data = self._current_item()
        if kind != "parent":
            return
        source = data["parent"]
        default = f"{source}-copy"
        new_name = self._prompt_text("Duplicate parent group", "New parent group name:", default)
        if not new_name:
            return
        success, msg = self.manager.duplicate_parent_group(source, new_name)
        self._log_event(msg, success)
        if success:
            self._rebuild_tree(refresh=True)
            self._select_parent_group(new_name)

    def _rename_child_group(self) -> None:
        kind, data = self._current_item()
        if kind != "child":
            return
        parent = data["parent"]
        current = data["child"]
        new_name = self._prompt_text("Rename child group", "New filename:", current)
        if not new_name:
            return
        success, msg = self.manager.rename_child_group(parent, current, new_name)
        self._log_event(msg, success)
        if success:
            self._remap_credential_prefix(
                f"{parent}::{current}::",
                f"{parent}::{new_name}::",
            )
            self._rebuild_tree(refresh=True)
            self._select_child_group(parent, new_name)

    def _duplicate_child_group(self) -> None:
        kind, data = self._current_item()
        if kind != "child":
            return
        parent = data["parent"]
        child = data["child"]
        default = f"{child}-copy"
        new_name = self._prompt_text("Duplicate child group", "New filename:", default)
        if not new_name:
            return
        success, msg = self.manager.duplicate_child_group(parent, child, new_name)
        self._log_event(msg, success)
        if success:
            self._rebuild_tree(refresh=True)
            self._select_child_group(parent, new_name)

    def _delete_parent_group(self) -> None:
        kind, data = self._current_item()
        if kind != "parent":
            return
        name = data["parent"]
        if not self._confirm(f"Delete parent group '{name}' and all hosts?"):
            return
        success, msg = self.manager.delete_parent_group(name)
        self._log_event(msg, success)
        if success:
            self._unbind_credential_prefix(f"{name}::")
            self._rebuild_tree(refresh=True)

    def _delete_child_group(self) -> None:
        kind, data = self._current_item()
        if kind != "child":
            return
        parent = data["parent"]
        child = data["child"]
        if not self._confirm(f"Delete child group '{parent}/{child}'?"):
            return
        success, msg = self.manager.delete_child_group(parent, child)
        self._log_event(msg, success)
        if success:
            self._unbind_credential_prefix(f"{parent}::{child}::")
            self._rebuild_tree(refresh=True)

    # -------------------------------------------------------------- misc helpers
    def _infer_target_group(self) -> Tuple[Optional[str], Optional[str]]:
        kind, data = self._current_item()
        if kind == "host":
            return data["parent"], data["child"]
        if kind == "child":
            return data["parent"], data["child"]
        if kind == "parent":
            return data["parent"], None
        return None, None

    def _find_tree_path(self, predicate) -> Optional[Gtk.TreePath]:
        def traverse(model, tree_iter):
            while tree_iter:
                if predicate(model, tree_iter):
                    return model.get_path(tree_iter)
                if model.iter_has_child(tree_iter):
                    child = traverse(model, model.iter_children(tree_iter))
                    if child is not None:
                        return child
                tree_iter = model.iter_next(tree_iter)
            return None

        return traverse(self.tree_store, self.tree_store.get_iter_first())

    def _select_path(self, path: Optional[Gtk.TreePath]) -> None:
        if path is None:
            return
        self.tree_view.set_cursor(path)
        self.tree_view.scroll_to_cell(path)

    def _select_host(self, parent: str, child: str, alias: str) -> None:
        path = self._find_tree_path(
            lambda model, tree_iter: (
                model.get_value(tree_iter, self.COLUMN_KIND) == "host"
                and model.get_value(tree_iter, self.COLUMN_PARENT) == parent
                and model.get_value(tree_iter, self.COLUMN_CHILD) == child
                and model.get_value(tree_iter, self.COLUMN_ALIAS) == alias
            )
        )
        self._select_path(path)

    def _select_child_group(self, parent: str, child: str) -> None:
        path = self._find_tree_path(
            lambda model, tree_iter: (
                model.get_value(tree_iter, self.COLUMN_KIND) == "child"
                and model.get_value(tree_iter, self.COLUMN_PARENT) == parent
                and model.get_value(tree_iter, self.COLUMN_CHILD) == child
            )
        )
        self._select_path(path)

    def _select_parent_group(self, parent: str) -> None:
        path = self._find_tree_path(
            lambda model, tree_iter: (
                model.get_value(tree_iter, self.COLUMN_KIND) == "parent"
                and model.get_value(tree_iter, self.COLUMN_PARENT) == parent
            )
        )
        self._select_path(path)

    def _make_host_key(self, parent: str, child: str, alias: str) -> str:
        return f"{parent}::{child}::{alias}"

    def _generate_duplicate_alias(self, host: HostEntry) -> str:
        base = f"{host.alias}-copy"
        candidate = base
        counter = 2
        existing = {
            h.alias
            for groups in self.manager.tree.values()
            for group in groups.values()
            for h in group.hosts
        }
        while candidate in existing:
            candidate = f"{base}-{counter}"
            counter += 1
        return candidate

    def _update_toolbar_state(self) -> None:
        hosts = self._selected_hosts()
        kind, data = self._current_item()
        has_hosts = bool(hosts)
        editable = True
        if hosts:
            editable = not self._host_is_read_only(hosts[0])
        if not has_hosts:
            if kind == "child":
                parent = data.get("parent", "")
                child = data.get("child", "")
                has_hosts = bool(self._collect_hosts_for_child_group(parent, child))
            elif kind == "parent":
                parent = data.get("parent", "")
                has_hosts = bool(self._collect_hosts_for_parent_group(parent))
        for name in ("host-edit", "host-duplicate", "host-move", "host-delete"):
            action = self.lookup_action(name)
            if action:
                action.set_enabled(has_hosts and editable)
        # Host selection state preserved for potential future toolbar controls
        # Bulk bar handled separately.

    def _log_event(self, message: str, success: bool) -> None:
        self._status_label.set_text(message)
        context = self._status_label.get_style_context()
        context.remove_class("error")
        context.remove_class("success")
        context.add_class("success" if success else "error")
        self._status_revealer.show()
        self._status_revealer.set_reveal_child(True)
        self._bulk_revealer.set_reveal_child(len(self._selected_hosts()) >= 2)
        if self._status_timeout_id:
            GLib.source_remove(self._status_timeout_id)
        self._status_timeout_id = GLib.timeout_add_seconds(5, self._hide_status)

    def _hide_status(self) -> bool:
        self._status_revealer.set_reveal_child(False)
        self._status_timeout_id = 0
        return GLib.SOURCE_REMOVE

    def _prompt_text(self, title: str, message: str, default: str = "") -> Optional[str]:
        dialog = Gtk.Dialog(title=title, transient_for=self, modal=True)
        dialog.add_button("_Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("_OK", Gtk.ResponseType.OK)
        dialog.set_default_response(Gtk.ResponseType.OK)

        box = dialog.get_content_area()
        box.set_border_width(16)
        box.set_spacing(12)
        box.pack_start(Gtk.Label(label=message), False, False, 0)
        entry = Gtk.Entry()
        entry.set_text(default)
        entry.set_activates_default(True)
        box.pack_start(entry, False, False, 0)

        dialog.show_all()
        response = dialog.run()
        text = entry.get_text().strip()
        dialog.destroy()
        if response == Gtk.ResponseType.OK and text:
            return text
        return None

    def _confirm(self, message: str) -> bool:
        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.NONE,
            text=message,
        )
        dialog.add_button("_Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("_Delete", Gtk.ResponseType.OK)
        dialog.set_default_response(Gtk.ResponseType.CANCEL)
        response = dialog.run()
        dialog.destroy()
        return response == Gtk.ResponseType.OK

    # -------------------------------------------------------------- UI state I/O
    def _apply_ui_font(self, size: int) -> None:
        clamped = max(10, min(48, int(size)))
        self._ui_font_size = clamped
        settings = Gtk.Settings.get_default()
        if not settings:
            return
        family = self._base_font_family or "Sans"
        desc = Pango.FontDescription()
        desc.set_family(family)
        desc.set_size(clamped * Pango.SCALE)
        settings.set_property("gtk-font-name", desc.to_string())

    def _apply_dark_theme(self, prefer_dark: bool) -> None:
        self._prefer_dark = prefer_dark
        settings = Gtk.Settings.get_default()
        if settings:
            settings.set_property("gtk-application-prefer-dark-theme", prefer_dark)

    def _load_ui_state(self) -> Dict[str, object]:
        if not self._ui_state_path.exists():
            return {}
        try:
            state = json.loads(self._ui_state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return state if isinstance(state, dict) else {}

    def _save_ui_state(self) -> None:
        try:
            self._ui_state_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            return
        width, height = self.get_size()
        sidebar_width = int(self._paned.get_position()) if self._sidebar_visible else self._sidebar_width_saved
        state = {
            "window_size": [int(width), int(height)],
            "is_maximized": self.is_maximized(),
            "sidebar_width": sidebar_width,
            "prefer_dark": self._prefer_dark,
            "font_scale": self.terminal_tabs.default_font_scale,
            "ui_font_size": self._ui_font_size,
            "sidebar_visible": self._sidebar_visible,
            "sidebar_prev_visible": self._sidebar_visible_before_max,
            "terminal_maximized": self._terminal_maximized,
            "group_colors": self._group_colors,
            "host_colors": self._host_colors,
        }
        try:
            write_json_atomic(self._ui_state_path, state, mode=0o600)
        except OSError:
            pass

    def _apply_ui_state(self) -> None:
        size = self._ui_state.get("window_size")
        if isinstance(size, list) and len(size) == 2:
            self.resize(int(size[0]), int(size[1]))
        if self._ui_state.get("is_maximized"):
            self.maximize()
        sidebar_width = int(self._ui_state.get("sidebar_width", 320))
        self._sidebar_width_saved = sidebar_width
        self._paned.set_position(sidebar_width)
        ui_font = self._ui_state.get("ui_font_size")
        if isinstance(ui_font, int):
            self._apply_ui_font(ui_font)
        visible = bool(self._ui_state.get("sidebar_visible", True))
        self._set_sidebar_visible(visible, initial=True)
        self._sidebar_visible_before_max = bool(
            self._ui_state.get("sidebar_prev_visible", self._sidebar_visible)
        )
        maximized = bool(self._ui_state.get("terminal_maximized", False))
        self._set_terminal_maximized(maximized, initial=True)

    def _register_actions(self) -> None:
        for name, callback in {
            "host-connect": self._connect_selected,
            "host-edit": self._edit_selected,
            "host-duplicate": self._duplicate_selected,
            "host-move": self._move_selected,
            "host-delete": self._delete_selected,
            "save-transcript": lambda: self.terminal_tabs.save_current_transcript(),
            "toggle-strip-ansi": lambda: self.terminal_tabs.toggle_strip_ansi_copy(),
            "copy-scp": self._copy_scp_command,
        }.items():
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda _act, _param, cb=callback: cb())
            self.add_action(action)

    # ------------------------------------------------------------------- signals
    def _on_delete_event(self, *_args) -> bool:
        self._save_ui_state()
        return False

    def _on_destroy(self, *_args) -> None:
        if self._status_timeout_id:
            GLib.source_remove(self._status_timeout_id)
        if self._shared_poll_id:
            GLib.source_remove(self._shared_poll_id)
            self._shared_poll_id = 0
        self._stop_git_watch_thread()
    def show_search_panel(self) -> None:
        if not self.search_revealer:
            return
        self.search_revealer.show_all()
        if not self.search_revealer.get_reveal_child():
            self.search_revealer.set_reveal_child(True)
        self._search_panel_escape_ready = False
        self._update_search_toggle_state()
        entry = getattr(self, "search_entry", None)

        def _focus() -> bool:
            if entry:
                entry.grab_focus()
                entry.select_region(0, -1)
            return GLib.SOURCE_REMOVE

        GLib.idle_add(_focus)

    def hide_search_panel(self) -> None:
        if not self.search_revealer:
            return
        if self.search_revealer.get_reveal_child():
            self.search_revealer.set_reveal_child(False)
        self._search_panel_escape_ready = False
        self._update_search_toggle_state()

    def clear_focus(self) -> None:
        focus_widget = self.get_focus()
        search_entry = getattr(self, "search_entry", None)
        filter_entry = getattr(self, "filter_entry", None)
        if focus_widget in {search_entry, filter_entry} and hasattr(self, "tree_view"):
            self.tree_view.grab_focus()

    def _toggle_search_panel(self) -> None:
        if not self.search_revealer:
            return
        if self.search_revealer.get_reveal_child():
            self.hide_search_panel()
        else:
            self.show_search_panel()

    def _on_search_panel_reveal(self, revealer: Gtk.Revealer, _param: GObject.ParamSpec) -> None:
        if revealer.get_child_revealed():
            revealer.show()
        else:
            revealer.hide()
        self._update_search_toggle_state()

    def _update_search_toggle_state(self) -> None:
        if not self._search_toggle_button or not self.search_revealer:
            return
        revealed = self.search_revealer.get_reveal_child()
        self._search_toggle_button.set_tooltip_text(
            "Hide search panel" if revealed else "Show search panel"
        )
        if self._search_toggle_icon:
            ctx = self._search_toggle_icon.get_style_context()
            if revealed:
                ctx.add_class("open")
            else:
                ctx.remove_class("open")

    def _on_window_size_allocate(self, _widget: Gtk.Widget, allocation: Gtk.Allocation) -> None:
        box = self._search_inputs_box
        if not box:
            return
        horizontal = allocation.width > 900
        orientation = Gtk.Orientation.HORIZONTAL if horizontal else Gtk.Orientation.VERTICAL
        if orientation == self._search_inputs_orientation:
            return
        box.set_orientation(orientation)
        box.set_homogeneous(horizontal)
        self._search_inputs_orientation = orientation
