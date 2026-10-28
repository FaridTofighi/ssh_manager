"""Persistent application settings for the SSH manager."""

from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

from .persistence import write_json_atomic

DEFAULT_CONFIG_FILENAME = "settings.json"
APP_NAME = "ssh-manager"
LOGGER = logging.getLogger(__name__)


def _default_config_dir() -> Path:
    home = os.environ.get("XDG_CONFIG_HOME")
    if home:
        return Path(home).expanduser() / APP_NAME
    return Path.home() / ".config" / APP_NAME


def _default_data_dir() -> Path:
    home = os.environ.get("XDG_DATA_HOME")
    if home:
        return Path(home).expanduser() / APP_NAME
    return Path.home() / ".local" / "share" / APP_NAME


@dataclass
class TerminalAppearanceSettings:
    """User-facing terminal appearance preferences."""

    use_system_mono_font: bool = True
    custom_font: Optional[str] = None
    font_size: Optional[float] = None

    color_scheme: str = "default"  # default, dark, light, solarized-dark, solarized-light
    use_custom_colors: bool = False
    fg_color: Optional[str] = None
    bg_color: Optional[str] = None
    palette: Optional[list[str]] = None

    cursor_shape: str = "block"  # block, ibeam, underline
    cursor_blink: bool = True
    bold_is_bright: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "use_system_mono_font": self.use_system_mono_font,
            "custom_font": self.custom_font,
            "font_size": self.font_size,
            "color_scheme": self.color_scheme,
            "use_custom_colors": self.use_custom_colors,
            "fg_color": self.fg_color,
            "bg_color": self.bg_color,
            "palette": self.palette,
            "cursor_shape": self.cursor_shape,
            "cursor_blink": self.cursor_blink,
            "bold_is_bright": self.bold_is_bright,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TerminalAppearanceSettings":
        if not isinstance(data, dict):
            return cls()

        def bool_value(key: str, default: bool) -> bool:
            value = data.get(key, default)
            return value if isinstance(value, bool) else default

        def str_value(key: str, default: str) -> str:
            value = data.get(key, default)
            return value if isinstance(value, str) else default

        def optional_str(key: str) -> Optional[str]:
            value = data.get(key)
            return value if isinstance(value, str) and value else None

        def optional_float(key: str) -> Optional[float]:
            value = data.get(key)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                return None
            number = float(value)
            return number if math.isfinite(number) and number > 0 else None

        def palette_value() -> Optional[list[str]]:
            value = data.get("palette")
            if not isinstance(value, list):
                return None
            colors = [color.strip() for color in value if isinstance(color, str) and color.strip()]
            return colors or None

        return cls(
            use_system_mono_font=bool_value("use_system_mono_font", True),
            custom_font=optional_str("custom_font"),
            font_size=optional_float("font_size"),
            color_scheme=str_value("color_scheme", "default"),
            use_custom_colors=bool_value("use_custom_colors", False),
            fg_color=optional_str("fg_color"),
            bg_color=optional_str("bg_color"),
            palette=palette_value(),
            cursor_shape=str_value("cursor_shape", "block"),
            cursor_blink=bool_value("cursor_blink", True),
            bold_is_bright=bool_value("bold_is_bright", True),
        )


CONFLICT_POLICIES = ("ask", "overwrite", "overwrite_newer", "skip", "rename", "resume")


@dataclass
class FileTransferPreferences:
    """Preferences → File transfer (FTP / FTPS / SFTP tabs and the transfer queue)."""

    download_dir: str = ""  # "" = the home folder
    conflict_policy: str = "ask"
    concurrency: int = 3  # global cap of running transfers (per host: its max_connections)
    warn_plain_ftp: bool = True
    open_with_default_app: bool = True  # "Open" a remote file with the desktop's default application
    clean_temp_on_exit: bool = True
    bandwidth_limit_kbps: int = 0  # global; 0 = unlimited
    show_hidden: bool = False
    remember_last_dirs: bool = True
    protocol_log: bool = False  # connection log pane (passwords always masked)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "FileTransferPreferences":
        prefs = cls()
        for key, default in vars(cls()).items():
            value = data.get(key, default)
            if isinstance(default, bool):
                value = value if isinstance(value, bool) else default
            elif isinstance(default, int):
                value = value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else default
            elif isinstance(default, str):
                value = value if isinstance(value, str) else default
            setattr(prefs, key, value)
        if prefs.conflict_policy not in CONFLICT_POLICIES:
            prefs.conflict_policy = "ask"
        prefs.concurrency = min(max(prefs.concurrency, 1), 16)
        return prefs

    def to_dict(self) -> Dict[str, Any]:
        return dict(vars(self))


RDP_ENGINE_PREFERENCES = ("auto", "embedded", "external")


@dataclass
class RemoteDesktopPreferences:
    """Preferences → Remote Desktop (RDP sessions through FreeRDP; ``docs/rdp.md``)."""

    preferred_engine: str = "auto"  # auto | embedded | external
    binary_path: str = ""  # a FreeRDP client to use instead of the one found on PATH
    max_sessions: int = 8  # RDP sessions running at the same time
    release_shortcut: str = "<Control><Alt>Control_R"  # gives the keyboard back to this app (embedded engines)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RemoteDesktopPreferences":
        prefs = cls()
        engine = data.get("preferred_engine")
        if engine in RDP_ENGINE_PREFERENCES:
            prefs.preferred_engine = engine
        path = data.get("binary_path")
        if isinstance(path, str) and len(path) <= 4096 and not any(ord(ch) < 32 for ch in path):
            prefs.binary_path = path.strip()
        count = data.get("max_sessions")
        if isinstance(count, int) and not isinstance(count, bool):
            prefs.max_sessions = min(max(count, 1), 64)
        shortcut = data.get("release_shortcut")
        if isinstance(shortcut, str) and 0 < len(shortcut) <= 64:
            prefs.release_shortcut = shortcut
        return prefs

    def to_dict(self) -> Dict[str, Any]:
        return dict(vars(self))


MIDDLE_CLICK_CHOICES = ("paste_primary", "paste_clipboard", "none")  # same as terminal_input.py
RIGHT_CLICK_CHOICES = ("menu", "paste_clipboard", "paste_primary", "none")


@dataclass
class TerminalInputSettings:
    """Preferences → Terminal: mouse and selection behaviour of every terminal (``docs/terminal.md``)."""

    copy_on_select: bool = True  # selecting copies to PRIMARY (what VTE does; always on in practice)
    copy_on_select_clipboard: bool = False  # … and to the clipboard as well
    middle_click: str = "paste_primary"  # paste_primary | paste_clipboard | none
    right_click: str = "menu"  # menu | paste_clipboard | paste_primary | none (Shift+right-click: always the menu)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TerminalInputSettings":
        prefs = cls()
        for name in ("copy_on_select", "copy_on_select_clipboard"):
            value = data.get(name)
            if isinstance(value, bool):
                setattr(prefs, name, value)
            elif value is not None:
                LOGGER.warning("settings: terminal_input.%s must be true or false; the default is used", name)
        for name, choices in (("middle_click", MIDDLE_CLICK_CHOICES), ("right_click", RIGHT_CLICK_CHOICES)):
            value = data.get(name)
            if value in choices:
                setattr(prefs, name, value)
            elif value is not None:
                LOGGER.warning("settings: terminal_input.%s must be one of %s; the default is used",
                               name, ", ".join(choices))
        return prefs

    def to_dict(self) -> Dict[str, Any]:
        return dict(vars(self))


@dataclass
class AppSettings:
    """Application-wide settings with JSON persistence."""

    config_path: Path = field(default_factory=lambda: _default_config_dir() / DEFAULT_CONFIG_FILENAME)
    remember_password_default: bool = True  # unused since passwords are only saved when asked (kept for old files)
    host_key_policy: str = "reject"  # or "auto-add"
    connect_timeout: float = 10.0
    auth_timeout: float = 10.0
    banner_timeout: float = 10.0
    use_paramiko_vte: bool = False
    master_password_hash: str = ""
    master_password_disabled: bool = False
    allowed_ciphers: list[str] = field(default_factory=list)
    allowed_kex: list[str] = field(default_factory=list)
    secrets_policy: Dict[str, Any] = field(default_factory=dict)
    enable_audit_logging: bool = False
    audit_log_path: str = field(default_factory=lambda: str(_default_data_dir() / "audit.log"))
    git_watch_enabled: bool = False
    git_watch_path: str = ""
    git_watch_interval: int = 600
    terminal_appearance: TerminalAppearanceSettings = field(default_factory=TerminalAppearanceSettings)
    file_transfer: FileTransferPreferences = field(default_factory=FileTransferPreferences)
    remote_desktop: RemoteDesktopPreferences = field(default_factory=RemoteDesktopPreferences)
    terminal_input: TerminalInputSettings = field(default_factory=TerminalInputSettings)

    @classmethod
    def load(cls, config_path: Optional[Path] = None) -> "AppSettings":
        path = config_path or (_default_config_dir() / DEFAULT_CONFIG_FILENAME)
        instance = cls(config_path=path)
        if not path.exists():
            return instance
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return instance
        if not isinstance(data, dict):
            return instance

        def get_value(container: Dict[str, Any], key: str, expected_type, default):
            value = container.get(key, default)
            if isinstance(value, expected_type):
                return value
            return default

        def bounded_float(value: Any, default: float, minimum: float, maximum: float) -> float:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return default
            number = float(value)
            if not math.isfinite(number) or number < minimum or number > maximum:
                return default
            return number

        def bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return default
            if isinstance(value, float) and not math.isfinite(value):
                return default
            number = int(value)
            if number < minimum or number > maximum:
                return default
            return number

        instance.remember_password_default = get_value(data, "remember_password_default", bool, instance.remember_password_default)
        host_key_policy = get_value(data, "host_key_policy", str, instance.host_key_policy)
        instance.host_key_policy = host_key_policy if host_key_policy in {"reject", "auto-add"} else "reject"
        instance.use_paramiko_vte = get_value(data, "use_paramiko_vte", bool, instance.use_paramiko_vte)
        instance.master_password_hash = get_value(data, "master_password_hash", str, instance.master_password_hash)
        instance.master_password_disabled = get_value(
            data,
            "master_password_disabled",
            bool,
            instance.master_password_disabled,
        )
        ciphers = data.get("allowed_ciphers")
        if isinstance(ciphers, list):
            instance.allowed_ciphers = [v.strip() for v in ciphers if isinstance(v, str) and v.strip()]
        kex = data.get("allowed_kex")
        if isinstance(kex, list):
            instance.allowed_kex = [v.strip() for v in kex if isinstance(v, str) and v.strip()]
        policy = data.get("secrets_policy")
        if isinstance(policy, dict):
            instance.secrets_policy = dict(policy)
        instance.enable_audit_logging = get_value(
            data,
            "enable_audit_logging",
            bool,
            instance.enable_audit_logging,
        )
        instance.audit_log_path = get_value(
            data,
            "audit_log_path",
            str,
            instance.audit_log_path,
        )
        instance.git_watch_enabled = get_value(
            data,
            "git_watch_enabled",
            bool,
            instance.git_watch_enabled,
        )
        instance.git_watch_path = get_value(
            data,
            "git_watch_path",
            str,
            instance.git_watch_path,
        )
        instance.git_watch_interval = bounded_int(
            data.get("git_watch_interval"),
            instance.git_watch_interval,
            60,
            3600,
        )
        appearance = data.get("terminal_appearance")
        instance.terminal_appearance = TerminalAppearanceSettings.from_dict(appearance if isinstance(appearance, dict) else {})
        transfer = data.get("file_transfer")
        instance.file_transfer = FileTransferPreferences.from_dict(transfer if isinstance(transfer, dict) else {})
        desktop = data.get("remote_desktop")
        instance.remote_desktop = RemoteDesktopPreferences.from_dict(desktop if isinstance(desktop, dict) else {})
        terminal_input = data.get("terminal_input")
        instance.terminal_input = TerminalInputSettings.from_dict(terminal_input if isinstance(terminal_input, dict) else {})

        timeouts = data.get("timeouts")
        if isinstance(timeouts, dict):
            instance.connect_timeout = bounded_float(timeouts.get("connect"), instance.connect_timeout, 1.0, 600.0)
            instance.auth_timeout = bounded_float(timeouts.get("auth"), instance.auth_timeout, 1.0, 600.0)
            instance.banner_timeout = bounded_float(timeouts.get("banner"), instance.banner_timeout, 1.0, 600.0)
        else:
            # Backwards compatibility with previous flat keys
            instance.connect_timeout = bounded_float(data.get("connect_timeout"), instance.connect_timeout, 1.0, 600.0)
            instance.auth_timeout = bounded_float(data.get("auth_timeout"), instance.auth_timeout, 1.0, 600.0)
            instance.banner_timeout = bounded_float(data.get("banner_timeout"), instance.banner_timeout, 1.0, 600.0)

        return instance

    def save(self) -> None:
        payload: Dict[str, Any] = {
            "remember_password_default": self.remember_password_default,
            "host_key_policy": self.host_key_policy,
            "timeouts": {
                "connect": self.connect_timeout,
                "auth": self.auth_timeout,
                "banner": self.banner_timeout,
            },
            "use_paramiko_vte": self.use_paramiko_vte,
            "master_password_hash": self.master_password_hash,
            "master_password_disabled": self.master_password_disabled,
            "allowed_ciphers": self.allowed_ciphers,
            "allowed_kex": self.allowed_kex,
            "secrets_policy": self.secrets_policy,
            "enable_audit_logging": self.enable_audit_logging,
            "audit_log_path": self.audit_log_path,
            "git_watch_enabled": self.git_watch_enabled,
            "git_watch_path": self.git_watch_path,
            "git_watch_interval": self.git_watch_interval,
            "terminal_appearance": self.terminal_appearance.to_dict(),
            "file_transfer": self.file_transfer.to_dict(),
            "remote_desktop": self.remote_desktop.to_dict(),
            "terminal_input": self.terminal_input.to_dict(),
        }
        try:
            write_json_atomic(self.config_path, payload, mode=0o600)
        except OSError:
            # Best effort; if settings cannot be saved we silently ignore here.
            pass

    def secrets_policy_with_defaults(self) -> Dict[str, Any]:
        defaults = {
            "allow_plaintext_host_passwords": True,
            "warn_on_plaintext_host_passwords": True,
            "prefer_vault_for_passwords": True,
            "rotation_hint_days": 90,
        }
        for key, value in (self.secrets_policy or {}).items():
            if key in defaults:
                defaults[key] = value
        return defaults
