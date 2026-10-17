from __future__ import annotations

import json
import logging
import math
import os
import re
import signal
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Vte", "2.91")

from gi.repository import Gdk, GLib, GObject, Gtk, Vte, Pango  # type: ignore  # noqa: E402

from ssh_manager.config import HostEntry
from ssh_manager.host_utils import host_connection_params
from ssh_manager.ssh_command import build_ssh_command
from ssh_manager.secure_store import SecureStore, SecureStoreError
from ssh_manager.text_utils import strip_ansi
from ssh_manager.persistence import write_text_atomic
from ssh_manager.password_change import PasswordChangeTracker
# --- BEGIN: PARAMIKO_VTE_IMPORTS ---
from ssh_manager.vte_bridge import VTEBridge
# --- END: PARAMIKO_VTE_IMPORTS ---
from ssh_manager.settings import TerminalAppearanceSettings

LogFn = Callable[[str, bool], None]
PasswordUpdateFn = Callable[[HostEntry, str], bool]
LOGGER = logging.getLogger(__name__)
AUTH_FAILURE_RE = re.compile(
    r"permission\s+denied,\s+please\s+try\s+again|"
    r"permission\s+denied[^\n\r]*\bpassword\b|"
    r"authentication\s+failed",
    re.IGNORECASE,
)

_REMOTE_STATS_SCRIPT = """
import json
import os
import time

def cpu_percent():
    with open('/proc/stat', 'r') as f:
        parts = f.readline().split()
    idle_a = int(parts[4])
    total_a = sum(int(x) for x in parts[1:])
    time.sleep(0.2)
    with open('/proc/stat', 'r') as f:
        parts = f.readline().split()
    idle_b = int(parts[4])
    total_b = sum(int(x) for x in parts[1:])
    idle_delta = idle_b - idle_a
    total_delta = total_b - total_a
    if total_delta <= 0:
        return 0.0
    return max(0.0, min(100.0, (1.0 - idle_delta / total_delta) * 100.0))

def mem_usage():
    info = {}
    with open('/proc/meminfo', 'r') as f:
        for line in f:
            key, val, *_ = line.split()
            info[key.rstrip(':')] = int(val)
    total = info.get('MemTotal', 0)
    available = info.get('MemAvailable', info.get('MemFree', 0))
    used = total - available
    return used / 1024.0, total / 1024.0

def disk_usage():
    st = os.statvfs('/')
    total = st.f_blocks * st.f_frsize
    free = st.f_bavail * st.f_frsize
    used = total - free
    gb = 1024.0 ** 3
    return used / gb, total / gb

cpu = cpu_percent()
mem_used, mem_total = mem_usage()
disk_used, disk_total = disk_usage()
print(json.dumps({
    'cpu': cpu,
    'mem_used': mem_used,
    'mem_total': mem_total,
    'disk_used': disk_used,
    'disk_total': disk_total
}))
"""


def parse_remote_stats_output(output: str) -> Optional[Dict[str, object]]:
    """Parse the JSON payload produced by the remote stats script."""

    text = output.strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def build_remote_stats_command(
    ssh_args: list[str],
    *,
    password: Optional[str] = None,
    sshpass_path: Optional[str] = None,
) -> Tuple[list[str], Optional[Dict[str, str]]]:
    """Build the auxiliary stats SSH command and optional environment."""

    command = list(ssh_args)
    if not command:
        return [], None

    if password and sshpass_path:
        command[1:1] = ["-o", "ConnectTimeout=6"]
        command.extend(["python3", "-"])
        env = os.environ.copy()
        env["SSHPASS"] = password
        return [sshpass_path, "-e", *command], env

    command[1:1] = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=6"]
    command.extend(["python3", "-"])
    return command, None


def session_tile_position(index: int, count: int) -> Tuple[int, int]:
    """Return row/column for a tab in the all-sessions grid."""

    if index < 0:
        raise ValueError("index must be non-negative")
    columns = max(1, math.ceil(math.sqrt(max(1, count))))
    return index // columns, index % columns


def terminal_key_event_payload(event: Gdk.EventKey) -> Optional[str]:
    """Return terminal bytes represented by a key event, if it is safe to mirror."""

    default_mask = Gtk.accelerator_get_default_mod_mask()
    modifiers = event.state & default_mask
    if modifiers & (Gdk.ModifierType.MOD1_MASK | Gdk.ModifierType.SUPER_MASK):
        return None
    if event.string:
        return event.string
    if modifiers & Gdk.ModifierType.CONTROL_MASK:
        return None

    keyval = Gdk.keyval_to_lower(event.keyval)
    controls = {
        Gdk.KEY_Return: "\r",
        Gdk.KEY_KP_Enter: "\r",
        Gdk.KEY_BackSpace: "\x7f",
        Gdk.KEY_Tab: "\t",
        Gdk.KEY_ISO_Left_Tab: "\t",
        Gdk.KEY_Escape: "\x1b",
        Gdk.KEY_Left: "\x1b[D",
        Gdk.KEY_Right: "\x1b[C",
        Gdk.KEY_Up: "\x1b[A",
        Gdk.KEY_Down: "\x1b[B",
        Gdk.KEY_Home: "\x1b[H",
        Gdk.KEY_End: "\x1b[F",
        Gdk.KEY_Delete: "\x1b[3~",
        Gdk.KEY_Page_Up: "\x1b[5~",
        Gdk.KEY_Page_Down: "\x1b[6~",
    }
    return controls.get(keyval)


def broadcast_target_tabs(
    tabs: List[object],
    source: object,
    included: Dict[object, bool],
    connected: Dict[object, bool],
) -> List[object]:
    """Return the sessions that should receive mirrored input."""

    if not included.get(source, True) or not connected.get(source, False):
        return []
    return [
        tab
        for tab in tabs
        if tab is not source
        and included.get(tab, True)
        and connected.get(tab, False)
    ]


def _terminal_text_result(value) -> str:
    if isinstance(value, tuple):
        return value[0] or ""
    return value or ""


def _select_all_terminal_text(*_args) -> bool:
    return True


def terminal_text(term: Vte.Terminal) -> str:
    """Return all visible/scrollback text from a VTE terminal."""

    try:
        result = term.get_text(_select_all_terminal_text, None, None)
    except TypeError:
        result = term.get_text(_select_all_terminal_text, None)
    return _terminal_text_result(result)


def auth_failure_output_detected(output: str) -> bool:
    """Return True when OpenSSH output shows a password auth failure."""

    return bool(output and AUTH_FAILURE_RE.search(strip_ansi(output)))


def icon_label_content(icon_name: str, label: str) -> Gtk.Box:
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
    image = Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.BUTTON)
    text = Gtk.Label(label=label)
    box.pack_start(image, False, False, 0)
    box.pack_start(text, False, False, 0)
    return box


class TerminalPaneContainer(Gtk.Box):
    """Wrapper for a single terminal pane (prep for future splits)."""

    def __init__(self, terminal: Vte.Terminal) -> None:
        Gtk.Box.__init__(self, orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.terminal = terminal
        self.terminal.set_hexpand(True)
        self.terminal.set_vexpand(True)
        self.pack_start(self.terminal, True, True, 0)
        self.show_all()


class TerminalTab(Gtk.Box):
    """Single SSH session embedded inside a Vte.Terminal."""

    __gtype_name__ = "TerminalTab"
    __gsignals__ = {
        "session-closed": (GObject.SignalFlags.RUN_LAST, None, (str, bool)),
        "status-changed": (GObject.SignalFlags.RUN_LAST, None, (str, bool)),
        "close-requested": (GObject.SignalFlags.RUN_LAST, None, ()),
    }

    def __init__(
        self,
        host: HostEntry,
        log: LogFn,
        font_scale: float = 1.0,
        password_store: Optional[SecureStore] = None,
        password_update_handler: Optional[PasswordUpdateFn] = None,
        autostart: bool = True,
        profile: Optional["SessionProfile"] = None,
        appearance: Optional[TerminalAppearanceSettings] = None,
    ) -> None:
        Gtk.Box.__init__(self, orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._host = host
        self._log = log
        self._font_scale = max(0.5, min(3.0, font_scale))
        self._session_active = False
        self._stats_source_id = 0
        self._stats_updating = False
        self._stats_lock = threading.Lock()
        self._secure_store = password_store
        self._password_update_handler = password_update_handler
        self._bridge: Optional[VTEBridge] = None
        self._session_client = None
        self._paramiko_mode = not autostart
        self._password_source: str = "none"
        self._password_to_save: Optional[str] = None
        self._password_change_tracker = PasswordChangeTracker()
        self._pending_detected_password_change: Optional[str] = None
        self._password_change_source_id = 0
        self._password_prompt_poll_source_id = 0
        self._password_prompt_poll_deadline = 0.0
        self._password_change_terminal: Optional[Vte.Terminal] = None
        self._input_commit_handlers: Dict[Vte.Terminal, int] = {}
        self._last_key_input: Tuple[Vte.Terminal, str, float] | None = None
        self._child_pid: Optional[int] = None
        self._auth_retry_pending = False
        self._ever_connected = False
        self._password_save_source_id = 0
        self._connection_confirm_source_id = 0
        self._retry_timeout_id = 0
        self._secondary_terminal: Optional[Vte.Terminal] = None
        self._paned: Optional[Gtk.Paned] = None
        self._focused_terminal: Optional[Vte.Terminal] = None
        self._input_observer: Optional[Callable[[TerminalTab, str], None]] = None
        self._profile = profile
        self._strip_ansi_on_copy = False
        self._appearance = appearance

        self.set_hexpand(True)
        self.set_vexpand(True)

        self._banner = Gtk.Revealer()
        self._banner.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self._banner.set_reveal_child(False)

        banner_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        banner_box.set_margin_top(6)
        banner_box.set_margin_bottom(6)
        banner_box.set_margin_start(12)
        banner_box.set_margin_end(12)
        banner_box.get_style_context().add_class("info")

        self._status_label = Gtk.Label()
        self._status_label.set_xalign(0.0)
        banner_box.pack_start(self._status_label, True, True, 0)

        self._reconnect_button = Gtk.Button()
        self._reconnect_button.set_relief(Gtk.ReliefStyle.NONE)
        recon_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        image = Gtk.Image.new_from_icon_name("media-playlist-repeat-symbolic", Gtk.IconSize.BUTTON)
        recon_box.pack_start(image, False, False, 0)
        recon_box.pack_start(Gtk.Label(label="Reconnect"), False, False, 0)
        self._reconnect_button.add(recon_box)
        self._reconnect_button.connect("clicked", self._on_reconnect_clicked)
        banner_box.pack_start(self._reconnect_button, False, False, 0)

        self._banner.add(banner_box)
        self.pack_start(self._banner, False, False, 0)

        control_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        duplicate_btn = Gtk.Button(label="Duplicate Pane")
        duplicate_btn.set_tooltip_text("Open a second pane for this same host.")
        duplicate_btn.connect("clicked", lambda *_: self._duplicate_pane_once())
        close_duplicate_btn = Gtk.Button(label="Close Duplicate")
        close_duplicate_btn.set_tooltip_text("Close the duplicate pane for this host.")
        close_duplicate_btn.connect("clicked", lambda *_: self._close_duplicate_pane())
        control_row.pack_start(duplicate_btn, False, False, 0)
        control_row.pack_start(close_duplicate_btn, False, False, 0)
        save_btn = Gtk.Button(label="Save Transcript")
        save_btn.connect("clicked", lambda *_: self.save_transcript())
        control_row.pack_start(save_btn, False, False, 0)
        strip_btn = Gtk.ToggleButton(label="Strip ANSI on Copy")
        strip_btn.set_active(False)
        strip_btn.connect("toggled", lambda btn: self._set_strip_ansi(btn.get_active()))
        control_row.pack_start(strip_btn, False, False, 0)
        stats_btn = Gtk.Button(label="Show Stats")
        stats_btn.connect("clicked", lambda *_: self._show_stats_once())
        control_row.pack_start(stats_btn, False, False, 0)
        self._control_row = control_row
        self.pack_start(control_row, False, False, 0)

        self._terminal = self._create_terminal()
        self._pane_container = TerminalPaneContainer(self._terminal)
        self._pane_holder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._pane_holder.set_hexpand(True)
        self._pane_holder.set_vexpand(True)
        self._pane_holder.pack_start(self._pane_container, True, True, 0)
        self.pack_start(self._pane_holder, True, True, 0)

        info_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        info_box.set_margin_top(6)
        info_box.set_margin_bottom(4)
        info_box.set_margin_start(10)
        info_box.set_margin_end(10)
        info_box.get_style_context().add_class("terminal-info-bar")
        info_box.set_hexpand(True)

        self._info_spinner = Gtk.Spinner()
        self._info_spinner.set_no_show_all(True)
        self._info_spinner.set_size_request(20, 20)
        info_box.pack_start(self._info_spinner, False, False, 0)

        self._cpu_label = Gtk.Label()
        self._cpu_label.set_xalign(0.0)
        info_box.pack_start(self._cpu_label, False, False, 0)

        self._mem_label = Gtk.Label()
        self._mem_label.set_xalign(0.0)
        info_box.pack_start(self._mem_label, False, False, 0)

        self._disk_label = Gtk.Label()
        self._disk_label.set_xalign(0.0)
        info_box.pack_start(self._disk_label, False, False, 0)

        self._info_revealer = Gtk.Revealer()
        self._info_revealer.set_transition_type(Gtk.RevealerTransitionType.CROSSFADE)
        self._info_revealer.set_transition_duration(220)
        self._info_revealer.set_reveal_child(True)
        self._info_revealer.add(info_box)
        self.pack_start(self._info_revealer, False, False, 0)
        self._reset_stats_labels()

        self._terminal.connect("key-press-event", self._on_terminal_key_press)
        self._terminal.connect("child-exited", self._on_child_exited)
        self._terminal.connect("focus-in-event", self._on_terminal_focus)
        self._focused_terminal = self._terminal
        if autostart:
            self._spawn()
        else:
            self._set_status("Connecting…", False)

    # ------------------------------------------------------------------ helpers
    def _reset_stats_labels(self) -> None:
        self._cpu_label.set_text("CPU: —")
        self._mem_label.set_text("Mem: —")
        self._disk_label.set_text("Disk: —")
        self._info_spinner.stop()
        self._info_spinner.hide()
        if hasattr(self, "_info_revealer"):
            self._info_revealer.set_reveal_child(False)
    def _colors_from_appearance(self) -> Tuple[Gdk.RGBA, Gdk.RGBA, Gdk.RGBA, Gdk.RGBA, list[Gdk.RGBA]]:
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

        profile_theme = (self._profile.theme.lower() if self._profile and self._profile.theme else "")
        appearance = self._appearance
        scheme = (appearance.color_scheme.lower() if appearance else "default") if appearance else "default"

        def solarized(dark: bool) -> Tuple[str, str, list[str]]:
            if dark:
                return (
                    "#002b36",
                    "#93a1a1",
                    [
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
                    ],
                )
            return (
                "#fdf6e3",
                "#586e75",
                [
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
                ],
            )

        if appearance and appearance.use_custom_colors and appearance.fg_color and appearance.bg_color:
            foreground_hex = appearance.fg_color
            background_hex = appearance.bg_color
        elif appearance:
            if scheme == "dark":
                background_hex = "#1f2329"
                foreground_hex = "#f5f6f8"
            elif scheme == "light":
                background_hex = "#f8fafc"
                foreground_hex = "#0f172a"
            elif scheme in {"solarized-dark", "solarized dark"}:
                background_hex, foreground_hex, palette_hex = solarized(True)
            elif scheme in {"solarized-light", "solarized light"}:
                background_hex, foreground_hex, palette_hex = solarized(False)
            elif scheme == "default" and profile_theme in {"dark", "light"}:
                if profile_theme == "light":
                    background_hex = "#f8fafc"
                    foreground_hex = "#0f172a"
                else:
                    background_hex = "#1f2329"
                    foreground_hex = "#f5f6f8"

        if appearance and appearance.palette:
            palette_hex = appearance.palette

        background = rgba(background_hex)
        foreground = rgba(foreground_hex)
        cursor = rgba(cursor_hex)
        highlight = rgba(highlight_hex)
        palette = [rgba(color) for color in palette_hex]
        return foreground, background, cursor, highlight, palette

    def _configure_terminal_appearance(self, term: Vte.Terminal) -> None:
        if self._appearance:
            desc = None
            if not self._appearance.use_system_mono_font:
                desc = Pango.FontDescription(self._appearance.custom_font or "Monospace")
                if self._appearance.font_size:
                    desc.set_size(int(self._appearance.font_size * Pango.SCALE))
            if desc:
                term.set_font(desc)
            shape = self._appearance.cursor_shape.lower()
            if shape == "ibeam":
                term.set_cursor_shape(Vte.CursorShape.IBEAM)
            elif shape == "underline":
                term.set_cursor_shape(Vte.CursorShape.UNDERLINE)
            else:
                term.set_cursor_shape(Vte.CursorShape.BLOCK)
            term.set_cursor_blink_mode(
                Vte.CursorBlinkMode.ON if self._appearance.cursor_blink else Vte.CursorBlinkMode.OFF
            )
            term.set_bold_is_bright(self._appearance.bold_is_bright)
        foreground, background, cursor, highlight, palette = self._colors_from_appearance()
        term.set_colors(foreground, background, palette)
        term.set_color_background(background)
        term.set_color_foreground(foreground)
        term.set_color_cursor(cursor)
        try:
            term.set_color_cursor_foreground(background)
        except AttributeError:
            pass
        term.set_color_highlight(highlight)
        try:
            term.set_color_highlight_foreground(foreground)
        except AttributeError:
            pass

    def _apply_terminal_theme(self) -> None:
        for term in self._terminals():
            self._configure_terminal_appearance(term)

    def _terminals(self) -> list[Vte.Terminal]:
        terms = [self._terminal]
        if self._secondary_terminal:
            terms.append(self._secondary_terminal)
        return terms

    def _active_terminal(self) -> Optional[Vte.Terminal]:
        return self._focused_terminal or self._terminal

    def _create_terminal(self) -> Vte.Terminal:
        term = Vte.Terminal()
        term.set_hexpand(True)
        term.set_vexpand(True)
        if self._profile and self._profile.scrollback_lines is not None:
            term.set_scrollback_lines(self._profile.scrollback_lines)
        else:
            term.set_scrollback_lines(50_000)
        term.set_allow_bold(True)
        term.set_cursor_blink_mode(Vte.CursorBlinkMode.SYSTEM)
        term.set_font_scale(self._font_scale)
        self._configure_terminal_appearance(term)
        try:
            handler_id = term.connect("commit", self._on_terminal_commit)
            self._input_commit_handlers[term] = handler_id
        except (TypeError, ValueError):
            pass
        return term

    @property
    def host(self) -> HostEntry:
        return self._host

    def copy_clipboard(self) -> None:
        term = self._active_terminal()
        if not term:
            return
        try:
            if self._strip_ansi_on_copy:
                cleaned = strip_ansi(terminal_text(term))
                clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
                clipboard.set_text(cleaned, -1)
            else:
                term.copy_clipboard()
        except Exception:
            term.copy_clipboard()

    def set_appearance(self, appearance: TerminalAppearanceSettings) -> None:
        self._appearance = appearance
        self._apply_terminal_theme()

    def paste_clipboard(self) -> None:
        term = self._active_terminal()
        if term:
            term.paste_clipboard()

    def set_input_observer(self, observer: Optional[Callable[["TerminalTab", str], None]]) -> None:
        self._input_observer = observer

    def feed_input(self, data: str) -> None:
        term = self._active_terminal()
        if term and data:
            term.feed_child(data.encode("utf-8"))

    def send_interrupt(self) -> None:
        term = self._active_terminal()
        if term:
            term.inject_terminal_control(3)

    def focus_terminal(self) -> None:
        term = self._active_terminal()
        if term is None:
            return

        def _attempt_focus() -> bool:
            if term is None:
                return False
            if not term.get_realized() or not term.get_mapped():
                return True
            term.grab_focus()
            return False

        GLib.idle_add(_attempt_focus)

    def increase_font(self) -> None:
        self.set_font_scale(self._font_scale + 0.1)

    def decrease_font(self) -> None:
        self.set_font_scale(self._font_scale - 0.1)

    def reset_font(self, scale: Optional[float] = None) -> None:
        base = 1.0 if scale is None else scale
        self.set_font_scale(base)

    def set_font_scale(self, scale: float) -> None:
        self._font_scale = max(0.5, min(3.0, scale))
        for term in self._terminals():
            if self._profile and self._profile.font_size:
                # Override with profile font size if provided
                term.set_font_scale(self._profile.font_size)
            else:
                term.set_font_scale(self._font_scale)

    def reconnect(self) -> None:
        if self._session_active:
            return
        if self._paramiko_mode:
            return
        self._spawn()

    def get_font_scale(self) -> float:
        return self._font_scale

    # ------------------------------------------------------------------ internal
    def _on_reconnect_clicked(self, _button: Gtk.Button) -> None:
        self.reconnect()

    def _set_status(self, message: str, connected: bool) -> None:
        indicator = "●" if connected else "○"
        self._status_label.set_text(f"{indicator} {message}")
        self._banner.set_reveal_child(not connected)
        self._reconnect_button.set_sensitive(not connected)
        self._session_active = connected
        if connected:
            # Mark as connected immediately so normal disconnects don't wipe stored passwords.
            self._ever_connected = True
            if not self._connection_confirm_source_id:
                self._connection_confirm_source_id = GLib.timeout_add_seconds(2, self._confirm_connection_alive)
            # Persist any freshly entered password immediately so it survives if the
            # session is closed soon after connecting.
            if self._password_to_save and not self._password_save_source_id:
                self._persist_password()
            self._start_password_prompt_poll()
        self.emit("status-changed", self._host.alias, connected)
        if connected:
            self._start_stats_updates()
        else:
            self._stop_stats_updates()
            self._stop_password_prompt_poll()
            if self._password_save_source_id:
                GLib.source_remove(self._password_save_source_id)
                self._password_save_source_id = 0
            if self._connection_confirm_source_id:
                GLib.source_remove(self._connection_confirm_source_id)
                self._connection_confirm_source_id = 0

    def _ssh_arguments(self) -> list[str]:
        return build_ssh_command(self._host)

    def _credential_components(self) -> Optional[Tuple[str, int, str]]:
        hostname, port, username = host_connection_params(self._host)
        if not hostname:
            return None
        return hostname, port, username

    def _prompt_for_password(self) -> Optional[str]:
        parent = self.get_toplevel()
        transient = parent if isinstance(parent, Gtk.Window) else None
        dialog = Gtk.Dialog(title="SSH Password", transient_for=transient, modal=True)
        dialog.set_destroy_with_parent(True)
        if transient:
            dialog.set_position(Gtk.WindowPosition.CENTER_ON_PARENT)
        else:
            dialog.set_position(Gtk.WindowPosition.CENTER_ALWAYS)
        dialog.add_button("_Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("_Connect", Gtk.ResponseType.OK)
        dialog.set_default_response(Gtk.ResponseType.OK)

        content = dialog.get_content_area()
        content.set_border_width(16)
        content.set_spacing(12)

        label = Gtk.Label(label=f"Enter password for {self._host.alias}")
        label.set_xalign(0.0)
        content.pack_start(label, False, False, 0)

        entry = Gtk.Entry()
        entry.set_visibility(False)
        entry.set_invisible_char("•")
        entry.set_activates_default(True)
        content.pack_start(entry, False, False, 0)

        dialog.show_all()
        entry.grab_focus()
        response = dialog.run()
        text = entry.get_text()
        dialog.destroy()

        if response == Gtk.ResponseType.OK and text:
            return text
        return None

    def _finalize_password_save(self) -> bool:
        self._password_save_source_id = 0
        if not self._session_active:
            self._password_to_save = None
            return GLib.SOURCE_REMOVE
        self._persist_password()
        return GLib.SOURCE_REMOVE

    def _confirm_connection_alive(self) -> bool:
        self._connection_confirm_source_id = 0
        if self._session_active:
            self._ever_connected = True
        return GLib.SOURCE_REMOVE

    def _schedule_retry_prompt(self) -> None:
        if self._retry_timeout_id:
            GLib.source_remove(self._retry_timeout_id)
        self._retry_timeout_id = GLib.timeout_add_seconds(1, self._prompt_retry_after_failure)

    def _prompt_retry_after_failure(self) -> bool:
        self._retry_timeout_id = 0
        if shutil.which("sshpass") is None:
            self._log(
                "Automatic password retry unavailable because sshpass is not installed.",
                False,
            )
            return GLib.SOURCE_REMOVE
        password = self._prompt_for_password()
        if password:
            self._log(f"Retrying {self._host.alias} with entered password…", False)

            def _start_retry() -> bool:
                self._spawn(
                    password,
                    password_origin="retry",
                    save_on_success=True,
                )
                return GLib.SOURCE_REMOVE

            GLib.idle_add(_start_retry)
        else:
            self._log(f"Password entry cancelled for {self._host.alias}.", False)
        return GLib.SOURCE_REMOVE

    def _terminal_tail(self, term: Vte.Terminal, limit: int = 4096) -> str:
        try:
            column, row = term.get_cursor_position()
            columns = max(80, int(term.get_column_count() or 80))
            rows = max(4, min(80, (limit // columns) + 2))
            start_row = max(0, int(row) - rows)
            end_col = max(columns, int(column) + 1)
            result = term.get_text_range_format(
                Vte.Format.TEXT,
                start_row,
                0,
                int(row),
                end_col,
            )
            return _terminal_text_result(result)[-limit:]
        except AttributeError:
            try:
                column, row = term.get_cursor_position()
                columns = max(80, int(term.get_column_count() or 80))
                rows = max(4, min(80, (limit // columns) + 2))
                start_row = max(0, int(row) - rows)
                end_col = max(columns, int(column) + 1)
                result = term.get_text_range(
                    start_row,
                    0,
                    int(row),
                    end_col,
                    _select_all_terminal_text,
                    None,
                )
                return _terminal_text_result(result)[-limit:]
            except Exception:
                return ""
        except Exception:
            return ""

    def _auth_failure_output(self, term: Vte.Terminal) -> str:
        tail = self._terminal_tail(term)
        if auth_failure_output_detected(tail):
            return tail
        try:
            text = terminal_text(term)
        except Exception:
            return tail
        return text or tail

    def _observe_terminal_input(self, term: Vte.Terminal, text: str) -> None:
        if not text:
            return
        password = self._password_change_tracker.observe_input(
            self._terminal_tail(term),
            text,
        )
        if password:
            self._stage_detected_password_change(password, term)

    def _on_terminal_commit(self, term: Vte.Terminal, text: str, _length: int) -> None:
        if self._last_key_input:
            last_term, last_text, last_time = self._last_key_input
            if last_term is term and last_text == text and time.monotonic() - last_time < 0.25:
                return
        self._observe_terminal_input(term, text)

    def _start_password_prompt_poll(self) -> None:
        self._password_prompt_poll_deadline = time.monotonic() + 120.0
        if self._password_prompt_poll_source_id:
            return
        self._password_prompt_poll_source_id = GLib.timeout_add(
            250,
            self._poll_password_change_output,
        )

    def _stop_password_prompt_poll(self) -> None:
        if self._password_prompt_poll_source_id:
            GLib.source_remove(self._password_prompt_poll_source_id)
            self._password_prompt_poll_source_id = 0
        self._password_prompt_poll_deadline = 0.0

    def _poll_password_change_output(self) -> bool:
        term = self._password_change_terminal or self._focused_terminal or self._terminal
        tail = self._terminal_tail(term)
        if auth_failure_output_detected(tail):
            if self._handle_auth_failure_retry():
                self._password_prompt_poll_source_id = 0
                return GLib.SOURCE_REMOVE

        password = self._password_change_tracker.observe_output(tail)
        if password:
            self._persist_detected_password_change(password)
            self._password_prompt_poll_source_id = 0
            return GLib.SOURCE_REMOVE

        if (
            time.monotonic() >= self._password_prompt_poll_deadline
            and not self._password_change_tracker.watching_output
            and not self._pending_detected_password_change
        ):
            self._password_prompt_poll_source_id = 0
            return GLib.SOURCE_REMOVE
        return True

    def _handle_auth_failure_retry(self) -> bool:
        if self._auth_retry_pending:
            return True
        if self._password_source not in {"stored", "prompt", "retry"}:
            return False
        self._auth_retry_pending = True
        self._password_to_save = None
        if self._secure_store and self._password_source == "stored":
            creds = self._credential_components()
            if creds:
                host, port, username = creds
                try:
                    self._secure_store.delete(host, port, username)
                except Exception:
                    pass
        self._terminate_current_child()
        self._set_status("Authentication failed", False)
        self._log(f"Authentication failed for {self._host.alias}. Enter a new password.", False)
        self._schedule_retry_prompt()
        return True

    def _terminate_current_child(self) -> None:
        if not self._child_pid:
            return
        try:
            os.kill(self._child_pid, signal.SIGTERM)
        except OSError:
            pass

    def _stage_detected_password_change(self, password: str, term: Vte.Terminal) -> None:
        self._pending_detected_password_change = password
        self._password_change_terminal = term
        if self._password_change_source_id:
            GLib.source_remove(self._password_change_source_id)
        self._password_change_source_id = GLib.timeout_add_seconds(
            3,
            self._finalize_detected_password_change,
        )

    def _finalize_detected_password_change(self) -> bool:
        self._password_change_source_id = 0
        term = self._password_change_terminal or self._terminal
        password = self._password_change_tracker.observe_output(self._terminal_tail(term))
        if password:
            self._persist_detected_password_change(password)
            return GLib.SOURCE_REMOVE

        password = self._password_change_tracker.pending_candidate
        if password:
            self._persist_detected_password_change(password)
            return GLib.SOURCE_REMOVE

        self._clear_detected_password_change()
        return GLib.SOURCE_REMOVE

    def _clear_detected_password_change(self) -> None:
        if self._password_change_source_id:
            GLib.source_remove(self._password_change_source_id)
            self._password_change_source_id = 0
        self._pending_detected_password_change = None
        self._password_change_terminal = None

    def _disconnect_input_commit_handler(self, term: Vte.Terminal) -> None:
        handler_id = self._input_commit_handlers.pop(term, None)
        if handler_id is None:
            return
        try:
            term.disconnect(handler_id)
        except Exception:
            pass

    def _save_password_secret(self, password: str) -> bool:
        if not self._secure_store:
            return False
        key = self._credential_components()
        if not key:
            return False
        host, port, username = key
        self._secure_store.set(host, port, username, password)
        return True

    def _persist_detected_password_change(self, password: str) -> None:
        saved = False
        try:
            if self._password_update_handler:
                saved = bool(self._password_update_handler(self._host, password))
            else:
                saved = self._save_password_secret(password)
        except SecureStoreError as exc:
            LOGGER.warning("password change save failed: %s", exc)
            saved = False
        except Exception as exc:
            LOGGER.warning("password change save failed: %s", exc)
            saved = False

        self._password_change_tracker.reset()
        self._clear_detected_password_change()
        if saved:
            self._log(f"Saved updated password for {self._host.alias}.", True)
        else:
            self._log(f"Could not save updated password for {self._host.alias}.", False)

    def _persist_password(self) -> None:
        if not self._secure_store or not self._password_to_save:
            return
        key = self._credential_components()
        if not key:
            return
        password = self._password_to_save
        host, port, username = key
        existing = self._secure_store.get(host, port, username)
        if existing != password:
            self._secure_store.set(host, port, username, password)
            self._log(f"Saved password for {self._host.alias}.", True)
        self._password_to_save = None

    def _spawn(
        self,
        override_password: Optional[str] = None,
        *,
        password_origin: str = "prompt",
        save_on_success: bool = False,
    ) -> None:
        term = self._terminal
        self._ever_connected = False
        self._password_source = "none"
        self._password_to_save = None
        self._password_change_tracker.reset()
        self._pending_detected_password_change = None
        self._password_change_terminal = None
        self._last_key_input = None
        self._auth_retry_pending = False
        self._child_pid = None
        self._stop_password_prompt_poll()
        if self._password_save_source_id:
            GLib.source_remove(self._password_save_source_id)
            self._password_save_source_id = 0
        if self._connection_confirm_source_id:
            GLib.source_remove(self._connection_confirm_source_id)
            self._connection_confirm_source_id = 0
        if self._retry_timeout_id:
            GLib.source_remove(self._retry_timeout_id)
            self._retry_timeout_id = 0

        helper_available = shutil.which("sshpass") is not None
        stored_password: Optional[str] = None
        credentials = self._credential_components()
        if self._secure_store and credentials:
            stored_password = self._secure_store.get(*credentials)

        password_for_attempt: Optional[str] = None
        if override_password is not None:
            if not helper_available:
                self._log(
                    "Cannot perform automatic password entry because sshpass is not installed.",
                    False,
                )
                self._password_source = "manual"
                return
            password_for_attempt = override_password
            self._password_source = password_origin
            if save_on_success:
                self._password_to_save = override_password
        elif stored_password:
            if helper_available:
                password_for_attempt = stored_password
                self._password_source = "stored"
            else:
                self._log(
                    f"Stored password available for {self._host.alias} but sshpass is missing. "
                    "Falling back to manual entry.",
                    False,
                )
                self._password_source = "manual"
        else:
            if helper_available:
                password = self._prompt_for_password()
                if password is None:
                    self._log(f"Password entry cancelled for {self._host.alias}.", False)
                    return
                password_for_attempt = password
                self._password_source = "prompt"
                self._password_to_save = password
            else:
                self._log(
                    f"Automatic password entry unavailable for {self._host.alias} (sshpass not installed).",
                    False,
                )
                self._password_source = "manual"

        argv = self._ssh_arguments()
        envv = [f"{key}={value}" for key, value in os.environ.items()]
        if self._profile and self._profile.env_vars:
            for k, v in self._profile.env_vars.items():
                envv.append(f"{k}={v}")

        if password_for_attempt:
            envv.append(f"SSHPASS={password_for_attempt}")
            argv = ["sshpass", "-e", *argv]

        term.reset(True, True)
        self._session_active = False
        self._set_status("Connecting…", False)

        def _on_spawn_finish(term: Vte.Terminal, pid: int, error: Optional[GLib.Error], _user_data) -> None:
            if error:
                self._log(f"Failed to open SSH session for {self._host.alias}: {error.message}", False)
                self._set_status("Connection failed", False)
                GLib.idle_add(self.emit, "session-closed", self._host.alias, False)
                return
            if pid == 0:
                self._log(f"SSH session exited immediately for {self._host.alias}", False)
                self._set_status("Connection closed", False)
                GLib.idle_add(self.emit, "session-closed", self._host.alias, False)
                return
            self._child_pid = pid
            self._set_status("Connected", True)
            self.focus_terminal()

        self._banner.set_reveal_child(False)
        self._reconnect_button.set_sensitive(False)
        term.spawn_async(
            Vte.PtyFlags.DEFAULT,
            None,
            argv,
            envv,
            GLib.SpawnFlags.DEFAULT,
            None,
            None,
            -1,
            None,
            _on_spawn_finish,
            None,
        )

    def _spawn_secondary(self, term: Vte.Terminal) -> None:
        argv = self._ssh_arguments()
        envv = [f"{key}={value}" for key, value in os.environ.items()]
        if self._profile and self._profile.env_vars:
            for k, v in self._profile.env_vars.items():
                envv.append(f"{k}={v}")
        term.reset(True, True)
        def _on_finish(t: Vte.Terminal, pid: int, error: Optional[GLib.Error], _ud) -> None:
            if error:
                self._log(f"Failed to open secondary session for {self._host.alias}: {error.message}", False)
        term.spawn_async(
            Vte.PtyFlags.DEFAULT,
            None,
            argv,
            envv,
            GLib.SpawnFlags.DEFAULT,
            None,
            None,
            -1,
            None,
            _on_finish,
            None,
        )

    # --- BEGIN: PARAMIKO_VTE_ATTACH ---
    def attach_session(self, session) -> None:
        try:
            if self._bridge:
                self._bridge.stop()
            self._session_client = session
            self._bridge = VTEBridge(
                self._terminal,
                session,
                LOGGER,
                on_remote_closed=self._on_paramiko_remote_closed,
            )
            self._bridge.start()
            self._set_status("Connected", True)
            self._start_stats_updates()
            self.focus_terminal()
        except Exception as exc:
            self._log("Failed to attach terminal session.", False)
            LOGGER.warning("attach_session failed: %s", exc)
            if self._bridge:
                try:
                    self._bridge.stop()
                except Exception:
                    pass
                self._bridge = None
            if self._session_client:
                try:
                    self._session_client.close()
                except Exception:
                    pass
                self._session_client = None

    def _stop_paramiko_bridge(self) -> None:
        if self._bridge:
            try:
                self._bridge.stop()
            except Exception:
                pass
            self._bridge = None
        if self._session_client:
            try:
                self._session_client.close()
            except Exception:
                pass
            self._session_client = None

    def _on_paramiko_remote_closed(self) -> None:
        self._stop_paramiko_bridge()
        self._set_status("Disconnected", False)
        self._log(f"{self._host.alias}: Session ended.", False)
        self.emit("session-closed", self._host.alias, True)
    # --- END: PARAMIKO_VTE_ATTACH ---

    def _on_child_exited(self, _term: Vte.Terminal, status: int) -> None:
        exited_normally = bool(os.WIFEXITED(status))
        self._child_pid = None
        if auth_failure_output_detected(self._auth_failure_output(_term)):
            if self._handle_auth_failure_retry():
                return
        self._set_status("Disconnected", False)
        if self._auth_retry_pending:
            GLib.idle_add(self.emit, "session-closed", self._host.alias, False)
            return
        if not self._ever_connected:
            schedule_retry = False
            if self._secure_store and self._password_source == "stored":
                creds = self._credential_components()
                if creds:
                    host, port, username = creds
                    try:
                        self._secure_store.delete(host, port, username)
                    except Exception:
                        pass
                self._log(
                    f"Authentication failed for {self._host.alias}. Stored password removed.",
                    False,
                )
                schedule_retry = True
            elif self._password_source in {"prompt", "retry"}:
                self._log(f"Authentication failed for {self._host.alias}.", False)
                schedule_retry = True
            self._password_to_save = None
            if schedule_retry:
                self._schedule_retry_prompt()
        GLib.idle_add(self.emit, "session-closed", self._host.alias, exited_normally)

    def _on_terminal_key_press(self, _term: Vte.Terminal, event: Gdk.EventKey) -> bool:
        keyval = event.keyval
        default_mask = Gtk.accelerator_get_default_mod_mask()
        modifiers = event.state & default_mask
        lower = Gdk.keyval_to_lower(keyval)

        if lower == Gdk.KEY_r and modifiers == 0:
            if not self._session_active:
                self.reconnect()
                return True
            return False

        if lower == Gdk.KEY_q and modifiers == 0:
            if not self._session_active:
                self._request_close()
                return True
        payload = terminal_key_event_payload(event)
        if payload:
            self._last_key_input = (_term, payload, time.monotonic())
            self._observe_terminal_input(_term, payload)
            if self._input_observer:
                self._input_observer(self, payload)
        return False

    def do_key_press_event(self, event):  # type: ignore[override]
        return Gtk.Box.do_key_press_event(self, event)

    def _request_close(self) -> None:
        self.emit("close-requested")

    def destroy(self) -> None:
        self._stop_stats_updates()
        self._stop_password_prompt_poll()
        self._clear_detected_password_change()
        for term in list(self._input_commit_handlers):
            self._disconnect_input_commit_handler(term)
        self._stop_paramiko_bridge()
        super().destroy()

    def _start_stats_updates(self) -> None:
        if self._stats_source_id:
            return
        self._info_revealer.set_reveal_child(True)
        self._update_stats()
        self._stats_source_id = GLib.timeout_add_seconds(10, self._update_stats)

    def _stop_stats_updates(self) -> None:
        if self._stats_source_id:
            GLib.source_remove(self._stats_source_id)
            self._stats_source_id = 0
        self._stats_updating = False
        self._reset_stats_labels()
        self._info_revealer.set_reveal_child(False)

    def _duplicate_pane_once(self) -> None:
        if self._secondary_terminal is not None:
            return
        self._secondary_terminal = self._create_terminal()
        self._apply_terminal_theme()
        self._secondary_terminal.connect("key-press-event", self._on_terminal_key_press)
        self._secondary_terminal.connect("focus-in-event", self._on_terminal_focus)
        self._secondary_terminal.connect("child-exited", self._on_secondary_child_exited)
        self._spawn_secondary(self._secondary_terminal)

        self._paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self._paned.set_hexpand(True)
        self._paned.set_vexpand(True)
        self._pane_holder.remove(self._pane_container)
        self._paned.pack1(self._pane_container, resize=True, shrink=True)

        second_container = TerminalPaneContainer(self._secondary_terminal)
        self._paned.pack2(second_container, resize=True, shrink=True)
        self._pane_holder.pack_start(self._paned, True, True, 0)
        self._pane_holder.show_all()

    def _close_duplicate_pane(self) -> None:
        if self._secondary_terminal is None:
            return
        if self._focused_terminal is self._secondary_terminal:
            self._focused_terminal = self._terminal
        try:
            self._secondary_terminal.disconnect_by_func(self._on_secondary_child_exited)
        except Exception:
            pass
        try:
            self._secondary_terminal.disconnect_by_func(self._on_terminal_key_press)
        except Exception:
            pass
        self._disconnect_input_commit_handler(self._secondary_terminal)
        try:
            self._secondary_terminal.disconnect_by_func(self._on_terminal_focus)
        except Exception:
            pass
        try:
            self._secondary_terminal.destroy()
        except Exception:
            pass
        self._secondary_terminal = None
        if self._paned:
            self._pane_holder.remove(self._paned)
            self._paned = None
        self._pane_holder.pack_start(self._pane_container, True, True, 0)
        self._pane_holder.show_all()
        self._focused_terminal = self._terminal
        self.focus_terminal()

    def _on_terminal_focus(self, term: Vte.Terminal, _event=None) -> None:
        self._focused_terminal = term

    def _on_secondary_child_exited(self, term: Vte.Terminal, _status: int) -> None:
        self._log(f"{self._host.alias}: Secondary session ended.", False)
        GLib.idle_add(self._close_duplicate_pane)

    def save_transcript(self) -> None:
        term = self._active_terminal()
        if not term:
            return
        chooser = Gtk.FileChooserDialog(
            title="Save Transcript",
            parent=self.get_toplevel(),
            action=Gtk.FileChooserAction.SAVE,
            buttons=(
                Gtk.STOCK_CANCEL,
                Gtk.ResponseType.CANCEL,
                Gtk.STOCK_SAVE,
                Gtk.ResponseType.OK,
            ),
        )
        resp = chooser.run()
        target = chooser.get_filename() if resp == Gtk.ResponseType.OK else None
        chooser.destroy()
        if not target:
            return
        try:
            cleaned = strip_ansi(terminal_text(term))
            write_text_atomic(Path(target), cleaned)
            self._log(f"Transcript saved to {target}", True)
        except Exception as exc:
            self._log(f"Transcript save failed: {exc}", False)

    def toggle_strip_ansi_copy(self) -> None:
        self._strip_ansi_on_copy = not self._strip_ansi_on_copy
        state = "on" if self._strip_ansi_on_copy else "off"
        self._log(f"Strip ANSI on copy {state}.", True)

    def _set_strip_ansi(self, active: bool) -> None:
        self._strip_ansi_on_copy = active
        state = "on" if active else "off"
        self._log(f"Strip ANSI on copy {state}.", True)

    def _update_stats(self) -> bool:
        if not self._session_active:
            return True
        with self._stats_lock:
            if self._stats_updating:
                return True
            self._stats_updating = True
        self._info_spinner.show()
        self._info_spinner.start()
        threading.Thread(target=self._fetch_stats_thread, daemon=True).start()
        return True

    def _fetch_stats_thread(self) -> None:
        data = self._fetch_stats_via_paramiko()
        if data is None:
            data = self._fetch_stats_via_ssh_command()
        GLib.idle_add(self._apply_stats_result, data)

    def _fetch_stats_via_paramiko(self) -> Optional[Dict[str, object]]:
        client = self._session_client
        if client is None:
            return None
        try:
            stdin, stdout, _stderr = client.exec_command("python3 -", timeout=9)
            stdin.write(_REMOTE_STATS_SCRIPT)
            stdin.flush()
            try:
                stdin.channel.shutdown_write()
            except Exception:
                stdin.close()
            raw = stdout.read()
            try:
                status = stdout.channel.recv_exit_status()
            except Exception:
                status = 0
            if status != 0:
                return None
            text = raw.decode("utf-8", errors="ignore") if isinstance(raw, bytes) else str(raw)
            return parse_remote_stats_output(text)
        except Exception as exc:
            LOGGER.debug("Paramiko stats fetch failed for %s: %s", self._host.alias, exc)
            return None

    def _stored_stats_password(self) -> Optional[str]:
        if not self._secure_store:
            return None
        credentials = self._credential_components()
        if not credentials:
            return None
        try:
            return self._secure_store.get(*credentials)
        except SecureStoreError as exc:
            LOGGER.debug("Unable to read stored password for stats: %s", exc)
            return None

    def _fetch_stats_via_ssh_command(self) -> Optional[Dict[str, object]]:
        try:
            password = self._stored_stats_password()
            command, env = build_remote_stats_command(
                self._ssh_arguments(),
                password=password,
                sshpass_path=shutil.which("sshpass") if password else None,
            )
            if not command:
                return None
            proc = subprocess.run(  # nosec B603
                command,
                input=_REMOTE_STATS_SCRIPT,
                capture_output=True,
                text=True,
                timeout=9,
                env=env,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                return parse_remote_stats_output(proc.stdout)
            LOGGER.debug(
                "Remote stats command failed for %s: rc=%s stderr=%s",
                self._host.alias,
                proc.returncode,
                proc.stderr.strip(),
            )
            return None
        except Exception as exc:
            LOGGER.debug("Remote stats command failed for %s: %s", self._host.alias, exc)
            return None

    def _show_stats_once(self) -> None:
        if not self._session_active:
            self._log("Remote stats require an active session.", False)
            return
        self._info_revealer.set_reveal_child(True)
        self._update_stats()

    def _apply_stats_result(self, data: Optional[Dict[str, float]]) -> bool:
        self._info_spinner.stop()
        self._info_spinner.hide()
        with self._stats_lock:
            self._stats_updating = False
        if not isinstance(data, dict):
            self._cpu_label.set_text("CPU: N/A")
            self._mem_label.set_text("Mem: N/A")
            self._disk_label.set_text("Disk: N/A")
            return False

        def numeric(value) -> Optional[float]:
            if isinstance(value, bool):
                return None
            if isinstance(value, (int, float)):
                return float(value)
            return None

        cpu = numeric(data.get("cpu"))
        if cpu is not None:
            self._cpu_label.set_text(f"CPU: {cpu:.1f}%")
        else:
            self._cpu_label.set_text("CPU: N/A")
        mem_used = numeric(data.get("mem_used"))
        mem_total = numeric(data.get("mem_total"))
        if mem_used is not None and mem_total is not None and mem_total > 0:
            self._mem_label.set_text(
                f"Mem: {mem_used / 1024:.1f}/{mem_total / 1024:.1f} GB"
            )
        else:
            self._mem_label.set_text("Mem: N/A")
        disk_used = numeric(data.get("disk_used"))
        disk_total = numeric(data.get("disk_total"))
        if disk_used is not None and disk_total is not None and disk_total > 0:
            self._disk_label.set_text(
                f"Disk: {disk_used:.1f}/{disk_total:.1f} GB"
            )
        else:
            self._disk_label.set_text("Disk: N/A")
        return False


class TerminalNotebook(Gtk.Box):
    """Container managing multiple terminal tabs."""

    def __init__(
        self,
        log: LogFn,
        default_font_scale: float = 1.0,
        password_store: Optional[SecureStore] = None,
        password_update_handler: Optional[PasswordUpdateFn] = None,
        appearance: Optional[TerminalAppearanceSettings] = None,
    ) -> None:
        Gtk.Box.__init__(self, orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.set_hexpand(True)
        self.set_vexpand(True)

        self._log = log
        self._default_font_scale = default_font_scale
        self._tab_labels: Dict[TerminalTab, Gtk.Label] = {}
        self._tab_status: Dict[TerminalTab, bool] = {}
        self._tabs: List[TerminalTab] = []
        self._tile_wrappers: Dict[TerminalTab, Gtk.Widget] = {}
        self._tile_mode = False
        self._broadcast_mode = False
        self._broadcast_included: Dict[TerminalTab, bool] = {}
        self._last_current_tab: Optional[TerminalTab] = None
        self._secure_store = password_store
        self._password_update_handler = password_update_handler
        self._appearance = appearance

        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        toolbar.set_margin_top(6)
        toolbar.set_margin_bottom(6)
        toolbar.set_margin_start(8)
        toolbar.set_margin_end(8)
        self._show_all_button = Gtk.ToggleButton(label="Show All Sessions")
        self._show_all_button.set_tooltip_text("Tile every open terminal session together.")
        self._show_all_button.set_sensitive(False)
        self._show_all_button.connect("toggled", self._on_show_all_toggled)
        toolbar.pack_start(self._show_all_button, False, False, 0)
        self._broadcast_all_button = Gtk.ToggleButton(label="Broadcast Input")
        self._broadcast_all_button.set_tooltip_text("Mirror typed input to every included active session.")
        self._broadcast_all_button.set_sensitive(False)
        self._broadcast_all_button.connect("toggled", self._on_broadcast_all_toggled)
        toolbar.pack_start(self._broadcast_all_button, False, False, 0)
        self.pack_start(toolbar, False, False, 0)

        self._notebook = Gtk.Notebook()
        self._notebook.set_scrollable(True)
        self._notebook.set_hexpand(True)
        self._notebook.set_vexpand(True)

        self._tile_grid = Gtk.Grid()
        self._tile_grid.set_row_spacing(8)
        self._tile_grid.set_column_spacing(8)
        self._tile_grid.set_hexpand(True)
        self._tile_grid.set_vexpand(True)
        self._tile_grid.set_margin_top(8)
        self._tile_grid.set_margin_bottom(8)
        self._tile_grid.set_margin_start(8)
        self._tile_grid.set_margin_end(8)
        self._tile_scroller = Gtk.ScrolledWindow()
        self._tile_scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self._tile_scroller.set_hexpand(True)
        self._tile_scroller.set_vexpand(True)
        self._tile_scroller.add(self._tile_grid)

        self._placeholder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self._placeholder.set_hexpand(True)
        self._placeholder.set_vexpand(True)
        self._placeholder.set_halign(Gtk.Align.CENTER)
        self._placeholder.set_valign(Gtk.Align.CENTER)
        icon = Gtk.Image.new_from_icon_name("utilities-terminal-symbolic", Gtk.IconSize.DIALOG)
        self._placeholder.pack_start(icon, False, False, 0)
        message = Gtk.Label(label="No active connections")
        self._placeholder.pack_start(message, False, False, 0)

        self._stack = Gtk.Stack()
        self._stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self._stack.set_hexpand(True)
        self._stack.set_vexpand(True)
        self._stack.add_named(self._placeholder, "placeholder")
        self._stack.add_named(self._notebook, "tabs")
        self._stack.add_named(self._tile_scroller, "tiles")
        self._stack.set_visible_child_name("placeholder")

        self.pack_start(self._stack, True, True, 0)

    # ----------------------------------------------------------------- tab api
    @property
    def default_font_scale(self) -> float:
        return self._default_font_scale

    def set_default_font_scale(self, scale: float) -> None:
        self._default_font_scale = max(0.5, min(3.0, scale))

    def apply_font_scale_to_all(self, scale: float) -> None:
        for tab in list(self._tabs):
            tab.set_font_scale(scale)

    def new_terminal(self, host: HostEntry, *, autostart: bool = True, profile: Optional["SessionProfile"] = None) -> TerminalTab:
        tab = TerminalTab(
            host,
            self._log,
            self._default_font_scale,
            password_store=self._secure_store,
            password_update_handler=self._password_update_handler,
            autostart=autostart,
            profile=profile,
            appearance=self._appearance,
        )
        tab.connect("status-changed", self._on_tab_status_changed)
        tab.connect("session-closed", self._on_tab_session_closed)
        tab.connect("close-requested", self._on_tab_close_requested)
        tab.connect("focus-in-event", self._on_tab_focus, tab)
        tab.set_input_observer(self._on_tab_input)

        self._tabs.append(tab)
        self._tab_status[tab] = False
        self._broadcast_included[tab] = True
        self._last_current_tab = tab

        if self._tile_mode:
            self._rebuild_tile_grid()
        else:
            self._append_tab_to_notebook(tab)
        self._update_placeholder()
        GLib.idle_add(tab.focus_terminal)
        return tab

    def _append_tab_to_notebook(self, tab: TerminalTab) -> int:
        self._detach_tab(tab)
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        header.set_margin_start(6)
        header.set_margin_end(6)

        label = Gtk.Label(label=self._tab_label_text(tab))
        label.set_xalign(0.0)
        header.pack_start(label, True, True, 0)

        close_button = Gtk.Button()
        close_button.set_relief(Gtk.ReliefStyle.NONE)
        close_button.set_tooltip_text("Close session")
        close_image = Gtk.Image.new_from_icon_name("window-close-symbolic", Gtk.IconSize.MENU)
        close_button.add(close_image)
        close_button.connect("clicked", self._on_close_button_clicked, tab)
        header.pack_start(close_button, False, False, 0)

        page_num = self._notebook.append_page(tab, header)
        tab.show_all()
        header.show_all()
        self._notebook.set_tab_reorderable(tab, True)
        self._notebook.set_current_page(page_num)

        self._tab_labels[tab] = label
        return page_num

    def set_appearance(self, appearance: TerminalAppearanceSettings) -> None:
        self._appearance = appearance
        for tab in list(self._tabs):
            tab.set_appearance(appearance)

    def close_tab(self, tab: Optional[TerminalTab] = None) -> None:
        target = tab or self._current_tab()
        if not target:
            return
        if target not in self._tabs:
            return
        self._detach_tab(target)
        self._tabs.remove(target)
        self._tab_status.pop(target, None)
        self._tab_labels.pop(target, None)
        self._tile_wrappers.pop(target, None)
        self._broadcast_included.pop(target, None)
        target.set_input_observer(None)
        if self._last_current_tab is target:
            self._last_current_tab = self._tabs[0] if self._tabs else None
        target.destroy()
        if self._tile_mode:
            self._rebuild_tile_grid()
        self._update_placeholder()

    def close_current(self) -> None:
        self.close_tab()

    def copy_current(self) -> None:
        tab = self._current_tab()
        if tab:
            tab.copy_clipboard()

    def paste_current(self) -> None:
        tab = self._current_tab()
        if tab:
            tab.paste_clipboard()

    def interrupt_current(self) -> None:
        tab = self._current_tab()
        if tab:
            tab.send_interrupt()

    def save_current_transcript(self) -> None:
        tab = self._current_tab()
        if tab:
            tab.save_transcript()

    def toggle_strip_ansi_copy(self) -> None:
        tab = self._current_tab()
        if tab:
            tab.toggle_strip_ansi_copy()

    def focus_current(self) -> None:
        tab = self._current_tab()
        if tab:
            tab.focus_terminal()

    def increase_font(self) -> None:
        tab = self._current_tab()
        if tab:
            tab.increase_font()

    def decrease_font(self) -> None:
        tab = self._current_tab()
        if tab:
            tab.decrease_font()

    def reset_font(self) -> None:
        tab = self._current_tab()
        if tab:
            tab.reset_font(self._default_font_scale)

    def tab_count(self) -> int:
        return len(self._tabs)

    # ----------------------------------------------------------------- helpers
    def _tab_label_text(self, tab: TerminalTab) -> str:
        connected = self._tab_status.get(tab, False)
        indicator = "●" if connected else "○"
        return f"{indicator} {tab.host.alias}"

    def _set_tab_label_text(self, tab: TerminalTab) -> None:
        label = self._tab_labels.get(tab)
        if label:
            label.set_text(self._tab_label_text(tab))

    def _detach_tab(self, tab: TerminalTab) -> None:
        page_num = self._notebook.page_num(tab)
        if page_num != -1:
            self._notebook.remove_page(page_num)
        parent = tab.get_parent()
        if parent and hasattr(parent, "remove"):
            parent.remove(tab)
        wrapper = self._tile_wrappers.pop(tab, None)
        if wrapper and wrapper.get_parent() is self._tile_grid:
            self._tile_grid.remove(wrapper)
        self._tab_labels.pop(tab, None)

    def _create_tile_wrapper(self, tab: TerminalTab) -> Gtk.Frame:
        self._detach_tab(tab)
        frame = Gtk.Frame()
        frame.set_hexpand(True)
        frame.set_vexpand(True)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_hexpand(True)
        box.set_vexpand(True)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        header.set_margin_top(4)
        header.set_margin_bottom(4)
        header.set_margin_start(6)
        header.set_margin_end(6)
        label = Gtk.Label(label=self._tab_label_text(tab))
        label.set_xalign(0.0)
        header.pack_start(label, True, True, 0)
        include = Gtk.CheckButton(label="Broadcast")
        include.set_tooltip_text("Include this session in broadcast input.")
        include.set_active(self._broadcast_included.get(tab, True))
        include.connect("toggled", self._on_broadcast_include_toggled, tab)
        header.pack_start(include, False, False, 0)
        close_button = Gtk.Button()
        close_button.set_relief(Gtk.ReliefStyle.NONE)
        close_button.set_tooltip_text("Close session")
        close_image = Gtk.Image.new_from_icon_name("window-close-symbolic", Gtk.IconSize.MENU)
        close_button.add(close_image)
        close_button.connect("clicked", self._on_close_button_clicked, tab)
        header.pack_start(close_button, False, False, 0)

        box.pack_start(header, False, False, 0)
        box.pack_start(tab, True, True, 0)
        frame.add(box)
        frame.show_all()
        self._tab_labels[tab] = label
        self._tile_wrappers[tab] = frame
        return frame

    def _rebuild_tile_grid(self) -> None:
        for tab in list(self._tabs):
            self._detach_tab(tab)
        for child in list(self._tile_grid.get_children()):
            self._tile_grid.remove(child)
        self._tile_wrappers.clear()
        count = len(self._tabs)
        for index, tab in enumerate(self._tabs):
            row, column = session_tile_position(index, count)
            wrapper = self._create_tile_wrapper(tab)
            self._tile_grid.attach(wrapper, column, row, 1, 1)
        self._tile_grid.show_all()

    def _set_tile_mode(self, active: bool) -> None:
        if active == self._tile_mode:
            return
        current = self._current_tab()
        self._tile_mode = active
        if active:
            if current:
                self._last_current_tab = current
            self._rebuild_tile_grid()
        else:
            for tab in list(self._tabs):
                self._detach_tab(tab)
            for tab in self._tabs:
                self._append_tab_to_notebook(tab)
            if self._last_current_tab in self._tabs:
                page_num = self._notebook.page_num(self._last_current_tab)
                if page_num != -1:
                    self._notebook.set_current_page(page_num)
        self._update_placeholder()

    def _on_show_all_toggled(self, button: Gtk.ToggleButton) -> None:
        if not button.get_active() and self._broadcast_mode:
            self._broadcast_all_button.set_active(False)
        self._set_tile_mode(button.get_active())

    def _on_broadcast_all_toggled(self, button: Gtk.ToggleButton) -> None:
        active = button.get_active()
        if active and len(self._tabs) < 2:
            button.set_active(False)
            return
        self._broadcast_mode = active
        if active and not self._tile_mode:
            self._show_all_button.set_active(True)
        if self._tile_mode:
            self._rebuild_tile_grid()
            self._update_placeholder()
        self._log(
            "Broadcast input enabled. Excluded sessions will not receive mirrored input."
            if active
            else "Broadcast input disabled.",
            active,
        )

    def _on_broadcast_include_toggled(self, button: Gtk.CheckButton, tab: TerminalTab) -> None:
        self._broadcast_included[tab] = button.get_active()

    def _on_tab_input(self, source: TerminalTab, data: str) -> None:
        self._last_current_tab = source
        if not self._broadcast_mode or not data:
            return
        for target in broadcast_target_tabs(
            self._tabs,
            source,
            self._broadcast_included,
            self._tab_status,
        ):
            target.feed_input(data)

    def _on_tab_focus(self, _widget, _event, tab: TerminalTab) -> bool:
        self._last_current_tab = tab
        return False

    def _current_tab(self) -> Optional[TerminalTab]:
        if self._tile_mode:
            if self._last_current_tab in self._tabs:
                return self._last_current_tab
            return self._tabs[0] if self._tabs else None
        index = self._notebook.get_current_page()
        if index < 0:
            return None
        child = self._notebook.get_nth_page(index)
        if isinstance(child, TerminalTab):
            self._last_current_tab = child
            return child
        return None

    def _on_close_button_clicked(self, _button: Gtk.Button, tab: TerminalTab) -> None:
        self.close_tab(tab)

    def _on_tab_status_changed(self, tab: TerminalTab, alias: str, connected: bool) -> None:
        self._tab_status[tab] = connected
        self._set_tab_label_text(tab)

    def _on_tab_session_closed(self, tab: TerminalTab, alias: str, exited_normally: bool) -> None:
        status = "Session ended cleanly" if exited_normally else "Session terminated unexpectedly"
        self._log(f"{alias}: {status}", exited_normally)

    def _on_tab_close_requested(self, tab: TerminalTab) -> None:
        GLib.idle_add(self._close_tab_after_idle, tab)

    def _close_tab_after_idle(self, tab: TerminalTab) -> bool:
        self.close_tab(tab)
        return GLib.SOURCE_REMOVE

    def _update_placeholder(self) -> None:
        if not self._tabs:
            visible = "placeholder"
        elif self._tile_mode:
            visible = "tiles"
        else:
            visible = "tabs"
        self._stack.set_visible_child_name(visible)
        self._show_all_button.set_sensitive(len(self._tabs) > 1)
        if len(self._tabs) <= 1 and self._tile_mode:
            self._show_all_button.set_active(False)
        self._broadcast_all_button.set_sensitive(len(self._tabs) > 1)
        if len(self._tabs) <= 1 and self._broadcast_mode:
            self._broadcast_all_button.set_active(False)
