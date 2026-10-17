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
from ssh_manager.gtkui.dialogs.config_viewer import ConfigViewerDialog
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
from ssh_manager.gtkui.theme.loader import ThemeMode, current_palette, get_theme_manager
from ssh_manager.gtkui.i18n import _
from ssh_manager.gtkui.theme.tokens import Level
from ssh_manager.gtkui.dialogs.confirm import confirm, show_message
from ssh_manager.gtkui.dialogs.base import is_accept
from ssh_manager.gtkui.dialogs.bulk_tag import BulkTagDialog
from ssh_manager.gtkui.dialogs.credentials import CredentialEditDialog, CredentialManagerDialog
from ssh_manager.gtkui.dialogs.files import choose_open_file, choose_save_file
from ssh_manager.gtkui.dialogs.host_dialog import HostDialog
from ssh_manager.gtkui.dialogs.preferences import PreferencesDialog, PreferencesValues
from ssh_manager.gtkui.dialogs.secrets import NewSecretDialog, UnlockDialog
from ssh_manager.gtkui.dialogs.vault import VaultDialog
from ssh_manager.gtkui.dialogs.group_picker import GroupPickerDialog
from ssh_manager.gtkui.dialogs.prompt import prompt_text
from ssh_manager.gtkui.validation import validate_child_path, validate_group_name
from ssh_manager.gtkui.controllers.notifications import ToastController
from ssh_manager.gtkui.controllers.shortcuts import build_shortcuts_window, register_actions
from ssh_manager.gtkui.controllers import details_presenter
from ssh_manager.gtkui.constants import Icons, RowKind, TreeColumn
from ssh_manager.gtkui.controllers import host_actions
from ssh_manager.gtkui.controllers.shortcuts import accel_label
from ssh_manager.gtkui.controllers.tree_controller import (
    TreeCallbacks,
    build_tree_rows,
    parse_filter,
    search_text_for_host,
    sidebar_page_for,
)
from ssh_manager.gtkui.views.session_tree import SessionTreeView, new_tree_store
from ssh_manager.gtkui.widgets.menus import build_context_menu, popup_menu
from ssh_manager.gtkui.dialogs.files import choose_folder
from ssh_manager.gtkui.views.details import DetailsView
from ssh_manager.gtkui.views.header import HeaderView
from ssh_manager.gtkui.views.sidebar import SidebarView, TreePage
from ssh_manager.gtkui.widgets.toast import ToastOverlay
from ssh_manager.ssh_command import build_ssh_command
from ssh_manager.gtkui.theme.terminal_schemes import SCHEMES as TERMINAL_SCHEMES, resolve_terminal_colors

UI_STATE_FILE = ".ui_state.json"
DND_TARGET = "ssh-manager/tree-row"

LOGGER = logging.getLogger(__name__)


def ui_state_path_for_base(base_dir: Path) -> Path:
    return (base_dir.parent if base_dir.is_file() else base_dir) / UI_STATE_FILE


def _int_state(value: object, default: int) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _float_state(value: object, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value)


def _system_font_family() -> str:
    settings = Gtk.Settings.get_default()
    if settings:
        font_name = settings.get_property("gtk-font-name") or ""
        if font_name:
            family = Pango.FontDescription(font_name).get_family()
            if family:
                return family
    return "Sans"


def audit_log_key(value: object) -> str:
    text = str(value).strip().lower()
    cleaned = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in text)
    return cleaned.strip("_") or "field"


def audit_log_value(value: object, *, default: str = "") -> str:
    text = " ".join(str(value).split())
    return text.replace(" ", "_") or default


@dataclass
class UIHostConfig:
    host: str
    port: int
    username: str
    timeout: float
    remember_password_default: bool
    parent_window: Gtk.Window
    options: Dict[str, str] = field(default_factory=dict)


class SSHManagerApplication(Gtk.Application):
    """GTK application entry point."""

    def __init__(self, base_dir: Path) -> None:
        Gtk.Application.__init__(self, application_id="org.codex.SSHManager")
        self._base_dir = base_dir
        self._window: Optional[SSHManagerWindow] = None
        self._theme = get_theme_manager()
        self._settings = AppSettings.load()
        self._master_password: Optional[str] = None

    def do_startup(self) -> None:  # type: ignore[override]
        Gtk.Application.do_startup(self)
        self._install_css(None)

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
        stored = self._settings.master_password_hash
        password = UnlockDialog(
            None,
            _("Unlock SSH Manager"),
            _("Enter your master password"),
            _("It protects saved SSH credentials."),
            lambda text: verify_master_password(text, stored),
            error_text=_("Incorrect master password. Please try again."),
        ).ask()
        if password is None:
            return False
        if master_hash_needs_upgrade(stored):
            self._settings.master_password_hash = hash_master_password(password)
            self._settings.save()
        self._master_password = password
        return True

    def _prompt_set_master_password(self) -> bool:
        password = NewSecretDialog(
            None,
            _("Set Master Password"),
            _("Create a master password"),
            _("It unlocks SSH Manager and encrypts passwords stored on this computer."),
            accept_label=_("_Save"),
        ).ask()
        if password is None:
            return False
        self._settings.master_password_hash = hash_master_password(password)
        self._settings.save()
        self._master_password = password
        return True

    def _install_css(self, prefer_dark: Optional[bool] = True) -> None:
        """Load the stylesheet; ``True``/``False`` force dark/light, ``None`` follows the system."""

        mode = ThemeMode.SYSTEM if prefer_dark is None else ThemeMode.from_prefer_dark(prefer_dark)
        self._theme.apply(mode)

    def set_theme_mode(self, mode: ThemeMode) -> bool:
        """Switch the theme at runtime. Returns whether the dark variant is active."""

        return self._theme.apply(mode)

    def do_shutdown(self) -> None:  # type: ignore[override]
        self._theme.shutdown()
        Gtk.Application.do_shutdown(self)


class SSHManagerWindow(Gtk.ApplicationWindow):
    """Main window for the SSH manager."""

    # Tree column indexes (aliases of TreeColumn kept for compatibility).
    COLUMN_ICON = TreeColumn.ICON
    COLUMN_TEXT = TreeColumn.TEXT
    COLUMN_KIND = TreeColumn.KIND
    COLUMN_PARENT = TreeColumn.PARENT
    COLUMN_CHILD = TreeColumn.CHILD
    COLUMN_ALIAS = TreeColumn.ALIAS
    COLUMN_HOST = TreeColumn.HOST
    COLUMN_SEARCH = TreeColumn.SEARCH
    COLUMN_COLOR = TreeColumn.COLOR
    COLUMN_STATUS = TreeColumn.STATUS
    COLUMN_STATUS_COLOR = TreeColumn.STATUS  # now stores the status name, not a color
    ICON_GROUP = Icons.GROUP
    ICON_SUBGROUP = Icons.SUBGROUP
    ICON_HOST = Icons.HOST

    def __init__(
        self,
        app: Gtk.Application,
        base_dir: Path,
        settings: AppSettings,
        *,
        master_password: Optional[str] = None,
    ) -> None:
        Gtk.ApplicationWindow.__init__(self, application=app)
        self.set_title(_("SSH Manager"))
        self.set_default_size(1280, 820)
        self.set_size_request(960, 600)
        self.set_name("ssh-manager-window")
        self.get_style_context().add_class("sm-window")

        self._init_services(base_dir, settings, master_password)
        self._init_state()
        self._build_ui()
        self._register_actions()
        self._apply_ui_state()
        self._rebuild_tree(refresh=True)
        self._refresh_git_watch()
        self._warn_if_password_file_locked()

        self.connect("delete-event", self._on_delete_event)
        self.connect("destroy", self._on_destroy)

    def _init_services(self, base_dir: Path, settings: AppSettings, master_password: Optional[str]) -> None:
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
        self._toasts = ToastController()
        self._shared_manager: Optional[ConfigManager] = None
        self._shared_path: Optional[Path] = None
        self._read_only_keys: set[str] = set()
        self._shared_poll_id: int = 0
        self._shared_mtime: Optional[float] = None
        self._shared_missing_warned = False

    def _init_state(self) -> None:
        state = self._ui_state
        colors = state.get("group_colors", {})
        self._group_colors: Dict[str, str] = (
            {str(k): str(v) for k, v in colors.items()} if isinstance(colors, dict) else {}
        )
        host_colors = state.get("host_colors", {})
        self._host_colors: Dict[str, str] = (
            {str(k): str(v) for k, v in host_colors.items()} if isinstance(host_colors, dict) else {}
        )
        self._color_pixbuf_cache: Dict[str, GdkPixbuf.Pixbuf] = {}
        self._theme_mode = ThemeMode.coerce(
            state.get("theme_mode"),
            ThemeMode.from_prefer_dark(state.get("prefer_dark", True)),
        )
        self._prefer_dark = self._theme_mode is ThemeMode.DARK
        self._ui_font_size = _int_state(state.get("ui_font_size"), 14)
        self._base_font_family = _system_font_family()
        self._apply_ui_font(self._ui_font_size)
        self._host_lookup: Dict[str, HostEntry] = {}
        self._host_status: Dict[str, str] = {}
        self._host_iters: Dict[str, Gtk.TreeIter] = {}
        last_filter = state.get("filter_query")
        self._filter_query: str = last_filter if isinstance(last_filter, str) else ""
        self._filter_error: str = ""
        self._smart_groups: List[SmartGroup] = load_smart_groups()
        self._git_watch_thread: Optional[threading.Thread] = None
        self._git_watch_stop = threading.Event()
        self._git_updates_available = False
        self._git_watch_path: Optional[Path] = None
        self._sidebar_visible = bool(state.get("sidebar_visible", True))
        self._sidebar_width_saved = _int_state(state.get("sidebar_width"), 320)
        self._sidebar_container: Optional[Gtk.Widget] = None
        self._sidebar_visible_before_max = self._sidebar_visible
        self._terminal_maximized = False
        self._details_visible_before_max = True
        self._compact = False
        self.tree_store = new_tree_store()

    def _warn_if_password_file_locked(self) -> None:
        backend = self._secure_store.backend
        if isinstance(backend, FileKeyring) and backend.locked:
            self._notify_warning(
                _("Saved passwords are encrypted but could not be unlocked; enable the master password to use them.")
            )

    # ------------------------------------------------------------------ setup UI
    def _build_ui(self) -> None:
        self._build_header()
        self.tree_view = self._build_tree_view()
        self._sidebar = self._build_sidebar(self.tree_view)
        self._sidebar_container = self._sidebar

        default_font = _float_state(self._ui_state.get("font_scale"), 1.0)
        self.terminal_tabs = TerminalNotebook(
            self._log_event,
            default_font_scale=default_font,
            password_store=self._secure_store,
            password_update_handler=self._save_detected_password_change,
            appearance=self._settings.terminal_appearance,
        )
        self.terminal_tabs.set_name("terminal-area")
        self.terminal_tabs.on_quick_connect = self._quick_connect
        self.terminal_tabs.on_new_host = lambda: self.prompt_new_host()
        self.terminal_tabs.on_duplicate_session = lambda tab: self._open_hosts([tab.host])

        self._details = DetailsView()
        self._details.on_expanded_changed = lambda _expanded: self._save_ui_state()

        right_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        right_box.pack_start(self.terminal_tabs, True, True, 0)
        right_box.pack_start(self._details, False, False, 0)

        self._paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self._paned.pack1(self._sidebar, resize=False, shrink=False)
        self._paned.pack2(right_box, resize=True, shrink=False)

        self._toast_overlay = ToastOverlay(self._toasts)
        self._toast_overlay.add(self._paned)
        self.add(self._toast_overlay)

        self._apply_theme_mode(self._theme_mode)
        get_theme_manager().add_listener(self._on_theme_changed)
        self.show_all()
        self.connect("size-allocate", self._on_window_size_allocate)

    def _build_header(self) -> None:
        templates = [(t.label, f"win.new-host-template('{t.id}')") for t in built_in_templates()]
        self._header = HeaderView(self._toasts.history, template_items=templates)
        self._header.set_inventory_path(str(self.manager.base_dir))
        self.set_titlebar(self._header)
        self._sidebar_button_handler = self._header.sidebar_button.connect("toggled", self._on_sidebar_button_toggled)
        self._maximize_button_handler = self._header.maximize_button.connect("toggled", self._on_maximize_button_toggled)

    def _build_sidebar(self, tree_view: Gtk.TreeView) -> SidebarView:
        sidebar = SidebarView(tree_view)
        sidebar.on_search_changed = lambda _text: self._rebuild_tree(refresh=False)
        sidebar.on_filter_changed = self._on_filter_text_changed
        sidebar.on_smart_group_activated = self._on_smart_group_activated
        sidebar.on_save_smart_group = self._save_current_filter_as_group
        sidebar.on_delete_smart_group = self._delete_smart_group
        sidebar.on_add_host = lambda: self.prompt_new_host()
        sidebar.on_open_folder = self._open_inventory_folder
        sidebar.on_escape_to_tree = lambda: self.tree_view.grab_focus()
        sidebar.set_filter_text(self._filter_query)
        sidebar.set_filter_active(self._filter_query)
        sidebar.set_smart_groups([group.name for group in self._smart_groups])
        return sidebar

    def _build_tree_view(self) -> SessionTreeView:
        tree_view = SessionTreeView(self.tree_store)
        tree_view.connect("row-activated", self._on_row_activated)
        tree_view.connect("button-press-event", self._on_tree_button_press)
        tree_view.connect("key-press-event", self._on_tree_key_press)
        tree_view.get_selection().connect("changed", self._on_selection_changed)
        tree_view.on_context_menu = self._show_context_menu_for_selection
        self.tree_view = tree_view
        self._configure_drag_and_drop()
        return tree_view

    # ------------------------------------------------------------ sidebar state
    def _toggle_sidebar(self) -> None:
        if self._terminal_maximized:
            self._set_terminal_maximized(False)
        self._set_sidebar_visible(not self._sidebar_visible)

    def _on_sidebar_button_toggled(self, button: Gtk.ToggleButton) -> None:
        if button.get_active() != self._sidebar_visible:
            if self._terminal_maximized and button.get_active():
                self._set_terminal_maximized(False)
            self._set_sidebar_visible(button.get_active())

    def _update_sidebar_toggle_ui(self) -> None:
        button = self._header.sidebar_button
        if button.get_active() != self._sidebar_visible:
            button.handler_block(self._sidebar_button_handler)
            button.set_active(self._sidebar_visible)
            button.handler_unblock(self._sidebar_button_handler)

    def _set_sidebar_visible(self, visible: bool, *, initial: bool = False, persist: bool = True) -> None:
        if self._sidebar_container is None:
            self._sidebar_visible = visible
            return
        if visible == self._sidebar_visible and not initial:
            self._update_sidebar_toggle_ui()
            return
        if visible:
            self._sidebar_container.show()
            width = max(220, self._sidebar_width_saved)
            GLib.idle_add(self._paned.set_position, width)
        else:
            if self._sidebar_visible:
                self._sidebar_width_saved = max(220, self._paned.get_position())
            self._sidebar_container.hide()
        self._sidebar_visible = visible
        if not self._terminal_maximized and persist:
            self._sidebar_visible_before_max = visible
        self._update_sidebar_toggle_ui()
        if not initial and persist:
            self._save_ui_state()

    # ---------------------------------------------------------- maximize state
    def _toggle_terminal_maximized(self) -> None:
        self._set_terminal_maximized(not self._terminal_maximized)

    def _on_maximize_button_toggled(self, button: Gtk.ToggleButton) -> None:
        if button.get_active() != self._terminal_maximized:
            self._set_terminal_maximized(button.get_active())

    def _update_terminal_toggle_ui(self) -> None:
        button = self._header.maximize_button
        if button.get_active() != self._terminal_maximized:
            button.handler_block(self._maximize_button_handler)
            button.set_active(self._terminal_maximized)
            button.handler_unblock(self._maximize_button_handler)
        self._header.set_maximized(self._terminal_maximized)

    def _set_terminal_maximized(self, maximized: bool, *, initial: bool = False) -> None:
        if maximized == self._terminal_maximized and not initial:
            self._update_terminal_toggle_ui()
            return
        self._terminal_maximized = maximized
        if maximized:
            self._sidebar_visible_before_max = self._sidebar_visible
            self._details_visible_before_max = self._details.get_visible()
            self._details.hide()
            if self._sidebar_visible_before_max:
                self._set_sidebar_visible(False, persist=False)
        else:
            if self._details_visible_before_max:
                self._details.show()
            if self._sidebar_visible_before_max:
                self._set_sidebar_visible(True, persist=False)
        self._update_terminal_toggle_ui()
        self._update_sidebar_toggle_ui()
        if not initial:
            self._save_ui_state()

    def _pick_group(self, title: str, **kwargs) -> Optional[Tuple[str, str, bool]]:
        """Run a :class:`GroupPickerDialog`; return ``(parent, child, create_child)``."""

        picker = GroupPickerDialog(self, self.manager, title, **kwargs)
        try:
            return picker.selected() if is_accept(picker.run()) else None
        finally:
            picker.destroy()

    def _run_host_dialog(self, dialog: HostDialog, parent: str, child: str) -> Tuple[Optional[HostEntry], Optional[str]]:
        """Run ``dialog``; return ``(host, credential_id)`` (``host`` is None when cancelled)."""

        try:
            if not is_accept(dialog.run()):
                return None, None
            return dialog.build_host(parent, child), dialog.selected_credential_id()
        finally:
            dialog.destroy()

    def _host_dialog(self, host: Optional[HostEntry] = None, **kwargs) -> HostDialog:
        binding = self._credential_store.binding_for_host(self._host_key(host)) if host else None
        return HostDialog(
            self,
            host,
            credentials=self._credential_store.list(),
            selected_credential_id=binding,
            metadata=host.metadata if host else {},
            vault_entries=self._vault.entries() if self._vault.unlocked else [],
            vault_unlocked=self._vault.unlocked,
            profiles=self._profiles,
            **kwargs,
        )

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
        selection = self._pick_group(_("Move Selected Hosts"), allow_new_child=True, accept_label=_("_Move"))
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
        result = BulkTagDialog(self, len(hosts)).ask()
        if result is None:
            return
        tags_to_add, color_val = result
        tag_val = ",".join(tags_to_add)

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

    def _inventories(self):
        inventories = [(self.manager.tree, False)]
        if self._shared_manager:
            inventories.append((self._shared_manager.tree, True))
        return inventories

    def _rebuild_tree(self, *, refresh: bool = False) -> None:
        if refresh:
            self.manager.refresh()
        query = self._sidebar.search_text
        terms, error = parse_filter(self._filter_query)
        self._filter_error = error
        self._sidebar.set_filter_error(error)
        inventories = self._inventories()
        rows = build_tree_rows(
            inventories,
            query,
            terms,
            TreeCallbacks(
                display_user=self._display_username_for_host,
                status=lambda key: self._host_status.get(key, "unknown"),
                color=self._item_color_for,
            ),
        )
        selected = self._current_item()
        self._host_lookup.clear()
        self._read_only_keys = set()
        for top in rows:
            for row in top.walk():
                if row.kind is RowKind.HOST and row.host is not None:
                    self._host_lookup[row.key] = row.host
                    self._host_status.setdefault(row.key, "unknown")
                    if row.shared:
                        self._read_only_keys.add(row.key)
        self._host_iters = self.tree_view.populate(rows)
        self._restore_selection(*selected)
        self._sidebar.set_page(TreePage(sidebar_page_for(inventories, rows, bool(query or terms))))
        self._update_details()
        self._update_toolbar_state()

    def _restore_selection(self, kind: str, data: Dict[str, str]) -> None:
        row_kind = RowKind.coerce(kind)
        if row_kind is RowKind.HOST:
            self._select_host(data.get("parent", ""), data.get("child", ""), data.get("alias", ""))
        elif row_kind is RowKind.CHILD:
            self._select_child_group(data.get("parent", ""), data.get("child", ""))
        elif row_kind is RowKind.PARENT:
            self._select_parent_group(data.get("parent", ""))

    def _host_has_plaintext_password(self, host: HostEntry) -> bool:
        for key, value in host.options.items():
            if value and "password" in key.lower():
                return True
        return False

    def _confirm_policy_override(self, message: str) -> bool:
        return confirm(
            self,
            _("Save plain-text password?"),
            message,
            confirm_label=_("_Save Anyway"),
            destructive=True,
        )

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
        return search_text_for_host(host, self._display_username_for_host(host))

    def _refresh_smart_group_list(self) -> None:
        self._sidebar.set_smart_groups([group.name for group in self._smart_groups])

    def _on_filter_text_changed(self, text: str) -> None:
        self._filter_query = text.strip()
        self._sidebar.set_filter_active(self._filter_query)
        self._rebuild_tree(refresh=False)

    def _on_smart_group_activated(self, index: int) -> None:
        if index < 0 or index >= len(self._smart_groups):
            return
        group = self._smart_groups[index]
        self._sidebar.set_filter_text(group.query)
        self._on_filter_text_changed(group.query)

    def _save_current_filter_as_group(self) -> None:
        if not self._filter_query:
            self._sidebar.set_filter_error(_("Enter a filter to save it as a smart group."))
            return
        name = self._prompt_text(
            _("Save Smart Group"),
            _("Name for the filter “{query}”:").format(query=self._filter_query),
            accept_label=_("_Save"),
        )
        if not name:
            return
        self._smart_groups.append(SmartGroup(name=name, query=self._filter_query))
        if not save_smart_groups(self._smart_groups):
            self._smart_groups.pop()
            self._notify_error(_("Could not save the smart group."))
            return
        self._refresh_smart_group_list()
        self._notify_info(_("Saved smart group “{name}”.").format(name=name))

    def _delete_smart_group(self, index: int) -> None:
        if index < 0 or index >= len(self._smart_groups):
            return
        group = self._smart_groups[index]
        if not self._confirm(
            _("Delete smart group “{name}”?").format(name=group.name),
            detail=_("Only the saved filter is deleted; hosts are not affected."),
            confirm_label=_("_Delete Smart Group"),
        ):
            return
        removed = self._smart_groups.pop(index)
        if not save_smart_groups(self._smart_groups):
            self._smart_groups.insert(index, removed)
            self._notify_error(_("Could not delete the smart group."))
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
        return current_palette().status_color(state)

    def _set_host_status_for_key(self, key: str, state: str) -> None:
        state = state if state in {"connected", "connecting", "failed", "unknown"} else "unknown"
        self._host_status[key] = state
        tree_iter = self._host_iters.get(key)
        if tree_iter:
            try:
                self.tree_store.set_value(tree_iter, self.COLUMN_STATUS, state)
            except Exception:
                pass
        details = getattr(self, "_details", None)
        if details is not None and self._selected_host() is not None:
            self._update_details()

    def _set_host_status(self, host: HostEntry, state: str) -> None:
        self._set_host_status_for_key(self._host_key(host), state)

    def _bind_tab_status(self, tab, host: HostEntry) -> None:
        host_key = self._host_key(host)
        notified = {"connected": False}

        def on_status(tab, _alias, connected: bool) -> None:
            status = getattr(tab, "status", None)
            state = status.value if status is not None else ("connected" if connected else "unknown")
            self._set_host_status_for_key(host_key, state)
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
        if not self._sidebar_visible:
            self._set_sidebar_visible(True)
        self._sidebar.focus_search()

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
                if not confirm(
                    self,
                    _("Connect to “{alias}” anyway?").format(alias=host_obj.alias),
                    _("Connecting is disabled for this host by its local permissions."),
                    confirm_label=_("C_onnect Anyway"),
                    destructive=True,
                ):
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
        self._notify(message, Level.INFO)

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
        self._notify(message, Level.WARNING)

    def _notify_error(self, message: str) -> None:
        self._notify(message, Level.ERROR)

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
    def _on_tree_key_press(self, _widget: Gtk.TreeView, event: Gdk.EventKey) -> bool:
        modifiers = event.state & Gtk.accelerator_get_default_mod_mask()
        if event.keyval == Gdk.KEY_Escape and not modifiers:
            if self.terminal_tabs.broadcast_active:
                self.terminal_tabs.set_broadcast_mode(False)
                return True
            if self._sidebar.search_text:
                self._sidebar.search_entry.set_text("")
                return True
            self.tree_view.get_selection().unselect_all()
            return True
        if event.keyval in (Gdk.KEY_Delete, Gdk.KEY_KP_Delete) and not modifiers:
            self._on_delete_clicked()
            return True
        return False

    def _on_selection_changed(self, *_args) -> None:
        self._update_details()
        self._update_toolbar_state()

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
        elif view.row_expanded(path):
            view.collapse_row(path)
        else:
            view.expand_row(path, False)

    def _on_tree_button_press(self, widget: Gtk.TreeView, event: Gdk.EventButton) -> bool:
        if event.button == 1:
            if not widget.get_path_at_pos(int(event.x), int(event.y)):
                widget.get_selection().unselect_all()
            return False
        if event.button != 3 or event.type != Gdk.EventType.BUTTON_PRESS:
            return False
        pos = widget.get_path_at_pos(int(event.x), int(event.y))
        if pos:
            path, column, _x, _y = pos
            widget.grab_focus()
            selection = widget.get_selection()
            # Preserve multi-selection when right-clicking on already-selected rows.
            if not selection.path_is_selected(path):
                widget.set_cursor(path, column, False)
        else:
            widget.get_selection().unselect_all()
        self._show_context_menu_for_selection(event)
        return True

    def _menu_accels(self) -> Dict[str, str]:
        return {name: accel_label(name) for name in ("host-connect", "rename", "host-delete", "new-host", "quick-connect")}

    def _context_menu_spec(self):
        kind_value, data = self._current_item()
        kind = RowKind.coerce(kind_value)
        accels = self._menu_accels()
        if kind is RowKind.HOST:
            host = self._selected_host()
            perms = self._permissions_for_host(host) if host else {}
            can_edit = bool(host) and not self._host_is_read_only(host)
            has_session = bool(host and self.terminal_tabs.find_tab(host))
            return host_actions.host_menu(self, can_edit=can_edit and perms.get("can_edit", True), has_session=has_session, accels=accels)
        if kind is not None:
            parent = data.get("parent", "")
            child = data.get("child", "")
            if kind is RowKind.PARENT:
                has_hosts = bool(self._collect_hosts_for_parent_group(parent))
            else:
                has_hosts = bool(self._collect_hosts_for_child_group(parent, child))
            return host_actions.group_menu(
                self, kind, read_only=parent.startswith("[Shared] "), has_hosts=has_hosts, accels=accels
            )
        return host_actions.blank_menu(self, accels=accels)

    def _show_context_menu_for_selection(self, event: Optional[Gdk.Event] = None) -> None:
        menu = build_context_menu(self._context_menu_spec(), attach_to=self.tree_view)
        if event is not None:
            popup_menu(menu, event)
            return
        rect = self.tree_view.selected_row_rect()
        window = self.tree_view.get_bin_window()
        if rect is not None and window is not None:
            menu.popup_at_rect(window, rect, Gdk.Gravity.SOUTH_WEST, Gdk.Gravity.NORTH_WEST, None)
        else:
            popup_menu(menu, None, widget=self.tree_view)

    def _reconnect_selected(self) -> None:
        host = self._selected_host()
        tab = self.terminal_tabs.find_tab(host) if host else None
        if tab is None:
            self._connect_selected()
            return
        self.terminal_tabs.focus_tab(tab)
        tab.reconnect()

    def _change_host_color(self) -> None:
        """Pick a color for the selected host (not its group)."""

        self._change_group_color()

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
        self._details.set_content(self._detail_content())

    def _detail_content(self):
        hosts = self._selected_hosts()
        if len(hosts) > 1:
            return details_presenter.multi_detail(hosts, self)
        kind_value, data = self._current_item()
        kind = RowKind.coerce(kind_value)
        if kind is RowKind.HOST:
            host = self._selected_host()
            if not host:
                return details_presenter.empty_detail()
            perms = self._permissions_for_host(host)
            key = self._make_host_key(data.get("parent", ""), data.get("child", ""), data.get("alias", ""))
            profile = self._profile_for_host(host)
            return details_presenter.host_detail(
                host,
                self,
                display_user=self._display_username_for_host(host),
                status=self._host_status.get(key, "unknown"),
                profile_name=profile.name if profile else "",
                permissions_text=self._render_permissions(host),
                read_only=self._host_is_read_only(host),
                can_view=perms.get("can_view", True),
                can_connect=perms.get("can_connect", True),
            )
        if kind is not None:
            parent = data.get("parent", "")
            child = data.get("child", "")
            if kind is RowKind.PARENT:
                children = self.manager.tree.get(parent, {})
                host_count = sum(len(group.hosts) for group in children.values())
                path = str(self.manager.base_dir / parent)
                return details_presenter.group_detail(
                    kind, parent, child, self, host_count=host_count, child_count=len(children), path=path,
                    read_only=parent.startswith("[Shared] "),
                )
            host_count = len(self._collect_hosts_for_child_group(parent, child))
            group = self.manager.tree.get(parent, {}).get(child)
            path = str(group.path) if group else str(self.manager.base_dir / parent / child)
            return details_presenter.group_detail(
                kind, parent, child, self, host_count=host_count, path=path,
                read_only=parent.startswith("[Shared] "),
            )
        return details_presenter.empty_detail()

    # ------------------------------------------------------------ toolbar actions
    def _on_refresh_clicked(self) -> None:
        self._rebuild_tree(refresh=True)

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
            self._theme_mode,
            self.terminal_tabs.default_font_scale,
            self._ui_font_size,
            self._settings,
            on_reset=self._reset_master_password_flow,
            on_export=self._export_settings_snapshot,
        )
        try:
            values = dialog.values() if is_accept(dialog.run()) else None
        finally:
            dialog.destroy()
        if values is not None:
            self._apply_preferences(values)

    def _apply_preferences(self, values: PreferencesValues) -> None:
        self._apply_theme_mode(values.theme_mode)
        self.terminal_tabs.set_default_font_scale(values.terminal_font_scale)
        self.terminal_tabs.apply_font_scale_to_all(values.terminal_font_scale)
        self._apply_ui_font(values.ui_font_size)
        self._ui_state["theme_mode"] = values.theme_mode.value
        self._ui_state["font_scale"] = values.terminal_font_scale
        self._ui_state["ui_font_size"] = self._ui_font_size
        settings = self._settings
        master_toggled = values.master_password_disabled != settings.master_password_disabled
        settings.remember_password_default = values.remember_password_default
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
        settings.save()
        self._save_ui_state()
        if master_toggled:
            if values.master_password_disabled:
                self._rekey_password_file(None)
            elif self._master_password:
                self._rekey_password_file(self._master_password)
            elif self._secure_store.uses_file_backend:
                self._notify_info(_("Saved passwords will be encrypted after the next master password unlock."))
        self.terminal_tabs.set_appearance(values.terminal_appearance)
        self._refresh_git_watch()
        self._notify(_("Settings saved."), Level.SUCCESS)

    def _export_settings_snapshot(self) -> None:
        timestamp = datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z"
        default_name = f"settings_export_{time.strftime('%Y%m%d_%H%M')}.json"
        target = choose_save_file(self, _("Export Settings & Policies"), current_name=default_name)
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
        CredentialManagerDialog(self, self._credential_store).run_and_destroy()
        self._update_details()

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
        ConfigViewerDialog(self, path).run_and_destroy()

    def _set_git_updates(self, available: bool) -> None:
        newly_available = available and not self._git_updates_available
        self._git_updates_available = available
        self._header.set_git_updates(available)
        if newly_available:
            self._notify(
                _("New commits are available in the tracked Git repository."),
                Level.INFO,
                action_label=_("_Details"),
                action=self._on_git_updates_clicked,
            )

    def _on_git_check(self) -> None:
        if self._git_updates_available:
            self._on_git_updates_clicked()
            return
        if not self._git_watch_path:
            self._notify(
                _("Git update checks are off. Enable them under Settings → Git Sync."),
                Level.INFO,
                action_label=_("Open _Settings"),
                action=self._on_settings_clicked,
            )
            return

        def worker() -> None:
            self._check_git_repo_for_updates()
            GLib.idle_add(self._report_git_check)

        threading.Thread(target=worker, daemon=True).start()
        self._notify_info(_("Checking {path} for updates…").format(path=self._git_watch_path))

    def _report_git_check(self) -> bool:
        if not self._git_updates_available:
            self._notify(_("The tracked repository is up to date."), Level.SUCCESS)
        return GLib.SOURCE_REMOVE

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
        VaultDialog(self, self._vault).run_and_destroy()
        self._update_details()

    def _on_git_updates_clicked(self) -> None:
        path_text = str(self._git_watch_path) if self._git_watch_path else ""
        message = "Remote updates are available. Run 'git pull' in the tracked directory to refresh."
        if path_text:
            message += f"\nDirectory: {path_text}"
        show_message(self, _("Git updates available"), message, level=Level.INFO)

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
        filename = choose_open_file(self, _("Open Shared Inventory"))
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

    def _reset_master_password_flow(self) -> None:
        stored = self._settings.master_password_hash
        if not stored:
            return
        dialog = NewSecretDialog(
            self,
            _("Change Master Password"),
            _("Choose a new master password"),
            _("Confirm the current password first. Passwords stored on this computer are re-encrypted."),
            accept_label=_("_Change"),
            verify_current=lambda text: verify_master_password(text, stored),
        )
        new_password = dialog.ask()
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
            selection = self._pick_group(
                _("Choose a Group for the New Host"),
                allow_new_child=True,
                initial_parent=parent,
                initial_child=child,
                accept_label=_("_Continue"),
            )
            if not selection:
                return
            parent, child, create_child = selection

        dialog = self._host_dialog(template_defaults=template.defaults if template else None)
        host, cred_id = self._run_host_dialog(dialog, parent, child)
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
        updated, cred_id = self._run_host_dialog(self._host_dialog(host), host.parent_group, host.child_group)
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
        dialog = self._host_dialog(host)
        dialog.set_alias(self._generate_duplicate_alias(host))
        dialog.set_title(_("Duplicate Host"))
        clone, cred_id = self._run_host_dialog(dialog, host.parent_group, host.child_group)
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
        selection = self._pick_group(
            _("Move Host “{alias}”").format(alias=host.alias),
            allow_new_child=True,
            initial_parent=host.parent_group,
            initial_child=host.child_group,
            accept_label=_("_Move"),
        )
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
        if not self._confirm(
            _("Delete host “{alias}”?").format(alias=host.alias),
            detail=_("The host entry is removed from {path}.").format(path=f"{host.parent_group}/{host.child_group}"),
            confirm_label=_("_Delete Host"),
        ):
            return
        success, msg = self.manager.delete_host(host.parent_group, host.child_group, host.alias)
        self._log_event(msg, success)
        if success:
            self._bind_credential_for_key(self._host_key(host), None)
            self._audit_log("delete", host)
            self._rebuild_tree(refresh=True)

    def _create_parent_group(self) -> None:
        name = self._prompt_text(
            _("New Group"),
            _("Name of the new top-level group (a folder under the config directory)."),
            validator=validate_group_name,
            accept_label=_("_Create"),
        )
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
        selection = self._pick_group(
            _("New Child Group"),
            allow_new_child=True,
            initial_parent=parent,
            initial_child=None,
            accept_label=_("_Create"),
        )
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
        host, cred_id = self._run_host_dialog(self._host_dialog(), parent, child)
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
        new_name = self._prompt_text(
            _("Rename Group"), _("New name for “{name}”:").format(name=current), current,
            validator=validate_group_name, accept_label=_("_Rename"),
        )
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
        new_name = self._prompt_text(
            _("Duplicate Group"), _("Name of the copy:"), default,
            validator=validate_group_name, accept_label=_("_Duplicate"),
        )
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
        new_name = self._prompt_text(
            _("Rename Child Group"), _("New file name for “{name}”:").format(name=current), current,
            validator=validate_child_path, accept_label=_("_Rename"),
        )
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
        new_name = self._prompt_text(
            _("Duplicate Child Group"), _("File name of the copy:"), default,
            validator=validate_child_path, accept_label=_("_Duplicate"),
        )
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
        if not self._confirm(
            _("Delete group “{name}”?").format(name=name),
            detail=_("All child groups and hosts in this folder are deleted. This cannot be undone."),
            confirm_label=_("_Delete Group"),
        ):
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
        if not self._confirm(
            _("Delete child group “{name}”?").format(name=f"{parent}/{child}"),
            detail=_("The config file and every host in it are deleted. This cannot be undone."),
            confirm_label=_("_Delete Child Group"),
        ):
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
        """Legacy two-state feedback hook (also used by the terminal notebook)."""

        self._notify(message, Level.SUCCESS if success else Level.ERROR)

    def _notify(self, message: str, level: Level = Level.INFO, *, action_label: str = "", action=None) -> None:
        if not message:
            return
        self._toasts.notify(message, level, action_label=action_label, action=action)

    def _prompt_text(
        self,
        title: str,
        message: str,
        default: str = "",
        *,
        validator=None,
        accept_label: str = "",
    ) -> Optional[str]:
        return prompt_text(self, title, message, default, validator=validator, accept_label=accept_label)

    def _confirm(
        self,
        message: str,
        *,
        detail: str = "",
        confirm_label: str = "",
        destructive: bool = True,
    ) -> bool:
        return confirm(
            self,
            message,
            detail,
            confirm_label=confirm_label or _("_Delete"),
            destructive=destructive,
        )

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
        self._apply_theme_mode(ThemeMode.DARK if prefer_dark else ThemeMode.LIGHT)

    def _apply_theme_mode(self, mode: ThemeMode) -> None:
        self._theme_mode = ThemeMode.coerce(mode)
        app = self.get_application()
        if app and hasattr(app, "set_theme_mode"):
            self._prefer_dark = app.set_theme_mode(self._theme_mode)
        else:
            self._prefer_dark = get_theme_manager().apply(self._theme_mode)

    def _on_theme_changed(self, _is_dark: bool) -> None:
        """Re-render rows whose colors come from the active palette."""

        self._color_pixbuf_cache.clear()
        if hasattr(self, "tree_view"):
            self.tree_view.clear_icon_cache()

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
            "theme_mode": getattr(getattr(self, "_theme_mode", None), "value", None)
            or ("dark" if self._prefer_dark else "light"),
            "font_scale": self.terminal_tabs.default_font_scale,
            "ui_font_size": self._ui_font_size,
            "sidebar_visible": self._sidebar_visible,
            "sidebar_prev_visible": self._sidebar_visible_before_max,
            "terminal_maximized": self._terminal_maximized,
            "details_expanded": bool(getattr(getattr(self, "_details", None), "expanded", True)),
            "filter_query": getattr(self, "_filter_query", ""),
            "group_colors": self._group_colors,
            "host_colors": self._host_colors,
        }
        try:
            write_json_atomic(self._ui_state_path, state, mode=0o600)
        except OSError:
            pass

    def _apply_ui_state(self) -> None:
        size = self._ui_state.get("window_size")
        if (
            isinstance(size, list)
            and len(size) == 2
            and all(isinstance(v, int) and not isinstance(v, bool) and v > 0 for v in size)
        ):
            self.resize(int(size[0]), int(size[1]))
        if self._ui_state.get("is_maximized") is True:
            self.maximize()
        sidebar_width = _int_state(self._ui_state.get("sidebar_width"), 320)
        self._sidebar_width_saved = sidebar_width
        self._paned.set_position(sidebar_width)
        ui_font = self._ui_state.get("ui_font_size")
        if isinstance(ui_font, int) and not isinstance(ui_font, bool):
            self._apply_ui_font(ui_font)
        self._details.set_expanded(self._ui_state.get("details_expanded", True) is not False, notify=False)
        visible = bool(self._ui_state.get("sidebar_visible", True))
        self._set_sidebar_visible(visible, initial=True)
        self._sidebar_visible_before_max = bool(
            self._ui_state.get("sidebar_prev_visible", self._sidebar_visible)
        )
        maximized = bool(self._ui_state.get("terminal_maximized", False))
        self._set_terminal_maximized(maximized, initial=True)

    def _register_actions(self) -> None:
        tabs = self.terminal_tabs
        callbacks = {
            "new-host": lambda: self.prompt_new_host(),
            "new-host-template": self._new_host_from_template,
            "new-group": self._create_parent_group,
            "new-child-group": self._create_child_group,
            "quick-connect": self._quick_connect,
            "focus-search": self.focus_search,
            "refresh": self._on_refresh_clicked,
            "settings": self._on_settings_clicked,
            "app-menu": self._header.open_app_menu,
            "shortcuts": self._show_shortcuts,
            "credentials": self._on_credentials_clicked,
            "vault": self._on_vault_clicked,
            "config-file": self._on_config_view_clicked,
            "open-shared": self._on_open_shared_clicked,
            "git-check": self._on_git_check,
            "about": self._show_about,
            "host-connect": self._on_connect_clicked,
            "host-edit": self._edit_selected,
            "host-duplicate": self._duplicate_selected,
            "host-move": self._move_selected,
            "host-delete": self._on_delete_clicked,
            "copy-ssh": self._copy_ssh_command,
            "copy-scp": self._copy_scp_command,
            "rename": self._rename_focused,
            "close-tab": tabs.close_current,
            "next-tab": tabs.next_tab,
            "prev-tab": tabs.prev_tab,
            "goto-tab": tabs.goto_tab,
            "duplicate-session": self._duplicate_current_session,
            "reconnect": tabs.reconnect_current,
            "save-transcript": tabs.save_current_transcript,
            "toggle-broadcast": tabs.toggle_broadcast,
            "toggle-tile": tabs.toggle_tile_mode,
            "split-pane": tabs.toggle_split_current,
            "toggle-stats": tabs.toggle_stats_current,
            "toggle-strip-ansi": tabs.toggle_strip_ansi_copy,
            "copy": tabs.copy_current,
            "paste": tabs.paste_current,
            "interrupt": tabs.interrupt_current,
            "font-increase": tabs.increase_font,
            "font-decrease": tabs.decrease_font,
            "font-reset": tabs.reset_font,
            "toggle-sidebar": self._toggle_sidebar,
            "toggle-details": self._details.toggle_expanded,
            "toggle-maximize": self._toggle_terminal_maximized,
        }
        self._actions = register_actions(self, callbacks)

    def _new_host_from_template(self, template_id: str) -> None:
        template = next((t for t in built_in_templates() if t.id == template_id), None)
        self.prompt_new_host(template=template)

    def _show_shortcuts(self) -> None:
        window = build_shortcuts_window(self)
        window.show_all()

    def _show_about(self) -> None:
        about = Gtk.AboutDialog(transient_for=self, modal=True)
        about.set_program_name(_("SSH Manager"))
        about.set_logo_icon_name("utilities-terminal")
        about.set_comments(_("Browse grouped OpenSSH configs, manage credentials and run embedded SSH sessions."))
        about.run()
        about.destroy()

    def _rename_focused(self) -> None:
        if self.tree_view.has_focus():
            self._on_edit_clicked()
        else:
            self.terminal_tabs.rename_current()

    def _duplicate_current_session(self) -> None:
        tab = self.terminal_tabs.current_tab()
        if tab is None:
            self._notify_info(_("Open a session first."))
            return
        self._open_hosts([tab.host])

    def _quick_connect(self) -> None:
        from ssh_manager.gtkui.dialogs.prompt import PromptDialog

        def validate(text: str) -> Optional[str]:
            if self._find_host_by_alias(text.strip()) is not None:
                return None
            if details_presenter.parse_quick_connect(text) is None:
                return _("Use [user@]host[:port] or the alias of a saved host.")
            return None

        value = PromptDialog(
            self,
            _("Quick Connect"),
            _("Connect to a saved host by alias, or to any server as [user@]host[:port]."),
            placeholder=_("deploy@web-01.example.com:22"),
            accept_label=_("C_onnect"),
            validator=validate,
        ).ask()
        if not value:
            return
        saved = self._find_host_by_alias(value)
        if saved is not None:
            self._open_hosts([saved])
            return
        parsed = details_presenter.parse_quick_connect(value)
        if parsed is None:
            return
        user, host, port = parsed
        options = {"hostname": host}
        if user:
            options["user"] = user
        if port:
            options["port"] = port
        adhoc = HostEntry(alias=value, options=options, parent_group=_("Quick Connect"), child_group="")
        self._open_hosts([adhoc])

    def _find_host_by_alias(self, alias: str) -> Optional[HostEntry]:
        for groups in self.manager.tree.values():
            for group in groups.values():
                for host in group.hosts:
                    if host.alias == alias:
                        return host
        return None

    def _open_inventory_folder(self) -> None:
        folder = choose_folder(self, _("Open SSH Config Folder"), current_folder=str(Path.home() / ".ssh"))
        if not folder:
            return
        self.manager = ConfigManager(Path(folder))
        self._header.set_inventory_path(str(self.manager.base_dir))
        self._rebuild_tree(refresh=False)
        self._notify_info(_("Loaded configuration from {path}.").format(path=folder))

    def _copy_ssh_command(self) -> None:
        host = self._selected_host()
        if not host:
            self._notify_info(_("Select a host to copy its SSH command."))
            return
        resolved = self._host_with_resolved_identity(host)
        command = shlex.join(build_ssh_command(resolved))
        Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(command, -1)
        self._notify(_("SSH command copied to the clipboard."), Level.SUCCESS)

    # ------------------------------------------------------------------- signals
    def _on_delete_event(self, *_args) -> bool:
        self._save_ui_state()
        return False

    def _on_destroy(self, *_args) -> None:
        get_theme_manager().remove_listener(self._on_theme_changed)
        self._toasts.shutdown()
        if self._shared_poll_id:
            GLib.source_remove(self._shared_poll_id)
            self._shared_poll_id = 0
        self._stop_git_watch_thread()
    def show_search_panel(self) -> None:
        """Focus the always-visible search entry (kept for compatibility)."""

        self.focus_search()

    def hide_search_panel(self) -> None:
        self.tree_view.grab_focus()

    def clear_focus(self) -> None:
        self.tree_view.grab_focus()

    def _on_window_size_allocate(self, _widget: Gtk.Widget, allocation: Gtk.Allocation) -> None:
        compact = allocation.width < 1100
        if compact != self._compact:
            self._compact = compact
            self._header.set_compact(compact)
