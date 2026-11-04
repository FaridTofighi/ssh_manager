from __future__ import annotations

import functools
import logging
import math
import os
import signal
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Dict, List, Optional, Protocol, Tuple

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Vte", "2.91")

from gi.repository import Gdk, Gio, GLib, GObject, Gtk, Vte, Pango  # type: ignore  # noqa: E402

from ophion.auth_flow import (
    AUTH_FAILURE_RE,
    HEURISTIC_DELAY_S,
    MAX_ATTEMPTS,
    NO_PASSWORD,
    NO_PASSWORD_METHOD_MESSAGE,
    RETRY_DELAY_S,
    SOURCE_PROMPTED,
    AuthOutcome,
    Decision,
    PasswordFlow,
    PasswordResolution,
    SaveContext,
    SaveOffer,
    PassphraseFill,
    passphrase_prompt,
    classify_exit,
    new_marker_path,
    probably_authenticated,
    sentinel_options,
    source_phrase,
    with_sentinel,
)
from ophion.config import HostEntry
from ophion.host_utils import host_connection_params, host_option
from ophion.ssh_command import build_ssh_command
from ophion.secure_store import SecureStore, SecureStoreError
from ophion.text_utils import strip_ansi
from ophion import host_key_preflight  # noqa: E402
from ophion.host_key_output import HostKeyOutputDetector, UnknownHostKeyPrompt, split_host  # noqa: E402
from ophion.gtkui.dialogs.host_key import ChangedKey, changed_key_names, remove_old_key, risk_text, show_details  # noqa: E402
from ophion.remote_stats import (  # noqa: E402,F401  (moved; kept importable from here)
    _REMOTE_STATS_SCRIPT, build_remote_stats_command, parse_remote_stats_output,
    HOST_KEY_CHANGED_TEXT, STATS_HOST_KEY_CHANGED, STATS_HOST_KEY_UNKNOWN, stats_failure,
)
from ophion.local_shell import terminal_env  # noqa: E402
from ophion.persistence import write_text_atomic
from ophion.password_change import PasswordChangeTracker
# --- BEGIN: PARAMIKO_VTE_IMPORTS ---
from ophion.vte_bridge import VTEBridge
# --- END: PARAMIKO_VTE_IMPORTS ---
from ophion.settings import LocalShellSettings, TerminalAppearanceSettings, TerminalInputSettings
from ophion.terminal_input import MouseAction, decide_mouse_action  # noqa: E402
from ophion.gtkui.theme.terminal_schemes import resolve_terminal_colors
from ophion.gtkui.constants import Icons
from ophion.gtkui.dialogs.files import choose_save_file
from ophion.gtkui.dialogs.password import PasswordRequestDialog, SavePasswordDialog
from ophion.gtkui.dialogs.prompt import prompt_text
from ophion.gtkui.i18n import _
from ophion.gtkui.menu_spec import MenuItemSpec, MenuSection
from ophion.gtkui.controllers.shortcuts import accel_label  # noqa: E402
from ophion.gtkui.view_models import status_text
from ophion.gtkui.theme.loader import get_theme_manager
from ophion.gtkui.theme.tokens import ConnectionStatus, Level, Motion, Size, Space
from ophion.gtkui.views.statusbar import SessionStatusBar, apply_stat_level, set_spinner_busy
from ophion.gtkui.views.workspace import (
    BroadcastStrip,
    SessionStrip,
    SessionTab,
    SessionToolbar,
    TileFrame,
    tab_menu_spec,
)
from ophion.gtkui.pane_activation import ActivePane
from ophion.gtkui.session_page import SessionPage
from ophion.gtkui.tab_labels import SessionLabelInfo, TabLabelMode, is_duplicate, pane_labels, session_tab_labels
from ophion.gtkui.widgets.pane_frame import PaneFooter, PaneFrame
from ophion.gtkui.widgets.empty_state import EmptyState
from ophion.gtkui.widgets.icon_button import IconMenuButton, set_accessible_name
from ophion.gtkui.widgets.menus import build_context_menu, build_menu, popup_menu

if TYPE_CHECKING:  # pragma: no cover
    from ophion.profiles import SessionProfile

LogFn = Callable[[str, object], None]  # message, success flag or Level
PasswordUpdateFn = Callable[[HostEntry, str], bool]
PasswordProvider = Callable[[], PasswordResolution]  # re-resolved at every (re)connect


class PasswordService(Protocol):
    """What a tab needs from the app to name, offer and save a password (``CredentialController``)."""

    def describe_password_source(self, resolution: PasswordResolution) -> str: ...

    def password_save_context(self, host: HostEntry) -> SaveContext: ...

    def save_host_password(self, host: HostEntry, password: str) -> Tuple[str, str]: ...
LOGGER = logging.getLogger(__name__)


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


def _copy_selection(term: Vte.Terminal) -> None:
    """The selection as plain text into the clipboard (no formatting)."""

    if hasattr(term, "copy_clipboard_format"):
        term.copy_clipboard_format(Vte.Format.TEXT)
    else:  # VTE < 0.50
        term.copy_clipboard()


def _terminal_contents(term: Vte.Terminal) -> Optional[str]:
    """The terminal's whole text (scrollback and screen) via ``write_contents_sync``; ``None`` if unavailable.

    For VTE < 0.72, which has no ``get_text_range_format``: there ``get_text`` / ``get_text_range`` print
    two warnings on every call through PyGObject (callback ignored, attributes array deprecated).
    """

    if not hasattr(term, "write_contents_sync"):
        return None
    try:
        stream = Gio.MemoryOutputStream.new_resizable()
        term.write_contents_sync(stream, Vte.WriteFlags.DEFAULT, None)
        stream.close(None)
        return bytes(stream.steal_as_bytes().get_data()).decode("utf-8", "replace")
    except Exception:
        return None


def terminal_text(term: Vte.Terminal) -> str:
    """Return all visible/scrollback text from a VTE terminal.

    VTE >= 0.76 asserts (``attributes == nullptr``) and returns nothing from the
    deprecated ``get_text`` when called through PyGObject, so the range-format
    API over the whole scrollback (the vertical adjustment's bounds) comes first.
    """

    if hasattr(term, "get_text_range_format"):
        adjustment = term.get_vadjustment() if hasattr(term, "get_vadjustment") else None
        if adjustment is not None:
            lower, upper = int(adjustment.get_lower()), int(adjustment.get_upper())
        else:
            lower, upper = 0, int(term.get_row_count() or 1)
        columns = max(1, int(term.get_column_count() or 1))
        result = term.get_text_range_format(Vte.Format.TEXT, lower, 0, max(lower, upper - 1), columns - 1)
        return _terminal_text_result(result)
    if hasattr(term, "get_text_format"):
        return _terminal_text_result(term.get_text_format(Vte.Format.TEXT))
    contents = _terminal_contents(term)  # VTE < 0.72
    if contents is not None:
        return contents
    try:  # legacy VTE (< 0.76)
        result = term.get_text(_select_all_terminal_text, None, None)
    except TypeError:
        result = term.get_text(_select_all_terminal_text, None)
    return _terminal_text_result(result)


def auth_failure_output_detected(output: str) -> bool:
    """Return True when OpenSSH output shows a password auth failure."""

    return bool(output and AUTH_FAILURE_RE.search(strip_ansi(output)))


FOCUS_RETRY_MS = 50  # focus_terminal(): retry interval while the terminal is not shown
FOCUS_RETRY_LIMIT = 40  # … and give up after ~2 s

_RESPONSE_RECONNECT = 1
_RESPONSE_HOST_KEY_DETAILS, _RESPONSE_HOST_KEY_REMOVE, _RESPONSE_HOST_KEY_COPY, _RESPONSE_HOST_KEY_DISMISS = 11, 12, 13, 14
_RESPONSE_HOST_KEY_REVIEW = 15
RECONNECT_STAGGER_MS = 150
_HOST_KEY_UNTRUSTED_WARNED: set = set()  # hosts whose untrusted key the stats probe reported
_HOST_KEY_WARNED: set = set()  # hosts whose changed key the stats probe reported (once per session)  # broadcast: ended sessions reconnect one after another, not all at once
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


_PANE_KEYS = {
    Gdk.KEY_Left: "left", Gdk.KEY_Right: "right", Gdk.KEY_Up: "up", Gdk.KEY_Down: "down",
    Gdk.KEY_KP_Left: "left", Gdk.KEY_KP_Right: "right", Gdk.KEY_KP_Up: "up", Gdk.KEY_KP_Down: "down",
}


class TerminalPaneContainer(Gtk.Box):
    """Wrapper for a single terminal pane (prep for future splits)."""

    def __init__(self, terminal: Vte.Terminal) -> None:
        Gtk.Box.__init__(self, orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.terminal = terminal
        self.terminal.set_hexpand(True)
        self.terminal.set_vexpand(True)
        self.pack_start(self.terminal, True, True, 0)
        self.show_all()


# envv is the complete environment (terminal_env(os.environ)): NO_PARENT_ENVV, else VTE starts from the
# app's own environment and its TERM (tmux-256color when started inside tmux) reaches the server.
_SSH_SPAWN_FLAGS = GLib.SpawnFlags(int(GLib.SpawnFlags.SEARCH_PATH) | int(getattr(Vte, "SPAWN_NO_PARENT_ENVV", 0)))


class TerminalTab(SessionPage):
    """Single SSH session embedded inside a Vte.Terminal (its signals come from :class:`SessionPage`)."""

    __gtype_name__ = "TerminalTab"
    __gsignals__ = {
        # ``q`` / the banner's Close on an ended session: the notebook may close its ended broadcast peers too.
        "close-ended-requested": (GObject.SignalFlags.RUN_LAST, None, ()),
        # ``r`` / the banner's Reconnect on an ended session: the notebook may reconnect its ended peers too.
        "reconnect-requested": (GObject.SignalFlags.RUN_LAST, None, ()),
    }
    is_terminal = True
    kind = "terminal"

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
        password_provider: Optional[PasswordProvider] = None,
        password_service: Optional[PasswordService] = None,
    ) -> None:
        Gtk.Box.__init__(self, orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._init_state(host, log, font_scale, password_store, password_update_handler, autostart, profile, appearance)
        self._password_provider: Optional[PasswordProvider] = password_provider
        # Names a rejected password, offers and saves a typed one; None = never offer (auxiliary sessions).
        self._password_service: Optional[PasswordService] = password_service
        self.set_hexpand(True)
        self.set_vexpand(True)
        self._build_infobar()
        self._build_host_key_bar()
        self._build_terminal_area()
        self._build_statusbar()

        self._terminal.connect("key-press-event", self._on_terminal_key_press)
        self._terminal.connect("child-exited", self._on_child_exited)
        self._terminal.connect("focus-in-event", self._on_terminal_focus)
        self._terminal.connect("size-allocate", self._on_terminal_size_allocate)
        self._panes.add(self._terminal)
        self._terminal.connect("contents-changed", self._on_auth_output)
        self._terminal.connect("contents-changed", self._on_host_key_output)
        self._terminal.connect("contents-changed", self._on_passphrase_output)
        self.connect("destroy", lambda *_a: self._on_tab_destroyed())
        if autostart:
            self._begin_connect()
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
        self._password_provider = None
        self._resolution: PasswordResolution = NO_PASSWORD  # the last resolution (for messages; never logged)
        self._password_source: str = "none"  # credential | host | legacy | session | prompt | retry | manual | none
        # Passwords live only in memory: the one sent in this attempt, and a typed one that worked in this tab.
        self._attempt_password: Optional[str] = None
        self._session_password: Optional[str] = None
        # Attempts, the typed password and the once-per-tab save offer (per tab: never shared between tabs).
        self._flow = PasswordFlow(host.alias)
        self._password_dialog: Optional[PasswordRequestDialog] = None
        self._save_dialog: Optional[SavePasswordDialog] = None
        self._authenticated = False  # verified: LocalCommand sentinel, or the fallback heuristic
        self._auth_method = "none"  # sentinel | heuristic
        self._marker: Optional[Path] = None
        self._auth_watch_source_id = 0
        self._spawned_at = 0.0
        self._output_events = 0
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
        self._ever_connected = False  # = authenticated (kept for the existing call sites)
        self._retry_timeout_id = 0
        self._secondary_terminal: Optional[Vte.Terminal] = None
        self._paned: Optional[Gtk.Paned] = None
        # Split pane: one PaneFrame (+ footer) per terminal; None while there is one pane.
        self._pane_frames: Dict[Vte.Terminal, PaneFrame] = {}
        self._pane_footers: Dict[Vte.Terminal, PaneFooter] = {}
        self._zoomed_pane: Optional[Vte.Terminal] = None
        self._pane_label = host.alias
        self.on_focus_neighbor: Optional[Callable[[str], bool]] = None  # Alt+arrow outside the split
        # The active pane (exactly one); every change goes through activate_pane().
        self._panes: ActivePane = ActivePane()
        self.on_activated: Optional[Callable[["TerminalTab"], None]] = None  # a pane of this tab became active
        self._input_observer: Optional[Callable[[TerminalTab, str], None]] = None
        self._macro_feeding = False  # macro playback is writing: not user input (password heuristics, broadcast)
        self._profile = profile
        self._strip_ansi_on_copy = False
        self._appearance = appearance

        # Mouse and selection (gnome-terminal conventions; ``terminal_input.py``).
        self._input_settings = TerminalInputSettings()
        self._handled_buttons: set = set()  # (terminal, button) presses handled here: their release too
        self._text_cursors: Dict[Vte.Terminal, Optional[Gdk.Cursor]] = {}
        self.on_open_preferences: Optional[Callable[[], None]] = None  # the terminal menu's "Preferences…"

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
        self._banner_alive = True
        self._banner.connect("destroy", self._on_banner_destroy)
        self.pack_start(self._banner, False, False, 0)

    def attach_info_bar(self, bar: Gtk.Widget) -> None:
        """Show ``bar`` (macro recording / playback) right below the connection banner."""

        self.pack_start(bar, False, False, 0)
        self.reorder_child(bar, self.get_children().index(self._banner) + 1)

    def _build_terminal_area(self) -> None:
        self._terminal = self._create_terminal()
        self._pane_container = TerminalPaneContainer(self._terminal)
        self._pane_holder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._pane_holder.get_style_context().add_class("terminal-card")
        self._card_css = Gtk.CssProvider()
        self._pane_holder.get_style_context().add_provider(self._card_css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)
        self._pane_holder.set_hexpand(True)
        self._pane_holder.set_vexpand(True)
        self._pane_holder.pack_start(self._pane_container, True, True, 0)
        self.pack_start(self._pane_holder, True, True, 0)

    @property
    def statusbar(self) -> SessionStatusBar:
        return self._statusbar

    def release_statusbar(self) -> SessionStatusBar:
        """Detach the status bar so the window can dock it (info bar)."""

        if self._statusbar.get_parent() is self:
            self.remove(self._statusbar)
        return self._statusbar

    def reclaim_statusbar(self) -> None:
        parent = self._statusbar.get_parent()
        if parent is self:
            return
        if parent is not None:
            parent.remove(self._statusbar)
        self.pack_start(self._statusbar, False, False, 0)
        self._statusbar.show()

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
        set_spinner_busy(self._info_spinner, False)
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
        self._paint_card(background)
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

    def _paint_card(self, background: Gdk.RGBA) -> None:
        """The rounded card around the terminal takes the terminal's background."""

        provider = getattr(self, "_card_css", None)
        if provider is None:
            return
        # Split panes sit on the canvas gutter; only a single pane takes the terminal color.
        css = ".terminal-card:not(.split) { background-color: %s; }" % background.to_string()
        try:
            provider.load_from_data(css.encode("utf-8"))
        except GLib.Error:
            pass

    def _apply_terminal_theme(self) -> None:
        for term in self._terminals():
            self._configure_terminal_appearance(term)

    def _terminals(self) -> list[Vte.Terminal]:
        terms = [self._terminal]
        if self._secondary_terminal:
            terms.append(self._secondary_terminal)
        return terms

    @property
    def _focused_terminal(self) -> Optional[Vte.Terminal]:
        return self._panes.active

    def _active_terminal(self) -> Optional[Vte.Terminal]:
        return self._focused_terminal or self._terminal

    @property
    def active_terminal(self) -> Optional[Vte.Terminal]:
        """The active pane's terminal (keyboard input, toolbar actions, broadcast source)."""

        return self._active_terminal()

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
        self._connect_mouse(term)
        return term

    # ------------------------------------------------------------ mouse and clipboard
    def set_input_settings(self, settings: TerminalInputSettings) -> None:
        """Mouse / selection preferences (shared object: later changes apply at once)."""

        self._input_settings = settings

    def _connect_mouse(self, term: Vte.Terminal) -> None:
        term.connect("button-press-event", self._on_terminal_button_press)
        # After VTE's own handler: it runs only when VTE did not take the press, i.e. the program in the
        # terminal has no mouse reporting on (VTE consumes every press it reports).
        term.connect_after("button-press-event", self._on_terminal_button_press_after)
        term.connect("button-release-event", self._on_terminal_button_release)
        term.connect("selection-changed", self._on_terminal_selection_changed)
        term.connect("realize", self._remember_text_cursor)

    @staticmethod
    def _pointer_window(term: Vte.Terminal) -> Optional[Gdk.Window]:
        window = term.get_window()
        if window is None:
            return None
        children = [child for child in window.get_children() if child.is_input_only()]
        return children[0] if children else window

    def _remember_text_cursor(self, term: Vte.Terminal) -> None:
        window = self._pointer_window(term)
        self._text_cursors[term] = window.get_cursor() if window is not None else None

    def _mouse_reporting(self, term: Vte.Terminal) -> bool:
        """Whether the program reads the mouse: VTE then shows the arrow instead of its text pointer.

        VTE has no getter for this; the pointer it sets is the only public trace. Used only for a
        non-default middle-click setting (right-click relies on VTE consuming the press instead).
        """

        window = self._pointer_window(term)
        text_cursor = self._text_cursors.get(term)
        if window is None or text_cursor is None:
            return False
        current = window.get_cursor()
        return current is not None and current != text_cursor

    def _on_terminal_button_press(self, term: Vte.Terminal, event: Gdk.EventButton) -> bool:
        if event.type != Gdk.EventType.BUTTON_PRESS:
            return False
        state = event.state & Gtk.accelerator_get_default_mod_mask()
        shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        settings = self._input_settings
        if event.button == 3 and shift:  # the menu, even while a program reads the mouse
            return self._perform_mouse_action(term, MouseAction.SHOW_MENU, event)
        if event.button == 2:
            if not shift and settings.middle_click == "paste_primary":
                return False  # VTE pastes PRIMARY itself (or reports the click to the program)
            tracking = not shift and self._mouse_reporting(term)
            action = decide_mouse_action(2, shift, ctrl, tracking, settings)
            if action is not MouseAction.PASS_TO_TERMINAL:
                return self._perform_mouse_action(term, action, event)
        return False

    def _on_terminal_button_press_after(self, term: Vte.Terminal, event: Gdk.EventButton) -> bool:
        if event.type != Gdk.EventType.BUTTON_PRESS or event.button != 3:
            return False
        state = event.state & Gtk.accelerator_get_default_mod_mask()
        if state & Gdk.ModifierType.SHIFT_MASK:
            return False
        action = decide_mouse_action(3, False, bool(state & Gdk.ModifierType.CONTROL_MASK), False, self._input_settings)
        if action is MouseAction.PASS_TO_TERMINAL:
            return False
        return self._perform_mouse_action(term, action, event)

    def _on_terminal_button_release(self, term: Vte.Terminal, event: Gdk.EventButton) -> bool:
        key = (term, event.button)
        if key in self._handled_buttons:
            self._handled_buttons.discard(key)
            return True  # no stray selection change from a press VTE never saw
        return False

    def _perform_mouse_action(self, term: Vte.Terminal, action: MouseAction, event: Gdk.EventButton) -> bool:
        term.grab_focus()
        if action is MouseAction.SHOW_MENU:
            self.show_terminal_menu(term, event)
        elif action is MouseAction.PASTE_CLIPBOARD:
            term.paste_clipboard()
        elif action is MouseAction.PASTE_PRIMARY:
            term.paste_primary()
        self._handled_buttons.add((term, event.button))
        return True

    def _on_terminal_selection_changed(self, term: Vte.Terminal) -> None:
        # VTE itself puts a mouse selection into PRIMARY; the clipboard only when asked, and never empty.
        if self._input_settings.copy_on_select_clipboard and term.get_has_selection():
            _copy_selection(term)

    def select_all_terminal(self, term: Optional[Vte.Terminal] = None) -> None:
        term = term or self._active_terminal()
        if term is None:
            return
        term.select_all()
        if self._input_settings.copy_on_select and hasattr(term, "copy_primary"):
            try:
                term.copy_primary()
            except Exception:  # no PRIMARY selection (some Wayland compositors): nothing to do
                pass

    def paste_primary(self, term: Optional[Vte.Terminal] = None) -> None:
        term = term or self._active_terminal()
        if term is not None:
            term.paste_primary()

    def clear_scrollback(self, term: Optional[Vte.Terminal] = None) -> None:
        term = term or self._active_terminal()
        if term is not None:
            term.feed(b"\x1b[3J")  # "erase saved lines": the scrollback only, the screen stays

    def reset_terminal(self, term: Optional[Vte.Terminal] = None) -> None:
        term = term or self._active_terminal()
        if term is not None:
            term.reset(True, False)

    def show_terminal_menu(self, term: Optional[Vte.Terminal] = None, event: Optional[Gdk.Event] = None) -> None:
        term = term or self._active_terminal()
        if term is None:
            return
        menu = build_context_menu(self.terminal_menu_spec(term, event), attach_to=term)
        popup_menu(menu, event, widget=term)

    def terminal_menu_spec(self, term: Vte.Terminal, event: Optional[Gdk.Event] = None) -> list[MenuSection]:
        """The terminal's context menu; every item acts on ``term`` (the pane under the pointer)."""

        del event  # no hyperlink / match handling is enabled in this app's terminals
        has_selection = bool(term.get_has_selection())
        try:
            can_paste = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).wait_is_text_available()
        except Exception:
            can_paste = True
        edit = MenuSection("edit", [
            MenuItemSpec(_("_Copy"), lambda: self.copy_clipboard(term), accel=accel_label("copy"),
                         sensitive=has_selection),
            MenuItemSpec(_("Copy as Plain _Text"), lambda: _copy_selection(term), sensitive=has_selection),
            MenuItemSpec(_("_Paste"), lambda: term.paste_clipboard(), accel=accel_label("paste"), sensitive=can_paste),
            MenuItemSpec(_("Paste _Selection"), lambda: term.paste_primary(), accel=accel_label("paste-primary")),
            MenuItemSpec(_("Select _All"), lambda: self.select_all_terminal(term),
                         accel=accel_label("select-all-terminal")),
        ])
        terminal = MenuSection("terminal", [
            MenuItemSpec(_("C_lear Scrollback"), lambda: self.clear_scrollback(term), accel=accel_label("clear-scrollback")),
            MenuItemSpec(_("_Reset Terminal"), lambda: self.reset_terminal(term), accel=accel_label("reset-terminal")),
        ])
        zoom = MenuSection("zoom", [
            MenuItemSpec(_("Zoom _In"), self.increase_font, accel=accel_label("font-increase")),
            MenuItemSpec(_("Zoom _Out"), self.decrease_font, accel=accel_label("font-decrease")),
            MenuItemSpec(_("_Normal Size"), self.reset_font, accel=accel_label("font-reset")),
        ])
        session = MenuSection("session", [
            MenuItemSpec(_("Close _Split") if self.is_split else _("Split _Pane"), self.toggle_split,
                         accel=accel_label("split-pane")),
            MenuItemSpec(_("_Reconnect"), self.reconnect, accel=accel_label("reconnect"),
                         visible=not self._session_active),
            MenuItemSpec(_("Save _Transcript…"), self.save_transcript, accel=accel_label("save-transcript")),
        ])
        preferences = MenuSection("preferences", [
            MenuItemSpec(_("_Preferences…"), lambda: self.on_open_preferences() if self.on_open_preferences else None,
                         sensitive=self.on_open_preferences is not None),
        ])
        return [edit, terminal, zoom, session, preferences]

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
    def terminal_size(self) -> Tuple[int, int]:
        """``(columns, rows)`` of the main terminal (0, 0 before it is allocated)."""

        return int(self._terminal.get_column_count() or 0), int(self._terminal.get_row_count() or 0)

    @property
    def custom_title(self) -> str:
        """The name the user gave this tab (Rename), or ``""``."""

        return getattr(self, "_custom_title", "")

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
        for frame in self._pane_frames.values():
            frame.set_broadcast(active)

    # ------------------------------------------------------------ panes
    @property
    def pane_frames(self) -> list:
        """The PaneFrames of a split tab, in order (empty with one pane)."""

        return [self._pane_frames[t] for t in self._terminals() if t in self._pane_frames]

    def set_pane_label(self, label: str) -> None:
        """The tab label (Phase 6 policy); split panes add ``· pane 1`` / ``· pane 2``."""

        self._pane_label = label
        self._sync_pane_frames()

    def _pane_address(self) -> str:
        try:
            hostname, port, user = host_connection_params(self._host)
        except Exception:
            return ""
        return f"{user}@{hostname}:{port}"

    def _sync_pane_frames(self) -> None:
        terms = [t for t in self._terminals() if t in self._pane_frames]
        if not terms:
            return
        labels = pane_labels(self._pane_label, len(terms))
        address = self._pane_address()
        focused = self._focused_terminal if self._focused_terminal in terms else terms[0]
        for term, label in zip(terms, labels):
            frame = self._pane_frames[term]
            frame.set_title(label, address)
            frame.set_status(self._status)
            frame.set_focused(term is focused)
            footer = self._pane_footers.get(term)
            if footer is not None:
                footer.set_status(self._status, int(term.get_column_count() or 0), int(term.get_row_count() or 0))

    def activate_pane(self, term: Optional[Vte.Terminal] = None, *, grab: bool = True) -> bool:
        """The one activation path for this tab's panes.

        A press anywhere in a pane, Alt+arrows, a new split, a closed pane and the
        terminal's own focus-in all come here: the pane becomes the active one
        (frames restyled once), the notebook hears about it (``on_activated``:
        current tab, toolbar, host bar, status bar) and, with ``grab``, keyboard
        focus moves to its terminal. Activating the active pane changes nothing.
        Returns whether the active pane changed.
        """

        term = term or self._active_terminal()
        if term not in self._terminals():
            return False
        changed = self._panes.activate(term)
        if changed:
            self._sync_pane_frames()
        if self.on_activated is not None:
            self.on_activated(self)
        if grab and not term.is_focus():  # is_focus: also right while the window is inactive
            self.focus_terminal()
        return changed

    def focus_pane(self, term: Vte.Terminal) -> None:
        self.activate_pane(term)

    def focus_neighbor(self, direction: str) -> bool:
        """Alt+←/→/↑/↓: the other pane of a split, else the notebook's next tile."""

        terms = self._terminals()
        if len(terms) > 1 and self._paned is not None:
            horizontal = self._paned.get_orientation() == Gtk.Orientation.HORIZONTAL
            if (direction in ("left", "right")) == horizontal:
                index = terms.index(self._active_terminal()) if self._active_terminal() in terms else 0
                target = index + (1 if direction in ("right", "down") else -1)
                if 0 <= target < len(terms):
                    self.focus_pane(terms[target])
                    return True
        return bool(self.on_focus_neighbor and self.on_focus_neighbor(direction))

    def toggle_pane_zoom(self, term: Optional[Vte.Terminal] = None) -> None:
        """Show one pane alone (the layout stays; restore brings the other back)."""

        if self._paned is None:
            return
        term = term or self._active_terminal()
        zoom = self._zoomed_pane is None
        self._zoomed_pane = term if zoom else None
        for other, frame in self._pane_frames.items():
            frame.set_visible(not zoom or other is term)
            frame.set_zoomed(zoom)
        if term is not None:
            self.focus_pane(term)

    @property
    def zoomed_pane(self) -> Optional[Vte.Terminal]:
        return self._zoomed_pane

    def _frame_for(self, term: Vte.Terminal, container: Gtk.Widget, *, closable: bool) -> PaneFrame:
        frame = PaneFrame(
            self._pane_label,
            on_focus=lambda t=term: self.focus_pane(t),
            on_zoom=lambda t=term: self.toggle_pane_zoom(t),
            on_close=self._close_duplicate_pane if closable else None,
        )
        frame.set_body(container)
        footer = PaneFooter()
        frame.set_footer(footer)
        self._pane_frames[term] = frame
        self._pane_footers[term] = footer
        term.connect("size-allocate", lambda *_: self._sync_pane_frames())
        return frame

    def _on_paned_press(self, paned: Gtk.Paned, event: Gdk.EventButton) -> bool:
        """Double-click on the divider: back to 50/50."""

        if event.type == Gdk.EventType._2BUTTON_PRESS and event.window == paned.get_handle_window():
            alloc = paned.get_allocation()
            size = alloc.width if paned.get_orientation() == Gtk.Orientation.HORIZONTAL else alloc.height
            paned.set_position(size // 2)
            return True
        return False

    def copy_clipboard(self, term: Optional[Vte.Terminal] = None) -> None:
        term = term or self._active_terminal()
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

    def paste_clipboard(self, term: Optional[Vte.Terminal] = None) -> None:
        term = term or self._active_terminal()
        if term:
            term.paste_clipboard()

    def set_input_observer(self, observer: Optional[Callable[["TerminalTab", str], None]]) -> None:
        self._input_observer = observer

    def feed_input(self, data: str) -> None:
        term = self._active_terminal()
        if term and data:
            term.feed_child(data.encode("utf-8"))

    @property
    def session_ended(self) -> bool:
        """Not connected and not connecting (the banner offers Reconnect / Close)."""

        return not self._session_active and self._status is not ConnectionStatus.CONNECTING

    def is_session_active(self) -> bool:
        """The ssh process (or the local shell) runs."""

        return bool(self._session_active)

    def macro_send(self, term: Vte.Terminal, data: bytes = b"", paste: Optional[str] = None) -> None:
        """Macro playback: straight to ``term`` (no broadcast; VTE's ``commit`` is ignored meanwhile)."""

        self._macro_feeding = True
        try:
            if paste is not None:
                term.paste_text(paste)  # honours the program's bracketed-paste mode
            elif data:
                term.feed_child(data)
        finally:
            self._macro_feeding = False

    def send_interrupt(self) -> None:
        term = self._active_terminal()
        if term:
            term.inject_terminal_control(3)

    def focus_terminal(self) -> None:
        term = self._active_terminal()
        if term is None:
            return

        attempts = [0]

        def _attempt_focus() -> bool:
            if term is None:
                return False
            if not term.get_realized() or not term.get_mapped():
                # Not shown yet (page switch, new tab): retry for a while, then give up
                # (a terminal that is never shown, e.g. a closed tab, must not spin the loop).
                attempts[0] += 1
                return attempts[0] < FOCUS_RETRY_LIMIT
            if term is self._active_terminal():  # not stale: another pane may be active by now
                term.grab_focus()
            return False

        if _attempt_focus():
            GLib.timeout_add(FOCUS_RETRY_MS, _attempt_focus)

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
        self._begin_connect()

    def get_font_scale(self) -> float:
        return self._font_scale

    # ------------------------------------------------------------------ internal
    def _on_reconnect_clicked(self, _button: Gtk.Button) -> None:
        self.reconnect()

    def _on_banner_response(self, _bar: Gtk.InfoBar, response: int) -> None:
        if response == _RESPONSE_RECONNECT:
            self._request_reconnect()
        elif response == _RESPONSE_CLOSE:
            self._request_close_ended()

    # --------------------------------------------------------- host key banner
    def _build_host_key_bar(self) -> None:
        """ssh's host-key messages, made visible: a changed key (warning) or an unknown one (information).

        It acts on this tab only (never mirrored to broadcast sessions); ssh's own question stays in
        the terminal: nothing is answered for the user.
        """

        self._host_key_change: Optional[ChangedKey] = None
        self._host_key_prompt: Optional[UnknownHostKeyPrompt] = None
        self._host_key_check_id = 0
        bar = Gtk.InfoBar()
        bar.set_show_close_button(False)
        set_accessible_name(bar, _("SSH host key"))
        self._host_key_icon = Gtk.Image()
        self._host_key_label = Gtk.Label()
        self._host_key_label.set_xalign(0.0)
        self._host_key_label.set_line_wrap(True)
        self._host_key_label.set_selectable(True)
        area = bar.get_content_area()
        area.pack_start(self._host_key_icon, False, False, 0)
        area.pack_start(self._host_key_label, True, True, 0)
        self._host_key_icon.show()
        self._host_key_label.show()
        self._host_key_buttons = {
            "details": bar.add_button(_("_Details…"), _RESPONSE_HOST_KEY_DETAILS),
            "remove": bar.add_button(_("_Remove Old Key and Reconnect…"), _RESPONSE_HOST_KEY_REMOVE),
            "copy": bar.add_button(_("_Copy Fingerprint"), _RESPONSE_HOST_KEY_COPY),
            "review": bar.add_button(_("_Review Host Key…"), _RESPONSE_HOST_KEY_REVIEW),
            "dismiss": bar.add_button(_("D_ismiss"), _RESPONSE_HOST_KEY_DISMISS),
        }
        bar.connect("response", self._on_host_key_response)
        bar.set_no_show_all(True)
        self._host_key_bar = bar
        self.pack_start(bar, False, False, 0)

    @property
    def host_key_banner(self) -> str:
        """``changed``, ``unknown`` or ``""`` (what the bar shows)."""

        if not self._host_key_bar.get_visible():
            return ""
        return "changed" if self._host_key_change is not None else "unknown"

    def _show_host_key_bar(self, kind: str, text: str) -> None:
        bar = self._host_key_bar
        changed = kind == "changed"
        bar.set_message_type(Gtk.MessageType.WARNING if changed else Gtk.MessageType.INFO)
        self._host_key_icon.set_from_icon_name("dialog-warning-symbolic" if changed else "dialog-information-symbolic",
                                               Gtk.IconSize.BUTTON)  # never colour only
        self._host_key_label.set_markup(text)
        for name, button in self._host_key_buttons.items():
            button.set_visible(name in {"changed": ("details", "remove", "dismiss"),
                                        "untrusted": ("review", "dismiss")}.get(kind, ("copy", "dismiss")))
        set_accessible_name(bar, _("SSH host key"))  # after set_message_type, which names it "Warning"
        bar.get_accessible().set_description(self._host_key_label.get_text())
        bar.show()

    def _hide_host_key_bar(self) -> None:
        self._host_key_change = None
        self._host_key_prompt = None
        if self._host_key_check_id:
            GLib.source_remove(self._host_key_check_id)
            self._host_key_check_id = 0
        self._host_key_bar.hide()

    def _host_key_names(self, ssh_host: str = "") -> list:
        hostname, port, _user = host_connection_params(self._host)
        extra = [split_host(ssh_host)] if ssh_host else []
        return changed_key_names(hostname, port, extra)

    def _check_host_key_changed(self, term: Vte.Terminal) -> bool:
        """At exit, before authentication: ssh's "REMOTE HOST IDENTIFICATION HAS CHANGED" block."""

        detector = HostKeyOutputDetector()
        detector.feed(self._terminal_tail(term, HostKeyOutputDetector.LIMIT))
        found = detector.changed(exited=True)
        if found is None:
            return False
        self._host_key_prompt = None
        self._host_key_change = ChangedKey(
            host_label=self._host.alias, names=self._host_key_names(found.host), new_fingerprint=found.new_fingerprint,
            key_type=found.key_type, known_hosts_path=found.known_hosts_path, line_no=found.line_no)
        self._show_host_key_bar("changed", "<b>{}</b>".format(GLib.markup_escape_text(risk_text())))
        self._log(_("{alias}: the server's host key has changed; the connection was refused.").format(
            alias=self._host.alias), Level.ERROR)
        return True

    def _on_host_key_output(self, *_args) -> None:
        if self._authenticated or self._child_pid is None or self._host_key_check_id:
            return
        self._host_key_check_id = GLib.timeout_add(150, self._check_host_key_prompt)

    def _check_host_key_prompt(self) -> bool:
        """While ssh waits at "Are you sure you want to continue connecting": show the fingerprint."""

        self._host_key_check_id = 0
        if self._authenticated or self._child_pid is None:
            return GLib.SOURCE_REMOVE
        detector = HostKeyOutputDetector()
        detector.feed(self._terminal_tail(self._terminal, HostKeyOutputDetector.LIMIT))
        prompt = detector.unknown()
        if prompt is None:
            if self._host_key_prompt is not None:  # answered: the question is gone
                self._hide_host_key_bar()
            return GLib.SOURCE_REMOVE
        if prompt != self._host_key_prompt:
            self._host_key_prompt = prompt
            text = _("{host} is not known yet. Compare its {type} fingerprint with the one you expect, then answer "
                     "ssh in the terminal:").format(host=GLib.markup_escape_text(self._host.alias),
                                                    type=GLib.markup_escape_text(prompt.key_type))
            self._show_host_key_bar("unknown", f"{text}\n<tt>{GLib.markup_escape_text(prompt.fingerprint)}</tt>")
        return GLib.SOURCE_REMOVE

    def _on_host_key_response(self, _bar: Gtk.InfoBar, response: int) -> None:
        change = self._host_key_change
        window = self.get_toplevel() if isinstance(self.get_toplevel(), Gtk.Window) else None
        if response == _RESPONSE_HOST_KEY_DISMISS:
            self._hide_host_key_bar()
        elif response == _RESPONSE_HOST_KEY_REVIEW:
            self._hide_host_key_bar()
            self.reconnect()  # the pre-flight reads the key again and asks
        elif response == _RESPONSE_HOST_KEY_COPY and self._host_key_prompt is not None:
            Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(self._host_key_prompt.fingerprint, -1)
        elif response == _RESPONSE_HOST_KEY_DETAILS and change is not None:
            dialogs = getattr(window, "app_dialogs", None)
            show_details(window, change, open_manager=(lambda: dialogs.open_known_hosts(self._host))
                         if dialogs is not None else None)
        elif response == _RESPONSE_HOST_KEY_REMOVE and change is not None:
            if self.remove_host_key(window, change):
                self._hide_host_key_bar()
                self.reconnect()  # ssh asks again in the terminal; that answer is the user's

    # ------------------------------------------------- key passphrase prompt
    passphrase_provider: Optional[Callable[[str], Optional[str]]] = None  # key path -> the saved passphrase
    _passphrase_check_id = 0

    def _on_passphrase_output(self, *_args) -> None:
        if (self.passphrase_provider is None or self._authenticated or self._child_pid is None
                or self._passphrase_check_id):
            return
        self._passphrase_check_id = GLib.timeout_add(120, self._check_passphrase_prompt)

    def _check_passphrase_prompt(self) -> bool:
        """ssh waits at "Enter passphrase for key '…':" for the host's own key: type the saved one, once."""

        self._passphrase_check_id = 0
        fill = getattr(self, "_passphrase_fill", None)
        if fill is None or self._authenticated or self._child_pid is None or self.passphrase_provider is None:
            return GLib.SOURCE_REMOVE
        path = passphrase_prompt(self._terminal_tail(self._terminal))
        if path is None or not fill.should_fill(path):
            return GLib.SOURCE_REMOVE
        secret = self.passphrase_provider(path)
        if secret:
            fill.filled(path)  # never twice: a wrong saved passphrase leaves ssh's next prompt to the user
            self._terminal.feed_child((secret + "\n").encode("utf-8"))
            LOGGER.info("%s: the saved key passphrase was entered", self._host.alias)
        return GLib.SOURCE_REMOVE

    def remove_host_key(self, window: Optional[Gtk.Window], change: ChangedKey) -> bool:
        return remove_old_key(window, change, action=_("_Remove and Reconnect"))

    def _on_banner_destroy(self, _banner) -> None:
        self._banner_alive = False

    def _reveal_banner(self, reveal: bool) -> None:
        # A destroyed GtkInfoBar has a NULL revealer: set_revealed() would log
        # "gtk_revealer_get_reveal_child: assertion 'GTK_IS_REVEALER' failed"
        # (the ssh child exits after its tab was closed).
        if not self._banner_alive:
            return
        if reveal:
            self._banner.show()
        self._banner.set_revealed(reveal)

    def _on_terminal_size_allocate(self, term: Vte.Terminal, _allocation) -> None:
        self._statusbar.set_size(int(term.get_column_count() or 0), int(term.get_row_count() or 0))

    def _set_status(self, message: str, connected: bool) -> None:
        state = _status_for_message(message, connected)
        self._status = state
        self._status_label.set_text(message)
        reveal = not connected and state is not ConnectionStatus.CONNECTING
        self._banner.set_message_type(Gtk.MessageType.ERROR if state is ConnectionStatus.FAILED else Gtk.MessageType.WARNING)
        self._reveal_banner(reveal)
        self._reconnect_button.set_sensitive(not connected)
        self._statusbar.set_status(state, message)
        self._session_active = connected
        if connected:
            # "Connected" here means the process runs; authentication is verified separately.
            self._start_password_prompt_poll()
        self._sync_pane_frames()
        self.emit("status-changed", self._host.alias, connected)
        if connected:
            self._start_stats_updates()
        else:
            self._stop_stats_updates()
            self._stop_password_prompt_poll()

    def _ssh_arguments(self) -> list[str]:
        return build_ssh_command(self._host)

    def _credential_components(self) -> Optional[Tuple[str, int, str]]:
        hostname, port, username = host_connection_params(self._host)
        if not hostname:
            return None
        return hostname, port, username

    @property
    def _attempts(self) -> int:
        """Password prompts shown in this user-initiated connect (max ``MAX_ATTEMPTS``)."""

        return self._flow.attempts

    @_attempts.setter
    def _attempts(self, value: int) -> None:
        self._flow.attempts = value

    def _parent_window(self) -> Optional[Gtk.Window]:
        parent = self.get_toplevel()
        return parent if isinstance(parent, Gtk.Window) else None

    def _ask_password(self, rejected: str = "") -> None:
        """Open the (non-blocking) password dialog; its answer starts the next attempt.

        ``rejected`` names the password the server refused ("" = first prompt).
        """

        hostname, port, username = host_connection_params(self._host)
        request = self._flow.next_request(f"{username}@{hostname}:{port}", rejected)
        self._close_password_dialog()
        self._password_dialog = PasswordRequestDialog(
            self._parent_window(), request, functools.partial(self._on_password_answer, retry=bool(rejected)))

    def _on_password_answer(self, password: Optional[str], *, retry: bool) -> None:
        self._password_dialog = None
        alias = self._host.alias
        if not password:  # Cancel, Escape or nothing typed: stop (the banner keeps Reconnect)
            self._log(_("Password entry cancelled for {alias}.").format(alias=alias), Level.INFO)
            self._set_status("Authentication failed" if retry else "Connection cancelled", False)
            return
        if retry:
            self._log(_("Retrying {alias} with the entered password…").format(alias=alias), Level.INFO)
        self._spawn(password, password_origin="retry" if retry else "prompt")  # not saved yet

    def _rejected_phrase(self) -> str:
        """How the retry dialog names the password that was just refused."""

        source = self._password_source
        if source == self._resolution.source and self._password_service is not None:
            try:
                return self._password_service.describe_password_source(self._resolution)
            except Exception as exc:  # naming is cosmetic; never block the retry
                LOGGER.warning("password source description failed: %s", type(exc).__name__)
        return source_phrase(source) or source_phrase("stored")

    def _close_password_dialog(self) -> None:
        dialog, self._password_dialog = self._password_dialog, None
        if dialog is not None:
            dialog.dismiss()

    def _on_tab_destroyed(self) -> None:
        """Tab closed: dismiss open password dialogs (doing nothing) and remove every GLib source."""

        self._destroyed = True  # a pre-flight still running starts nothing
        self._close_password_dialog()
        dialog, self._save_dialog = self._save_dialog, None
        if dialog is not None:
            dialog.dismiss()
        self._stop_auth_sources()

    def _schedule_retry_prompt(self) -> None:
        """Ask again after ≥ 1 s (no lock-out bursts), unless the attempts are used up."""

        if self._retry_timeout_id:
            GLib.source_remove(self._retry_timeout_id)
            self._retry_timeout_id = 0
        if self._attempts >= MAX_ATTEMPTS:
            self._log(_("Authentication failed for {alias} after {n} attempts. Use Reconnect to try again.").format(
                alias=self._host.alias, n=MAX_ATTEMPTS), Level.ERROR)
            return
        self._retry_timeout_id = GLib.timeout_add_seconds(RETRY_DELAY_S, self._prompt_retry_after_failure)

    def _prompt_retry_after_failure(self) -> bool:
        self._retry_timeout_id = 0
        if shutil.which("sshpass") is None:
            self._log(_("Automatic password retry unavailable because sshpass is not installed."), Level.WARNING)
            return GLib.SOURCE_REMOVE
        self._ask_password(self._rejected_phrase())
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
            contents = _terminal_contents(term)  # VTE < 0.72: no warnings, unlike get_text_range
            if contents is not None:
                return contents.rstrip("\n")[-limit:]
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
        if getattr(self, "_macro_feeding", False):
            return  # a macro, not the user: never fed to the password-change heuristics
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
        if not self._authenticated and auth_failure_output_detected(tail):  # later text is the remote shell's
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

    _PASSWORD_SOURCES = {"credential", "host", "legacy", "session", "prompt", "retry", "stored"}

    def _handle_auth_failure_retry(self) -> bool:
        """The password was rejected: stop the child and ask again. Stored secrets are never touched."""

        if self._auth_retry_pending:
            return True
        if self._password_source not in self._PASSWORD_SOURCES:
            return False
        self._auth_retry_pending = True
        if self._password_source == "session":
            self._session_password = None  # it no longer works
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

    def _begin_connect(self) -> None:
        """A user-initiated connect / reconnect: fresh attempt budget."""

        self._flow.begin_connect()
        self._close_password_dialog()  # a reconnect replaces an open retry prompt
        if self._retry_timeout_id:
            GLib.source_remove(self._retry_timeout_id)
            self._retry_timeout_id = 0
        self._spawn()

    def _spawn(self, override_password: Optional[str] = None, *, password_origin: str = "prompt") -> None:
        """Start (or restart) the SSH child process for this session."""

        self._reset_attempt_state()
        proceed, password = self._password_for_attempt(override_password, password_origin)
        if not proceed:
            return
        self._attempt_password = password
        self._flow.attempt(self._password_source, password)
        argv, envv = self._spawn_command(password)
        if password:
            self._preflight_then_start(argv, envv)
        else:
            self._start_child(argv, envv)  # plain ssh asks about an unknown key itself (the host-key bar)

    # ------------------------------------------------- host-key pre-flight (password sessions)
    _preflight_generation = 0
    _destroyed = False

    def confirm_host_key(self, alias: str, key_type: str, fingerprint: str) -> bool:
        """The first-connection trust dialog; ``True`` = trust the key and connect."""

        from ophion.gtkui.dialogs.trust import trust_host_key

        return trust_host_key(self._parent_window(), alias, key_type, fingerprint)

    def _preflight_then_start(self, argv: List[str], envv: List[str]) -> None:
        """sshpass cannot answer ssh's "Are you sure…" (it exits 6): check the key first, off the main thread."""

        self._preflight_generation += 1
        generation = self._preflight_generation
        self._terminal.reset(True, True)
        self._set_status("Connecting…", False)
        ssh_argv = self._ssh_arguments()

        def work() -> None:
            try:
                result = host_key_preflight.check(ssh_argv)
            except Exception as exc:  # never block a connect on the check itself: ssh still verifies the key
                LOGGER.warning("host-key pre-flight failed: %s", type(exc).__name__)
                result = host_key_preflight.Preflight(host_key_preflight.SKIP)
            GLib.idle_add(self._on_preflight, generation, result, argv, envv)

        threading.Thread(target=work, name="host-key-preflight", daemon=True).start()

    def _on_preflight(self, generation: int, result, argv: List[str], envv: List[str]) -> bool:
        if generation != self._preflight_generation or self._destroyed:
            return GLib.SOURCE_REMOVE  # reconnected or closed meanwhile
        alias = self._host.alias
        if result.state == host_key_preflight.UNREADABLE:
            self._log(_("Could not read the server's key for {alias} ({error}); nothing was sent.").format(
                alias=alias, error=result.error), Level.ERROR)
            self._set_status("Connection failed", False)
            self.emit("session-closed", alias, False)
            return GLib.SOURCE_REMOVE
        if result.state == host_key_preflight.UNKNOWN:
            if not self.confirm_host_key(alias, result.key_type, result.fingerprint):
                self._log(_("Connection to {alias} cancelled; its host key was not trusted.").format(alias=alias),
                          Level.INFO)
                self._set_status("Connection cancelled", False)
                self.emit("session-closed", alias, False)
                return GLib.SOURCE_REMOVE
            if generation != self._preflight_generation or self._destroyed:
                return GLib.SOURCE_REMOVE
            try:
                path = host_key_preflight.trust(result)
            except Exception as exc:  # the key file could not be written: do not connect as if it were
                self._log(_("Could not save the host key for {alias}: {error}").format(alias=alias, error=exc),
                          Level.ERROR)
                self._set_status("Connection failed", False)
                self.emit("session-closed", alias, False)
                return GLib.SOURCE_REMOVE
            self._log(_("Trusted the {type} host key of {alias} ({file}).").format(
                type=result.key_type, alias=alias, file=path), Level.SUCCESS)
        self._start_child(argv, envv)
        return GLib.SOURCE_REMOVE

    def _resolve_password(self) -> PasswordResolution:
        if self._password_provider is not None:
            try:
                return self._password_provider()
            except Exception as exc:  # storage problems never block a connect
                LOGGER.warning("password resolution failed: %s", type(exc).__name__)
                return NO_PASSWORD
        credentials = self._credential_components()
        if not self._secure_store or not credentials:
            return NO_PASSWORD
        try:
            stored = self._secure_store.get(*credentials)
        except SecureStoreError as exc:
            LOGGER.warning("stored password unavailable: %s", type(exc).__name__)
            return NO_PASSWORD
        return PasswordResolution(stored, "legacy", None) if stored else NO_PASSWORD

    def _reset_attempt_state(self) -> None:
        self._stop_auth_sources(keep_retry=True)
        self._passphrase_fill = PassphraseFill(host_option(self._host, "identityfile") or "")
        if hasattr(self, "_host_key_bar"):
            self._hide_host_key_bar()
        self._ever_connected = False
        self._authenticated = False
        self._auth_method = "none"
        self._password_source = "none"
        self._attempt_password = None
        self._password_change_tracker.reset()
        self._pending_detected_password_change = None
        self._password_change_terminal = None
        self._last_key_input = None
        self._auth_retry_pending = False
        self._child_pid = None
        self._stop_password_prompt_poll()

    def _password_for_attempt(self, override_password: Optional[str], password_origin: str) -> Tuple[bool, Optional[str]]:
        """Decide which password (if any) sshpass should send. Returns ``(proceed, password)``.

        Order: an explicit override (the retry prompt) → a typed password that already
        worked in this tab → the resolution (credential → legacy). Nothing is saved.
        """

        helper_available = shutil.which("sshpass") is not None
        alias = self._host.alias
        if override_password is not None:
            if not helper_available:
                self._log(_("Cannot perform automatic password entry because sshpass is not installed."), Level.WARNING)
                self._password_source = "manual"
                return False, None
            self._password_source = password_origin
            return True, override_password
        if self._session_password and helper_available:
            self._password_source = "session"
            return True, self._session_password
        resolution = self._resolve_password()
        self._resolution = resolution
        self._flow.stored = resolution.password  # a typed password equal to it is never offered again
        stored_password = resolution.password
        if stored_password:
            if helper_available:
                self._password_source = resolution.source
                return True, stored_password
            self._log(_("Stored password available for {alias} but sshpass is missing. Falling back to manual entry.").format(alias=alias), Level.WARNING)
            self._password_source = "manual"
            return True, None
        if not helper_available:
            self._log(_("Automatic password entry unavailable for {alias} (sshpass not installed).").format(alias=alias), Level.WARNING)
            self._password_source = "manual"
            return True, None
        if not self._flow.can_prompt():
            return False, None
        self._ask_password()  # non-blocking: its answer spawns with password_origin="prompt"
        return False, None

    def _spawn_command(self, password: Optional[str]) -> Tuple[List[str], List[str]]:
        argv = self._ssh_arguments()
        envv = [f"{key}={value}" for key, value in terminal_env(os.environ).items()]
        if self._profile and self._profile.env_vars:
            envv.extend(f"{k}={v}" for k, v in self._profile.env_vars.items())
        self._marker = None
        try:
            marker = new_marker_path()
            extra, method = sentinel_options(self._host.options, marker)
        except OSError as exc:
            LOGGER.warning("no private marker directory (%s); using the fallback heuristic", type(exc).__name__)
            extra, method = [], "heuristic"
        if extra:
            self._marker = marker
            argv = with_sentinel(argv, extra)
        self._auth_method = method
        if password:
            envv.append(f"SSHPASS={password}")  # the password never goes into argv
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
            self._watch_authentication()
            self.focus_terminal()

        self._reveal_banner(False)
        self._reconnect_button.set_sensitive(False)
        term.spawn_async(
            Vte.PtyFlags.DEFAULT, None, argv, envv, _SSH_SPAWN_FLAGS,  # argv names "ssh" / "sshpass"
            None, None, -1, None, _on_spawn_finish, None,
        )

    def _spawn_secondary(self, term: Vte.Terminal) -> None:
        argv = self._ssh_arguments()
        envv = [f"{key}={value}" for key, value in terminal_env(os.environ).items()]
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
            _SSH_SPAWN_FLAGS,
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
        exit_code = os.WEXITSTATUS(status) if exited_normally else -1
        self._child_pid = None
        self._check_marker_now()  # a short session may end before the next poll
        self._stop_auth_sources(keep_retry=True)
        if self._auth_retry_pending:  # handled while the child was still running
            self._set_status("Authentication failed", False)
            GLib.idle_add(self.emit, "session-closed", self._host.alias, False)
            return
        if not self._authenticated and hasattr(self, "_host_key_bar") and self._check_host_key_changed(_term):
            self._set_status("Host key changed", False)
            GLib.idle_add(self.emit, "session-closed", self._host.alias, exited_normally)
            return
        if not self._authenticated:
            outcome = classify_exit(exit_code, self._auth_failure_output(_term))
            if outcome is AuthOutcome.AUTH_FAILED and self._handle_auth_failure_retry():
                return
            if outcome is AuthOutcome.HOSTKEY_UNKNOWN and hasattr(self, "_host_key_bar"):
                self._show_host_key_bar("untrusted", GLib.markup_escape_text(self._failure_message(outcome)))
            message = self._failure_message(outcome)
            if message:
                self._log(message, Level.ERROR)
        self._set_status("Disconnected", False)
        GLib.idle_add(self.emit, "session-closed", self._host.alias, exited_normally)

    def _failure_message(self, outcome: AuthOutcome) -> str:
        alias = self._host.alias
        return {
            AuthOutcome.AUTH_FAILED: _("Authentication failed for {alias}.").format(alias=alias),
            AuthOutcome.NO_PASSWORD_METHOD: _(NO_PASSWORD_METHOD_MESSAGE) + f" ({alias}).",
            AuthOutcome.HOSTKEY: _("Host key verification failed for {alias}; check the server's key.").format(alias=alias),
            AuthOutcome.HOSTKEY_UNKNOWN: _("Host key for {alias} is not trusted yet. Click to review it.").format(
                alias=alias),
            AuthOutcome.NETWORK: _("Could not reach {alias}.").format(alias=alias),
            AuthOutcome.OTP_REQUIRED: _("{alias} asks for a one-time code; type it in the terminal.").format(alias=alias),
        }.get(outcome, "")

    # ------------------------------------------------- verified authentication
    def _on_auth_output(self, *_args) -> None:
        self._output_events += 1

    def _watch_authentication(self) -> None:
        """Poll for the LocalCommand marker (≤ 250 ms), or apply the fallback heuristic."""

        self._spawned_at = time.monotonic()
        self._output_events = 0
        if self._auth_watch_source_id:
            GLib.source_remove(self._auth_watch_source_id)
        if self._marker is not None:
            self._auth_watch_source_id = GLib.timeout_add(250, self._poll_marker)
        else:
            self._auth_watch_source_id = GLib.timeout_add_seconds(HEURISTIC_DELAY_S, self._poll_heuristic)

    def _check_marker_now(self) -> bool:
        marker = self._marker
        if marker is None or self._authenticated or not marker.exists():
            return False
        try:
            marker.unlink()
        except OSError:
            pass
        self._marker = None
        self._on_authenticated("sentinel")
        return True

    def _poll_marker(self) -> bool:
        if self._check_marker_now() or self._child_pid is None:
            self._auth_watch_source_id = 0
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def _poll_heuristic(self) -> bool:
        if self._child_pid is None or self._authenticated:
            self._auth_watch_source_id = 0
            return GLib.SOURCE_REMOVE
        tail = self._terminal_tail(self._terminal)
        if probably_authenticated(tail, alive_s=time.monotonic() - self._spawned_at,
                                  output_after_password=self._output_events > 0):
            self._auth_watch_source_id = 0
            self._on_authenticated("heuristic")
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE  # check again (every HEURISTIC_DELAY_S) while the child runs

    def _on_authenticated(self, method: str) -> None:
        self._authenticated = True
        if self._host_key_prompt is not None:
            self._hide_host_key_bar()
        self._ever_connected = True
        self._auth_method = method
        self._attempts = 0
        LOGGER.info("%s authenticated (%s)", self._host.alias, "verified" if method == "sentinel" else "probable")
        if self._password_source in SOURCE_PROMPTED and self._attempt_password:
            self._session_password = self._attempt_password  # this tab; written only if the user says Save
        self._offer_save(method)

    def offer_save_password(self, password: str, *, verified: bool = True) -> None:
        """A backend that authenticated by itself (Paramiko) with a typed ``password``: maybe ask to save it."""

        self._password_source = "retry"
        self._attempt_password = password
        self._session_password = password
        self._flow.attempt("retry", password)
        self._offer_save("sentinel" if verified else "heuristic")

    def _offer_save(self, method: str) -> None:
        """After a verified login with a typed password: "Save this password for ‘alias’?" (once per tab)."""

        service = self._password_service
        if service is None:
            return
        try:
            context = service.password_save_context(self._host)
        except Exception as exc:  # storage problems never break the session
            LOGGER.warning("password save context unavailable: %s", type(exc).__name__)
            context = SaveContext(can_save=False, reason=_("Saved passwords are unavailable right now."))
        offer, notice = self._flow.on_authenticated(method, context)
        if notice:
            self._log(_(notice), Level.INFO)
        if offer is not None and self._attempt_password:
            self._show_save_dialog(offer, self._attempt_password)

    def _show_save_dialog(self, offer: SaveOffer, password: str) -> None:
        if self._save_dialog is not None:
            self._save_dialog.dismiss()
        self._save_dialog = SavePasswordDialog(
            self._parent_window(), offer, functools.partial(self._on_save_decision, password=password))

    def _on_save_decision(self, decision: Decision, *, password: str) -> Optional[str]:
        """Apply the user's answer; an error message keeps the dialog open (Retry Save)."""

        self._flow.decide(decision)
        if decision is not Decision.SAVE or self._password_service is None:
            self._save_dialog = None
            return None
        try:
            status, message = self._password_service.save_host_password(self._host, password)
        except Exception as exc:  # the session stays connected whatever happens here
            LOGGER.warning("saving the host password failed: %s", type(exc).__name__)
            status, message = "error", _("Could not save the password. The session stays connected.")
        if status == "error":
            self._log(message, Level.WARNING)
            return message
        self._save_dialog = None
        self._log(message, Level.SUCCESS if status == "saved" else Level.WARNING)
        return None

    def _stop_auth_sources(self, keep_retry: bool = False) -> None:
        """Remove every auth-related GLib source (also on destroy) and the marker file."""

        names = ["_auth_watch_source_id"] + ([] if keep_retry else ["_retry_timeout_id"])
        for name in names:
            source_id = getattr(self, name, 0)
            if source_id:
                GLib.source_remove(source_id)
                setattr(self, name, 0)
        marker = getattr(self, "_marker", None)
        if marker is not None and not keep_retry:
            try:
                marker.unlink()
            except OSError:
                pass

    @property
    def authenticated(self) -> bool:
        return self._authenticated

    def _on_terminal_key_press(self, _term: Vte.Terminal, event: Gdk.EventKey) -> bool:
        keyval = event.keyval
        default_mask = Gtk.accelerator_get_default_mod_mask()
        modifiers = event.state & default_mask
        lower = Gdk.keyval_to_lower(keyval)

        direction = _PANE_KEYS.get(keyval) if modifiers == Gdk.ModifierType.MOD1_MASK else None
        if direction is not None and self.focus_neighbor(direction):
            return True  # Alt+arrow moved focus to another pane (otherwise the terminal gets it)

        if lower == Gdk.KEY_r and modifiers == 0:
            if not self._session_active:
                self._request_reconnect()
                return True
            return False

        if lower == Gdk.KEY_q and modifiers == 0:
            if not self._session_active:
                self._request_close_ended()
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

    def _has_handler(self, signal: str) -> bool:
        return GObject.signal_has_handler_pending(self, GObject.signal_lookup(signal, TerminalTab), 0, False)

    def _request_close_ended(self) -> None:
        """The user closes this ended session (``q``, the banner): in broadcast, its ended peers go too."""

        if self._has_handler("close-ended-requested"):
            self.emit("close-ended-requested")
        else:  # not in a notebook (tests)
            self._request_close()

    def _request_reconnect(self) -> None:
        """The user reconnects this ended session (``r``, the banner): in broadcast, its ended peers too."""

        if self._has_handler("reconnect-requested"):
            self.emit("reconnect-requested")
        else:  # not in a notebook (tests)
            self.reconnect()

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
        self._panes.add(self._secondary_terminal, activate=False)  # activated once it is shown
        self._watch_activity(self._secondary_terminal)
        self._spawn_secondary(self._secondary_terminal)

        self._paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self._paned.get_style_context().add_class("pane-split")
        self._paned.set_wide_handle(True)
        self._paned.set_hexpand(True)
        self._paned.set_vexpand(True)
        self._paned.connect("button-press-event", self._on_paned_press)
        self._pane_holder.remove(self._pane_container)
        first = self._frame_for(self._terminal, self._pane_container, closable=False)
        second_container = TerminalPaneContainer(self._secondary_terminal)
        second = self._frame_for(self._secondary_terminal, second_container, closable=True)
        # shrink=False: the frames' minimum size (Size.PANE_MIN) keeps both panes visible.
        self._paned.pack1(first, resize=True, shrink=False)
        self._paned.pack2(second, resize=True, shrink=False)
        self._pane_holder.get_style_context().add_class("split")
        self._pane_holder.pack_start(self._paned, True, True, 0)
        self._pane_holder.show_all()
        self._sync_pane_frames()
        self.activate_pane(self._secondary_terminal)  # the new pane is the active one
        GLib.idle_add(self._center_divider)

    def _close_duplicate_pane(self) -> None:
        if self._secondary_terminal is None:
            return
        self._panes.remove(self._secondary_terminal)  # its neighbor becomes active
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
            first = self._pane_frames.get(self._terminal)
            if first is not None:
                first.release_body()  # the main terminal's container goes back to the holder
            self._pane_holder.remove(self._paned)
            self._paned.destroy()
            self._paned = None
        self._pane_frames.clear()
        self._pane_footers.clear()
        self._zoomed_pane = None
        self._pane_holder.get_style_context().remove_class("split")
        self._pane_holder.pack_start(self._pane_container, True, True, 0)
        self._pane_holder.show_all()
        self.activate_pane(self._terminal)  # the remaining pane gets the space and the focus

    def _on_terminal_focus(self, term: Vte.Terminal, _event=None) -> bool:
        # Keyboard / programmatic focus: same path as a click, without re-grabbing.
        self.activate_pane(term, grab=False)
        return False

    def _center_divider(self) -> bool:
        if self._paned is not None:
            alloc = self._paned.get_allocation()
            if alloc.width > 1:
                self._paned.set_position(alloc.width // 2)
        return GLib.SOURCE_REMOVE

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
        set_spinner_busy(self._info_spinner, True)
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
        """The password of this session's authenticated attempt, else the same resolution as connecting."""

        if self._authenticated and self._attempt_password:
            return self._attempt_password
        return self._resolve_password().password

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
            failure = stats_failure(proc.returncode, proc.stderr)
            if failure:
                return {"error": failure}
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

    def _stats_host_key_changed(self) -> None:
        """A background probe never opens a dialog: the stats say so, and the log once per host and session."""

        self._cpu_label.set_text(_("Host key changed"))
        _set_stat_tooltip(self._cpu_label, _("The server's host key has changed; remote stats are stopped. See "
                                             "Known Hosts."))
        self._mem_label.set_text("Mem: —")
        self._disk_label.set_text("Disk: —")
        alias = self._host.alias
        if alias not in _HOST_KEY_WARNED:
            _HOST_KEY_WARNED.add(alias)
            self._log(_("{alias}: the server's host key has changed; remote stats could not be read.").format(
                alias=alias), Level.WARNING)

    def _stats_host_key_untrusted(self) -> None:
        """The stats command never asks about (or accepts) a key: the stats say so, the log once per host."""

        self._cpu_label.set_text(_("Host key not trusted"))
        _set_stat_tooltip(self._cpu_label, host_key_preflight.untrusted_text())
        self._mem_label.set_text("Mem: —")
        self._disk_label.set_text("Disk: —")
        alias = self._host.alias
        if alias not in _HOST_KEY_UNTRUSTED_WARNED:
            _HOST_KEY_UNTRUSTED_WARNED.add(alias)
            self._log(_("{alias}: remote stats: {reason}").format(
                alias=alias, reason=host_key_preflight.untrusted_text()), Level.WARNING)

    def _apply_stats_result(self, data: Optional[Dict[str, float]]) -> bool:
        set_spinner_busy(self._info_spinner, False)
        with self._stats_lock:
            self._stats_updating = False
        if isinstance(data, dict) and data.get("error") == STATS_HOST_KEY_CHANGED:
            self._stats_host_key_changed()
            return False
        if isinstance(data, dict) and data.get("error") == STATS_HOST_KEY_UNKNOWN:
            self._stats_host_key_untrusted()
            return False
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
        self._tab_labels: Dict[TerminalTab, SessionTab] = {}
        self._label_mode = TabLabelMode.NAME
        self._open_order: List[TerminalTab] = []  # label counters follow opening order
        self._label_counters: Dict[TerminalTab, int] = {}
        self._labels: Dict[TerminalTab, str] = {}
        self._tab_status: Dict[TerminalTab, bool] = {}
        self._tabs: List[TerminalTab] = []
        self._tile_wrappers: Dict[TerminalTab, TileFrame] = {}
        self._tile_mode = False
        self._rebuilding_layout = False  # tile ↔ tabs: re-attaching pages must not change the current session
        self._user_layout_change = False  # the user's own toggle: the current session gets the keyboard after
        self._zoomed_tile: Optional[TerminalTab] = None
        self._broadcast_mode = False
        self._broadcast_included: Dict[TerminalTab, bool] = {}
        self._last_current_tab: Optional[TerminalTab] = None
        self._secure_store = password_store
        self._password_update_handler = password_update_handler
        self._appearance = appearance
        self.on_quick_connect: Optional[Callable[[], None]] = None
        self.on_new_host: Optional[Callable[[], None]] = None
        self.input_settings = TerminalInputSettings()  # the window passes AppSettings.terminal_input (shared)
        self.local_shell_settings = LocalShellSettings()  # … and AppSettings.local_shell
        self.on_terminal_preferences: Optional[Callable[[], None]] = None  # terminal menu → Preferences
        self.on_duplicate_session: Optional[Callable[[TerminalTab], None]] = None
        self.on_sessions_changed: Optional[Callable[[], None]] = None
        self.on_strip_changed: Optional[Callable[[], None]] = None  # session strip shown/hidden
        self.on_toggle_focus_mode: Optional[Callable[[], None]] = None
        self._status_dock: Optional[Gtk.Box] = None
        # Terminal macros (gtkui/controllers/macro_controller.py): the toolbar's Record button and the
        # macro entries of the "More session actions" menu.
        self.on_record_clicked: Optional[Callable[[], None]] = None
        self.macro_menu: Optional[Callable[[], List[MenuSection]]] = None

        self._build_toolbar()
        self._build_strip()
        self._broadcast_strip = BroadcastStrip(lambda: self._as_user(lambda: self.set_broadcast_mode(False)))
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
        bar.record.connect("clicked", lambda *_: self.on_record_clicked() if self.on_record_clicked else None)
        bar.set_overflow(self._overflow_menu_spec())
        # In tabs mode the toolbar lives at the right end of the session strip
        # (the notebook's tab row); in tile mode it gets this row of its own.
        self._toolbar_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self._toolbar_row.get_style_context().add_class("session-toolbar-row")
        self._toolbar_row.pack_end(bar, False, False, 0)
        self._toolbar_row.show_all()
        self._toolbar_row.set_no_show_all(True)  # _place_toolbar() owns its visibility
        self.pack_start(self._toolbar_row, False, False, 0)

    def _build_strip(self) -> None:
        """One row: session tabs, ``+`` after the last tab, the all-sessions menu and the toolbar."""

        strip = SessionStrip(self.new_local_terminal, on_quick_connect=self._request_quick_connect)
        strip.on_reorder = self._on_strip_reorder
        self._tabs_menu_button = IconMenuButton(Icons.CHEVRON_DOWN, _("All sessions"))
        self._tabs_menu_button.connect("toggled", self._on_tabs_menu_toggled)
        strip.actions.pack_start(self._tabs_menu_button, False, False, 0)
        separator = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        self._strip_separator = separator
        strip.actions.pack_start(separator, False, False, 0)
        self._toolbar_slot = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        strip.actions.pack_start(self._toolbar_slot, False, False, 0)
        strip.show_all()
        strip.set_no_show_all(True)  # _update_placeholder() owns its visibility
        strip.hide()
        self._strip = strip
        self.pack_start(strip, False, False, 0)

    def _build_stack(self) -> None:
        self._notebook = Gtk.Notebook()
        self._notebook.set_scrollable(True)
        self._notebook.set_show_border(False)
        self._notebook.set_hexpand(True)
        self._notebook.set_vexpand(True)
        self._notebook.get_style_context().add_class("session-tabs")
        self._notebook.connect("switch-page", self._on_switch_page)
        self._notebook.connect("page-reordered", lambda *_: self._sync_tab_order())
        self._notebook.set_show_tabs(False)  # SessionStrip shows the tabs

        self._tile_grid = Gtk.Grid()
        self._tile_grid.set_row_homogeneous(True)
        self._tile_grid.set_column_homogeneous(True)
        self._tile_grid.set_hexpand(True)
        self._tile_grid.set_vexpand(True)
        self._tile_grid.get_style_context().add_class("pane-gutter")
        self._tile_scroller = Gtk.ScrolledWindow()
        self._tile_scroller.get_style_context().add_class("pane-gutter")
        # Tiles shrink to the workspace width; only scroll vertically when there are many.
        self._tile_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._tile_scroller.add(self._tile_grid)

        self._placeholder = EmptyState(
            Icons.TERMINAL,
            _("No open sessions"),
            _(
                "Double-click a host or press Enter to connect. Ctrl+T opens the selected host, "
                "Ctrl+Shift+T a local terminal and Ctrl+Alt+T Quick Connect."
            ),
            action_label=_("New _Local Terminal"),
            on_action=self.new_local_terminal,
            secondary_label=_("_Quick Connect…"),
            on_secondary=self._request_quick_connect,
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
        self.apply_density()

    # ------------------------------------------------------------- density
    def apply_density(self) -> None:
        """Spacing of the strip and the tile grid from the active density preset."""

        self._strip_separator.set_margin_top(Space.S)
        self._strip_separator.set_margin_bottom(Space.S)
        self._strip_separator.set_margin_start(Space.XS)
        self._strip_separator.set_margin_end(Space.XS)
        self._tile_grid.set_row_spacing(Size.PANE_GUTTER)
        self._tile_grid.set_column_spacing(Size.PANE_GUTTER)
        self._tile_grid.set_border_width(Size.PANE_GUTTER)
        self._placeholder.apply_density()

    @property
    def strip_visible(self) -> bool:
        """The session tabs are on screen (tabs mode with at least one session)."""

        return self._stack.get_visible_child_name() == "tabs"

    # ---------------------------------------------------------- tab labels
    @property
    def label_mode(self) -> TabLabelMode:
        return self._label_mode

    def set_label_mode(self, mode: object) -> None:
        """Settings → Appearance → Tab label: name, IP address, or both."""

        self._label_mode = TabLabelMode.coerce(mode)
        self._refresh_labels()

    def label_for(self, tab: TerminalTab) -> str:
        return self._labels.get(tab) or tab.title

    def labels(self) -> List[str]:
        """Labels of the open sessions in tab order."""

        return [self.label_for(tab) for tab in self._ordered_tabs()]

    def _label_info(self, tab: TerminalTab) -> SessionLabelInfo:
        if getattr(tab, "is_local", False):  # the shell's title ("Local" until it sets one)
            return SessionLabelInfo(key=tab, alias=tab.title, hostname=tab.title, port=0, custom_name=tab.custom_title)
        try:
            hostname, port, _user = host_connection_params(tab.host)
        except Exception:  # malformed host entry: fall back to the alias
            hostname, port = tab.host.alias, 22
        return SessionLabelInfo(key=tab, alias=tab.host.alias, hostname=hostname, port=port,
                                custom_name=tab.custom_title)

    def _refresh_labels(self) -> None:
        order = [tab for tab in self._open_order if tab in self._tabs]
        labels, self._label_counters = session_tab_labels(
            [self._label_info(tab) for tab in order], self._label_mode, self._label_counters
        )
        self._labels = dict(zip(order, labels))
        for tab in order:
            self._set_tab_label_text(tab)

    def tab_tooltip(self, tab: TerminalTab) -> str:
        """``alias`` / ``user@host:port`` / ``Connected · 121×38`` (+ duplicate hint)."""

        if getattr(tab, "is_local", False):
            details = [tab.title, _("Local terminal: {shell}").format(shell=tab.shell_path)]
            folder = tab.current_directory()
            if folder:
                details.append(folder)
            return "\n".join(details)
        lines = [tab.custom_title or tab.host.alias]
        if tab.custom_title:
            lines.append(tab.host.alias)
        try:
            hostname, port, user = host_connection_params(tab.host)
            lines.append(f"{user}@{hostname}:{port}")
        except Exception:
            pass
        status = status_text(tab.status)
        columns, rows = tab.terminal_size
        lines.append(f"{status} · {columns}×{rows}" if columns and rows else status)
        if is_duplicate(self._label_counters.get(tab, 1)):
            lines.append(_("Duplicate session"))
        return "\n".join(lines)

    def _on_strip_reorder(self, widget: SessionTab, index: int) -> None:
        tab = next((t for t, label in self._tab_labels.items() if label is widget), None)
        if tab is not None and not self._tile_mode:
            self._notebook.reorder_child(tab, index)  # emits page-reordered → _sync_tab_order
            self._sync_strip()

    def _sync_strip(self) -> None:
        self._strip.set_tabs([self._tab_labels[t] for t in self._ordered_tabs() if t in self._tab_labels])

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
            MenuSection("view", [
                MenuItemSpec(_("_Focus Mode"), self._toggle_focus_mode, Icons.MAXIMIZE, "F11"),
            ]),
            *(self.macro_menu() if self.macro_menu is not None else []),
            MenuSection("close", [
                MenuItemSpec(_("Close _Other Sessions"), lambda: self.close_others(self._current_tab())),
                MenuItemSpec(_("_Close Session"), self.close_current, Icons.CLOSE, "Ctrl+W"),
            ]),
        ]

    def refresh_overflow_menu(self) -> None:
        """Rebuild "More session actions" (the macros changed)."""

        self._toolbar.set_overflow(self._overflow_menu_spec())

    def set_macro_recording(self, tab: Optional[TerminalTab], recording: bool, status: str = "") -> None:
        """The toolbar's Record / Stop state and the red dot on the recorded tab's label."""

        self._toolbar.set_recording(recording, status)
        label = self._tab_labels.get(tab) if tab is not None else None
        if label is not None:
            label.set_recording(recording)

    # ----------------------------------------------------------------- tab api
    @property
    def default_font_scale(self) -> float:
        return self._default_font_scale

    def set_default_font_scale(self, scale: float) -> None:
        self._default_font_scale = max(0.5, min(3.0, scale))

    def apply_font_scale_to_all(self, scale: float) -> None:
        for tab in list(self._tabs):
            tab.set_font_scale(scale)

    def new_terminal(self, host: HostEntry, *, autostart: bool = True, profile: Optional["SessionProfile"] = None,
                     password_provider: Optional[PasswordProvider] = None,
                     password_service: Optional[PasswordService] = None) -> TerminalTab:
        tab = TerminalTab(
            host,
            self._log,
            self._default_font_scale,
            password_store=self._secure_store,
            password_update_handler=self._password_update_handler,
            autostart=autostart,
            profile=profile,
            appearance=self._appearance,
            password_provider=password_provider,
            password_service=password_service,
        )
        self.add_page(tab)
        return tab

    def new_local_terminal(self, cwd: Optional[str] = None) -> "TerminalTab":
        """A new tab with the user's shell (``local_shell``), focused; not part of broadcast by default."""

        from ophion.gtkui.local_terminal import LocalTerminalTab

        if cwd is None and self.local_shell_settings.start_in == "current":
            current = self._current_tab()
            if getattr(current, "is_local", False):
                cwd = current.current_directory()
        tab = LocalTerminalTab(self._log, self._default_font_scale, settings=self.local_shell_settings, cwd=cwd,
                               appearance=self._appearance)
        tab.on_title_changed = self._refresh_labels
        self.add_page(tab)
        self._broadcast_included[tab] = False
        self._update_broadcast_ui()
        return tab

    def add_page(self, tab: SessionPage) -> SessionPage:
        """Add any session page (terminal, file browser …) to the tabs / tile grid."""

        tab.connect("status-changed", self._on_tab_status_changed)
        tab.connect("session-closed", self._on_tab_session_closed)
        tab.connect("close-requested", self._on_tab_close_requested)
        if tab.is_terminal:
            tab.connect("close-ended-requested", lambda t: self.close_with_broadcast_peers(t))
            tab.connect("reconnect-requested", lambda t: self.reconnect_with_broadcast_peers(t))
        tab.on_activated = self._on_tab_activated
        tab.on_focus_neighbor = lambda direction, t=tab: self._focus_neighbor_tile(t, direction)
        tab.set_input_observer(self._on_tab_input)
        tab.connect_activity(self._on_tab_activity)
        if hasattr(tab, "set_input_settings"):  # terminals (file browsers have none)
            tab.set_input_settings(self.input_settings)
            tab.on_open_preferences = lambda: self.on_terminal_preferences() if self.on_terminal_preferences else None

        self._tabs.append(tab)
        self._open_order.append(tab)
        self._tab_status[tab] = False
        self._broadcast_included[tab] = True
        self._last_current_tab = tab

        if self._tile_mode:
            self._rebuild_tile_grid()
        else:
            self._append_tab_to_notebook(tab)
        self._refresh_labels()
        self._update_placeholder()
        self._update_broadcast_ui()
        self._emit_sessions_changed()
        GLib.idle_add(tab.focus_terminal)
        return tab

    def _append_tab_to_notebook(self, tab: TerminalTab) -> int:
        self._detach_tab(tab)
        label = SessionTab(
            self.label_for(tab),
            on_activate=lambda t=tab: self._focus_tab(t),
            on_close=lambda t=tab: self.close_tab(t),
            on_context_menu=lambda event, t=tab: self._show_tab_menu(t, event),
            tooltip_text=lambda t=tab: self.tab_tooltip(t),
        )
        label.set_title(self.label_for(tab), tab.status)
        self._tab_labels[tab] = label
        page_num = self._notebook.append_page(tab, None)
        tab.show_all()
        self._sync_strip()
        self._notebook.set_current_page(page_num)
        return page_num

    def set_appearance(self, appearance: TerminalAppearanceSettings) -> None:
        self._appearance = appearance
        for tab in list(self._tabs):
            tab.set_appearance(appearance)

    def close_tab(self, tab: Optional[TerminalTab] = None) -> None:
        target = tab or self._current_tab()
        if not target or target not in self._tabs:
            return
        if not target.confirm_close():  # e.g. "3 transfers are in progress. Cancel them and close?"
            return
        restore = self._focus_in_sessions()  # before: the closed session may hold the keyboard
        self._detach_tab(target)
        self._tabs.remove(target)
        if target in self._open_order:
            self._open_order.remove(target)
        self._tab_status.pop(target, None)
        self._tab_labels.pop(target, None)
        self._refresh_labels()
        self._tile_wrappers.pop(target, None)
        self._broadcast_included.pop(target, None)
        target.set_input_observer(None)
        if self._last_current_tab is target:
            self._last_current_tab = self._tabs[0] if self._tabs else None
        target.destroy()
        if self._tile_mode:
            self._rebuild_tile_grid()  # re-parents every tile: GTK drops the keyboard focus
        self._update_placeholder()
        self._update_broadcast_ui()
        self._emit_sessions_changed()
        current = self._current_tab()
        if restore and current is not None:
            GLib.idle_add(self._focus_after_layout, current)

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

    def activate_pane(self, tab: TerminalTab, terminal: Optional[Vte.Terminal] = None) -> bool:
        """Make ``tab`` (and ``terminal``, one of its panes) active and give it the keyboard.

        The single activation path for the workspace: clicks on a pane or tile,
        Alt+arrows, Ctrl+Tab, Alt+1…9, the session strip and the tab menu all end
        up in :meth:`TerminalTab.activate_pane`, which reports back through
        :meth:`_on_tab_activated`. Returns whether the active pane of ``tab`` changed.
        """

        if tab not in self._tabs:
            return False
        return tab.activate_pane(terminal)

    def _focus_tab(self, tab: TerminalTab) -> None:
        self.activate_pane(tab)

    def _on_tab_activated(self, tab: TerminalTab) -> None:
        """A pane of ``tab`` became active: it is the current session (no-op if it already is)."""

        if tab not in self._tabs:
            return
        if self._tile_mode:
            if self._last_current_tab is tab:
                return
            self._last_current_tab = tab
            self._sync_tile_frames()
            self._sync_toolbar()
            self._emit_sessions_changed()  # host bar, status bar, health
            return
        self._last_current_tab = tab
        page = self._notebook.page_num(tab)
        if page != -1 and page != self._notebook.get_current_page():
            self._notebook.set_current_page(page)  # _on_switch_page: strip, host bar, status bar
            self._sync_toolbar()

    def _ordered_tabs(self) -> List[TerminalTab]:
        if self._tile_mode:
            return list(self._tabs)
        pages = [self._notebook.get_nth_page(i) for i in range(self._notebook.get_n_pages())]
        return [page for page in pages if isinstance(page, SessionPage)]

    def _sync_tab_order(self) -> None:
        ordered = self._ordered_tabs()
        if len(ordered) == len(self._tabs):
            self._tabs = ordered
        if not self._tile_mode:
            self._sync_strip()

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

    def terminal_action(self, name: str, focus: Optional[Gtk.Widget]) -> bool:
        """Run terminal action ``name`` on the focused terminal (``True`` when something handled it).

        Terminal shortcuts (copy, paste, Shift+Insert …) must never reach a terminal in the background:
        in a text field they do the field's own copy / paste / select all, anywhere else nothing.
        """

        if isinstance(focus, Vte.Terminal):
            tab = next((t for t in self._tabs if getattr(t, "is_terminal", False) and focus in t._terminals()), None)
            if tab is None:
                return False
            run = {
                "copy": lambda: tab.copy_clipboard(focus),
                "paste": lambda: tab.paste_clipboard(focus),
                "paste-primary": lambda: tab.paste_primary(focus),
                "select-all-terminal": lambda: tab.select_all_terminal(focus),
                "clear-scrollback": lambda: tab.clear_scrollback(focus),
                "reset-terminal": lambda: tab.reset_terminal(focus),
                "terminal-menu": lambda: tab.show_terminal_menu(focus),
            }.get(name)
            if run is None:
                return False
            run()
            return True
        if isinstance(focus, (Gtk.Editable, Gtk.TextView)):
            signal = {"copy": "copy-clipboard", "paste": "paste-clipboard", "paste-primary": "paste-clipboard"}.get(name)
            if signal is not None:
                focus.emit(signal)  # what Ctrl+Insert / Shift+Insert do in the field itself
            elif name == "select-all-terminal":
                if isinstance(focus, Gtk.TextView):
                    focus.emit("select-all", True)
                else:
                    focus.select_region(0, -1)
            return True
        return False

    def reconnect_current(self) -> None:
        tab = self._current_tab()
        if tab:
            self.reconnect_with_broadcast_peers(tab)

    def duplicate_current(self) -> None:
        tab = self._current_tab()
        if tab:
            self.duplicate_tab(tab)

    def duplicate_tab(self, tab: TerminalTab) -> None:
        if getattr(tab, "is_local", False):  # another shell in the same folder
            self.new_local_terminal(cwd=tab.current_directory())
            return
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
            self._refresh_labels()

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
        self._as_user(lambda: self.set_tile_mode(not self._tile_mode))

    def _as_user(self, change: Callable[[], None]) -> None:
        """A toggle by the user (button, menu, shortcut): the current session gets the keyboard afterwards.

        Also when the layout stays (broadcast off keeps the tile view): a click on the toolbar button
        would otherwise leave the keyboard on the button.
        """

        self._user_layout_change = True
        try:
            change()
        finally:
            self._user_layout_change = False
        current = self._current_tab()
        if current is not None:
            GLib.idle_add(self._focus_after_layout, current)

    @property
    def broadcast_active(self) -> bool:
        return self._broadcast_mode

    def set_broadcast_mode(self, active: bool) -> None:
        active = bool(active)
        if active == self._broadcast_mode:
            self._sync_toolbar()
            return
        if active and len(self._terminal_tabs()) < 2:  # file browsers never take part
            self._log(_("Broadcast needs at least two open terminal sessions."), Level.INFO)
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
        self._as_user(lambda: self.set_broadcast_mode(not self._broadcast_mode))

    def _terminal_tabs(self) -> List[SessionPage]:
        return [tab for tab in self._tabs if tab.is_terminal]

    def _update_broadcast_ui(self) -> None:
        targets = [t for t in self._terminal_tabs()
                   if self._broadcast_included.get(t, True) and self._tab_status.get(t, False)]
        self._broadcast_strip.set_target_count(len(targets))
        self._broadcast_strip.set_reveal_child(self._broadcast_mode)
        for tab in self._tabs:
            # In tile mode the tile frame shows it; the tab's own outline is for tabs mode.
            tab.set_broadcast_highlight(self._broadcast_mode and self._broadcast_included.get(tab, True))
        self._sync_tile_frames()

    def _on_broadcast_include_toggled(self, tab: TerminalTab, active: bool) -> None:
        self._broadcast_included[tab] = active
        self._update_broadcast_ui()

    def macro_targets(self, source: TerminalTab) -> List[TerminalTab]:
        """Sessions a macro run from ``source`` types into: ``source`` first, then what typed input reaches.

        The same rule as typed input (:func:`broadcast_target_tabs`): with broadcast on and ``source``
        included, every other included, connected terminal session (in tab order); otherwise only
        ``source``. ``[]`` when ``source`` is not a connected terminal session.
        """

        if source not in self._tabs or not getattr(source, "is_terminal", False) \
                or not self._tab_status.get(source, False):
            return []
        if not self._broadcast_mode:
            return [source]
        others = broadcast_target_tabs(self._terminal_tabs(), source, self._broadcast_included, self._tab_status)
        return [source] + self._in_tab_order(others, source)

    def macro_unreachable(self, source: TerminalTab) -> List[TerminalTab]:
        """Sessions included in the broadcast that a macro from ``source`` skips because they are not connected."""

        if not self._broadcast_mode or source not in self._tabs:
            return []
        everyone = {tab: True for tab in self._terminal_tabs()}
        reachable = set(broadcast_target_tabs(self._terminal_tabs(), source, self._broadcast_included,
                                              self._tab_status))
        if not self._tab_status.get(source, False):
            return []
        wanted = broadcast_target_tabs(self._terminal_tabs(), source, self._broadcast_included, everyone)
        return self._in_tab_order([tab for tab in wanted if tab not in reachable], source)

    def _in_tab_order(self, tabs: List[TerminalTab], source: TerminalTab) -> List[TerminalTab]:
        order = {tab: index for index, tab in enumerate(self._ordered_tabs())}
        unique = [tab for tab in dict.fromkeys(tabs) if tab is not source]
        return sorted(unique, key=lambda tab: order.get(tab, len(order)))

    def _on_tab_input(self, source: TerminalTab, data: str) -> None:
        if source is not self._current_tab():
            self._on_tab_activated(source)  # typing into a pane makes it the broadcast source
        if not self._broadcast_mode or not data:
            return
        for target in broadcast_target_tabs(self._terminal_tabs(), source, self._broadcast_included,
                                            self._tab_status):
            target.feed_input(data)

    def _create_tile_wrapper(self, tab: TerminalTab) -> TileFrame:
        self._detach_tab(tab)
        frame = TileFrame(
            self.label_for(tab),
            tab.status,
            included=self._broadcast_included.get(tab, True),
            on_include_toggled=lambda active, t=tab: self._on_broadcast_include_toggled(t, active),
            on_close=lambda t=tab: self.close_tab(t),
            on_focus=lambda t=tab: self._focus_tab(t),
            on_zoom=lambda t=tab: self.toggle_tile_zoom(t),
            address=tab._pane_address(),
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
        self._zoomed_tile = None
        count = len(self._tabs)
        for index, tab in enumerate(self._tabs):
            row, column = session_tile_position(index, count)
            self._tile_grid.attach(self._create_tile_wrapper(tab), column, row, 1, 1)
        self._tile_grid.show_all()
        self._sync_tile_frames()

    def _sync_tile_frames(self) -> None:
        """Focused tile, broadcast borders and titles of the tile frames."""

        current = self._current_tab()
        for tab, frame in self._tile_wrappers.items():
            frame.set_focused(tab is current, dim_others=len(self._tile_wrappers) > 1)
            frame.set_broadcast(self._broadcast_mode and self._broadcast_included.get(tab, True))
            ctx = frame.style()
            (ctx.add_class if self._broadcast_mode else ctx.remove_class)("broadcasting")

    def toggle_tile_zoom(self, tab: Optional[TerminalTab] = None) -> None:
        """Show one tile alone without leaving tile mode; again to restore the grid."""

        if not self._tile_mode:
            return
        tab = tab or self._current_tab()
        zoom = self._zoomed_tile is None and tab is not None
        self._zoomed_tile = tab if zoom else None
        for other, frame in self._tile_wrappers.items():
            frame.set_visible(not zoom or other is tab)
            frame.set_zoomed(zoom)
        if tab is not None:
            self._focus_tab(tab)

    def _focus_neighbor_tile(self, tab: TerminalTab, direction: str) -> bool:
        """Alt+arrows in tile mode: the tile next to ``tab`` in the grid."""

        if not self._tile_mode or tab not in self._tabs or len(self._tabs) < 2:
            return False
        count = len(self._tabs)
        positions = {t: session_tile_position(i, count) for i, t in enumerate(self._tabs)}
        row, column = positions[tab]
        d_row, d_col = {"left": (0, -1), "right": (0, 1), "up": (-1, 0), "down": (1, 0)}[direction]
        target = next((t for t, pos in positions.items() if pos == (row + d_row, column + d_col)), None)
        if target is None:
            return False
        self._focus_tab(target)
        return True

    def _set_tile_mode(self, active: bool) -> None:
        """Tile ↔ tabs. The session the user was in stays current and gets the keyboard back once shown.

        Re-parenting the tabs drops GTK's focus, and the notebook's switch-page fired by removing and
        re-adding pages used to make another session current: both are handled here, for every path
        (toolbar, menu, shortcut, broadcast, the last-but-one session closed).
        """

        if active == self._tile_mode:
            return
        keep = self._current_tab() or (self._tabs[0] if self._tabs else None)
        restore = self._focus_in_sessions()
        self._tile_mode = active
        self._rebuilding_layout = True
        try:
            if active:
                self._last_current_tab = keep
                self._rebuild_tile_grid()
            else:
                for tab in list(self._tabs):
                    self._detach_tab(tab)
                for tab in self._tabs:
                    self._append_tab_to_notebook(tab)
        finally:
            self._rebuilding_layout = False
        self._last_current_tab = keep
        if active:
            self._sync_tile_frames()
        elif keep in self._tabs:
            page_num = self._notebook.page_num(keep)
            if page_num != -1:
                self._notebook.set_current_page(page_num)
                self._on_switch_page(self._notebook, keep, page_num)  # labels, strip (no signal if already current)
        self._update_placeholder()
        self._dock_statusbar()
        self._sync_toolbar()
        self._emit_sessions_changed()  # host bar, status bar
        if restore and keep is not None and not self._user_layout_change:  # a user toggle: _as_user does it
            GLib.idle_add(self._focus_after_layout, keep)

    def _focus_in_sessions(self) -> bool:
        """Keyboard focus is in the sessions area, or nowhere (then the session may take it)."""

        toplevel = self.get_toplevel()
        focus = toplevel.get_focus() if isinstance(toplevel, Gtk.Window) else None
        return focus is None or focus is self or focus.is_ancestor(self)

    def _focus_after_layout(self, tab: TerminalTab) -> bool:
        """Once the new layout is shown: the kept session's active pane gets the keyboard (if still current)."""

        if tab in self._tabs and tab is self._current_tab():
            tab.focus_terminal()  # waits until the terminal is realized and mapped
        return GLib.SOURCE_REMOVE

    # ----------------------------------------------------------------- helpers
    def _tab_label_text(self, tab: TerminalTab) -> str:
        connected = self._tab_status.get(tab, False)
        indicator = "●" if connected else "○"
        return f"{indicator} {self.label_for(tab)}"

    def _set_tab_label_text(self, tab: TerminalTab) -> None:
        label = self._tab_labels.get(tab)
        if label:
            label.set_title(self.label_for(tab), tab.status)
        frame = self._tile_wrappers.get(tab)
        if frame:
            frame.set_title(self.label_for(tab), tab.status)
        tab.set_pane_label(self.label_for(tab))

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
        self._sync_strip()

    def _current_tab(self) -> Optional[TerminalTab]:
        if self._tile_mode:
            if self._last_current_tab in self._tabs:
                return self._last_current_tab
            return self._tabs[0] if self._tabs else None
        index = self._notebook.get_current_page()
        if index < 0:
            return None
        child = self._notebook.get_nth_page(index)
        if isinstance(child, SessionPage):
            self._last_current_tab = child
            return child
        return None

    def _on_close_button_clicked(self, _button: Gtk.Button, tab: TerminalTab) -> None:
        self.close_tab(tab)

    def _on_switch_page(self, _notebook: Gtk.Notebook, page: Gtk.Widget, _num: int) -> None:
        if self._rebuilding_layout:  # pages removed / re-added by _set_tile_mode: not the user's choice
            return
        if isinstance(page, SessionPage):
            self._last_current_tab = page
        for tab, label in self._tab_labels.items():
            label.set_current(tab is page)
        current = self._tab_labels.get(page)
        if current is not None:
            self._strip.scroll_to(current)
        GLib.idle_add(self._sync_toolbar_idle)
        GLib.idle_add(self._emit_sessions_changed_idle)

    def _emit_sessions_changed_idle(self) -> bool:
        self._emit_sessions_changed()
        return GLib.SOURCE_REMOVE

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
        self._emit_sessions_changed()

    def _emit_sessions_changed(self) -> None:
        self._dock_statusbar()
        if self.on_sessions_changed is not None:
            self.on_sessions_changed()

    # ------------------------------------------------------- status docking
    def set_status_dock(self, dock: Optional[Gtk.Box]) -> None:
        """Show the current session's status bar in ``dock`` (tabs mode only)."""

        self._status_dock = dock
        self._dock_statusbar()

    def _dock_statusbar(self) -> None:
        dock = self._status_dock
        if dock is None:
            return
        current = None if self._tile_mode else self._current_tab()
        for tab in self._tabs:
            if tab is not current:
                tab.reclaim_statusbar()
        for child in list(dock.get_children()):
            if current is None or child is not current.statusbar:
                if isinstance(child, SessionStatusBar):
                    owner = next((t for t in self._tabs if t.statusbar is child), None)
                    if owner is not None:
                        owner.reclaim_statusbar()
                    else:
                        dock.remove(child)
        if current is not None and current.statusbar.get_parent() is not dock:
            bar = current.release_statusbar()
            dock.pack_start(bar, True, True, 0)
            bar.show()

    def has_session_for(self, host) -> bool:
        return any(self._same_host(tab.host, host) for tab in self._tabs)

    def focus_session_for(self, host) -> bool:
        for tab in self._ordered_tabs():
            if self._same_host(tab.host, host):
                self.focus_tab(tab)
                return True
        return False

    @staticmethod
    def _same_host(a, b) -> bool:
        if a is None or b is None:
            return False
        return (a.parent_group, a.child_group, a.alias) == (b.parent_group, b.child_group, b.alias)

    def session_states(self) -> List[ConnectionStatus]:
        return [tab.status for tab in self._tabs]

    def _on_tab_session_closed(self, tab: TerminalTab, alias: str, exited_normally: bool) -> None:
        status = _("Session ended cleanly") if exited_normally else _("Session terminated unexpectedly")
        self._log(_("{alias}: {status}").format(alias=alias, status=status), exited_normally)
        self._set_tab_label_text(tab)

    def _on_tab_close_requested(self, tab: TerminalTab) -> None:
        GLib.idle_add(self._close_tab_after_idle, tab)

    def ended_broadcast_peers(self, tab: SessionPage) -> List[SessionPage]:
        """The other ended terminal sessions in the broadcast, in tab order; ``[]`` unless broadcast is on and
        ``tab`` is an included, ended terminal session (sessions still connected are never included)."""

        def ended(page: SessionPage) -> bool:
            return bool(page.is_terminal and self._broadcast_included.get(page, True)
                        and getattr(page, "session_ended", False))

        if not self._broadcast_mode or tab not in self._tabs or not ended(tab):
            return []
        return [page for page in self._ordered_tabs() if page is not tab and ended(page)]

    def close_with_broadcast_peers(self, tab: SessionPage) -> None:
        """Close ``tab`` and its ended broadcast peers, each through :meth:`close_tab` (asks, cleans up)."""

        for target in [tab, *self.ended_broadcast_peers(tab)]:
            GLib.idle_add(self._close_tab_after_idle, target)

    def reconnect_with_broadcast_peers(self, tab: SessionPage) -> None:
        """Reconnect ``tab`` at once, then its ended broadcast peers 150 ms apart (in tab order), each on its own."""

        for index, peer in enumerate(self.ended_broadcast_peers(tab), start=1):
            GLib.timeout_add(RECONNECT_STAGGER_MS * index, self._reconnect_peer, peer)
        self._reconnect_peer(tab)

    def _reconnect_peer(self, tab: SessionPage) -> bool:
        if tab in self._tabs:  # closed meanwhile: nothing to do
            try:
                tab.reconnect()
            except Exception as exc:  # one session failing never stops the others
                LOGGER.warning("reconnecting %s failed: %s", getattr(tab, "title", "a session"), type(exc).__name__)
        return GLib.SOURCE_REMOVE

    def _close_tab_after_idle(self, tab: TerminalTab) -> bool:
        self.close_tab(tab)
        return GLib.SOURCE_REMOVE

    def _toggle_focus_mode(self) -> None:
        if self.on_toggle_focus_mode is not None:
            self.on_toggle_focus_mode()

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
            terminal=tab.is_terminal,
        )
        popup_menu(build_context_menu(spec, attach_to=self), event)

    def _on_tabs_menu_toggled(self, button: Gtk.MenuButton) -> None:
        if not button.get_active():
            return
        items = [
            MenuItemSpec(f"{number}. {self.label_for(tab)}", lambda t=tab: self._focus_tab(t), accel=f"Alt+{number}" if number < 10 else "")
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
        changed = self._stack.get_visible_child_name() != visible
        self._stack.set_visible_child_name(visible)
        self._strip.set_visible(visible == "tabs")
        self._place_toolbar(in_strip=visible == "tabs")
        if changed and self.on_strip_changed is not None:
            self.on_strip_changed()
        if len(self._tabs) <= 1:
            if self._broadcast_mode:
                self.set_broadcast_mode(False)
            if self._tile_mode:
                self._set_tile_mode(False)
        self._sync_toolbar()

    # ------------------------------------------------------------- toolbar
    def _place_toolbar(self, *, in_strip: bool) -> None:
        target = self._toolbar_slot if in_strip else self._toolbar_row
        parent = self._toolbar.get_parent()
        if parent is not target:
            if parent is not None:
                parent.remove(self._toolbar)
            if in_strip:
                target.pack_start(self._toolbar, False, False, 0)
            else:
                target.pack_end(self._toolbar, False, False, 0)
            self._toolbar.show_all()
        self._toolbar_row.set_visible(not in_strip)

    @property
    def toolbar_in_strip(self) -> bool:
        return self._toolbar.get_parent() is self._toolbar_slot

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
        terminal = bool(tab is not None and tab.is_terminal)  # file browsers: no split, transcript, stats …
        multiple = len(self._tabs) > 1
        for widget in (bar.split, bar.save, bar.plain_copy, bar.stats, bar.more):
            widget.set_sensitive(terminal)
        bar.reconnect.set_sensitive(has_tab)
        bar.tile.set_sensitive(multiple)
        bar.broadcast.set_sensitive(len(self._terminal_tabs()) > 1)
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
        self._as_user(lambda: self.set_tile_mode(button.get_active()))

    def _on_broadcast_toggled(self, button: Gtk.ToggleButton) -> None:
        self._as_user(lambda: self.set_broadcast_mode(button.get_active()))

    def _on_stats_toggled(self, button: Gtk.ToggleButton) -> None:
        tab = self._current_tab()
        if tab:
            tab.set_stats_visible(button.get_active())
