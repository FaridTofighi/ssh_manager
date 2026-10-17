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
from typing import TYPE_CHECKING, Callable, Dict, List, Optional, Tuple

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
from ssh_manager.gtkui.theme.terminal_schemes import resolve_terminal_colors
from ssh_manager.gtkui.constants import Icons
from ssh_manager.gtkui.dialogs.files import choose_save_file
from ssh_manager.gtkui.dialogs.prompt import prompt_text
from ssh_manager.gtkui.i18n import _
from ssh_manager.gtkui.menu_spec import MenuItemSpec, MenuSection
from ssh_manager.gtkui.theme.loader import get_theme_manager
from ssh_manager.gtkui.theme.tokens import ConnectionStatus, Level, Motion, Space
from ssh_manager.gtkui.views.statusbar import SessionStatusBar, apply_stat_level
from ssh_manager.gtkui.views.workspace import BroadcastStrip, SessionToolbar, TabLabel, TileFrame, tab_menu_spec
from ssh_manager.gtkui.widgets.empty_state import EmptyState
from ssh_manager.gtkui.widgets.icon_button import IconButton, IconMenuButton
from ssh_manager.gtkui.widgets.menus import build_context_menu, build_menu, popup_menu

if TYPE_CHECKING:  # pragma: no cover
    from ssh_manager.profiles import SessionProfile

LogFn = Callable[[str, object], None]  # message, success flag or Level
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


_RESPONSE_RECONNECT = 1
_RESPONSE_CLOSE = 2
_FAILURE_WORDS = ("failed", "denied", "terminated", "error")


def _status_for_message(message: str, connected: bool) -> ConnectionStatus:
    """Map a status message to a :class:`ConnectionStatus`."""

    if connected:
        return ConnectionStatus.CONNECTED
    text = (message or "").lower()
    if text.startswith("connecting"):
        return ConnectionStatus.CONNECTING
    if any(word in text for word in _FAILURE_WORDS):
        return ConnectionStatus.FAILED
    return ConnectionStatus.UNKNOWN


def _set_stat_tooltip(label: object, text: str) -> None:
    setter = getattr(label, "set_tooltip_text", None)
    if callable(setter):
        setter(text)


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
        self._init_state(host, log, font_scale, password_store, password_update_handler, autostart, profile, appearance)
        self.set_hexpand(True)
        self.set_vexpand(True)
        self._build_infobar()
        self._build_terminal_area()
        self._build_statusbar()

        self._terminal.connect("key-press-event", self._on_terminal_key_press)
        self._terminal.connect("child-exited", self._on_child_exited)
        self._terminal.connect("focus-in-event", self._on_terminal_focus)
        self._terminal.connect("size-allocate", self._on_terminal_size_allocate)
        self._focused_terminal = self._terminal
        if autostart:
            self._spawn()
        else:
            self._set_status("Connecting…", False)

    def _init_state(
        self,
        host: HostEntry,
        log: LogFn,
        font_scale: float,
        password_store: Optional[SecureStore],
        password_update_handler: Optional[PasswordUpdateFn],
        autostart: bool,
        profile: Optional["SessionProfile"],
        appearance: Optional[TerminalAppearanceSettings],
    ) -> None:
        """Initialise session, password-tracking and timer state."""

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

        self._status = ConnectionStatus.UNKNOWN
        self._stats_enabled = True
        self._custom_title = ""
        self._activity_callback: Optional[Callable[["TerminalTab"], None]] = None

    # ------------------------------------------------------------------ building
    def _build_infobar(self) -> None:
        """Reconnect banner (``Gtk.InfoBar``) shown while the session is not connected."""

        self._banner = Gtk.InfoBar()
        self._banner.set_message_type(Gtk.MessageType.WARNING)
        self._banner.set_show_close_button(False)
        self._status_label = Gtk.Label()
        self._status_label.set_xalign(0.0)
        self._status_label.set_line_wrap(True)
        self._banner.get_content_area().pack_start(self._status_label, True, True, 0)
        self._reconnect_button = self._banner.add_button(_("_Reconnect"), _RESPONSE_RECONNECT)
        self._banner.add_button(_("_Close Session"), _RESPONSE_CLOSE)
        self._banner.connect("response", self._on_banner_response)
        self._banner.set_revealed(False)
        self._banner.set_no_show_all(True)
        self.pack_start(self._banner, False, False, 0)

    def _build_terminal_area(self) -> None:
        self._terminal = self._create_terminal()
        self._pane_container = TerminalPaneContainer(self._terminal)
        self._pane_holder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._pane_holder.set_hexpand(True)
        self._pane_holder.set_vexpand(True)
        self._pane_holder.pack_start(self._pane_container, True, True, 0)
        self.pack_start(self._pane_holder, True, True, 0)

    def _build_statusbar(self) -> None:
        self._statusbar = SessionStatusBar()
        # Attribute names kept so the stats helpers (and their tests) stay unchanged.
        self._info_spinner = self._statusbar.spinner
        self._cpu_label = self._statusbar.cpu_label
        self._mem_label = self._statusbar.mem_label
        self._disk_label = self._statusbar.disk_label
        self.pack_start(self._statusbar, False, False, 0)
        self._reset_stats_labels()

    # ------------------------------------------------------------------ helpers
    def _reset_stats_labels(self) -> None:
        for label, text in ((self._cpu_label, "CPU: —"), (self._mem_label, "Mem: —"), (self._disk_label, "Disk: —")):
            label.set_text(text)
            label.set_tooltip_text(None)
            apply_stat_level(label, None)
        self._info_spinner.stop()
        self._info_spinner.hide()
        if hasattr(self, "_statusbar"):
            self._statusbar.set_stats_visible(False)

    def _colors_from_appearance(self) -> Tuple[Gdk.RGBA, Gdk.RGBA, Gdk.RGBA, Gdk.RGBA, list[Gdk.RGBA]]:
        profile_theme = self._profile.theme if self._profile and self._profile.theme else ""
        colors = resolve_terminal_colors(self._appearance, profile_theme)

        def rgba(hex_color: str) -> Gdk.RGBA:
            color = Gdk.RGBA()
            color.parse(hex_color)
            return color

        return (
            rgba(colors.foreground),
            rgba(colors.background),
            rgba(colors.cursor),
            rgba(colors.highlight),
            [rgba(color) for color in colors.palette],
        )

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

    @property
    def title(self) -> str:
        return getattr(self, "_custom_title", "") or self._host.alias

    @title.setter
    def title(self, value: str) -> None:
        text = (value or "").strip()
        self._custom_title = "" if text == self._host.alias else text

    @property
    def is_split(self) -> bool:
        return self._secondary_terminal is not None

    def toggle_split(self) -> None:
        if self._secondary_terminal is None:
            self._duplicate_pane_once()
        else:
            self._close_duplicate_pane()

    @property
    def stats_visible(self) -> bool:
        return getattr(self, "_stats_enabled", True)

    def set_stats_visible(self, visible: bool) -> None:
        self._stats_enabled = bool(visible)
        if visible:
            self._show_stats_once()
        else:
            self._stop_stats_updates()

    @property
    def strip_ansi_on_copy(self) -> bool:
        return self._strip_ansi_on_copy

    @property
    def status(self) -> ConnectionStatus:
        return getattr(self, "_status", ConnectionStatus.UNKNOWN)

    @property
    def profile(self):
        return self._profile

    def connect_activity(self, callback: Callable[["TerminalTab"], None]) -> None:
        """Call ``callback(tab)`` whenever a terminal of this session produces output."""

        self._activity_callback = callback
        for term in self._terminals():
            self._watch_activity(term)

    def _watch_activity(self, term: Vte.Terminal) -> None:
        if self._activity_callback is None:
            return
        term.connect("contents-changed", lambda *_: self._activity_callback and self._activity_callback(self))

    def set_broadcast_highlight(self, active: bool) -> None:
        ctx = self._pane_holder.get_style_context()
        (ctx.add_class if active else ctx.remove_class)("broadcast-active")

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

    def _on_banner_response(self, _bar: Gtk.InfoBar, response: int) -> None:
        if response == _RESPONSE_RECONNECT:
            self.reconnect()
        elif response == _RESPONSE_CLOSE:
            self._request_close()

    def _on_terminal_size_allocate(self, term: Vte.Terminal, _allocation) -> None:
        self._statusbar.set_size(int(term.get_column_count() or 0), int(term.get_row_count() or 0))

    def _set_status(self, message: str, connected: bool) -> None:
        state = _status_for_message(message, connected)
        self._status = state
        self._status_label.set_text(message)
        reveal = not connected and state is not ConnectionStatus.CONNECTING
        self._banner.set_message_type(Gtk.MessageType.ERROR if state is ConnectionStatus.FAILED else Gtk.MessageType.WARNING)
        if reveal:
            self._banner.show()
        self._banner.set_revealed(reveal)
        self._reconnect_button.set_sensitive(not connected)
        self._statusbar.set_status(state, message)
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
        hostname, port, username = host_connection_params(self._host)
        return prompt_text(
            parent if isinstance(parent, Gtk.Window) else None,
            _("SSH Password"),
            _("Enter the password for {target}.").format(target=f"{username}@{hostname}:{port}"),
            secret=True,
            accept_label=_("C_onnect"),
        )

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
            self._log(_("Automatic password retry unavailable because sshpass is not installed."), Level.WARNING)
            return GLib.SOURCE_REMOVE
        password = self._prompt_for_password()
        if password:
            self._log(_("Retrying {alias} with entered password…").format(alias=self._host.alias), Level.INFO)

            def _start_retry() -> bool:
                self._spawn(
                    password,
                    password_origin="retry",
                    save_on_success=True,
                )
                return GLib.SOURCE_REMOVE

            GLib.idle_add(_start_retry)
        else:
            self._log(_("Password entry cancelled for {alias}.").format(alias=self._host.alias), Level.INFO)
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
        self._log(_("Authentication failed for {alias}. Enter a new password.").format(alias=self._host.alias), Level.ERROR)
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
            self._log(_("Saved updated password for {alias}.").format(alias=self._host.alias), Level.SUCCESS)
        else:
            self._log(_("Could not save updated password for {alias}.").format(alias=self._host.alias), Level.WARNING)

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
            self._log(_("Saved password for {alias}.").format(alias=self._host.alias), Level.SUCCESS)
        self._password_to_save = None

    def _spawn(
        self,
        override_password: Optional[str] = None,
        *,
        password_origin: str = "prompt",
        save_on_success: bool = False,
    ) -> None:
        """Start (or restart) the SSH child process for this session."""

        self._reset_attempt_state()
        proceed, password = self._password_for_attempt(override_password, password_origin, save_on_success)
        if not proceed:
            return
        argv, envv = self._spawn_command(password)
        self._start_child(argv, envv)

    def _reset_attempt_state(self) -> None:
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
        for attr in ("_password_save_source_id", "_connection_confirm_source_id", "_retry_timeout_id"):
            source_id = getattr(self, attr)
            if source_id:
                GLib.source_remove(source_id)
                setattr(self, attr, 0)

    def _password_for_attempt(
        self, override_password: Optional[str], password_origin: str, save_on_success: bool
    ) -> Tuple[bool, Optional[str]]:
        """Decide which password (if any) sshpass should send. Returns ``(proceed, password)``."""

        helper_available = shutil.which("sshpass") is not None
        credentials = self._credential_components()
        stored_password = self._secure_store.get(*credentials) if self._secure_store and credentials else None
        alias = self._host.alias
        if override_password is not None:
            if not helper_available:
                self._log(_("Cannot perform automatic password entry because sshpass is not installed."), Level.WARNING)
                self._password_source = "manual"
                return False, None
            self._password_source = password_origin
            if save_on_success:
                self._password_to_save = override_password
            return True, override_password
        if stored_password:
            if helper_available:
                self._password_source = "stored"
                return True, stored_password
            self._log(_("Stored password available for {alias} but sshpass is missing. Falling back to manual entry.").format(alias=alias), Level.WARNING)
            self._password_source = "manual"
            return True, None
        if not helper_available:
            self._log(_("Automatic password entry unavailable for {alias} (sshpass not installed).").format(alias=alias), Level.WARNING)
            self._password_source = "manual"
            return True, None
        password = self._prompt_for_password()
        if password is None:
            self._log(_("Password entry cancelled for {alias}.").format(alias=alias), Level.INFO)
            return False, None
        self._password_source = "prompt"
        self._password_to_save = password
        return True, password

    def _spawn_command(self, password: Optional[str]) -> Tuple[List[str], List[str]]:
        argv = self._ssh_arguments()
        envv = [f"{key}={value}" for key, value in os.environ.items()]
        if self._profile and self._profile.env_vars:
            envv.extend(f"{k}={v}" for k, v in self._profile.env_vars.items())
        if password:
            envv.append(f"SSHPASS={password}")
            argv = ["sshpass", "-e", *argv]
        return argv, envv

    def _start_child(self, argv: List[str], envv: List[str]) -> None:
        term = self._terminal
        term.reset(True, True)
        self._session_active = False
        self._set_status("Connecting…", False)
        alias = self._host.alias

        def _on_spawn_finish(_term: Vte.Terminal, pid: int, error: Optional[GLib.Error], _user_data) -> None:
            if error:
                self._log(_("Failed to open SSH session for {alias}: {error}").format(alias=alias, error=error.message), Level.ERROR)
                self._set_status("Connection failed", False)
                GLib.idle_add(self.emit, "session-closed", alias, False)
                return
            if pid == 0:
                self._log(_("SSH session exited immediately for {alias}").format(alias=alias), Level.WARNING)
                self._set_status("Connection closed", False)
                GLib.idle_add(self.emit, "session-closed", alias, False)
                return
            self._child_pid = pid
            self._set_status("Connected", True)
            self.focus_terminal()

        self._banner.set_revealed(False)
        self._reconnect_button.set_sensitive(False)
        term.spawn_async(
            Vte.PtyFlags.DEFAULT, None, argv, envv, GLib.SpawnFlags.DEFAULT,
            None, None, -1, None, _on_spawn_finish, None,
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
                self._log(_("Failed to open secondary session for {alias}: {error}").format(alias=self._host.alias, error=error.message), Level.ERROR)
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
            self._log(_("Failed to attach terminal session."), Level.ERROR)
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
        self._log(_("{alias}: Session ended.").format(alias=self._host.alias), Level.INFO)
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
                    _("Authentication failed for {alias}. Stored password removed.").format(alias=self._host.alias),
                    Level.ERROR,
                )
                schedule_retry = True
            elif self._password_source in {"prompt", "retry"}:
                self._log(_("Authentication failed for {alias}.").format(alias=self._host.alias), Level.ERROR)
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
        if self._stats_source_id or not self._stats_enabled:
            return
        self._statusbar.set_stats_visible(True)
        self._update_stats()
        self._stats_source_id = GLib.timeout_add_seconds(10, self._update_stats)

    def _stop_stats_updates(self) -> None:
        if self._stats_source_id:
            GLib.source_remove(self._stats_source_id)
            self._stats_source_id = 0
        self._stats_updating = False
        self._reset_stats_labels()

    def _duplicate_pane_once(self) -> None:
        if self._secondary_terminal is not None:
            return
        self._secondary_terminal = self._create_terminal()
        self._apply_terminal_theme()
        self._secondary_terminal.connect("key-press-event", self._on_terminal_key_press)
        self._secondary_terminal.connect("focus-in-event", self._on_terminal_focus)
        self._secondary_terminal.connect("child-exited", self._on_secondary_child_exited)
        self._watch_activity(self._secondary_terminal)
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
        self._log(_("{alias}: Secondary session ended.").format(alias=self._host.alias), Level.INFO)
        GLib.idle_add(self._close_duplicate_pane)

    def save_transcript(self) -> None:
        term = self._active_terminal()
        if not term:
            return
        parent = self.get_toplevel()
        target = choose_save_file(
            parent if isinstance(parent, Gtk.Window) else None,
            _("Save Transcript"),
            current_name=f"{self.title}-{time.strftime('%Y%m%d-%H%M%S')}.txt",
        )
        if not target:
            return
        try:
            cleaned = strip_ansi(terminal_text(term))
            write_text_atomic(Path(target), cleaned)
            self._log(_("Transcript saved to {path}").format(path=target), Level.SUCCESS)
        except Exception as exc:
            self._log(_("Transcript save failed: {error}").format(error=exc), Level.ERROR)

    def toggle_strip_ansi_copy(self) -> None:
        self._strip_ansi_on_copy = not self._strip_ansi_on_copy
        state = "on" if self._strip_ansi_on_copy else "off"
        self._log(_("Strip ANSI on copy {state}.").format(state=state), Level.INFO)

    def _set_strip_ansi(self, active: bool) -> None:
        self._strip_ansi_on_copy = active
        state = "on" if active else "off"
        self._log(_("Strip ANSI on copy {state}.").format(state=state), Level.INFO)

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
            self._log(_("Remote stats require an active session."), Level.INFO)
            return
        self._start_stats_updates()

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
            _set_stat_tooltip(self._cpu_label, _("CPU usage: {pct:.1f}%").format(pct=cpu))
        else:
            self._cpu_label.set_text("CPU: N/A")
        apply_stat_level(self._cpu_label, cpu)
        mem_used = numeric(data.get("mem_used"))
        mem_total = numeric(data.get("mem_total"))
        mem_pct = None
        if mem_used is not None and mem_total is not None and mem_total > 0:
            mem_pct = mem_used / mem_total * 100.0
            self._mem_label.set_text(
                f"Mem: {mem_used / 1024:.1f}/{mem_total / 1024:.1f} GB"
            )
            _set_stat_tooltip(
                self._mem_label,
                _("Memory: {used:.0f} MiB of {total:.0f} MiB ({pct:.0f}%)").format(used=mem_used, total=mem_total, pct=mem_pct),
            )
        else:
            self._mem_label.set_text("Mem: N/A")
        apply_stat_level(self._mem_label, mem_pct)
        disk_used = numeric(data.get("disk_used"))
        disk_total = numeric(data.get("disk_total"))
        disk_pct = None
        if disk_used is not None and disk_total is not None and disk_total > 0:
            disk_pct = disk_used / disk_total * 100.0
            self._disk_label.set_text(
                f"Disk: {disk_used:.1f}/{disk_total:.1f} GB"
            )
            _set_stat_tooltip(
                self._disk_label,
                _("Disk (/): {used:.2f} GB of {total:.2f} GB ({pct:.0f}%)").format(used=disk_used, total=disk_total, pct=disk_pct),
            )
        else:
            self._disk_label.set_text("Disk: N/A")
        apply_stat_level(self._disk_label, disk_pct)
        return False


class TerminalNotebook(Gtk.Box):
    """Session workspace: one toolbar, the broadcast strip and tabs or tiles."""

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
        self._tab_labels: Dict[TerminalTab, TabLabel] = {}
        self._tab_status: Dict[TerminalTab, bool] = {}
        self._tabs: List[TerminalTab] = []
        self._tile_wrappers: Dict[TerminalTab, TileFrame] = {}
        self._tile_mode = False
        self._broadcast_mode = False
        self._broadcast_included: Dict[TerminalTab, bool] = {}
        self._last_current_tab: Optional[TerminalTab] = None
        self._secure_store = password_store
        self._password_update_handler = password_update_handler
        self._appearance = appearance
        self.on_quick_connect: Optional[Callable[[], None]] = None
        self.on_new_host: Optional[Callable[[], None]] = None
        self.on_duplicate_session: Optional[Callable[[TerminalTab], None]] = None

        self._build_toolbar()
        self._broadcast_strip = BroadcastStrip(lambda: self.set_broadcast_mode(False))
        self.pack_start(self._broadcast_strip, False, False, 0)
        self._build_stack()
        self._sync_toolbar()

    # ------------------------------------------------------------ building
    def _build_toolbar(self) -> None:
        bar = SessionToolbar()
        self._toolbar = bar
        self._toggle_handlers = {
            bar.split: bar.split.connect("toggled", self._on_split_toggled),
            bar.plain_copy: bar.plain_copy.connect("toggled", self._on_plain_copy_toggled),
            bar.tile: bar.tile.connect("toggled", self._on_tile_toggled),
            bar.broadcast: bar.broadcast.connect("toggled", self._on_broadcast_toggled),
            bar.stats: bar.stats.connect("toggled", self._on_stats_toggled),
        }
        bar.save.connect("clicked", lambda *_: self.save_current_transcript())
        bar.reconnect.connect("clicked", lambda *_: self.reconnect_current())
        bar.set_overflow(self._overflow_menu_spec())
        self.pack_start(bar, False, False, 0)

    def _build_stack(self) -> None:
        self._notebook = Gtk.Notebook()
        self._notebook.set_scrollable(True)
        self._notebook.set_show_border(False)
        self._notebook.set_hexpand(True)
        self._notebook.set_vexpand(True)
        self._notebook.get_style_context().add_class("session-tabs")
        self._notebook.connect("switch-page", self._on_switch_page)
        self._notebook.connect("page-reordered", lambda *_: self._sync_tab_order())
        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        actions.pack_start(
            IconButton(Icons.NEW, _("Quick connect (Ctrl+Shift+T)"), self._request_quick_connect),
            False,
            False,
            0,
        )
        self._tabs_menu_button = IconMenuButton(Icons.MORE, _("All sessions"))
        self._tabs_menu_button.connect("toggled", self._on_tabs_menu_toggled)
        actions.pack_start(self._tabs_menu_button, False, False, 0)
        actions.show_all()
        self._notebook.set_action_widget(actions, Gtk.PackType.END)

        self._tile_grid = Gtk.Grid()
        self._tile_grid.set_row_spacing(Space.S)
        self._tile_grid.set_column_spacing(Space.S)
        self._tile_grid.set_row_homogeneous(True)
        self._tile_grid.set_column_homogeneous(True)
        self._tile_grid.set_hexpand(True)
        self._tile_grid.set_vexpand(True)
        self._tile_grid.set_border_width(Space.S)
        self._tile_scroller = Gtk.ScrolledWindow()
        # Tiles shrink to the workspace width; only scroll vertically when there are many.
        self._tile_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._tile_scroller.add(self._tile_grid)

        self._placeholder = EmptyState(
            Icons.TERMINAL,
            _("No open sessions"),
            _(
                "Double-click a host or press Enter to connect. Ctrl+T opens the selected host; "
                "Ctrl+Shift+T opens Quick Connect."
            ),
            action_label=_("_Quick Connect…"),
            on_action=self._request_quick_connect,
            secondary_label=_("_New Host…"),
            on_secondary=lambda: self.on_new_host() if self.on_new_host else None,
        )

        self._stack = Gtk.Stack()
        self._stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self._stack.set_hhomogeneous(False)
        self._stack.set_transition_duration(get_theme_manager().motion_ms(Motion.FAST))
        self._stack.add_named(self._placeholder, "placeholder")
        self._stack.add_named(self._notebook, "tabs")
        self._stack.add_named(self._tile_scroller, "tiles")
        self._stack.set_visible_child_name("placeholder")
        self.pack_start(self._stack, True, True, 0)

    def _overflow_menu_spec(self):
        return [
            MenuSection("terminal", [
                MenuItemSpec(_("Send _Interrupt"), self.interrupt_current, Icons.INTERRUPT, "Shift+Ctrl+K"),
                MenuItemSpec(_("Zoom _In"), self.increase_font, "zoom-in-symbolic", "Ctrl++"),
                MenuItemSpec(_("Zoom _Out"), self.decrease_font, "zoom-out-symbolic", "Ctrl+-"),
                MenuItemSpec(_("_Reset Zoom"), self.reset_font, "zoom-original-symbolic", "Ctrl+0"),
            ]),
            MenuSection("session", [
                MenuItemSpec(_("_Duplicate Session"), self.duplicate_current, Icons.DUPLICATE, "Shift+Ctrl+D"),
                MenuItemSpec(_("Re_name Session…"), self.rename_current, Icons.EDIT, "F2"),
            ]),
            MenuSection("close", [
                MenuItemSpec(_("Close _Other Sessions"), lambda: self.close_others(self._current_tab())),
                MenuItemSpec(_("_Close Session"), self.close_current, Icons.CLOSE, "Ctrl+W"),
            ]),
        ]

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
        tab.connect_activity(self._on_tab_activity)

        self._tabs.append(tab)
        self._tab_status[tab] = False
        self._broadcast_included[tab] = True
        self._last_current_tab = tab

        if self._tile_mode:
            self._rebuild_tile_grid()
        else:
            self._append_tab_to_notebook(tab)
        self._update_placeholder()
        self._update_broadcast_ui()
        GLib.idle_add(tab.focus_terminal)
        return tab

    def _append_tab_to_notebook(self, tab: TerminalTab) -> int:
        self._detach_tab(tab)
        label = TabLabel(
            tab.title,
            on_close=lambda t=tab: self.close_tab(t),
            on_context_menu=lambda event, t=tab: self._show_tab_menu(t, event),
        )
        label.set_title(tab.title, tab.status)
        page_num = self._notebook.append_page(tab, label)
        tab.show_all()
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
        if not target or target not in self._tabs:
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
        self._update_broadcast_ui()

    def close_current(self) -> None:
        self.close_tab()

    def close_others(self, keep: Optional[TerminalTab]) -> None:
        for tab in [t for t in self._tabs if t is not keep]:
            self.close_tab(tab)

    def close_to_right(self, tab: TerminalTab) -> None:
        ordered = self._ordered_tabs()
        if tab not in ordered:
            return
        for other in ordered[ordered.index(tab) + 1:]:
            self.close_tab(other)

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
            self._sync_toolbar()

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

    # ------------------------------------------------------- session commands
    def current_tab(self) -> Optional[TerminalTab]:
        return self._current_tab()

    def tabs(self) -> List[TerminalTab]:
        return list(self._tabs)

    def find_tab(self, host: HostEntry) -> Optional[TerminalTab]:
        """Return the first open session for ``host`` (matched by group path and alias)."""

        for tab in self._tabs:
            other = tab.host
            if (other.parent_group, other.child_group, other.alias) == (host.parent_group, host.child_group, host.alias):
                return tab
        return None

    def focus_tab(self, tab: TerminalTab) -> None:
        if tab in self._tabs:
            self._focus_tab(tab)

    def _focus_tab(self, tab: TerminalTab) -> None:
        self._last_current_tab = tab
        if not self._tile_mode:
            page = self._notebook.page_num(tab)
            if page != -1:
                self._notebook.set_current_page(page)
        tab.focus_terminal()
        self._sync_toolbar()

    def _ordered_tabs(self) -> List[TerminalTab]:
        if self._tile_mode:
            return list(self._tabs)
        pages = [self._notebook.get_nth_page(i) for i in range(self._notebook.get_n_pages())]
        return [page for page in pages if isinstance(page, TerminalTab)]

    def _sync_tab_order(self) -> None:
        ordered = self._ordered_tabs()
        if len(ordered) == len(self._tabs):
            self._tabs = ordered

    def _step_tab(self, delta: int) -> None:
        ordered = self._ordered_tabs()
        if not ordered:
            return
        current = self._current_tab()
        index = ordered.index(current) if current in ordered else 0
        self._focus_tab(ordered[(index + delta) % len(ordered)])

    def next_tab(self) -> None:
        self._step_tab(1)

    def prev_tab(self) -> None:
        self._step_tab(-1)

    def goto_tab(self, number: int) -> None:
        """Focus session ``number`` (1-based, in tab order)."""

        ordered = self._ordered_tabs()
        if 1 <= number <= len(ordered):
            self._focus_tab(ordered[number - 1])

    def reconnect_current(self) -> None:
        tab = self._current_tab()
        if tab:
            tab.reconnect()

    def duplicate_current(self) -> None:
        tab = self._current_tab()
        if tab:
            self.duplicate_tab(tab)

    def duplicate_tab(self, tab: TerminalTab) -> None:
        if self.on_duplicate_session is not None:
            self.on_duplicate_session(tab)
        else:
            self.new_terminal(tab.host, profile=tab.profile)

    def toggle_split_current(self) -> None:
        tab = self._current_tab()
        if tab:
            tab.toggle_split()
            self._sync_toolbar()

    def toggle_stats_current(self) -> None:
        tab = self._current_tab()
        if tab:
            tab.set_stats_visible(not tab.stats_visible)
            self._sync_toolbar()

    def rename_current(self) -> None:
        tab = self._current_tab()
        if tab:
            self.rename_tab(tab)

    def rename_tab(self, tab: TerminalTab) -> None:
        toplevel = self.get_toplevel()
        title = prompt_text(
            toplevel if isinstance(toplevel, Gtk.Window) else None,
            _("Rename Session"),
            _("Tab title for {alias} (enter the host alias to reset):").format(alias=tab.host.alias),
            tab.title,
            accept_label=_("_Rename"),
        )
        if title:
            tab.title = title
            self._set_tab_label_text(tab)

    # ------------------------------------------------------- tiles/broadcast
    @property
    def tile_mode(self) -> bool:
        return self._tile_mode

    def set_tile_mode(self, active: bool) -> None:
        active = bool(active) and len(self._tabs) > 1
        if not active and self._broadcast_mode:
            self.set_broadcast_mode(False)
        self._set_tile_mode(active)
        self._sync_toolbar()

    def toggle_tile_mode(self) -> None:
        self.set_tile_mode(not self._tile_mode)

    @property
    def broadcast_active(self) -> bool:
        return self._broadcast_mode

    def set_broadcast_mode(self, active: bool) -> None:
        active = bool(active)
        if active == self._broadcast_mode:
            self._sync_toolbar()
            return
        if active and len(self._tabs) < 2:
            self._log(_("Broadcast needs at least two open sessions."), Level.INFO)
            self._sync_toolbar()
            return
        self._broadcast_mode = active
        if active and not self._tile_mode:
            self._set_tile_mode(True)
        self._update_broadcast_ui()
        self._sync_toolbar()
        self._log(
            _("Broadcast input enabled. Excluded sessions will not receive mirrored input.")
            if active
            else _("Broadcast input disabled."),
            Level.WARNING if active else Level.INFO,
        )

    def toggle_broadcast(self) -> None:
        self.set_broadcast_mode(not self._broadcast_mode)

    def _update_broadcast_ui(self) -> None:
        targets = [t for t in self._tabs if self._broadcast_included.get(t, True) and self._tab_status.get(t, False)]
        self._broadcast_strip.set_target_count(len(targets))
        self._broadcast_strip.set_reveal_child(self._broadcast_mode)
        for tab in self._tabs:
            tab.set_broadcast_highlight(self._broadcast_mode and self._broadcast_included.get(tab, True))

    def _on_broadcast_include_toggled(self, tab: TerminalTab, active: bool) -> None:
        self._broadcast_included[tab] = active
        self._update_broadcast_ui()

    def _on_tab_input(self, source: TerminalTab, data: str) -> None:
        self._last_current_tab = source
        if not self._broadcast_mode or not data:
            return
        for target in broadcast_target_tabs(self._tabs, source, self._broadcast_included, self._tab_status):
            target.feed_input(data)

    def _create_tile_wrapper(self, tab: TerminalTab) -> TileFrame:
        self._detach_tab(tab)
        frame = TileFrame(
            tab.title,
            tab.status,
            included=self._broadcast_included.get(tab, True),
            on_include_toggled=lambda active, t=tab: self._on_broadcast_include_toggled(t, active),
            on_close=lambda t=tab: self.close_tab(t),
            on_focus=lambda t=tab: self._focus_tab(t),
        )
        frame.set_body(tab)
        frame.show_all()
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
            self._tile_grid.attach(self._create_tile_wrapper(tab), column, row, 1, 1)
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

    # ----------------------------------------------------------------- helpers
    def _tab_label_text(self, tab: TerminalTab) -> str:
        connected = self._tab_status.get(tab, False)
        indicator = "●" if connected else "○"
        return f"{indicator} {tab.title}"

    def _set_tab_label_text(self, tab: TerminalTab) -> None:
        label = self._tab_labels.get(tab)
        if label:
            label.set_title(tab.title, tab.status)
        frame = self._tile_wrappers.get(tab)
        if frame:
            frame.set_title(tab.title, tab.status)

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

    def _on_tab_focus(self, _widget, _event, tab: TerminalTab) -> bool:
        if self._last_current_tab is not tab:
            self._last_current_tab = tab
            self._sync_toolbar()
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

    def _on_switch_page(self, _notebook: Gtk.Notebook, page: Gtk.Widget, _num: int) -> None:
        if isinstance(page, TerminalTab):
            self._last_current_tab = page
        for tab, label in self._tab_labels.items():
            label.set_current(tab is page)
        GLib.idle_add(self._sync_toolbar_idle)

    def _sync_toolbar_idle(self) -> bool:
        self._sync_toolbar()
        return GLib.SOURCE_REMOVE

    def _on_tab_activity(self, tab: TerminalTab) -> None:
        current = self._current_tab()
        if tab is current or self._tile_mode:
            return
        label = self._tab_labels.get(tab)
        if label:
            label.set_activity(True)

    def _on_tab_status_changed(self, tab: TerminalTab, alias: str, connected: bool) -> None:
        self._tab_status[tab] = connected
        self._set_tab_label_text(tab)
        self._update_broadcast_ui()
        if tab is self._current_tab():
            self._sync_toolbar()

    def _on_tab_session_closed(self, tab: TerminalTab, alias: str, exited_normally: bool) -> None:
        status = _("Session ended cleanly") if exited_normally else _("Session terminated unexpectedly")
        self._log(_("{alias}: {status}").format(alias=alias, status=status), exited_normally)
        self._set_tab_label_text(tab)

    def _on_tab_close_requested(self, tab: TerminalTab) -> None:
        GLib.idle_add(self._close_tab_after_idle, tab)

    def _close_tab_after_idle(self, tab: TerminalTab) -> bool:
        self.close_tab(tab)
        return GLib.SOURCE_REMOVE

    def _request_quick_connect(self) -> None:
        if self.on_quick_connect is not None:
            self.on_quick_connect()

    def _show_tab_menu(self, tab: TerminalTab, event: Gdk.EventButton) -> None:
        ordered = self._ordered_tabs()
        index = ordered.index(tab) if tab in ordered else -1
        spec = tab_menu_spec(
            reconnect=tab.reconnect,
            duplicate=lambda: self.duplicate_tab(tab),
            split=lambda: (tab.toggle_split(), self._sync_toolbar()),
            is_split=tab.is_split,
            rename=lambda: self.rename_tab(tab),
            save=tab.save_transcript,
            close=lambda: self.close_tab(tab),
            close_others=lambda: self.close_others(tab),
            close_right=lambda: self.close_to_right(tab),
            has_others=len(self._tabs) > 1,
            has_right=0 <= index < len(ordered) - 1,
        )
        popup_menu(build_context_menu(spec, attach_to=self), event)

    def _on_tabs_menu_toggled(self, button: Gtk.MenuButton) -> None:
        if not button.get_active():
            return
        items = [
            MenuItemSpec(f"{number}. {tab.title}", lambda t=tab: self._focus_tab(t), accel=f"Alt+{number}" if number < 10 else "")
            for number, tab in enumerate(self._ordered_tabs(), start=1)
        ]
        sections = [MenuSection("tabs", items)] if items else [
            MenuSection("empty", [MenuItemSpec(_("No open sessions"), sensitive=False)])
        ]
        button.set_popup(build_menu(sections))

    def _update_placeholder(self) -> None:
        if not self._tabs:
            visible = "placeholder"
        elif self._tile_mode:
            visible = "tiles"
        else:
            visible = "tabs"
        self._stack.set_visible_child_name(visible)
        if len(self._tabs) <= 1:
            if self._broadcast_mode:
                self.set_broadcast_mode(False)
            if self._tile_mode:
                self._set_tile_mode(False)
        self._sync_toolbar()

    # ------------------------------------------------------------- toolbar
    def _set_toggle(self, button: Gtk.ToggleButton, active: bool) -> None:
        if button.get_active() == active:
            return
        handler = self._toggle_handlers[button]
        button.handler_block(handler)
        button.set_active(active)
        button.handler_unblock(handler)

    def _sync_toolbar(self) -> None:
        bar = self._toolbar
        tab = self._current_tab()
        has_tab = tab is not None
        multiple = len(self._tabs) > 1
        for widget in (bar.split, bar.save, bar.plain_copy, bar.stats, bar.reconnect, bar.more):
            widget.set_sensitive(has_tab)
        bar.tile.set_sensitive(multiple)
        bar.broadcast.set_sensitive(multiple)
        self._set_toggle(bar.split, bool(tab and tab.is_split))
        self._set_toggle(bar.plain_copy, bool(tab and tab.strip_ansi_on_copy))
        self._set_toggle(bar.stats, bool(tab and tab.stats_visible))
        self._set_toggle(bar.tile, self._tile_mode)
        self._set_toggle(bar.broadcast, self._broadcast_mode)

    def _on_split_toggled(self, button: Gtk.ToggleButton) -> None:
        tab = self._current_tab()
        if tab and tab.is_split != button.get_active():
            tab.toggle_split()
        self._sync_toolbar()

    def _on_plain_copy_toggled(self, button: Gtk.ToggleButton) -> None:
        tab = self._current_tab()
        if tab:
            tab._set_strip_ansi(button.get_active())

    def _on_tile_toggled(self, button: Gtk.ToggleButton) -> None:
        self.set_tile_mode(button.get_active())

    def _on_broadcast_toggled(self, button: Gtk.ToggleButton) -> None:
        self.set_broadcast_mode(button.get_active())

    def _on_stats_toggled(self, button: Gtk.ToggleButton) -> None:
        tab = self._current_tab()
        if tab:
            tab.set_stats_visible(button.get_active())
