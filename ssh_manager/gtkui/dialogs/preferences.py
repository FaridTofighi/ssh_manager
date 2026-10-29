"""Application settings dialog.

Pages are small builder methods on top of :class:`SettingsPageLayout`. The
dialog returns a :class:`PreferencesValues` dataclass instead of the previous
19-item positional tuple.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Vte", "2.91")
from gi.repository import Gdk, Gtk, Pango, Vte  # type: ignore  # noqa: E402

from ...settings import (  # noqa: E402
    AppSettings, FileTransferPreferences, LocalShellSettings, RemoteDesktopPreferences, TerminalAppearanceSettings,
    TerminalInputSettings,
)
from ..i18n import _  # noqa: E402
from ..tab_labels import TabLabelMode  # noqa: E402
from ..theme.density import DEFAULT_DENSITY, Density  # noqa: E402
from ..theme.loader import ThemeMode  # noqa: E402
from ..theme.terminal_schemes import SCHEMES, resolve_terminal_colors  # noqa: E402
from ..theme.tokens import Size, Space  # noqa: E402
from ..widgets.forms import FormSection, entry  # noqa: E402
from ..widgets.icon_button import set_accessible_name  # noqa: E402
from .base import BaseDialog, DialogSize  # noqa: E402
from .files import choose_folder, choose_open_file, choose_save_file  # noqa: E402
from .settings_layout import SettingsPageLayout  # noqa: E402

TAB_LABEL_CHOICES = [
    (TabLabelMode.NAME.value, _("Name")),
    (TabLabelMode.IP.value, _("IP address")),
    (TabLabelMode.NAME_IP.value, _("Name and IP")),
]
DENSITY_CHOICES = [
    (Density.COMPACT.value, _("Compact")),
    (Density.COMFORTABLE.value, _("Comfortable")),
]
THEME_CHOICES = [
    (ThemeMode.SYSTEM.value, _("Follow system")),
    (ThemeMode.LIGHT.value, _("Light")),
    (ThemeMode.DARK.value, _("Dark")),
]
CURSOR_SHAPES = [("block", _("Block")), ("ibeam", _("I-beam")), ("underline", _("Underline"))]
CONFLICT_CHOICES = [
    ("ask", _("Ask every time")),
    ("overwrite", _("Overwrite")),
    ("overwrite_newer", _("Overwrite if the source is newer")),
    ("skip", _("Skip")),
    ("rename", _("Keep both (rename)")),
    ("resume", _("Resume when possible")),
]


@dataclass
class PreferencesValues:
    """Everything the Preferences dialog edits."""

    theme_mode: ThemeMode
    terminal_font_scale: float
    ui_font_size: int
    host_key_policy: str
    connect_timeout: float
    auth_timeout: float
    banner_timeout: float
    use_paramiko_vte: bool
    master_password_disabled: bool
    allowed_ciphers: List[str]
    allowed_kex: List[str]
    secrets_policy: Dict[str, Any]
    enable_audit_logging: bool
    audit_log_path: str
    git_watch_enabled: bool
    git_watch_path: str
    git_watch_interval: int
    latency_probe: bool = True
    terminal_appearance: TerminalAppearanceSettings = field(default_factory=TerminalAppearanceSettings)
    density: Density = DEFAULT_DENSITY
    tab_label_mode: TabLabelMode = TabLabelMode.NAME
    file_transfer: Optional[FileTransferPreferences] = None
    remote_desktop: Optional[RemoteDesktopPreferences] = None
    terminal_input: Optional[TerminalInputSettings] = None
    shortcuts: Optional[Dict[str, List[str]]] = None  # the user's shortcut changes (only differences)
    local_shell: Optional[LocalShellSettings] = None

    @property
    def prefer_dark(self) -> bool:
        return self.theme_mode is ThemeMode.DARK


MIDDLE_CLICK_LABELS = (
    ("paste_primary", _("Pastes the selection")),
    ("paste_clipboard", _("Pastes the clipboard")),
    ("none", _("Does nothing")),
)
RIGHT_CLICK_LABELS = (
    ("menu", _("Opens the menu")),
    ("paste_clipboard", _("Pastes the clipboard")),
    ("paste_primary", _("Pastes the selection")),
    ("none", _("Does nothing")),
)


def _combo(choices, active: str) -> Gtk.ComboBoxText:
    combo = Gtk.ComboBoxText()
    for key, label in choices:
        combo.append(key, label)
    if not combo.set_active_id(active):
        combo.set_active(0)
    return combo


def _spin(low: float, high: float, step: float, value: float, digits: int = 0) -> Gtk.SpinButton:
    spin = Gtk.SpinButton.new_with_range(low, high, step)
    spin.set_digits(digits)
    spin.set_value(value)
    spin.set_halign(Gtk.Align.START)
    return spin


def _rgba(color: Optional[str]) -> Optional[Gdk.RGBA]:
    rgba = Gdk.RGBA()
    return rgba if color and rgba.parse(color) else None


def _hex(rgba: Gdk.RGBA) -> str:
    return "#{:02x}{:02x}{:02x}".format(*(int(c * 255 + 0.5) for c in (rgba.red, rgba.green, rgba.blue)))


def _path_row(initial: str, on_browse: Callable[[Gtk.Entry], None]) -> tuple:
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
    box.get_style_context().add_class("linked")
    path_entry = entry(initial)
    path_entry.set_hexpand(True)
    box.pack_start(path_entry, True, True, 0)
    browse = Gtk.Button.new_with_mnemonic(_("_Choose…"))
    browse.connect("clicked", lambda *_: on_browse(path_entry))
    box.pack_start(browse, False, False, 0)
    return box, path_entry


class PreferencesDialog(BaseDialog):
    """Settings for appearance, terminal, connections, security and integrations."""

    def __init__(
        self,
        parent: Gtk.Window,
        theme_mode: object,
        terminal_font_scale: float,
        ui_font_size: int,
        settings: AppSettings,
        *,
        on_reset: Optional[Callable[[], None]] = None,
        on_export: Optional[Callable[[], None]] = None,
        latency_probe: bool = True,
        density: object = DEFAULT_DENSITY,
        tab_label_mode: object = TabLabelMode.NAME,
        inventory: Optional[Dict[str, object]] = None,
        shortcuts: object = None,
    ) -> None:
        BaseDialog.__init__(self, parent, _("Settings"), size=DialogSize.LARGE, height=600)
        self._latency_probe = latency_probe
        self._density = Density.coerce(density)
        self._tab_label_mode = TabLabelMode.coerce(tab_label_mode)
        content = self.get_content_area()
        for setter in (content.set_margin_start, content.set_margin_end, content.set_margin_top):
            setter(0)
        self._settings = settings
        self._on_reset = on_reset
        self._on_export = on_export
        self._appearance = settings.terminal_appearance or TerminalAppearanceSettings()
        self._custom_font = self._appearance.custom_font
        if isinstance(theme_mode, bool):
            theme_mode = ThemeMode.from_prefer_dark(theme_mode)
        self._labels = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)

        self.layout = SettingsPageLayout()
        self._build_appearance(ThemeMode.coerce(theme_mode), terminal_font_scale, ui_font_size)
        self._build_terminal()
        self._build_connection()
        self._build_security()
        self._build_secrets()
        self._build_master_password()
        self._build_file_transfer()
        self._build_remote_desktop()
        self._build_git()
        self._build_audit()
        if inventory:
            self._build_inventory(inventory)
        self.shortcuts_page = None
        if shortcuts is not None:
            self._build_shortcuts(shortcuts)
        self.add_body(self.layout, expand=True)

        self.add_cancel()
        self.add_primary(_("_Apply"))
        self._update_font_controls()
        self._update_color_controls()
        self.show_all()

    # ------------------------------------------------------------- pages
    def _section(self, page, title: str = "", description: str = "") -> FormSection:
        section = FormSection(title, description)
        page.append(section)
        return section

    def _build_appearance(self, theme_mode: ThemeMode, terminal_font_scale: float, ui_font_size: int) -> None:
        page = self.layout.new_page("appearance", _("Appearance"), _("Theme and text size of the application."))
        sec = self._section(page)
        self._theme_combo = _combo(THEME_CHOICES, theme_mode.value)
        sec.add(_("_Theme"), self._theme_combo, label_group=self._labels,
                help_text=_("“Follow system” uses the desktop's light or dark preference."))
        self._density_combo = _combo(DENSITY_CHOICES, self._density.value)
        sec.add(_("Interface _density"), self._density_combo, label_group=self._labels,
                help_text=_("Compact gives the terminal more room; Comfortable uses larger text and spacing."))
        self._tab_label_combo = _combo(TAB_LABEL_CHOICES, self._tab_label_mode.value)
        sec.add(_("Tab _label"), self._tab_label_combo, label_group=self._labels,
                help_text=_("What session tabs show. A name given with Rename (F2) always wins."))
        self._ui_font_spin = _spin(10, 48, 1, max(10, min(48, ui_font_size)))
        sec.add(_("Application _font size"), self._ui_font_spin, label_group=self._labels,
                help_text=_("Scales all text in the interface (points)."))
        self._font_spin = _spin(0.6, 2.5, 0.1, terminal_font_scale, digits=1)
        sec.add(_("Terminal _zoom"), self._font_spin, label_group=self._labels,
                help_text=_("Default zoom for new sessions (Ctrl+plus / Ctrl+minus change it per session)."))

    def _build_terminal(self) -> None:
        page = self.layout.new_page("terminal", _("Terminal"), _("Font, colors and cursor of session terminals."))
        appearance = self._appearance
        font = self._section(page, _("Font"))
        self._use_system_font = Gtk.CheckButton.new_with_mnemonic(_("Use the system _monospace font"))
        self._use_system_font.set_active(appearance.use_system_mono_font)
        self._use_system_font.connect("toggled", lambda *_: self._update_font_controls())
        font.add_widget(self._use_system_font)
        self._font_button = Gtk.Button(label=self._custom_font or _("Choose font…"))
        self._font_button.connect("clicked", self._choose_font)
        font.add(_("Custom _font"), self._font_button, label_group=self._labels)
        self._font_size_spin = _spin(8.0, 36.0, 0.5, appearance.font_size or 11.0, digits=1)
        font.add(_("Font _size"), self._font_size_spin, label_group=self._labels)
        self._font_size_spin.connect("value-changed", lambda *_: self._update_preview_terminal())

        colors = self._section(page, _("Colors"))
        self._scheme_combo = _combo(SCHEMES, appearance.color_scheme)
        self._scheme_combo.connect("changed", lambda *_: self._update_preview_terminal())
        colors.add(_("Color _scheme"), self._scheme_combo, label_group=self._labels)
        self._custom_colors_check = Gtk.CheckButton.new_with_mnemonic(_("Use _custom foreground and background"))
        self._custom_colors_check.set_active(appearance.use_custom_colors)
        self._custom_colors_check.connect("toggled", lambda *_: self._update_color_controls())
        colors.add_widget(self._custom_colors_check)
        self._fg_button = Gtk.ColorButton()
        self._bg_button = Gtk.ColorButton()
        for button, value in ((self._fg_button, appearance.fg_color), (self._bg_button, appearance.bg_color)):
            rgba = _rgba(value)
            if rgba is not None:
                button.set_rgba(rgba)
            button.set_halign(Gtk.Align.START)
            button.connect("color-set", lambda *_: self._update_preview_terminal())
        colors.add(_("_Foreground"), self._fg_button, label_group=self._labels)
        colors.add(_("_Background"), self._bg_button, label_group=self._labels)

        mouse = self._section(page, _("Mouse and selection"),
                              _("Shift+right-click always opens the terminal menu, also in programs that use the mouse."))
        inputs = self._settings.terminal_input
        self._middle_click_combo = _combo(MIDDLE_CLICK_LABELS, inputs.middle_click)
        mouse.add(_("_Middle-click"), self._middle_click_combo, label_group=self._labels)
        self._right_click_combo = _combo(RIGHT_CLICK_LABELS, inputs.right_click)
        mouse.add(_("_Right-click"), self._right_click_combo, label_group=self._labels)
        self._copy_select_clipboard_check = Gtk.CheckButton.new_with_mnemonic(
            _("Selecting text also copies it to the _clipboard (it always goes to the selection buffer)"))
        self._copy_select_clipboard_check.set_active(inputs.copy_on_select_clipboard)
        mouse.add_widget(self._copy_select_clipboard_check)

        local = self._section(page, _("Local terminal"), _("The shell of a new tab (Ctrl+Shift+T or “+”)."))
        shell = self._settings.local_shell
        self._shell_entry = Gtk.Entry()
        self._shell_entry.set_text(shell.command)
        self._shell_entry.set_placeholder_text(_("Automatic ($SHELL)"))
        local.add(_("S_hell"), self._shell_entry, label_group=self._labels)
        self._login_shell_check = Gtk.CheckButton.new_with_mnemonic(_("Run it as a _login shell"))
        self._login_shell_check.set_active(shell.login_shell)
        local.add_widget(self._login_shell_check)
        self._start_in_combo = _combo((("current", _("The current local terminal's folder")),
                                       ("home", _("The home folder"))), shell.start_in)
        local.add(_("_Start in"), self._start_in_combo, label_group=self._labels)
        self._on_exit_combo = _combo((("close", _("Close the tab")), ("hold", _("Keep the tab open")),
                                      ("hold_on_error", _("Keep it open when the shell failed"))), shell.on_exit)
        local.add(_("When the shell _exits"), self._on_exit_combo, label_group=self._labels)

        cursor = self._section(page, _("Cursor and rendering"))
        self._cursor_combo = _combo(CURSOR_SHAPES, appearance.cursor_shape)
        self._cursor_combo.connect("changed", lambda *_: self._update_preview_terminal())
        cursor.add(_("Cursor _shape"), self._cursor_combo, label_group=self._labels)
        self._cursor_blink_check = Gtk.CheckButton.new_with_mnemonic(_("Cursor _blinks"))
        self._cursor_blink_check.set_active(appearance.cursor_blink)
        self._bold_is_bright_check = Gtk.CheckButton.new_with_mnemonic(_("_Bold text is bright"))
        self._bold_is_bright_check.set_active(appearance.bold_is_bright)
        for check in (self._cursor_blink_check, self._bold_is_bright_check):
            check.connect("toggled", lambda *_: self._update_preview_terminal())
            cursor.add_widget(check)

        preview_title = Gtk.Label(label=_("Preview"))
        preview_title.set_xalign(0.0)
        preview_title.get_style_context().add_class("section-title")
        page.append(preview_title)
        self._preview_term = Vte.Terminal()
        self._preview_term.set_size_request(-1, Size.TERMINAL_PREVIEW)
        self._preview_term.set_scrollback_lines(100)
        self._preview_term.feed(b"ssh-manager terminal preview\r\n\x1b[1;32mdeploy@web-01\x1b[0m:\x1b[1;34m~\x1b[0m$ echo example\r\nexample\r\n")
        frame = Gtk.Frame()
        frame.set_shadow_type(Gtk.ShadowType.IN)
        frame.add(self._preview_term)
        page.append(frame)

    def _build_connection(self) -> None:
        settings = self._settings
        page = self.layout.new_page("connection", _("Connection"), _("Timeouts, passwords and the connection backend."))
        timeouts = self._section(page, _("Timeouts"), _("Seconds before giving up."))
        self._connect_timeout_spin = _spin(1.0, 600.0, 1.0, settings.connect_timeout, 1)
        self._auth_timeout_spin = _spin(1.0, 600.0, 1.0, settings.auth_timeout, 1)
        self._banner_timeout_spin = _spin(1.0, 600.0, 1.0, settings.banner_timeout, 1)
        timeouts.add(_("_Connection"), self._connect_timeout_spin, label_group=self._labels)
        timeouts.add(_("_Authentication"), self._auth_timeout_spin, label_group=self._labels)
        timeouts.add(_("_Banner"), self._banner_timeout_spin, label_group=self._labels)

        behaviour = self._section(page, _("Behavior"))
        self._paramiko_check = Gtk.CheckButton.new_with_mnemonic(_("Use the _Paramiko + VTE backend (experimental)"))
        self._paramiko_check.set_active(settings.use_paramiko_vte)
        self._paramiko_check.set_tooltip_text(_("Authenticate in-process instead of running the system ssh client."))
        behaviour.add_widget(self._paramiko_check)
        self._latency_check = Gtk.CheckButton.new_with_mnemonic(_("Measure _latency to the selected host"))
        self._latency_check.set_active(self._latency_probe)
        self._latency_check.set_tooltip_text(
            _("Times a TCP connection to the selected host's SSH port. Hosts behind a jump host are never probed.")
        )
        behaviour.add_widget(self._latency_check)

    def _build_security(self) -> None:
        settings = self._settings
        page = self.layout.new_page("ssh_security", _("SSH Security"), _("Host key checking and allowed algorithms (Paramiko backend)."))
        policy = self._section(page, _("Host keys"))
        self._policy_strict = Gtk.RadioButton.new_with_mnemonic(None, _("_Strict — reject unknown hosts"))
        self._policy_auto = Gtk.RadioButton.new_with_mnemonic_from_widget(self._policy_strict, _("_Auto-add new hosts"))
        (self._policy_auto if settings.host_key_policy == "auto-add" else self._policy_strict).set_active(True)
        policy.add_widget(self._policy_strict)
        policy.add_widget(self._policy_auto)
        algorithms = self._section(page, _("Algorithms"), _("Comma-separated lists; leave empty to allow the defaults."))
        self._cipher_entry = entry(", ".join(settings.allowed_ciphers), "aes256-gcm@openssh.com, aes128-ctr")
        self._kex_entry = entry(", ".join(settings.allowed_kex), "curve25519-sha256")
        algorithms.add(_("_Ciphers"), self._cipher_entry, label_group=self._labels)
        algorithms.add(_("_Key exchange"), self._kex_entry, label_group=self._labels)

    def _build_secrets(self) -> None:
        policy = self._settings.secrets_policy_with_defaults()
        page = self.layout.new_page("secrets", _("Secrets Policy"), _("How plain-text passwords in host entries are treated."))
        sec = self._section(page)
        self._allow_plaintext_check = Gtk.CheckButton.new_with_mnemonic(_("_Allow plain-text passwords on hosts"))
        self._allow_plaintext_check.set_active(bool(policy.get("allow_plaintext_host_passwords", True)))
        self._warn_plaintext_check = Gtk.CheckButton.new_with_mnemonic(_("_Warn when saving plain-text passwords"))
        self._warn_plaintext_check.set_active(bool(policy.get("warn_on_plaintext_host_passwords", True)))
        for check in (self._allow_plaintext_check, self._warn_plaintext_check):
            sec.add_widget(check)
        self._rotation_spin = _spin(1, 3650, 1, float(policy.get("rotation_hint_days", 90)))
        sec.add(_("Suggest _rotation after (days)"), self._rotation_spin, label_group=self._labels)

    def _build_master_password(self) -> None:
        page = self.layout.new_page("master_password", _("Master Password"),
                                    _("The master password unlocks the app and encrypts locally stored passwords."))
        sec = self._section(page)
        self._disable_master_check = Gtk.CheckButton.new_with_mnemonic(_("_Don't ask for the master password on startup"))
        self._disable_master_check.set_active(self._settings.master_password_disabled)
        sec.add_widget(self._disable_master_check)
        warning = Gtk.Label(label=_("When disabled, passwords saved in the local password file are stored unencrypted."))
        warning.set_xalign(0.0)
        warning.set_line_wrap(True)
        warning.get_style_context().add_class("form-help")
        sec.add_widget(warning)
        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.S)
        reset = Gtk.Button.new_with_mnemonic(_("_Change Master Password…"))
        reset.connect("clicked", lambda *_: self._on_reset and self._on_reset())
        export = Gtk.Button.new_with_mnemonic(_("_Export Settings & Policies…"))
        export.connect("clicked", lambda *_: self._on_export and self._on_export())
        buttons.pack_start(reset, False, False, 0)
        buttons.pack_start(export, False, False, 0)
        page.append(buttons)

    def _build_file_transfer(self) -> None:
        prefs = self._settings.file_transfer
        page = self.layout.new_page("file_transfer", _("File Transfer"),
                                    _("FTP, FTPS and SFTP file browsers and the transfer queue."))
        files = self._section(page, _("Files"))
        row, self._download_dir_entry = _path_row(prefs.download_dir, self._choose_download_dir)
        self._download_dir_entry.set_placeholder_text(_("Home folder"))
        set_accessible_name(self._download_dir_entry, _("Download folder"))
        files.add(_("_Download folder"), row, label_group=self._labels,
                  help_text=_("The local side of a new file browser starts here (unless the host sets a folder)."))
        self._conflict_combo = _combo(CONFLICT_CHOICES, prefs.conflict_policy)
        files.add(_("When a file _exists"), self._conflict_combo, label_group=self._labels)
        self._show_hidden_check = Gtk.CheckButton.new_with_mnemonic(_("Show _hidden files"))
        self._show_hidden_check.set_active(prefs.show_hidden)
        self._remember_dirs_check = Gtk.CheckButton.new_with_mnemonic(_("_Remember the last folders of each host"))
        self._remember_dirs_check.set_active(prefs.remember_last_dirs)
        self._open_default_check = Gtk.CheckButton.new_with_mnemonic(
            _("_Open remote files with the default application"))
        self._open_default_check.set_active(prefs.open_with_default_app)
        self._clean_temp_check = Gtk.CheckButton.new_with_mnemonic(_("_Delete temporary copies when closing"))
        self._clean_temp_check.set_active(prefs.clean_temp_on_exit)
        for check in (self._show_hidden_check, self._remember_dirs_check, self._open_default_check,
                      self._clean_temp_check):
            files.add_widget(check)

        queue = self._section(page, _("Transfers"))
        self._concurrency_spin = _spin(1, 16, 1, float(prefs.concurrency))
        queue.add(_("_Parallel transfers"), self._concurrency_spin, label_group=self._labels,
                  help_text=_("At most this many files at once; each host also has its own connection limit."))
        self._bandwidth_spin = _spin(0, 10_000_000, 64, float(prefs.bandwidth_limit_kbps))
        queue.add(_("_Bandwidth limit (KiB/s)"), self._bandwidth_spin, label_group=self._labels,
                  help_text=_("0 means unlimited."))

        safety = self._section(page, _("Security and diagnostics"))
        self._warn_ftp_check = Gtk.CheckButton.new_with_mnemonic(_("_Warn before connecting with unencrypted FTP"))
        self._warn_ftp_check.set_active(prefs.warn_plain_ftp)
        safety.add_widget(self._warn_ftp_check)
        self._protocol_log_check = Gtk.CheckButton.new_with_mnemonic(_("Keep a _protocol log file"))
        self._protocol_log_check.set_active(prefs.protocol_log)
        safety.add_widget(self._protocol_log_check)
        note = Gtk.Label(label=_("The log records server commands and replies for troubleshooting. Passwords are "
                                 "always masked; the file is rotated and readable only by you."))
        note.set_xalign(0.0)
        note.set_line_wrap(True)
        note.get_style_context().add_class("form-help")
        safety.add_widget(note)

    def _build_remote_desktop(self) -> None:
        prefs = self._settings.remote_desktop
        page = self.layout.new_page("remote_desktop", _("Remote Desktop"),
                                    _("RDP sessions run through FreeRDP, which is installed separately."))
        engine = self._section(page, _("Engine"))
        self._rdp_engine_combo = _combo([("auto", _("Automatic (embedded when possible)")),
                                         ("embedded", _("Embedded in the tab")),
                                         ("external", _("External window"))], prefs.preferred_engine)
        engine.add(_("_Show sessions"), self._rdp_engine_combo, label_group=self._labels,
                   help_text=_("Embedded needs gtk-frdp, or FreeRDP on X11. A host can choose its own engine."))
        row, self._rdp_binary_entry = _path_row(prefs.binary_path, self._choose_rdp_binary)
        self._rdp_binary_entry.set_placeholder_text(_("Found automatically"))
        set_accessible_name(self._rdp_binary_entry, _("FreeRDP client"))
        engine.add(_("FreeRDP _client"), row, label_group=self._labels,
                   help_text=_("Only when the right client is not on PATH (xfreerdp3, xfreerdp, sdl-freerdp…)."))
        self._rdp_sessions_spin = _spin(1, 64, 1, float(prefs.max_sessions))
        engine.add(_("_Sessions at once"), self._rdp_sessions_spin, label_group=self._labels)
        diagnostics = Gtk.Button.new_with_mnemonic(_("_Diagnostics…"))
        diagnostics.set_tooltip_text(_("Which FreeRDP clients and engines were found, and why others are not used"))
        diagnostics.connect("clicked", lambda *_: self.open_rdp_diagnostics())
        diagnostics.set_halign(Gtk.Align.START)
        engine.add_widget(diagnostics)

    def open_rdp_diagnostics(self):
        from .rdp_diagnostics import RdpDiagnosticsDialog

        dialog = RdpDiagnosticsDialog(self, preference=self._rdp_engine_combo.get_active_id() or "auto",
                                      override=self._rdp_binary_entry.get_text().strip())
        dialog.connect("response", lambda d, _r: d.destroy())
        return dialog

    def remote_desktop_values(self) -> RemoteDesktopPreferences:
        return RemoteDesktopPreferences.from_dict({
            "preferred_engine": self._rdp_engine_combo.get_active_id() or "auto",
            "binary_path": self._rdp_binary_entry.get_text().strip(),
            "max_sessions": int(self._rdp_sessions_spin.get_value()),
            "release_shortcut": self._settings.remote_desktop.release_shortcut,
        })

    def file_transfer_values(self) -> FileTransferPreferences:
        return FileTransferPreferences.from_dict({
            "download_dir": self._download_dir_entry.get_text().strip(),
            "conflict_policy": self._conflict_combo.get_active_id() or "ask",
            "concurrency": int(self._concurrency_spin.get_value()),
            "warn_plain_ftp": self._warn_ftp_check.get_active(),
            "open_with_default_app": self._open_default_check.get_active(),
            "clean_temp_on_exit": self._clean_temp_check.get_active(),
            "bandwidth_limit_kbps": int(self._bandwidth_spin.get_value()),
            "show_hidden": self._show_hidden_check.get_active(),
            "remember_last_dirs": self._remember_dirs_check.get_active(),
            "protocol_log": self._protocol_log_check.get_active(),
        })

    def _build_git(self) -> None:
        settings = self._settings
        page = self.layout.new_page("git", _("Git Sync"), _("Watch a Git checkout of your SSH configs and get notified about new commits."))
        sec = self._section(page)
        self._git_watch_enable = Gtk.CheckButton.new_with_mnemonic(_("_Check the remote for updates"))
        self._git_watch_enable.set_active(settings.git_watch_enabled)
        sec.add_widget(self._git_watch_enable)
        row, self._git_path_entry = _path_row(settings.git_watch_path, self._choose_git_path)
        self._git_path_row = sec.add(_("_Repository"), row, label_group=self._labels,
                                     help_text=_("Defaults to the shared inventory folder when empty."))
        self._git_interval_spin = _spin(60, 3600, 30, float(settings.git_watch_interval))
        sec.add(_("Check _interval (seconds)"), self._git_interval_spin, label_group=self._labels)
        self._git_watch_enable.connect("toggled", lambda btn: self._set_sensitive([row, self._git_interval_spin], btn.get_active()))
        self._set_sensitive([row, self._git_interval_spin], settings.git_watch_enabled)

    def _build_inventory(self, inventory: Dict[str, object]) -> None:
        """Where the app keeps its groups and hosts, with backup, reveal and export."""

        page = self.layout.new_page("inventory", _("Inventory"),
                                    _("SSH Manager keeps its groups and hosts in one database."))
        sec = self._section(page)
        location = Gtk.Label(label=str(inventory.get("location", "")), xalign=0.0)
        location.set_selectable(True)
        location.set_line_wrap(True)
        location.get_style_context().add_class("mono")
        sec.add(_("Location"), location, label_group=self._labels,
                help_text=str(inventory.get("detail") or _("Backups are kept in the backups folder next to it.")))
        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.S)
        self.inventory_buttons = {}
        for key, label in (("backup", _("_Back Up Now")), ("reveal", _("_Reveal in File Manager")),
                           ("export", _("_Export…"))):
            button = Gtk.Button.new_with_mnemonic(label)
            callback = inventory.get(key)
            button.set_sensitive(callable(callback))
            if callable(callback):
                button.connect("clicked", lambda _b, cb=callback: cb())
            buttons.pack_start(button, False, False, 0)
            self.inventory_buttons[key] = button
        sec.add_widget(buttons)

    def _build_audit(self) -> None:
        settings = self._settings
        page = self.layout.new_page("audit", _("Audit Logging"), _("Record connections and edits to a local log file."))
        sec = self._section(page)
        self._audit_enable = Gtk.CheckButton.new_with_mnemonic(_("_Enable audit logging"))
        self._audit_enable.set_active(settings.enable_audit_logging)
        sec.add_widget(self._audit_enable)
        row, self._audit_path_entry = _path_row(settings.audit_log_path, self._choose_audit_path)
        sec.add(_("Log _file"), row, label_group=self._labels)
        self._audit_enable.connect("toggled", lambda btn: row.set_sensitive(btn.get_active()))
        row.set_sensitive(settings.enable_audit_logging)

    @staticmethod
    def _set_sensitive(widgets, sensitive: bool) -> None:
        for widget in widgets:
            widget.set_sensitive(sensitive)

    # ------------------------------------------------------------ values
    def values(self) -> PreferencesValues:
        return PreferencesValues(
            theme_mode=ThemeMode.coerce(self._theme_combo.get_active_id()),
            terminal_font_scale=float(self._font_spin.get_value()),
            ui_font_size=int(self._ui_font_spin.get_value()),
            host_key_policy="auto-add" if self._policy_auto.get_active() else "reject",
            connect_timeout=float(self._connect_timeout_spin.get_value()),
            auth_timeout=float(self._auth_timeout_spin.get_value()),
            banner_timeout=float(self._banner_timeout_spin.get_value()),
            use_paramiko_vte=self._paramiko_check.get_active(),
            master_password_disabled=self._disable_master_check.get_active(),
            allowed_ciphers=[v.strip() for v in self._cipher_entry.get_text().split(",") if v.strip()],
            allowed_kex=[v.strip() for v in self._kex_entry.get_text().split(",") if v.strip()],
            secrets_policy={
                "allow_plaintext_host_passwords": self._allow_plaintext_check.get_active(),
                "warn_on_plaintext_host_passwords": self._warn_plaintext_check.get_active(),
                "rotation_hint_days": int(self._rotation_spin.get_value()),
            },
            enable_audit_logging=self._audit_enable.get_active(),
            audit_log_path=self._audit_path_entry.get_text().strip(),
            git_watch_enabled=self._git_watch_enable.get_active(),
            git_watch_path=self._git_path_entry.get_text().strip(),
            git_watch_interval=int(self._git_interval_spin.get_value()),
            latency_probe=self._latency_check.get_active(),
            terminal_appearance=self._appearance_from_controls(),
            density=Density.coerce(self._density_combo.get_active_id()),
            tab_label_mode=TabLabelMode.coerce(self._tab_label_combo.get_active_id()),
            file_transfer=self.file_transfer_values(),
            remote_desktop=self.remote_desktop_values(),
            terminal_input=self.terminal_input_values(),
            shortcuts=self.shortcuts_page.editor.to_dict() if self.shortcuts_page is not None else None,
            local_shell=LocalShellSettings.from_dict({
                "command": self._shell_entry.get_text().strip(),
                "login_shell": self._login_shell_check.get_active(),
                "start_in": self._start_in_combo.get_active_id() or "current",
                "on_exit": self._on_exit_combo.get_active_id() or "close",
            }),
        )

    def _build_shortcuts(self, registry) -> None:
        from .shortcuts_page import ShortcutEditor, ShortcutsPage

        page = self.layout.new_page("shortcuts", _("Keyboard Shortcuts"),
                                    _("Click a row to change it. Shortcuts that are plain keys or Ctrl+letter take "
                                      "those keys away from programs in the terminal."))
        self.shortcuts_page = ShortcutsPage(ShortcutEditor(registry), lambda: self)
        page.append(self.shortcuts_page, expand=True)

    def terminal_input_values(self) -> TerminalInputSettings:
        return TerminalInputSettings.from_dict({
            "copy_on_select": self._settings.terminal_input.copy_on_select,
            "copy_on_select_clipboard": self._copy_select_clipboard_check.get_active(),
            "middle_click": self._middle_click_combo.get_active_id() or "paste_primary",
            "right_click": self._right_click_combo.get_active_id() or "menu",
        })

    def _appearance_from_controls(self) -> TerminalAppearanceSettings:
        custom = self._custom_colors_check.get_active()
        system_font = self._use_system_font.get_active()
        return TerminalAppearanceSettings(
            use_system_mono_font=system_font,
            custom_font=None if system_font else self._custom_font,
            font_size=None if system_font else float(self._font_size_spin.get_value()),
            color_scheme=self._scheme_combo.get_active_id() or "default",
            use_custom_colors=custom,
            fg_color=_hex(self._fg_button.get_rgba()) if custom else None,
            bg_color=_hex(self._bg_button.get_rgba()) if custom else None,
            palette=self._appearance.palette,
            cursor_shape=self._cursor_combo.get_active_id() or "block",
            cursor_blink=self._cursor_blink_check.get_active(),
            bold_is_bright=self._bold_is_bright_check.get_active(),
        )

    # ------------------------------------------------------------ terminal
    def _update_font_controls(self) -> None:
        custom = not self._use_system_font.get_active()
        self._font_button.set_sensitive(custom)
        self._font_size_spin.set_sensitive(custom)
        self._update_preview_terminal()

    def _update_color_controls(self) -> None:
        custom = self._custom_colors_check.get_active()
        self._fg_button.set_sensitive(custom)
        self._bg_button.set_sensitive(custom)
        self._scheme_combo.set_sensitive(not custom)
        self._update_preview_terminal()

    def _choose_font(self, _button: Gtk.Button) -> None:
        chooser = Gtk.FontChooserDialog(title=_("Choose Terminal Font"), transient_for=self, modal=True)
        chooser.set_filter_func(lambda family, _face, _data=None: family.is_monospace(), None)
        if self._custom_font:
            chooser.set_font(self._custom_font)
        response = chooser.run()
        font_name = chooser.get_font() if response == Gtk.ResponseType.OK else None
        chooser.destroy()
        if font_name:
            self._custom_font = font_name
            self._font_button.set_label(font_name)
            self._update_preview_terminal()

    def _update_preview_terminal(self) -> None:
        if not hasattr(self, "_preview_term"):
            return
        appearance = self._appearance_from_controls()
        term = self._preview_term
        if not appearance.use_system_mono_font:
            desc = Pango.FontDescription(appearance.custom_font or "Monospace")
            if appearance.font_size:
                desc.set_size(int(appearance.font_size * Pango.SCALE))
            term.set_font(desc)
        else:
            term.set_font(None)
        colors = resolve_terminal_colors(appearance)
        palette = [_rgba(c) for c in colors.palette]
        term.set_colors(_rgba(colors.foreground), _rgba(colors.background), [c for c in palette if c is not None])
        term.set_color_cursor(_rgba(colors.cursor))
        term.set_color_highlight(_rgba(colors.highlight))
        term.set_bold_is_bright(appearance.bold_is_bright)
        shape = {"ibeam": Vte.CursorShape.IBEAM, "underline": Vte.CursorShape.UNDERLINE}.get(appearance.cursor_shape, Vte.CursorShape.BLOCK)
        term.set_cursor_shape(shape)
        term.set_cursor_blink_mode(Vte.CursorBlinkMode.ON if appearance.cursor_blink else Vte.CursorBlinkMode.OFF)

    # --------------------------------------------------------------- paths
    def _choose_audit_path(self, path_entry: Gtk.Entry) -> None:
        current = path_entry.get_text()
        filename = choose_save_file(self, _("Audit Log File"), current_file=current, current_name="audit.log")
        if filename:
            path_entry.set_text(filename)

    def _choose_rdp_binary(self, path_entry: Gtk.Entry) -> None:
        current = path_entry.get_text() or "/usr/bin"
        filename = choose_open_file(self, _("FreeRDP Client"), current_folder=os.path.dirname(current) or "/usr/bin")
        if filename:
            path_entry.set_text(filename)

    def _choose_download_dir(self, path_entry: Gtk.Entry) -> None:
        current = path_entry.get_text() or os.path.expanduser("~")
        folder = choose_folder(self, _("Download Folder"), current_folder=current)
        if folder:
            path_entry.set_text(folder)

    def _choose_git_path(self, path_entry: Gtk.Entry) -> None:
        current = path_entry.get_text() or os.path.expanduser("~")
        folder = choose_folder(self, _("Git Repository"), current_folder=current)
        if folder:
            path_entry.set_text(folder)
