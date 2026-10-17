"""GTK application and main window (composition root).

``SSHManagerWindow`` builds the views (``gtkui/views``), creates the
controllers (``gtkui/controllers``) and wires them together. It holds the
shared services (config manager, stores, settings) and the persisted UI
state, but no business logic of its own.

Names that other modules and tests import from here are re-exported below.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, Optional

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Vte", "2.91")

from gi.repository import Gdk, GdkPixbuf, GLib, Gtk, Pango, Vte  # type: ignore  # noqa: E402,F401

from ssh_manager.config import ConfigManager  # noqa: E402
from ssh_manager.credentials import CredentialStore  # noqa: E402
from ssh_manager.gtk_terminal import TerminalNotebook  # noqa: E402
from ssh_manager.gtkui.constants import Icons, TreeColumn  # noqa: E402
from ssh_manager.gtkui.controllers.actions import WindowActions  # noqa: E402
from ssh_manager.gtkui.controllers.app_dialogs import AppDialogs  # noqa: E402
from ssh_manager.gtkui.controllers.colors import ColorController  # noqa: E402
from ssh_manager.gtkui.controllers.credentials_controller import CredentialController  # noqa: E402
from ssh_manager.gtkui.controllers.drag_drop import DND_TARGET, DragDropController  # noqa: E402,F401
from ssh_manager.gtkui.controllers.group_actions import GroupActions  # noqa: E402
from ssh_manager.gtkui.controllers.host_actions import HostActions  # noqa: E402
from ssh_manager.gtkui.controllers.inventory import InventoryController  # noqa: E402
from ssh_manager.gtkui.controllers.layout import LayoutController  # noqa: E402
from ssh_manager.gtkui.controllers.notifications import ToastController  # noqa: E402
from ssh_manager.gtkui.controllers.session_controller import (  # noqa: E402,F401
    SessionController,
    UIHostConfig,
    audit_log_key,
    audit_log_value,
)
from ssh_manager.gtkui.controllers.shortcuts import register_actions  # noqa: E402
from ssh_manager.gtkui.controllers.tree import TreeController  # noqa: E402
from ssh_manager.gtkui.controllers.ui_state import (  # noqa: E402,F401
    UI_STATE_FILE,
    UiState,
    load_ui_state,
    save_ui_state,
    ui_state_path_for_base,
)
from ssh_manager.gtkui.dialogs.confirm import confirm  # noqa: E402
from ssh_manager.gtkui.dialogs.prompt import prompt_text  # noqa: E402
from ssh_manager.gtkui.dialogs.secrets import NewSecretDialog, UnlockDialog  # noqa: E402
from ssh_manager.gtkui.i18n import _  # noqa: E402
from ssh_manager.gtkui.theme.loader import ThemeMode, get_theme_manager  # noqa: E402
from ssh_manager.gtkui.theme.tokens import Level  # noqa: E402
from ssh_manager.gtkui.views.details import DetailsView  # noqa: E402
from ssh_manager.gtkui.views.header import HeaderView  # noqa: E402
from ssh_manager.gtkui.views.session_tree import SessionTreeView, new_tree_store  # noqa: E402
from ssh_manager.gtkui.views.sidebar import SidebarView  # noqa: E402
from ssh_manager.gtkui.widgets.toast import ToastOverlay  # noqa: E402
from ssh_manager.master_auth import hash_master_password, master_hash_needs_upgrade, verify_master_password  # noqa: E402
from ssh_manager.password_dialog import PasswordDialog  # noqa: E402
from ssh_manager.profiles import SessionProfile, built_in_profiles  # noqa: E402
from ssh_manager.secure_store import SecureStore  # noqa: E402
from ssh_manager.settings import AppSettings  # noqa: E402
from ssh_manager.templates import built_in_templates  # noqa: E402
from ssh_manager.vault import CredentialVault, default_vault_path  # noqa: E402

# Re-exported dialogs (moved to ssh_manager.gtkui.dialogs).
from ssh_manager.gtkui.dialogs.config_viewer import ConfigViewerDialog  # noqa: E402,F401
from ssh_manager.gtkui.dialogs.credentials import CredentialEditDialog, CredentialManagerDialog  # noqa: E402,F401
from ssh_manager.gtkui.dialogs.group_picker import GroupPickerDialog  # noqa: E402,F401
from ssh_manager.gtkui.dialogs.host_dialog import HostDialog  # noqa: E402,F401
from ssh_manager.gtkui.dialogs.preferences import PreferencesDialog  # noqa: E402,F401
from ssh_manager.gtkui.dialogs.vault import VaultDialog  # noqa: E402,F401

from ssh_manager.gtkui.health import compute_health  # noqa: E402
from ssh_manager.gtkui.identity import local_user_initials  # noqa: E402
from ssh_manager.gtkui.theme.fonts import first_available  # noqa: E402
from ssh_manager.gtkui.workspaces import workspace_list  # noqa: E402
from ssh_manager.gtkui.theme.tokens import UI_FONT_FAMILIES  # noqa: E402
from ssh_manager.gtkui.widgets.icons import register_icon_path  # noqa: E402

LOGGER = logging.getLogger(__name__)


def _system_font_family() -> str:
    """The Relay UI family when installed, else the desktop font family."""

    relay = first_available(UI_FONT_FAMILIES)
    if relay:
        return relay
    settings = Gtk.Settings.get_default()
    if settings:
        font_name = settings.get_property("gtk-font-name") or ""
        if font_name:
            family = Pango.FontDescription(font_name).get_family()
            if family:
                return family
    return "Sans"


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
        register_icon_path()
        self._install_css(None)

    def do_activate(self) -> None:  # type: ignore[override]
        if not self._window:
            if not self._ensure_master_password():
                self.quit()
                return
            self._window = SSHManagerWindow(self, self._base_dir, self._settings, master_password=self._master_password)
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
    """Main window: composes views and controllers."""

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

        self.closing = False
        self._init_services(base_dir, settings, master_password)
        self._init_controllers()
        self._build_ui()
        self._register_actions()
        self._apply_ui_state()
        self.tree.rebuild(refresh=True)
        self.inventory.refresh_git_watch()
        self.creds.warn_if_password_file_locked()

        self.connect("delete-event", self._on_delete_event)
        self.connect("destroy", self._on_destroy)

    # ---------------------------------------------------------------- setup
    def _init_services(self, base_dir: Path, settings: AppSettings, master_password: Optional[str]) -> None:
        self.manager = ConfigManager(base_dir)
        self._settings = settings
        self._master_password = master_password
        self._secure_store = SecureStore.with_default_backend(master_password=master_password)
        self._credential_store = CredentialStore(self._secure_store)
        self._vault = CredentialVault(default_vault_path())
        self._password_dialog = PasswordDialog()
        self._profiles: Dict[str, SessionProfile] = {p.id: p for p in built_in_profiles()}
        self._toasts = ToastController()
        self._ui_state_path = ui_state_path_for_base(base_dir)
        self._ui = UiState.from_dict(self._load_ui_state())
        self._theme_mode = ThemeMode.coerce(self._ui.theme_mode, ThemeMode.from_prefer_dark(
            self._ui.prefer_dark if self._ui.prefer_dark is not None else True
        ))
        self._prefer_dark = self._theme_mode is ThemeMode.DARK
        self._base_font_family = _system_font_family()
        self._ui_font_size = self._ui.ui_font_size
        self._apply_ui_font(self._ui_font_size)
        self.tree_store = new_tree_store()

    def _init_controllers(self) -> None:
        self.actions = WindowActions(self)
        self.layout = LayoutController(self)
        self.colors = ColorController(self)
        self.creds = CredentialController(self)
        self.tree = TreeController(self)
        self.dnd = DragDropController(self)
        self.sessions = SessionController(self)
        self.hosts = HostActions(self)
        self.groups = GroupActions(self)
        self.inventory = InventoryController(self)
        self.app_dialogs = AppDialogs(self)

    def _build_ui(self) -> None:
        self._build_header()
        self.tree_view = self._build_tree_view()
        self._sidebar = self._build_sidebar(self.tree_view)
        self._sidebar_container = self._sidebar
        self.terminal_tabs = self._build_workspace()
        self._details = DetailsView()
        self._details.on_expanded_changed = lambda _expanded: self._save_ui_state()

        right_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        right_box.pack_start(self.terminal_tabs, True, True, 0)
        right_box.pack_start(self._details, False, False, 0)
        self._paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self._paned.get_style_context().add_class("main-paned")
        self._paned.set_wide_handle(False)
        self._paned.pack1(self._sidebar, resize=False, shrink=False)
        self._paned.pack2(right_box, resize=True, shrink=False)
        self._paned.connect("notify::position", self.layout.on_paned_position)
        self._toast_overlay = ToastOverlay(self._toasts)
        self._toast_overlay.add(self._paned)
        self.add(self._toast_overlay)

        self._apply_theme_mode(self._theme_mode)
        get_theme_manager().add_listener(self._on_theme_changed)
        self.show_all()
        self.connect("size-allocate", self.layout.on_size_allocate)

    def _build_header(self) -> None:
        self._header = HeaderView(self._toasts.history)
        self._header.set_user_initials(local_user_initials())
        self._header.on_workspace_selected = self.inventory.switch_workspace
        self.set_titlebar(self._header)
        self.refresh_workspaces()
        self._toasts.history.add_listener(self._update_health)

    def refresh_workspaces(self) -> None:
        shared = self.inventory.shared_path
        self._header.set_workspaces(workspace_list(self.manager.base_dir, self._ui.recent_inventories, shared))

    def _update_health(self) -> None:
        if not hasattr(self, "_header"):
            return
        history = self._toasts.history
        sessions = self.terminal_tabs.session_states() if hasattr(self, "terminal_tabs") else []
        self._header.set_health(
            compute_health(
                sessions,
                warnings=history.unread_count(levels={Level.WARNING}),
                errors=history.unread_count(levels={Level.ERROR}),
            )
        )

    def _build_sidebar(self, tree_view: Gtk.TreeView) -> SidebarView:
        templates = [(t.label, f"win.new-host-template('{t.id}')") for t in built_in_templates()]
        sidebar = SidebarView(tree_view, template_items=templates)
        sidebar.on_search_changed = lambda _text: self.tree.rebuild(refresh=False)
        sidebar.on_filter_changed = self.tree.on_filter_text_changed
        sidebar.on_smart_group_activated = self.tree.on_smart_group_activated
        sidebar.on_save_smart_group = self.tree.save_current_filter_as_group
        sidebar.on_delete_smart_group = self.tree.delete_smart_group
        sidebar.on_add_host = self.hosts.prompt_new_host
        sidebar.on_open_folder = self.inventory.open_inventory_folder
        sidebar.on_escape_to_tree = lambda: self.tree_view.grab_focus()
        sidebar.set_filter_text(self.tree.filter_query)
        sidebar.set_filter_active(self.tree.filter_query)
        sidebar.set_smart_groups([group.name for group in self.tree.smart_groups])
        return sidebar

    def _build_tree_view(self) -> SessionTreeView:
        tree_view = SessionTreeView(self.tree_store)
        tree_view.connect("row-activated", self.tree.on_row_activated)
        tree_view.connect("button-press-event", self.tree.on_button_press)
        tree_view.connect("key-press-event", self.tree.on_key_press)
        tree_view.get_selection().connect("changed", self.tree.on_selection_changed)
        tree_view.on_context_menu = self.tree.show_context_menu
        self.tree_view = tree_view
        self.dnd.configure()
        return tree_view

    def _build_workspace(self) -> TerminalNotebook:
        tabs = TerminalNotebook(
            self._log_event,
            default_font_scale=self._ui.font_scale,
            password_store=self._secure_store,
            password_update_handler=self.creds.save_detected_password_change,
            appearance=self._settings.terminal_appearance,
        )
        tabs.set_name("terminal-area")
        tabs.on_quick_connect = self.sessions.quick_connect
        tabs.on_new_host = self.hosts.prompt_new_host
        tabs.on_duplicate_session = lambda tab: self.sessions.open_hosts([tab.host])
        tabs.on_sessions_changed = self._update_health
        return tabs

    def _register_actions(self) -> None:
        tabs, hosts, sessions, dialogs = self.terminal_tabs, self.hosts, self.sessions, self.app_dialogs
        callbacks = {
            "new-host": hosts.prompt_new_host,
            "new-host-template": hosts.new_host_from_template,
            "new-group": self.groups.create_parent_group,
            "new-child-group": self.groups.create_child_group,
            "quick-connect": sessions.quick_connect,
            "focus-search": self.focus_search,
            "refresh": self.inventory.refresh,
            "settings": dialogs.open_settings,
            "app-menu": self._header.open_app_menu,
            "shortcuts": dialogs.show_shortcuts,
            "credentials": dialogs.open_credentials,
            "vault": dialogs.open_vault,
            "config-file": dialogs.open_config_file,
            "open-shared": self.inventory.open_shared_inventory,
            "git-check": self.inventory.check_git_now,
            "about": dialogs.show_about,
            "documentation": dialogs.open_documentation,
            "open-folder": self.inventory.open_inventory_folder,
            "quit": self.close,
            "host-connect": sessions.connect_current_item,
            "host-edit": hosts.edit_selected,
            "host-duplicate": hosts.duplicate_selected,
            "host-move": hosts.move_selected,
            "host-delete": hosts.delete_current_item,
            "copy-ssh": sessions.copy_ssh_command,
            "copy-scp": sessions.copy_scp_command,
            "rename": hosts.rename_focused,
            "close-tab": tabs.close_current,
            "next-tab": tabs.next_tab,
            "prev-tab": tabs.prev_tab,
            "goto-tab": tabs.goto_tab,
            "duplicate-session": sessions.duplicate_current_session,
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
            "toggle-sidebar": self.layout.toggle_sidebar,
            "toggle-details": self._details.toggle_expanded,
            "toggle-maximize": self.layout.toggle_maximized,
        }
        self._actions = register_actions(self, callbacks)

    # ------------------------------------------------------------ feedback
    def _log_event(self, message: str, success: object) -> None:
        """Feedback hook used by the terminal notebook and config results.

        ``success`` is a :class:`Level`, or a bool (``True`` → success, ``False`` → error).
        """

        level = success if isinstance(success, Level) else (Level.SUCCESS if success else Level.ERROR)
        self._notify(message, level)

    def _notify(self, message: str, level: Level = Level.INFO, *, action_label: str = "", action=None) -> None:
        if message:
            self._toasts.notify(message, level, action_label=action_label, action=action)

    def _notify_info(self, message: str) -> None:
        self._notify(message, Level.INFO)

    def _notify_warning(self, message: str) -> None:
        self._notify(message, Level.WARNING)

    def _notify_error(self, message: str) -> None:
        self._notify(message, Level.ERROR)

    def _prompt_text(self, title: str, message: str, default: str = "", *, validator=None, accept_label: str = "") -> Optional[str]:
        return prompt_text(self, title, message, default, validator=validator, accept_label=accept_label)

    def _confirm(self, message: str, *, detail: str = "", confirm_label: str = "", destructive: bool = True) -> bool:
        return confirm(self, message, detail, confirm_label=confirm_label or _("_Delete"), destructive=destructive)

    # ---------------------------------------------------------- public API
    def focus_search(self) -> None:
        if self._pass_ctrl_k_to_terminal():
            return
        if not self.layout.sidebar_visible:
            self.layout.set_sidebar_visible(True)
        self._sidebar.focus_search()

    def _pass_ctrl_k_to_terminal(self) -> bool:
        """Ctrl+K inside a terminal stays readline's kill-line instead of searching."""

        event = Gtk.get_current_event()
        focus = self.get_focus()
        if event is None or not isinstance(focus, Vte.Terminal):
            return False
        ok, keyval = event.get_keyval()
        if not ok or Gdk.keyval_to_lower(keyval) != Gdk.KEY_k:
            return False
        focus.feed_child(b"\x0b")
        return True

    def open_selected_host(self) -> None:
        self.sessions.open_selected_host()

    def prompt_new_host(self, template=None) -> None:
        self.hosts.prompt_new_host(template=template)

    def show_search_panel(self) -> None:
        """Focus the always-visible search entry (kept for compatibility)."""

        self.focus_search()

    def hide_search_panel(self) -> None:
        self.tree_view.grab_focus()

    def clear_focus(self) -> None:
        self.tree_view.grab_focus()

    # -------------------------------------------------------- theme / font
    def _apply_ui_font(self, size: int) -> None:
        self._ui_font_size = max(10, min(48, int(size)))
        settings = Gtk.Settings.get_default()
        if not settings:
            return
        desc = Pango.FontDescription()
        desc.set_family(self._base_font_family or "Sans")
        desc.set_size(self._ui_font_size * Pango.SCALE)
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

        if hasattr(self, "tree_view"):
            self.tree_view.clear_icon_cache()

    # ------------------------------------------------------------- UI state
    def _load_ui_state(self) -> Dict[str, object]:
        return load_ui_state(self._ui_state_path)

    def _collect_ui_state(self) -> UiState:
        width, height = self.get_size()
        layout = self.layout
        ui = self._ui
        ui.window_size = (int(width), int(height))
        ui.is_maximized = bool(self.is_maximized())
        ui.sidebar_width = int(self._paned.get_position()) if layout.sidebar_visible else layout.sidebar_width_saved
        ui.sidebar_visible = layout.sidebar_visible
        ui.sidebar_prev_visible = layout.sidebar_visible_before_max
        ui.terminal_maximized = layout.maximized
        ui.theme_mode = self._theme_mode.value
        ui.prefer_dark = self._prefer_dark
        ui.font_scale = self.terminal_tabs.default_font_scale
        ui.ui_font_size = self._ui_font_size
        ui.details_expanded = self._details.expanded
        ui.filter_query = self.tree.filter_query
        ui.group_colors = dict(self.colors.group_colors)
        ui.host_colors = dict(self.colors.host_colors)
        return ui

    def _save_ui_state(self) -> None:
        if not hasattr(self, "_details"):
            return  # still constructing
        save_ui_state(self._ui_state_path, self._collect_ui_state().to_dict())

    def _apply_ui_state(self) -> None:
        ui = self._ui
        if ui.window_size:
            self.resize(*ui.window_size)
        if ui.is_maximized:
            self.maximize()
        self.layout.sidebar_width_saved = ui.sidebar_width
        self._paned.set_position(ui.sidebar_width)
        self._details.set_expanded(ui.details_expanded, notify=False)
        self.layout.set_sidebar_visible(ui.sidebar_visible, initial=True)
        self.layout.sidebar_visible_before_max = ui.sidebar_prev_visible
        self.layout.set_maximized(ui.terminal_maximized, initial=True)

    # -------------------------------------------------------------- signals
    def _on_delete_event(self, *_args) -> bool:
        self._save_ui_state()
        self.closing = True
        return False

    def _on_destroy(self, *_args) -> None:
        self.closing = True
        get_theme_manager().remove_listener(self._on_theme_changed)
        self._toasts.shutdown()
        self.inventory.shutdown()
