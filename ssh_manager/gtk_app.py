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

from ssh_manager.config import ConfigManager, HostEntry  # noqa: E402
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
from ssh_manager.gtkui.controllers.mobaxterm_import import MobaXtermImportController  # noqa: E402
from ssh_manager.gtkui.controllers.store_actions import StoreActions  # noqa: E402
from ssh_manager.inventory.manager import StoreConfigManager  # noqa: E402
from ssh_manager.inventory.startup import Choice, open_inventory_store  # noqa: E402
from ssh_manager.inventory.store import default_inventory_path  # noqa: E402
from ssh_manager.gtkui.controllers.notifications import ToastController  # noqa: E402
from ssh_manager.gtkui.controllers.session_controller import (  # noqa: E402,F401
    SessionController,
    UIHostConfig,
    audit_log_key,
    audit_log_value,
)
from ssh_manager.gtkui.controllers.shortcut_registry import ShortcutRegistry  # noqa: E402
from ssh_manager.gtkui.controllers.shortcuts import (  # noqa: E402
    refresh_accel_tooltips, register_actions, set_active_registry,
)
from ssh_manager.gtkui.controllers.tree import TreeController  # noqa: E402
from ssh_manager.gtkui.controllers.ui_state import (  # noqa: E402,F401
    UI_STATE_FILE,
    UiState,
    load_ui_state,
    save_ui_state,
    ui_state_path_for_base,
)
from ssh_manager.gtkui.dialogs.confirm import confirm, show_message  # noqa: E402
from ssh_manager.gtkui.dialogs.prompt import prompt_text  # noqa: E402
from ssh_manager.gtkui.dialogs.secrets import NewSecretDialog, UnlockDialog  # noqa: E402
from ssh_manager.gtkui.i18n import _  # noqa: E402
from ssh_manager.gtkui.theme import density as density_tokens  # noqa: E402
from ssh_manager.gtkui.theme.density import Density  # noqa: E402
from ssh_manager.gtkui.theme.loader import ThemeMode, get_theme_manager  # noqa: E402
from ssh_manager.gtkui.theme.tokens import Level  # noqa: E402
from ssh_manager.gtkui.controllers.host_page import HostPageController  # noqa: E402
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
from ssh_manager.gtkui.host_history import HOST_HISTORY_FILE  # noqa: E402
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


def default_inventory_folder() -> Path:
    """The folder the app keeps its groups in by default."""

    return Path.home() / ".ssh" / "conf.d"


def _home_relative(path: Path) -> str:
    try:
        return "~/" + str(Path(path).expanduser().relative_to(Path.home()))
    except ValueError:
        return str(path)


class SSHManagerApplication(Gtk.Application):
    """GTK application entry point."""

    def __init__(self, base_dir: Optional[Path] = None, *, inventory: Optional[Path] = None,
                 startup_host: Optional[HostEntry] = None) -> None:
        Gtk.Application.__init__(self, application_id="org.codex.SSHManager")
        self._base_dir = base_dir  # a legacy folder inventory (--base-dir); None = the app store
        self._inventory = inventory  # --inventory: another database file
        self._window: Optional[SSHManagerWindow] = None
        self._theme = get_theme_manager()
        self._settings = AppSettings.load()
        self._master_password: Optional[str] = None
        self._startup_host = startup_host  # --rdp / rdp://: opened once the window exists, never saved

    def do_startup(self) -> None:  # type: ignore[override]
        Gtk.Application.do_startup(self)
        register_icon_path()
        self._install_css(None)

    def _open_startup_host(self, host: HostEntry) -> bool:
        if self._window is not None:
            self._window.sessions.open_unsaved(host)
        return False

    def do_activate(self) -> None:  # type: ignore[override]
        if not self._window:
            if not self._ensure_master_password():
                self.quit()
                return
            manager = None
            if self._base_dir is None:
                store = open_inventory_store(self._inventory or default_inventory_path(), self._ask_startup)
                if store is None:
                    self.quit()
                    return
                manager = StoreConfigManager(store)
            base = manager.base_dir if manager is not None else self._base_dir
            self._window = SSHManagerWindow(self, base, self._settings, master_password=self._master_password,
                                            manager=manager)
            if manager is not None and not manager.read_only:
                GLib.idle_add(lambda: self._window and self._window.store_actions.offer_first_run_import() and False)
            if self._startup_host is not None:
                host, self._startup_host = self._startup_host, None
                GLib.timeout_add(500, self._open_startup_host, host)
        self._window.present()

    def _ask_startup(self, kind: str, message: str) -> Choice:
        """A start-up problem with the inventory: ask what to do (never crash, never lose data silently)."""

        buttons = {
            "locked": [(_("_Quit"), Choice.QUIT), (_("Open _Read-Only"), Choice.READ_ONLY), (_("_Retry"), Choice.RETRY)],
            "corrupt": [(_("_Quit"), Choice.QUIT), (_("Start _Empty"), Choice.START_EMPTY),
                        (_("_Restore Latest Backup"), Choice.RESTORE)],
            "too_new": [(_("_Quit"), Choice.QUIT), (_("Open _Empty and Read-Only"), Choice.READ_ONLY)],
            "unavailable": [(_("_Quit"), Choice.QUIT), (_("Open _Read-Only"), Choice.READ_ONLY)],
        }[kind]
        titles = {
            "locked": _("The inventory is in use"),
            "corrupt": _("The inventory is damaged"),
            "too_new": _("The inventory is from a newer SSH Manager"),
            "unavailable": _("The inventory cannot be saved"),
        }
        detail = message
        if kind == "corrupt":
            detail += "\n\n" + _("Start Empty keeps the damaged file next to the new one.")
        dialog = Gtk.MessageDialog(modal=True, message_type=Gtk.MessageType.WARNING, text=titles[kind])
        dialog.format_secondary_text(detail)
        for index, (label, _choice) in enumerate(buttons):
            dialog.add_button(label, index)
        dialog.set_default_response(len(buttons) - 1)
        response = dialog.run()
        dialog.destroy()
        return buttons[response][1] if 0 <= response < len(buttons) else Choice.QUIT

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
        manager=None,
    ) -> None:
        Gtk.ApplicationWindow.__init__(self, application=app)
        self.set_title(_("SSH Manager"))
        self.set_default_size(1280, 820)
        self.set_size_request(960, 600)
        self.set_name("ssh-manager-window")
        self.get_style_context().add_class("sm-window")

        self.closing = False
        self._init_services(base_dir, settings, master_password, manager)
        self._init_controllers()
        self._build_ui()
        self._register_actions()
        self._apply_ui_state()
        self.tree.rebuild(refresh=True)
        self.inventory.refresh_git_watch()
        self.sessions.refresh_rdp_engines()  # FreeRDP clients, in the background (RDP hosts)
        self.creds.warn_if_password_file_locked()
        # Another window/instance changed the store: reload (PRAGMA data_version).
        self._external_poll = GLib.timeout_add_seconds(2, self._check_external_changes)

        self.connect("delete-event", self._on_delete_event)
        self.connect("destroy", self._on_destroy)

    # ---------------------------------------------------------------- setup
    def _init_services(self, base_dir: Path, settings: AppSettings, master_password: Optional[str], manager=None) -> None:
        # The app store (StoreConfigManager) by default; a folder inventory for --base-dir and tests.
        self.manager = manager if manager is not None else ConfigManager(base_dir)
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
        # Views read the active density while they are built, so set it first.
        self._density = Density.coerce(self._ui.density)
        density_tokens.set_active(self._density, notify=False)
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
        self.store_actions = StoreActions(self)
        self.importer = MobaXtermImportController(self)
        from ssh_manager.gtkui.controllers.rdp_files import RdpFileActions

        self.rdp_files = RdpFileActions(self)
        self.host_page = HostPageController(self, history_path=self._ui_state_path.parent / HOST_HISTORY_FILE)

    def _build_ui(self) -> None:
        self._build_header()
        self.tree_view = self._build_tree_view()
        self._sidebar = self._build_sidebar(self.tree_view)
        self._sidebar_container = self._sidebar
        self.terminal_tabs = self._build_workspace()
        self._host_page = self.host_page.build(self.terminal_tabs)
        self.terminal_tabs.set_status_dock(self._host_page.info_bar.live)

        right_box = self._host_page
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
        density_tokens.add_listener(self._on_density_changed)
        self.terminal_tabs.on_toggle_focus_mode = self.layout.toggle_maximized
        self.show_all()
        # Sidebar and top bar visibility belong to LayoutController (sidebar
        # toggle, focus mode); a later show_all() must not bring them back.
        for part in (self._sidebar, self._header):
            part.show_all()
            part.set_no_show_all(True)
        self.connect("size-allocate", self.layout.on_size_allocate)
        self.connect("key-press-event", self._on_window_key_press)

    def _build_header(self) -> None:
        self._header = HeaderView(self._toasts.history)
        self._header.set_user_initials(local_user_initials())
        self._header.on_workspace_selected = self.inventory.switch_workspace
        self._header.history_button.host_activity_provider = self.host_page.host_activity
        self.set_titlebar(self._header)
        self.refresh_workspaces()
        self._toasts.history.add_listener(self._update_health)

    def refresh_workspaces(self) -> None:
        shared = self.inventory.shared_path
        self._header.set_workspaces(workspace_list(self.manager.base_dir, self._ui.recent_inventories, shared))

    def _on_password_changed(self, host, password: str) -> bool:
        saved = self.creds.save_detected_password_change(host, password)
        if saved:
            self.host_page.on_password_changed(host)
        return saved

    def _on_sessions_changed(self) -> None:
        self._update_health()
        if hasattr(self, "_host_page"):
            self.host_page.refresh()

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
        sidebar.on_import_mobaxterm = lambda: self.importer.open()
        sidebar.on_escape_to_tree = lambda: self.tree_view.grab_focus()
        sidebar.on_bulk_connect = self.actions.bulk_connect_selected
        sidebar.on_bulk_move = self.actions.bulk_move_selected
        sidebar.on_bulk_tag = self.actions.bulk_tag_selected
        sidebar.on_bulk_clear = lambda: self.tree_view.get_selection().unselect_all()
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
            password_update_handler=self._on_password_changed,
            appearance=self._settings.terminal_appearance,
        )
        tabs.set_name("terminal-area")
        tabs.on_quick_connect = self.sessions.quick_connect
        tabs.on_new_host = self.hosts.prompt_new_host
        tabs.input_settings = self._settings.terminal_input  # shared: Preferences changes apply at once
        tabs.on_terminal_preferences = lambda: self.app_dialogs.open_settings("terminal")
        tabs.on_duplicate_session = lambda tab: self.sessions.open_hosts([tab.host])
        tabs.on_sessions_changed = self._on_sessions_changed
        tabs.set_label_mode(self._ui.tab_label_mode)
        return tabs

    def _register_actions(self) -> None:
        tabs, hosts, sessions, dialogs = self.terminal_tabs, self.hosts, self.sessions, self.app_dialogs
        # The user's shortcuts (Preferences → Keyboard Shortcuts): menus, the shortcuts window and the
        # accelerators below all read this registry.
        self.shortcut_registry = ShortcutRegistry(overrides=self._settings.shortcuts)
        set_active_registry(self.shortcut_registry)
        self.connect("destroy", lambda *_a: self._release_shortcut_registry())

        def terminal(name: str):  # acts on the focused terminal; in a text field, the field's own action
            return lambda: tabs.terminal_action(name, self.get_focus())

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
            "import-mobaxterm": self.importer.open,
            "import-openssh": lambda: self.store_actions.import_openssh(),
            "import-rdp-files": lambda: self.rdp_files.import_files(),
            "export-rdp-file": lambda: self.rdp_files.export_selected(),
            "export-openssh": lambda: self.store_actions.export_openssh(),
            "backup-inventory": lambda: self.store_actions.backup_now(),
            "quit": self.close,
            "host-connect": sessions.connect_current_item,
            "host-edit": hosts.edit_selected,
            "host-duplicate": hosts.duplicate_selected,
            "host-move": hosts.move_selected,
            "host-delete": hosts.delete_current_item,
            "copy-ssh": sessions.copy_ssh_command,
            "copy-scp": sessions.copy_scp_command,
            "rename": hosts.rename_focused,
            "expand-all-groups": self.tree.expand_all_groups,
            "collapse-all-groups": self.tree.collapse_all_groups,
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
            "copy": terminal("copy"),
            "paste": terminal("paste"),
            "paste-primary": terminal("paste-primary"),
            "terminal-menu": terminal("terminal-menu"),
            "select-all-terminal": terminal("select-all-terminal"),
            "clear-scrollback": terminal("clear-scrollback"),
            "reset-terminal": terminal("reset-terminal"),
            "interrupt": tabs.interrupt_current,
            "font-increase": tabs.increase_font,
            "font-decrease": tabs.decrease_font,
            "font-reset": tabs.reset_font,
            "toggle-sidebar": self.layout.toggle_sidebar,
            "toggle-details": self.toggle_host_details,
            "host-activity": self.host_page.show_activity,
            "toggle-maximize": self.layout.toggle_maximized,
        }
        self._actions = register_actions(self, callbacks)
        self.shortcut_registry.add_listener(self._on_shortcuts_changed)

    def _on_shortcuts_changed(self) -> None:
        """The user changed a shortcut: live accelerators, hints, the shortcuts window, the settings file."""

        self.shortcut_registry.apply(self.get_application(), self._actions)
        self.emit("keys-changed")  # GTK caches the window's key lookup; without this the old keys keep working
        refresh_accel_tooltips()
        self.app_dialogs.refresh_shortcuts_window()
        self._settings.shortcuts = self.shortcut_registry.to_dict()
        self._settings.save()

    def _release_shortcut_registry(self) -> None:
        from ssh_manager.gtkui.controllers import shortcuts

        if shortcuts._ACTIVE_REGISTRY is getattr(self, "shortcut_registry", None):
            set_active_registry(None)

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

    def _check_external_changes(self) -> bool:
        if self.closing:
            return GLib.SOURCE_REMOVE
        changed = getattr(self.manager, "changed_externally", None)
        if changed is not None and changed():
            self.manager.refresh()
            self.tree.rebuild(refresh=False)
        return GLib.SOURCE_CONTINUE

    def ensure_editable_inventory(self) -> bool:
        """Before a change: explain a read-only store, or offer a folder for a single-file inventory."""

        manager = getattr(self, "manager", None)
        if manager is None or not manager.read_only:
            return True
        if getattr(manager, "store", None) is not None:
            show_message(self, _("The inventory is read-only"), manager.read_only_reason, level=Level.WARNING)
            return False
        folder = default_inventory_folder()
        if not confirm(
            self,
            _("Use a folder for your groups?"),
            _("‘{path}’ is a file. SSH Manager keeps its groups in a folder. Use ‘{folder}’ instead?").format(
                path=_home_relative(manager.base_dir), folder=_home_relative(folder)),
            confirm_label=_("_Use Folder"),
        ):
            return False
        try:
            folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError as exc:
            self._notify_error(_("Cannot create {folder}: {error}").format(folder=folder, error=exc.strerror or exc))
            return False
        self.inventory.load_workspace(folder)
        return not self.manager.read_only

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

        return self._pass_ctrl_key_to_terminal(Gdk.KEY_k, b"\x0b")

    def _pass_ctrl_key_to_terminal(self, key: int, data: bytes) -> bool:
        """Send Ctrl+<key> to a focused terminal instead of running the app shortcut."""

        event = Gtk.get_current_event()
        focus = self.get_focus()
        if event is None or not isinstance(focus, Vte.Terminal):
            return False
        ok, keyval = event.get_keyval()
        if not ok or Gdk.keyval_to_lower(keyval) != key:
            return False
        focus.feed_child(data)
        return True

    def toggle_host_details(self) -> None:
        """Ctrl+I / F9 / ⓘ: the host details panel. Inside a terminal Ctrl+I stays Tab (0x09)."""

        if self._pass_ctrl_key_to_terminal(Gdk.KEY_i, b"\x09"):
            return
        self.host_page.toggle_details()

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
        get_theme_manager().set_ui_font_size(self._ui_font_size)  # the 11px type floor depends on it
        settings = Gtk.Settings.get_default()
        if not settings:
            return
        desc = Pango.FontDescription()
        desc.set_family(self._base_font_family or "Sans")
        # The density scales the base font (Compact: 13px body at 11pt); em sizes follow it.
        desc.set_size(int(round(self._ui_font_size * density_tokens.active().base_font_scale * Pango.SCALE)))
        name = desc.to_string()
        if settings.get_property("gtk-font-name") != name:  # a change restyles every widget of the process
            settings.set_property("gtk-font-name", name)

    def _apply_dark_theme(self, prefer_dark: bool) -> None:
        self._apply_theme_mode(ThemeMode.DARK if prefer_dark else ThemeMode.LIGHT)

    def _apply_theme_mode(self, mode: ThemeMode) -> None:
        self._theme_mode = ThemeMode.coerce(mode)
        app = self.get_application()
        if app and hasattr(app, "set_theme_mode"):
            self._prefer_dark = app.set_theme_mode(self._theme_mode)
        else:
            self._prefer_dark = get_theme_manager().apply(self._theme_mode)

    # ------------------------------------------------------------- density
    @property
    def density(self) -> Density:
        return self._density

    def set_density(self, density: object) -> None:
        """Switch Compact / Comfortable at runtime: new CSS, then every view lays itself out again."""

        density = Density.coerce(density)
        self._density = density
        self._ui.density = density.value
        density_tokens.set_active(density)  # → _on_density_changed (and other windows' listeners), if it changed

    def _on_density_changed(self, _tokens) -> None:
        if self.closing or not hasattr(self, "_host_page"):
            return
        get_theme_manager().reload()
        self._apply_ui_font(self._ui_font_size)  # base font follows the density
        self._header.apply_density()
        self.refresh_workspaces()
        self._sidebar.apply_density()
        self.tree_view.apply_density()
        self.terminal_tabs.apply_density()
        self.host_page.apply_density()
        other = density_tokens.COMFORTABLE if density_tokens.active().compact else density_tokens.COMPACT
        self.layout.on_density_changed(previous_default=other.size.SIDEBAR - 1)
        self.layout.set_narrow(self.layout.compact, force=True)
        self._density = density_tokens.active().density

    def _on_window_key_press(self, _widget, event: Gdk.EventKey) -> bool:
        """Esc closes the host panel; else leaves focus mode unless a terminal has focus."""

        if event.keyval != Gdk.KEY_Escape or event.state & Gtk.accelerator_get_default_mod_mask():
            return False
        if isinstance(self.get_focus(), Vte.Terminal):
            return False  # the terminal needs Esc
        if self.host_page.close_panel():
            return True
        if not self.layout.maximized:
            return False
        if self.terminal_tabs.broadcast_active:
            self.terminal_tabs.set_broadcast_mode(False)
        else:
            self.layout.set_maximized(False)
        return True

    def _on_theme_changed(self, _is_dark: bool) -> None:
        """Re-render rows whose colors come from the active palette."""

        if hasattr(self, "tree_view"):
            self.tree_view.clear_icon_cache()
        if hasattr(self, "_host_page"):
            self.host_page.refresh()  # markup colors (ready-state body) come from the palette

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
        ui.density = self._density.value
        ui.filter_query = self.tree.filter_query
        ui.group_colors = dict(self.colors.group_colors)
        ui.host_colors = dict(self.colors.host_colors)
        return ui

    def _save_ui_state(self) -> None:
        if not hasattr(self, "_host_page"):
            return  # still constructing
        save_ui_state(self._ui_state_path, self._collect_ui_state().to_dict())

    def _apply_ui_state(self) -> None:
        ui = self._ui
        if ui.window_size:
            self.resize(*ui.window_size)
        if ui.is_maximized:
            self.maximize()
        self._paned.set_position(self.layout.sidebar_width_saved)
        self.layout.set_sidebar_visible(ui.sidebar_visible, initial=True)
        self.layout.sidebar_visible_before_max = ui.sidebar_prev_visible
        self.layout.set_maximized(ui.terminal_maximized, initial=True)

    # -------------------------------------------------------------- signals
    def _on_delete_event(self, *_args) -> bool:
        desktops = sum(1 for tab in self.terminal_tabs.tabs() if getattr(tab, "rdp_connected", lambda: False)())
        if desktops and not self._confirm(
            _("Disconnect the remote desktops?"),
            detail=_("{n} remote desktop sessions are open. Disconnect them and quit?").format(n=desktops),
            confirm_label=_("Disconnect and _Quit"),
        ):
            return True  # keep the window open
        running = sum(getattr(tab, "running_transfers", lambda: 0)() for tab in self.terminal_tabs.tabs())
        if running and not self._confirm(
            _("Cancel the transfers?"),
            detail=_("{n} transfers are in progress. Cancel them and quit?").format(n=running),
            confirm_label=_("Cancel Transfers and _Quit"),
        ):
            return True  # keep the window open
        self._save_ui_state()
        self.closing = True
        return False

    def _on_destroy(self, *_args) -> None:
        self.closing = True
        if getattr(self, "_external_poll", 0):
            GLib.source_remove(self._external_poll)
            self._external_poll = 0
        get_theme_manager().remove_listener(self._on_theme_changed)
        density_tokens.remove_listener(self._on_density_changed)
        self._toasts.shutdown()
        self.inventory.shutdown()
        self.host_page.shutdown()
        runtime = getattr(self.sessions, "_rdp_runtime", None)
        if runtime is not None:
            runtime.stop_all()  # no FreeRDP client outlives the window
