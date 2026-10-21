"""An RDP session tab: the remote desktop inside the tab where the platform allows it, else a session card.

Engines (``docs/rdp.md``): **gtk-frdp** (the ``Frdp.Display`` widget), **FreeRDP
in the tab** (``/parent-window`` into a ``Gtk.Socket``, X11 only) or an
**external window**. The external window, a failed embedding (no FreeRDP
window within :data:`~ssh_manager.gtkui.rdp.display.EMBED_TIMEOUT_S` seconds of
connecting — remembered per engine and session type) and every state that is
not "connected" show the **session card**: status, server, engine, security,
certificate, size, time connected, the connection log, **Focus Window**,
**Disconnect** and **Reconnect**.

One connection attempt:

1. **Password:** the stored one (vault → credential → host password), or the
   password dialog ("Ask every time", or nothing stored).
2. **Probe** (worker thread): reachable? which security? which certificate?
3. **Trust** (:mod:`ssh_manager.rdp.trust_flow`): a pinned certificate
   connects, a new one asks (once / remember), a changed one warns (Cancel is
   the default), "Do not check" asks every time.
4. **Launch:** FreeRDP gets the password only through its stdin pipe
   (:mod:`ssh_manager.rdp.command`); gtk-frdp gets it as a GObject property.
5. **Connected:** FreeRDP's "Local framebuffer format" line (or gtk-frdp's
   ``rdp-connected``), else :data:`HEURISTIC_S` s without errors. Only a
   verified NLA login offers "Save this password for this host?".
6. **End:** only "wrong password" asks again (≤ 3, ≥ 1 s apart). A dropped
   network reconnects on its own with backoff; an idle timeout or a takeover
   asks first; a sign-out never reconnects (:mod:`ssh_manager.rdp.reconnect`).

The toolbar (:class:`RdpToolbar`) changes scale, resolution, clipboard and
keyboard for *this session* (FreeRDP cannot change them while running, so it
reconnects), goes full screen, saves screenshots and shows connection info.

Threads never touch GTK: results come back through ``GLib.idle_add``; every
timeout source is removed when the tab is destroyed; closing the tab stops
FreeRDP's process group.
"""

from __future__ import annotations

import functools
import logging
import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gio, GLib, Gtk, Pango  # type: ignore  # noqa: E402

from ...auth_flow import (  # noqa: E402
    NO_PASSWORD, RETRY_DELAY_S, AuthOutcome, Decision, FailureAction, PasswordFlow, PasswordResolution, SaveContext,
    SaveOffer, source_phrase,
)
from ... import gc_guard  # noqa: E402
from ...host_utils import host_connection_params  # noqa: E402
from ...rdp.clipboard import ClipboardPolicy  # noqa: E402
from ...rdp.command import LaunchError, build_launch  # noqa: E402
from ...rdp.engines import CAPABILITY_LABELS, Engine, EngineReport, cached_report, choose_engine, install_hint  # noqa: E402
from ...rdp.keyboard import Effect, KeyboardGrab, release_hint  # noqa: E402
from ...rdp.outcome import Kind, RdpOutcome, classify_rdp_exit, connected_line  # noqa: E402
from ...rdp.probe import ProbeResult, probe, probe_tls  # noqa: E402
from ...rdp.tunnel import open_tunnel  # noqa: E402
from ...transfer.backend import AuthError, BackendError, UnknownHostKey  # noqa: E402
from ...rdp.process import RdpProcess  # noqa: E402
from ...rdp.reconnect import Plan, ReconnectScheduler, plan_for  # noqa: E402
from ...rdp.settings import RdpSettings, resolve_identity  # noqa: E402
from ...rdp.trust_flow import Action, decide  # noqa: E402
from ...rdp.window_focus import focus_pid, focus_tool  # noqa: E402
from ...transfer.protolog import ConnectionLog  # noqa: E402
from ...trust import pin_key  # noqa: E402
from ..constants import Icons  # noqa: E402
from ..dialogs.confirm import confirm  # noqa: E402
from ..dialogs.password import PasswordRequestDialog, SavePasswordDialog  # noqa: E402
from ..dialogs.trust import confirm_unchecked_certificate, rdp_trust_choice, trust_certificate  # noqa: E402
from ..i18n import _  # noqa: E402
from ..session_page import SessionPage  # noqa: E402
from ..theme.tokens import ConnectionStatus, Level, Size, Space  # noqa: E402
from ..views.statusbar import SessionStatusBar  # noqa: E402
from ..widgets.icon_button import set_accessible_name  # noqa: E402
from ..widgets.icons import make_icon  # noqa: E402
from .display import FrdpSurface, SocketSurface, Surface  # noqa: E402
from .runtime import RdpRuntime  # noqa: E402
from .toolbar import RdpToolbar  # noqa: E402

LOGGER = logging.getLogger(__name__)
HEURISTIC_S = 10  # no "connected" log line: alive this long without an error = probably connected
_RESPONSE_RECONNECT = 1
_RESPONSE_CLOSE = 2
LogFn = Callable[[str, Level], None]


def isolate(text: str) -> str:
    """Bidi-isolate a name from the inventory or the server (Persian aliases next to Latin text)."""

    return "⁨" + (text or "") + "⁩"


class RdpSessionTab(SessionPage):
    __gtype_name__ = "RdpSessionTab"
    kind = "rdp"

    def __init__(self, host: Any, log: LogFn, *, runtime: RdpRuntime, app_settings: Any = None,
                 password_provider: Optional[Callable[[], PasswordResolution]] = None, password_service: Any = None,
                 report_provider: Callable[[], Optional[EngineReport]] = cached_report,
                 probe_fn: Optional[Callable[..., ProbeResult]] = None,
                 process_factory: Optional[Callable[..., RdpProcess]] = None,
                 surface_factory: Optional[Callable[[Engine], Optional[Surface]]] = None,
                 tls_probe_fn: Optional[Callable[..., ProbeResult]] = None,
                 tunnel_factory: Optional[Callable[[str], Any]] = None,
                 gateway_password_provider: Optional[Callable[[str], Optional[str]]] = None,
                 autostart: bool = True) -> None:
        Gtk.Box.__init__(self, orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.get_style_context().add_class("rdp-session")
        self._host = host
        self._log = log
        self._custom_title = ""
        self._status = ConnectionStatus.UNKNOWN
        self._runtime = runtime
        self._app_settings = app_settings
        self._password_provider = password_provider
        self._password_service = password_service
        self._report_provider = report_provider
        self._probe = probe_fn or (lambda *args, **kwargs: probe(*args, **kwargs))  # looked up when used
        self._process_factory = process_factory or (lambda *args, **kwargs: RdpProcess(*args, **kwargs))
        self._surface_factory = surface_factory or self._default_surface
        self._tls_probe = tls_probe_fn or (lambda *args, **kwargs: probe_tls(*args, **kwargs))
        self._tunnel_factory = tunnel_factory  # SSH host id -> an SftpBackend for that host (not connected)
        self._gateway_password_provider = gateway_password_provider  # credential id -> password
        self.tunnel: Any = None
        self._latency: Optional[float] = None
        self.settings, warnings = RdpSettings.for_host(host)  # a copy: toolbar changes last for this session
        self.connection_log = ConnectionLog(host.alias)
        self._flow = PasswordFlow(host.alias)
        self._resolution: PasswordResolution = NO_PASSWORD
        self._typed_password: Optional[str] = None
        self._attempt_password: Optional[str] = None
        self.process: Optional[RdpProcess] = None
        self.surface: Optional[Surface] = None
        self.engine: Optional[Engine] = None
        self.security = ""  # what the probe saw the server select
        self.certificate_checked = True
        self.connected_method = ""  # "log" | "signal" | "heuristic" | ""
        self.last_outcome: Optional[RdpOutcome] = None
        self.scheduler = ReconnectScheduler(self.settings.reconnect_max if self.settings.auto_reconnect else 1)
        self.on_fullscreen: Optional[Callable[[bool], None]] = None  # set by the window (focus mode)
        self._session_pins: List[str] = []  # certificates trusted for this tab only ("Trust once")
        self._force_external = False
        self._embedded_child = 0
        self._connected = False
        self._connected_at = 0.0
        self._reconnect_after_exit = False
        self._reconnecting = False
        self._network_handler = 0
        self._sources: List[int] = []
        self._dialogs: List[Gtk.Widget] = []
        self._closed = False
        self._log_flush_pending = False
        self.keyboard = KeyboardGrab(enabled=self.settings.grab_keyboard)
        self.connection_log.listeners.append(self._on_log_line)
        self._build()
        self.connect("unmap", lambda *_: self._keyboard_event(self.keyboard.tab_switch()))
        self.connect("destroy", lambda *_: self._on_destroy())
        for warning in warnings:
            self._log(_("{alias}: {warning}").format(alias=host.alias, warning=_(warning)), Level.WARNING)
        if autostart:
            self._sources.append(GLib.idle_add(self._begin_connect))

    # ---------------------------------------------------------- building
    def _build(self) -> None:
        self._banner = Gtk.InfoBar()
        self._banner.set_message_type(Gtk.MessageType.WARNING)
        self._banner_label = Gtk.Label()
        self._banner_label.set_xalign(0.0)
        self._banner_label.set_line_wrap(True)
        self._banner.get_content_area().pack_start(self._banner_label, True, True, 0)
        self._banner_label.show()  # the bar itself is no-show-all: its contents must be shown explicitly
        self._banner.add_button(_("_Reconnect"), _RESPONSE_RECONNECT)
        self._banner.add_button(_("_Close Session"), _RESPONSE_CLOSE)
        self._banner.connect("response", self._on_banner_response)
        self._banner.set_revealed(False)
        self._banner.set_no_show_all(True)
        self.pack_start(self._banner, False, False, 0)

        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.NONE)  # a native X child cannot fade anyway
        self.stack.add_named(self._build_card(), "card")
        self.display_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.toolbar = RdpToolbar()
        self._wire_toolbar()
        self.display_box.pack_start(self.toolbar, False, False, 0)
        self.stack.add_named(self.display_box, "display")
        self.stack.set_visible_child_name("card")
        self.pack_start(self.stack, True, True, 0)

        self._statusbar = SessionStatusBar()
        self._statusbar.size_label.set_tooltip_text(_("Remote screen size"))
        self.pack_start(self._statusbar, False, False, 0)
        self._set_status(ConnectionStatus.CONNECTING, _("Starting…"))

    def _build_card(self) -> Gtk.Widget:
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=Space.M)
        card.get_style_context().add_class("rdp-card")
        card.set_halign(Gtk.Align.CENTER)
        card.set_valign(Gtk.Align.CENTER)
        for setter in (card.set_margin_top, card.set_margin_bottom, card.set_margin_start, card.set_margin_end):
            setter(Space.XL)
        card.pack_start(make_icon(Icons.DESKTOP, Size.EMPTY_CARD_ICON), False, False, 0)
        self.title_label = Gtk.Label(label=isolate(self._host.alias))
        self.title_label.get_style_context().add_class("page-title")
        self.title_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.title_label.set_max_width_chars(48)
        card.pack_start(self.title_label, False, False, 0)
        self.status_label = Gtk.Label()
        self.status_label.set_line_wrap(True)
        self.status_label.set_max_width_chars(60)
        self.status_label.set_justify(Gtk.Justification.CENTER)
        card.pack_start(self.status_label, False, False, 0)
        self.reconnect_spinner = Gtk.Spinner()
        self.reconnect_spinner.set_no_show_all(True)
        card.pack_start(self.reconnect_spinner, False, False, 0)

        grid = Gtk.Grid(column_spacing=Space.M, row_spacing=Space.XS)
        grid.set_halign(Gtk.Align.CENTER)
        self.facts: Dict[str, Gtk.Label] = {}
        for row, (key, label) in enumerate((("server", _("Server")), ("engine", _("Engine")),
                                            ("security", _("Security")), ("certificate", _("Certificate")),
                                            ("size", _("Screen")), ("elapsed", _("Connected for")))):
            name = Gtk.Label(label=label)
            name.set_xalign(1.0)
            name.get_style_context().add_class("dim-label")
            value = Gtk.Label(label="—")
            value.set_xalign(0.0)
            value.set_selectable(True)
            value.set_ellipsize(Pango.EllipsizeMode.END)
            value.set_max_width_chars(48)
            grid.attach(name, 0, row, 1, 1)
            grid.attach(value, 1, row, 1, 1)
            self.facts[key] = value
        card.pack_start(grid, False, False, 0)
        hostname, port, _user = host_connection_params(self._host)
        self.facts["server"].set_text(f"{hostname}:{port}")
        self.facts["size"].set_text(self._size_text())

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.S)
        buttons.set_halign(Gtk.Align.CENTER)
        self.focus_button = Gtk.Button.new_with_mnemonic(_("_Focus Window"))
        self.focus_button.connect("clicked", lambda *_: self.focus_window())
        self.disconnect_button = Gtk.Button.new_with_mnemonic(_("_Disconnect"))
        self.disconnect_button.connect("clicked", lambda *_: self.disconnect())
        self.reconnect_button = Gtk.Button.new_with_mnemonic(_("_Reconnect"))
        self.reconnect_button.connect("clicked", lambda *_: self.reconnect())
        self.cancel_button = Gtk.Button.new_with_mnemonic(_("_Stop Reconnecting"))
        self.cancel_button.connect("clicked", lambda *_: self.cancel_reconnect())
        self.cancel_button.set_no_show_all(True)
        for button in (self.focus_button, self.disconnect_button, self.reconnect_button, self.cancel_button):
            buttons.pack_start(button, False, False, 0)
        card.pack_start(buttons, False, False, 0)

        self.log_expander = Gtk.Expander.new_with_mnemonic(_("Connection _log"))
        log_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=Space.XS)
        self.log_view = Gtk.TextView()
        self.log_view.set_editable(False)
        self.log_view.set_cursor_visible(False)
        self.log_view.set_monospace(True)
        self.log_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        set_accessible_name(self.log_view, _("Connection log"))
        log_scroller = Gtk.ScrolledWindow()
        log_scroller.set_shadow_type(Gtk.ShadowType.IN)
        log_scroller.set_min_content_height(8 * Size.TREE_ROW)
        log_scroller.set_min_content_width(60 * Space.S)
        log_scroller.add(self.log_view)
        copy = Gtk.Button.new_with_mnemonic(_("Copy _Log"))
        copy.set_halign(Gtk.Align.END)
        copy.connect("clicked", lambda *_: self.copy_log())
        log_box.pack_start(log_scroller, True, True, 0)
        log_box.pack_start(copy, False, False, 0)
        self.log_expander.add(log_box)
        self.log_expander.connect("notify::expanded", lambda *_: self._flush_log())
        card.pack_start(self.log_expander, False, False, 0)
        scroller.add(card)
        return scroller

    def _wire_toolbar(self) -> None:
        bar = self.toolbar
        bar.on_fullscreen = self.set_fullscreen
        bar.on_scale = self.set_scale_mode
        bar.on_resolution = self.set_resolution
        bar.on_keyboard = self.toggle_keyboard
        bar.on_clipboard = self.set_clipboard
        bar.on_screenshot = self.save_screenshot
        bar.on_reconnect = lambda: self.reconnect()
        bar.on_disconnect = self.disconnect
        bar.on_open_drives = self.open_shared_folder
        bar.info_lines = self.info_lines
        bar.drive_names = lambda: [d.name for d in self.settings.drives]

    def _size_text(self) -> str:
        s = self.settings
        if s.size_mode == "fullscreen":
            return _("Full screen")
        if s.size_mode == "fixed":
            return f"{s.width} × {s.height}"
        return _("Follows the window") if self._embedded() else \
            _("{w} × {h}, follows the window").format(w=s.width, h=s.height)

    def _window(self) -> Optional[Gtk.Window]:
        toplevel = self.get_toplevel()
        return toplevel if isinstance(toplevel, Gtk.Window) else None

    def _embedded(self) -> bool:
        return self.engine is not None and self.engine.embedded

    def _prefs(self) -> Any:
        return getattr(self._app_settings, "remote_desktop", None)

    # ------------------------------------------------------------ status
    def _set_status(self, state: ConnectionStatus, message: str, *, banner: bool = False,
                    error: bool = False) -> None:
        self._status = state
        self._statusbar.set_status(state, message)
        self.status_label.set_text(message)
        self.toolbar.set_status(message)
        if not self._closed:
            if banner:
                self._banner_label.set_text(message)
                self._banner.set_message_type(Gtk.MessageType.ERROR if error else Gtk.MessageType.INFO)
                self._banner.show()
            self._banner.set_revealed(banner)
            show_display = state is ConnectionStatus.CONNECTED and self.surface is not None and \
                (self._embedded_child or isinstance(self.surface, FrdpSurface))
            self.stack.set_visible_child_name("display" if show_display else "card")
            self.emit("status-changed", self._host.alias, state is ConnectionStatus.CONNECTED)
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        running = self.rdp_connected()
        tool = focus_tool()
        external = not self._embedded()
        self.focus_button.set_visible(external)
        self.focus_button.set_sensitive(running and bool(tool))
        self.focus_button.set_tooltip_text(
            _("Bring the remote desktop window to the front") if tool else
            _("Not possible here: Wayland does not let programs raise other windows, and on X11 it needs "
              "xdotool or wmctrl."))
        self.disconnect_button.set_sensitive(running)
        self.reconnect_button.set_sensitive(not self._reconnecting)
        self.cancel_button.set_visible(self._reconnecting)
        self.reconnect_spinner.set_visible(self._reconnecting)
        (self.reconnect_spinner.start if self._reconnecting else self.reconnect_spinner.stop)()
        self._sync_toolbar()

    def _sync_toolbar(self) -> None:
        engine = self.engine
        reasons: Dict[str, str] = {}

        def missing(capability: str, key: str = "") -> None:
            if engine is None or not engine.supports(capability):
                reasons[key or capability] = _("{feature} is not available with {engine}.").format(
                    feature=_(CAPABILITY_LABELS.get(capability, capability)),
                    engine=_(engine.label) if engine else _("this engine"))

        missing("send_keys")
        if engine is not None and engine.id == "xfreerdp-embed":
            reasons["send_keys"] = _("FreeRDP cannot receive keys from another program; while the remote screen has "
                                     "the keyboard, Ctrl+Alt+End sends Ctrl+Alt+Del.")
        missing("screenshot")
        missing("clipboard")
        missing("smart_sizing", "scale")
        if not self.settings.drives:
            reasons["drives"] = _("This host shares no folder (Devices page of the host dialog).")
        self.toolbar.set_capabilities(reasons)
        engine_id = engine.id if engine else "external"
        hint = _(release_hint(engine_id, getattr(self._prefs(), "release_shortcut", None)))
        grabbed = self.keyboard.grabbed
        if isinstance(self.surface, FrdpSurface):
            grabbed = self.surface.keyboard_grabbed()
        self.toolbar.set_keyboard_state(grabbed, hint)

    def _on_banner_response(self, _bar, response: int) -> None:
        if response == _RESPONSE_RECONNECT:
            self.reconnect()
        elif response == _RESPONSE_CLOSE:
            self.emit("close-requested")

    # ------------------------------------------------------- public API
    def focus_terminal(self) -> None:
        if isinstance(self.surface, SocketSurface) and self._embedded_child:
            self.surface.socket.grab_focus()
        elif isinstance(self.surface, FrdpSurface):
            self.surface.display.grab_focus()
        elif self.rdp_connected():
            self.focus_window()
        else:
            self.reconnect_button.grab_focus()

    def focus_window(self) -> bool:
        tool = focus_tool()
        if self.process is None or not tool or self._embedded():
            return False
        pid = self.process.pid
        threading.Thread(target=gc_guard.worker(focus_pid), args=(pid, tool), daemon=True,
                         name="sshm-rdp-focus").start()
        return True

    def disconnect(self) -> None:
        self.cancel_reconnect()
        if self.process is not None and self.process.running:
            self.connection_log.append("Disconnect requested")
            self.process.stop(user=True)
        elif isinstance(self.surface, FrdpSurface):
            self.connection_log.append("Disconnect requested")
            self._user_stop = True
            self.surface.close()

    def reconnect(self) -> None:
        self._reconnect_clicked()

    def _reconnect_clicked(self) -> None:
        self.cancel_reconnect()
        self.scheduler.reset()
        if self.process is not None and self.process.running:
            self._reconnect_after_exit = True
            self.process.stop(user=True)
            return
        if isinstance(self.surface, FrdpSurface) and self._connected:
            self._reconnect_after_exit = True
            self.surface.close()
            return
        if self.status is ConnectionStatus.CONNECTING and not self._banner.get_revealed():
            return
        self._begin_connect()

    def cancel_reconnect(self) -> None:
        if not self._reconnecting:
            return
        self._reconnecting = False
        self.scheduler.cancel()
        self._stop_network_wait()
        self.connection_log.append("Reconnecting cancelled")
        self._set_status(ConnectionStatus.UNKNOWN, _("Reconnecting cancelled."), banner=True)

    def rdp_connected(self) -> bool:
        if self.process is not None and self.process.running:
            return True
        return isinstance(self.surface, FrdpSurface) and self._connected

    def confirm_close(self) -> bool:
        if not self.rdp_connected():
            return True
        return confirm(self._window(), _("Disconnect “{alias}”?").format(alias=self._host.alias),
                       _("The remote desktop session is still open. Closing the tab disconnects it (the programs on "
                         "the remote computer keep running)."),
                       confirm_label=_("_Disconnect and Close"), destructive=True)

    def copy_log(self) -> str:
        from gi.repository import Gdk

        text = self.connection_log.text()
        Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(text, -1)
        return text

    def info_lines(self) -> List[tuple]:
        engine = self.engine
        s = self.settings
        devices = [name for name, on in ((_("clipboard"), s.clipboard != "off"), (_("printers"), s.printers),
                                         (_("sound"), s.audio_out == "local"), (_("microphone"), s.audio_in),
                                         (_("smart cards"), s.smartcard), (_("USB"), bool(s.usb)),
                                         (_("shared folders"), bool(s.drives) or s.home_drive)) if on]
        size = self._statusbar.size_label.get_text() or self._size_text()
        route = _("direct")
        if self.settings.gateway.enabled:
            route = _("through the RD Gateway {host}").format(host=self.settings.gateway.host)
        elif self.tunnel is not None:
            route = _("through an SSH tunnel (127.0.0.1:{port})").format(port=self.tunnel.port)
        return [(_("Server"), self.facts["server"].get_text()), (_("Route"), route),
                (_("Engine"), engine.label if engine else "—"),
                (_("Security"), self.facts["security"].get_text()),
                (_("Certificate"), self.facts["certificate"].get_text()),
                (_("Screen"), size), (_("Colors"), f"{s.color_depth} bit"),
                (_("Connected"), self.connected_method and _("yes ({method})").format(method=self.connected_method)),
                (_("Devices"), ", ".join(devices) or _("none"))]

    # ------------------------------------------------- toolbar actions
    def set_fullscreen(self, active: bool) -> None:
        window = self._window()
        if window is not None:
            (window.fullscreen if active else window.unfullscreen)()
        if self.on_fullscreen is not None:
            self.on_fullscreen(active)
        self.toolbar.set_fullscreen(active)
        if active:
            self.toolbar.conceal()

    def _apply_and_reconnect(self, description: str) -> None:
        self.connection_log.append(f"Changed for this session: {description}")
        self.facts["size"].set_text(self._size_text())
        self._sync_toolbar()
        if self.rdp_connected():
            self._reconnect_clicked()

    def set_scale_mode(self, mode: str) -> None:
        s = self.settings
        if mode == "fit":
            s.size_mode, s.smart_sizing = "window", False
        elif mode == "actual":
            s.size_mode, s.smart_sizing = "fixed", False
        else:
            s.size_mode = "fixed" if s.size_mode == "window" else s.size_mode
            s.smart_sizing = True
        self._apply_and_reconnect(f"scale {mode}")

    def set_resolution(self, size: Optional[tuple]) -> None:
        s = self.settings
        if size is None:
            s.size_mode = "window"
        else:
            s.size_mode, s.width, s.height = "fixed", int(size[0]), int(size[1])
        self._apply_and_reconnect("resolution " + (f"{size[0]}x{size[1]}" if size else "fit"))

    def set_clipboard(self, mode: str) -> None:
        self.settings.clipboard = mode
        self._apply_and_reconnect(f"clipboard {mode}")

    def toggle_keyboard(self) -> None:
        if isinstance(self.surface, FrdpSurface):
            self._sync_toolbar()  # gtk-frdp grabs on its own; the toolbar shows its state
            return
        self.settings.grab_keyboard = not self.settings.grab_keyboard
        self._keyboard_event(self.keyboard.toggle())
        self._apply_and_reconnect("keyboard grab " + ("on" if self.settings.grab_keyboard else "off"))

    def clipboard_policy(self) -> ClipboardPolicy:
        return ClipboardPolicy(self.settings.clipboard, files_supported=False)

    def save_screenshot(self, path: Optional[str] = None) -> Optional[str]:
        pixbuf = self.surface.screenshot() if self.surface is not None else None
        if pixbuf is None:
            self._log(_("No screenshot: the remote screen is not shown in the tab."), Level.WARNING)
            return None
        if path is None:
            chooser = Gtk.FileChooserNative.new(_("Save Screenshot"), self._window(), Gtk.FileChooserAction.SAVE,
                                                _("_Save"), _("_Cancel"))
            chooser.set_do_overwrite_confirmation(True)
            chooser.set_current_name(f"{self._host.alias}-{time.strftime('%Y%m%d-%H%M%S')}.png")
            try:
                if chooser.run() != Gtk.ResponseType.ACCEPT:
                    return None
                path = chooser.get_filename()
            finally:
                chooser.destroy()
        if not path:
            return None
        try:
            pixbuf.savev(path, "png", [], [])
        except GLib.Error as exc:
            self._log(_("The screenshot could not be saved: {error}").format(error=exc.message), Level.ERROR)
            return None
        self._log(_("Screenshot saved."), Level.SUCCESS)
        return path

    def open_shared_folder(self, name: Optional[str] = None) -> bool:
        folder = next((d for d in self.settings.drives if (name is None or d.name == name)
                       and os.path.isdir(os.path.expanduser(d.path))), None)
        if folder is None:
            return False
        uri = Gio.File.new_for_path(os.path.expanduser(folder.path)).get_uri()
        try:
            Gio.AppInfo.launch_default_for_uri(uri, None)
        except GLib.Error as exc:
            self._log(_("The folder could not be opened: {error}").format(error=exc.message), Level.WARNING)
            return False
        return True

    # -------------------------------------------------------- connecting
    def _begin_connect(self) -> bool:
        if self._closed:
            return GLib.SOURCE_REMOVE
        self._flow.begin_connect()
        self._start(None, "stored")
        return GLib.SOURCE_REMOVE

    def _session_type(self, report: EngineReport) -> str:
        return report.platform.session_type or ("x11" if report.platform.gdk_x11 else "unknown")

    def _choose_engine(self) -> Optional[Engine]:
        report = self._report_provider()
        if report is None:
            self._fail(_("FreeRDP is still being looked for; try again in a moment."), "no report")
            return None
        prefs = self._prefs()
        engine, note = choose_engine(report.engines, getattr(prefs, "preferred_engine", "auto"),
                                     self.settings.engine)
        if engine is None:
            self._fail(_("No FreeRDP client was found. {hint}").format(hint=install_hint(report.platform)),
                       "no engine")
            return None
        external = next((e for e in report.engines if e.id == "external" and e.available), None)
        if engine.embedded and (self._force_external or
                                self._runtime.embed_failed(engine.id, self._session_type(report))):
            if external is None:
                self._fail(_("Embedding the remote desktop failed before, and no FreeRDP client for an external "
                             "window was found."), "engine")
                return None
            self.connection_log.append(f"{engine.label} failed here before; using the external window")
            engine = external
        if engine.embedded and not self.get_realized():
            engine = external or engine  # not in a window yet: nothing to embed into
        if note:
            self.connection_log.append(note)
        return engine

    def _start(self, typed: Optional[str], origin: str) -> None:
        self._banner.set_revealed(False)
        engine = self._choose_engine()
        if engine is None:
            return
        if engine.binary is None and engine.id != "frdp":
            return
        self.engine = engine
        binary = engine.binary
        self.facts["engine"].set_text(engine.label + (f" · {binary.name} {binary.version_text}" if binary else ""))
        if typed is None:
            if self.settings.prompt_for_credentials and not self._typed_password:
                self._ask_password()
                return
            try:
                self._resolution = self._password_provider() if self._password_provider else NO_PASSWORD
            except Exception as exc:  # storage problems never block a connect
                LOGGER.warning("password resolution failed: %s", type(exc).__name__)
                self._resolution = NO_PASSWORD
            password = self._typed_password or self._resolution.password
            source = "session" if self._typed_password else self._resolution.source
            if not password:
                self._ask_password()
                return
        else:
            password, source = typed, origin
        self._flow.attempt(source, password if source in ("prompt", "retry") else None,
                           stored=self._resolution.password, resolved=True)
        self._attempt_password = password
        if not self._runtime.limiter.acquire(self):
            self._fail(_("At most {n} remote desktop sessions can run at once (Preferences → Remote Desktop).")
                       .format(n=self._runtime.limiter.limit), "session limit")
            return
        self._close_tunnel()
        if self.settings.via_ssh and not self.settings.gateway.enabled:
            self._open_tunnel()
            return
        self._probe_now()

    def _pins(self) -> List[str]:
        return list(self.settings.pinned_sha256) + [p for p in (self._stored_pin(),) if p] + self._session_pins

    def _pin_key(self) -> str:
        hostname, port, _user = host_connection_params(self._host)
        return pin_key(getattr(self._host, "id", "") or self._host.alias, hostname, port, scope="rdp")

    def _stored_pin(self) -> str:
        try:
            return self._runtime.trust.get(self._pin_key())
        except Exception:
            return ""

    def _remember(self, fingerprint: str, subject: str, key: Optional[str] = None) -> None:
        try:
            self._runtime.trust.set(key or self._pin_key(), fingerprint, subject)
            self.connection_log.append(f"Certificate remembered: {fingerprint}")
        except (OSError, ValueError) as exc:
            self._log(_("Could not remember the certificate: {error}").format(error=type(exc).__name__),
                      Level.WARNING)

    # ------------------------------------------------- tunnel and probe
    def _target(self) -> tuple:
        """Where FreeRDP and the probe connect: the server, or the SSH tunnel's loopback port."""

        if self.tunnel is not None:
            return "127.0.0.1", self.tunnel.port
        hostname, port, _user = host_connection_params(self._host)
        return hostname, port

    def _open_tunnel(self) -> None:
        if self._tunnel_factory is None:
            self._runtime.limiter.release(self)
            self._fail(_("This window cannot open SSH tunnels."), "tunnel", kind=Kind.NETWORK)
            return
        hostname, port, _user = host_connection_params(self._host)
        self._set_status(ConnectionStatus.CONNECTING, _("Opening the SSH tunnel…"))
        factory = self._tunnel_factory
        via = self.settings.via_ssh

        def work() -> None:
            try:
                backend = factory(via)
                tunnel = open_tunnel(backend, hostname, port, log=self.connection_log)
            except UnknownHostKey as exc:
                GLib.idle_add(self._tunnel_host_key, exc)
                return
            except Exception as exc:  # BackendError, LookupError …: explained, never a traceback
                GLib.idle_add(self._tunnel_failed, exc)
                return
            GLib.idle_add(self._tunnel_ready, tunnel)

        threading.Thread(target=gc_guard.worker(work), daemon=True, name="sshm-rdp-tunnel-open").start()

    def _tunnel_host_key(self, exc: Any) -> bool:
        if self._closed:
            self._runtime.limiter.release(self)
            return GLib.SOURCE_REMOVE
        from ..dialogs.trust import trust_host_key

        label = _("{host} (SSH tunnel)").format(host=exc.hostname)
        if not trust_host_key(self._window(), label, exc.key_type, exc.fingerprint):
            return self._cancelled(_("The SSH host's key was not accepted."))
        try:
            factory = self._tunnel_factory
            assert factory is not None
            known = getattr(factory(self.settings.via_ssh), "accept_host_key")
            known(exc, remember=True)
        except Exception as error:
            self._runtime.limiter.release(self)
            self._fail(_("Could not remember the SSH host key: {error}").format(error=type(error).__name__),
                       "tunnel host key", kind=Kind.NETWORK)
            return GLib.SOURCE_REMOVE
        self._open_tunnel()
        return GLib.SOURCE_REMOVE

    def _tunnel_failed(self, exc: BaseException) -> bool:
        self._runtime.limiter.release(self)
        if self._closed:
            return GLib.SOURCE_REMOVE
        if isinstance(exc, AuthError):
            message = _("The SSH host for the tunnel did not accept the login.")
        elif isinstance(exc, LookupError):
            message = _("The SSH host for the tunnel no longer exists; choose another one on the RDP Connection page.")
        elif isinstance(exc, BackendError):
            message = _("The SSH tunnel failed: {error}").format(error=_(exc.message))
        else:
            message = _("The SSH tunnel failed: {error}").format(error=type(exc).__name__)
        self._fail(message, "tunnel: " + (getattr(exc, "technical", "") or type(exc).__name__), kind=Kind.NETWORK)
        return GLib.SOURCE_REMOVE

    def _tunnel_ready(self, tunnel: Any) -> bool:
        if self._closed:
            tunnel.close()
            self._runtime.limiter.release(self)
            return GLib.SOURCE_REMOVE
        self.tunnel = tunnel
        self._probe_now()
        return GLib.SOURCE_REMOVE

    def _close_tunnel(self) -> None:
        tunnel, self.tunnel = self.tunnel, None
        if tunnel is not None:
            tunnel.close()

    def _probe_now(self) -> None:
        self._set_status(ConnectionStatus.CONNECTING, _("Checking the server…"))
        settings = self.settings
        connect_host, connect_port = self._target()
        pins = tuple(self._pins())
        gateway = settings.gateway

        def work() -> None:
            result: Optional[ProbeResult] = None
            gateway_result: Optional[ProbeResult] = None
            try:
                if gateway.enabled:  # the server sits behind the gateway: only the gateway can be checked first
                    gateway_result = self._tls_probe(gateway.host, gateway.port,
                                                     timeout=float(settings.connect_timeout_s),
                                                     pinned=tuple(self._gateway_pins()))
                else:
                    result = self._probe(connect_host, connect_port, timeout=float(settings.connect_timeout_s),
                                         pinned=pins, configured_security=settings.security)
            except Exception as exc:  # never let a worker die silently
                result = ProbeResult(connect_host, connect_port, error=f"probe failed: {type(exc).__name__}")
            GLib.idle_add(self._after_probe, result, gateway_result)

        threading.Thread(target=gc_guard.worker(work), daemon=True, name="sshm-rdp-probe").start()

    def _gateway_key(self) -> str:
        gateway = self.settings.gateway
        return pin_key(getattr(self._host, "id", "") or self._host.alias, gateway.host, gateway.port, scope="rdp-gw")

    def _gateway_pins(self) -> List[str]:
        try:
            stored = self._runtime.trust.get(self._gateway_key())
        except Exception:
            stored = ""
        return [p for p in (stored,) if p] + self._session_pins

    def _trust(self, decision: Any, cert: Any, label: str, key: str) -> Optional[str]:
        """Show the trust dialogs a decision needs; the accepted fingerprint, or ``None`` (refused / cancelled).

        ``CONFIRM_IGNORE`` returns ``"ignore"``; ``NO_TLS`` returns ``""``.
        """

        action = decision.action
        if action is Action.CONNECT:
            return decision.fingerprint
        if action is Action.REMEMBER and cert is not None:
            self._remember(decision.fingerprint, cert.subject, key)
            return decision.fingerprint
        if action is Action.ASK and cert is not None:
            choice = rdp_trust_choice(self._window(), label, cert)
            if choice == "cancel":
                self._cancelled(_("The certificate was not trusted."))
                return None
            if choice == "remember":
                self._remember(decision.fingerprint, cert.subject, key)
            else:
                self._session_pins.append(decision.fingerprint)
            return decision.fingerprint
        if action is Action.CHANGED and cert is not None:
            if not trust_certificate(self._window(), label, cert, changed=True, expected=decision.expected):
                self._cancelled(_("The server's certificate has changed; not connected."), error=True)
                return None
            self._remember(decision.fingerprint, cert.subject, key)
            return decision.fingerprint
        if action is Action.REFUSE:
            self._runtime.limiter.release(self)
            self._fail(_("The server's certificate is not a trusted one, and this host refuses unknown "
                         "certificates."), "certificate refused", kind=Kind.CERT_REJECTED)
            return None
        if action is Action.CONFIRM_IGNORE:
            if not confirm_unchecked_certificate(self._window(), label):
                self._cancelled(_("Connection cancelled."))
                return None
            return "ignore"
        if action is Action.NO_TLS:
            return ""
        return None  # UNREADABLE is handled by the caller

    def _after_probe(self, result: Optional[ProbeResult], gateway: Optional[ProbeResult] = None) -> bool:
        if self._closed:
            self._runtime.limiter.release(self)
            return GLib.SOURCE_REMOVE
        if gateway is not None:
            return self._after_gateway_probe(gateway)
        assert result is not None
        for line in result.lines():
            self.connection_log.append("probe: " + line)
        if not result.reachable:
            self._runtime.limiter.release(self)
            self._fail(_("The server could not be reached: {error}").format(error=result.error or _("no answer")),
                       "probe: unreachable", kind=Kind.NETWORK, outcome_connected=False)
            return GLib.SOURCE_REMOVE
        self._latency = result.latency_ms if self.tunnel is None else None  # through a tunnel it means nothing
        security = "rdp" if (self.settings.security == "rdp" or result.security == "rdp") else result.security
        self.security = security
        self.facts["security"].set_text(result.security_label or _("unknown"))
        if not result.security and result.failure:
            self._runtime.limiter.release(self)
            self._fail(_("The server refused the security request: {error}").format(error=result.error),
                       result.failure, kind=Kind.SECURITY_MISMATCH)
            return GLib.SOURCE_REMOVE
        decision = decide(self.settings.cert_policy, result.certificate, self._pins(), security=security)
        if decision.action is Action.UNREADABLE:
            self._runtime.limiter.release(self)
            self._fail(_("The server's certificate could not be read: {error}").format(
                error=result.error or _("no TLS answer")), "certificate unreadable", kind=Kind.TLS)
            return GLib.SOURCE_REMOVE
        accepted = self._trust(decision, result.certificate, self._host.alias, self._pin_key())
        if accepted is None:
            return GLib.SOURCE_REMOVE
        ignore = accepted == "ignore"
        fingerprints = [result.certificate.sha256] if ignore and result.certificate else             ([accepted] if accepted else [])
        self.certificate_checked = not ignore and security != "rdp"
        self.facts["certificate"].set_text(
            _("Not verified") if ignore else (_("Not used (standard RDP security)") if security == "rdp"
                                              else _("Trusted · {fp}").format(fp=fingerprints[0][:23] + "…")))
        if self.engine is not None and self.engine.id == "frdp":
            self._launch_frdp(fingerprints, ignore)
        else:
            self._launch(fingerprints, ignore)
        return GLib.SOURCE_REMOVE

    def _after_gateway_probe(self, gateway: ProbeResult) -> bool:
        for line in gateway.lines():
            self.connection_log.append("gateway probe: " + line)
        if not gateway.reachable:
            self._runtime.limiter.release(self)
            self._fail(_("The RD Gateway could not be reached: {error}").format(error=gateway.error or _("no answer")),
                       "gateway unreachable", kind=Kind.NETWORK)
            return GLib.SOURCE_REMOVE
        policy = self.settings.cert_policy
        decision = decide(policy, gateway.certificate, self._gateway_pins())
        if decision.action is Action.UNREADABLE:
            self._runtime.limiter.release(self)
            self._fail(_("The RD Gateway's certificate could not be read: {error}").format(
                error=gateway.error or _("no TLS answer")), "gateway certificate unreadable", kind=Kind.TLS)
            return GLib.SOURCE_REMOVE
        label = _("{host} (RD Gateway)").format(host=self.settings.gateway.host)
        accepted = self._trust(decision, gateway.certificate, label, self._gateway_key())
        if accepted is None:
            return GLib.SOURCE_REMOVE
        self.security = ""  # the server's security is negotiated through the gateway, unseen by the app
        self.facts["security"].set_text(_("Through the RD Gateway"))
        if accepted == "ignore":
            self.certificate_checked = False
            self.facts["certificate"].set_text(_("Not verified"))
            self._launch([], True)
            return GLib.SOURCE_REMOVE
        server_pins = self._pins()
        tofu = policy in ("ask", "tofu", "pinned") and not server_pins
        if policy == "deny" and not server_pins:
            self._runtime.limiter.release(self)
            self._fail(_("This host refuses unknown certificates, and the server behind the RD Gateway has no "
                         "pinned certificate."), "no server pin", kind=Kind.CERT_REJECTED)
            return GLib.SOURCE_REMOVE
        self.facts["certificate"].set_text(
            _("Gateway trusted; server: trusted on first use by FreeRDP") if tofu else
            _("Gateway and server trusted"))
        self._launch(server_pins + [accepted], False, tofu=tofu)
        return GLib.SOURCE_REMOVE

    def _cancelled(self, message: str, *, error: bool = False) -> bool:
        self._runtime.limiter.release(self)
        self._close_tunnel()
        self._reconnecting = False
        self._set_status(ConnectionStatus.FAILED if error else ConnectionStatus.UNKNOWN, message, banner=True,
                         error=error)
        return GLib.SOURCE_REMOVE

    # ------------------------------------------------------- surfaces
    def _default_surface(self, engine: Engine) -> Optional[Surface]:
        if engine.id == "xfreerdp-embed":
            return SocketSurface()
        if engine.id == "frdp":
            try:
                report = self._report_provider()
                gi.require_version("Frdp", report.platform.frdp_version if report else "0.2")
                from gi.repository import Frdp  # type: ignore

                return FrdpSurface(Frdp.Display)
            except (ImportError, ValueError, AttributeError) as exc:
                LOGGER.warning("gtk-frdp could not be loaded: %s", type(exc).__name__)
        return None

    def _install_surface(self, engine: Engine) -> Optional[Surface]:
        self._remove_surface()
        surface = self._surface_factory(engine)
        if surface is None:
            return None
        self.surface = surface
        self.display_box.pack_start(surface, True, True, 0)
        surface.show_all()
        if isinstance(surface, SocketSurface):
            surface.on_embedded = self._on_embedded
            surface.on_timeout = self._on_embed_timeout
        target = surface.socket if isinstance(surface, SocketSurface) else getattr(surface, "display", None)
        if target is not None:
            target.connect("focus-in-event", self._on_surface_focus_in)
            target.connect("focus-out-event", self._on_surface_focus_out)
        return surface

    def _on_surface_focus_in(self, *_args) -> bool:
        self._keyboard_event(self.keyboard.focus_in())
        return False

    def _on_surface_focus_out(self, *_args) -> bool:
        self._keyboard_event(self.keyboard.focus_out())
        return False

    def _keyboard_event(self, effects: List[Effect]) -> None:
        """The grab indicator follows the focus; the engine itself grabs and releases the keys (docs/rdp.md)."""

        if Effect.RELEASE_MODIFIERS in effects:
            self.connection_log.append("Focus left the remote screen: modifier keys released")
        if not self._closed and effects:
            self._sync_toolbar()

    def _remove_surface(self) -> None:
        surface, self.surface = self.surface, None
        self._embedded_child = 0
        if surface is not None:
            self.display_box.remove(surface)
            surface.destroy()

    def _on_embedded(self, child: int) -> None:
        self._embedded_child = child
        self.connection_log.append("The remote screen is shown in the tab")
        self.facts["size"].set_text(self._size_text())
        self._set_status(self._status, self.status_label.get_text())  # switch to the display
        self.focus_terminal()

    def _on_embed_timeout(self) -> None:
        """No FreeRDP window in the tab: remember it and continue in an external window."""

        report = self._report_provider()
        engine = self.engine
        if engine is not None and report is not None:
            self._runtime.note_embed_failure(engine.id, self._session_type(report))
        self.connection_log.append("FreeRDP's window did not appear in the tab; switching to an external window")
        self._log(_("“{alias}”: the remote desktop could not be shown in the tab; it opens in its own window "
                    "instead.").format(alias=self._host.alias), Level.WARNING)
        self._force_external = True
        self._reconnect_clicked()

    # ---------------------------------------------------------- launch
    def _launch(self, fingerprints: List[str], ignore: bool, *, tofu: bool = False) -> None:
        engine = self.engine
        assert engine is not None and engine.binary is not None
        real_host, _real_port, _user = host_connection_params(self._host)
        hostname, port = self._target()
        gateway = self.settings.gateway
        gateway_password = None
        if gateway.enabled and not gateway.use_same_credentials and gateway.credential_id:
            try:
                gateway_password = self._gateway_password_provider(gateway.credential_id) \
                    if self._gateway_password_provider else None
            except Exception as exc:  # a locked store: FreeRDP asks itself, nothing breaks
                LOGGER.warning("gateway password unavailable: %s", type(exc).__name__)
        identity = resolve_identity(self._host.option("user") or "", self.settings)
        parent = None
        window_size = None
        if engine.id == "xfreerdp-embed":
            surface = self._install_surface(engine)
            if isinstance(surface, SocketSurface):
                allocation = self.stack.get_allocation()
                scale = max(1, self.get_scale_factor())
                window_size = (max(200, allocation.width * scale), max(200, allocation.height * scale))
                parent = surface.xid()
        else:
            self._remove_surface()
        try:
            plan = build_launch(hostname=hostname, port=port, settings=self.settings, identity=identity,
                                password=self._attempt_password, client=engine.binary, title=self._host.alias,
                                accepted_fingerprints=fingerprints if not ignore else [], ignore_certificate=ignore,
                                parent_window=parent, window_size=window_size, gateway_password=gateway_password,
                                server_name=real_host if self.tunnel is not None else "",
                                latency_ms=self._latency, cert_tofu=tofu)
        except LaunchError as exc:
            self._runtime.limiter.release(self)
            self._fail(_("Cannot start the session: {error}").format(error=_(str(exc))), "launch")
            return
        for note in plan.notes:
            self.connection_log.append("note: " + note)
        self.connection_log.append(f"Starting {engine.binary.name} {engine.binary.version_text} "
                                   f"(password via {plan.channel if plan.password_sent else 'nothing'})")
        self.connection_log.append("Options: " + " ".join(plan.display_args))
        self._connected = False
        self.connected_method = ""
        self._set_status(ConnectionStatus.CONNECTING, _("Connecting…"))
        try:
            process = self._process_factory(plan, on_line=self._on_output, on_exit=self._on_exit_thread,
                                            registry=self._runtime.registry)
        except OSError as exc:
            self._runtime.limiter.release(self)
            self._fail(_("FreeRDP could not be started: {error}").format(error=exc.strerror or type(exc).__name__),
                       "spawn")
            return
        del plan  # the payload (with the password) is not kept
        self.process = process
        self._runtime.track(process)
        self._sources.append(GLib.timeout_add_seconds(1, self._tick))
        self._sync_buttons()

    def _launch_frdp(self, fingerprints: List[str], ignore: bool) -> None:
        engine = self.engine
        assert engine is not None
        surface = self._install_surface(engine)
        if not isinstance(surface, FrdpSurface):
            self._runtime.limiter.release(self)
            self._force_external = True
            self.connection_log.append("gtk-frdp could not be used; switching to an external window")
            self._begin_connect()
            return
        hostname, port, _user = host_connection_params(self._host)
        identity = resolve_identity(self._host.option("user") or "", self.settings)
        surface.accepted = list(fingerprints)
        surface.on_connected = lambda: self._mark_connected("signal")
        surface.on_disconnected = lambda: self._frdp_finished(None)
        surface.on_error = lambda message: self._frdp_finished(("error", message))
        surface.on_auth_failure = lambda message: self._frdp_finished(("auth", message))
        surface.on_certificate_refused = lambda _fp: self._frdp_finished(("cert", ""))
        self._connected = False
        self.connected_method = ""
        self._user_stop = False
        self._set_status(ConnectionStatus.CONNECTING, _("Connecting…"))
        self.connection_log.append(f"Starting gtk-frdp (password as a property, never a command line)")
        surface.open(hostname, port, identity.user, self._attempt_password, identity.domain,
                     self.settings.smart_sizing or self.settings.size_mode == "window")

    def _frdp_finished(self, problem: Optional[tuple]) -> None:
        if self._closed:
            return
        self._runtime.limiter.release(self)
        if problem is None:
            reason = "user_disconnect" if getattr(self, "_user_stop", False) else "closed"
            outcome = RdpOutcome(Kind.OK_DISCONNECT, reason, "The session ended.", "rdp-disconnected",
                                 self._connected)
        elif problem[0] == "auth":
            outcome = RdpOutcome(Kind.AUTH_FAILED, "auth", "The user name or password is not correct.",
                                 "rdp-auth-failure", self._connected)
        elif problem[0] == "cert":
            outcome = RdpOutcome(Kind.CERT_REJECTED, "certificate",
                                 "The server's certificate was not the trusted one, so the connection was stopped.",
                                 "certificate", self._connected)
        else:
            outcome = RdpOutcome(Kind.NETWORK if self._connected else Kind.OTHER, "error",
                                 problem[1] or "The connection failed.", "rdp-error", self._connected)
        self._remove_surface()
        self._handle_outcome(outcome)

    # ------------------------------------------------------------ output
    def _on_output(self, line: str) -> None:
        """Worker thread: log the (already masked) line; a "connected" line goes to the GTK thread."""

        self.connection_log.append(line)
        if connected_line(line):
            GLib.idle_add(self._mark_connected, "log")

    def _tick(self) -> bool:
        if self._closed or not self.rdp_connected():
            return GLib.SOURCE_REMOVE
        process = self.process
        if self._connected:
            seconds = int(time.monotonic() - self._connected_at)
            self.facts["elapsed"].set_text(f"{seconds // 3600}:{seconds // 60 % 60:02d}:{seconds % 60:02d}")
        elif process is not None and process.alive_s() >= HEURISTIC_S and \
                not any("[ERROR]" in line for line in process.output()):
            self._mark_connected("heuristic")
        return GLib.SOURCE_CONTINUE

    def _mark_connected(self, method: str) -> bool:
        if self._closed or self._connected or (self.process is None and not isinstance(self.surface, FrdpSurface)):
            return GLib.SOURCE_REMOVE
        self._connected = True
        self._reconnecting = False
        self.scheduler.succeeded()
        self._stop_network_wait()
        self._connected_at = time.monotonic()
        self.connected_method = method
        self.connection_log.append({"log": "Connected (verified by FreeRDP's log)",
                                    "signal": "Connected (gtk-frdp reported it)"}.get(
            method, f"Probably connected (no error for {HEURISTIC_S} s; FreeRDP did not log the connection)"))
        if isinstance(self.surface, FrdpSurface):
            self._sources.append(GLib.timeout_add_seconds(1, self._tick))
        state = _("Connected") if self.certificate_checked else _("Connected · certificate not verified")
        where = "" if self._embedded() else " · " + _("the remote desktop is in its own window")
        self._set_status(ConnectionStatus.CONNECTED, state + where)
        if isinstance(self.surface, SocketSurface):
            self.surface.start_watch()
        verified_login = method in ("log", "signal") and self.security in ("nla", "nla_ext")
        if self._attempt_password and self._flow.source in ("prompt", "retry"):
            self._typed_password = self._attempt_password  # Reconnect keeps using it
            if verified_login:
                self._offer_save(self._attempt_password)
        self._attempt_password = None
        return GLib.SOURCE_REMOVE

    def _on_exit_thread(self, code: int) -> None:
        GLib.idle_add(self._finished, code)

    def _finished(self, code: int) -> bool:
        process = self.process
        self._runtime.limiter.release(self)
        if process is None:
            return GLib.SOURCE_REMOVE
        version = self.engine.binary.major if self.engine and self.engine.binary else 3
        outcome = classify_rdp_exit(code, process.output(), version, connected=self._connected,
                                    stopped_by_user=process.stopped_by_user)
        self.process = None
        self._remove_surface()
        if self._closed:
            return GLib.SOURCE_REMOVE
        self._handle_outcome(outcome)
        return GLib.SOURCE_REMOVE

    def _handle_outcome(self, outcome: RdpOutcome) -> None:
        self._close_tunnel()  # a new attempt opens a new one
        self.last_outcome = outcome
        self.connection_log.append(f"Ended: {outcome.kind.value} ({outcome.technical})")
        was_connected = self._connected
        self._connected = False
        if self._reconnect_after_exit:
            self._reconnect_after_exit = False
            self._begin_connect()
            return
        if outcome.retry_password and not was_connected:
            self._typed_password = None
            action = self._flow.on_failure(AuthOutcome.AUTH_FAILED)
            if action is FailureAction.PROMPT:
                self._set_status(ConnectionStatus.FAILED, _(outcome.message))
                self._sources.append(GLib.timeout_add_seconds(RETRY_DELAY_S, self._ask_after_delay))
                return
            self._fail(_("Sign-in failed after {n} attempts. Use Reconnect to try again.").format(
                n=self._flow.max_attempts), outcome.technical, kind=Kind.AUTH_FAILED)
            self.emit("session-closed", self._host.alias, False)
            return
        plan = plan_for(outcome, enabled=self.settings.auto_reconnect)
        if plan is Plan.AUTO and (was_connected or self._reconnecting):
            if self._schedule_reconnect(outcome):
                return
        if outcome.kind is Kind.OK_DISCONNECT:
            text = _("Disconnected: {reason}").format(reason=_(outcome.message))
            if plan is Plan.ASK:
                text += " " + _("Use Reconnect to connect again.")
            self._set_status(ConnectionStatus.UNKNOWN, text, banner=outcome.reason != "user_disconnect")
        else:
            self._fail(_(outcome.message), outcome.technical, kind=outcome.kind)
        self._reconnecting = False
        self._sync_buttons()
        self.emit("session-closed", self._host.alias, not outcome.is_error)

    # --------------------------------------------------------- reconnect
    def _schedule_reconnect(self, outcome: RdpOutcome) -> bool:
        delay = self.scheduler.next_delay()
        if delay is None:
            self.connection_log.append("Reconnect attempts used up")
            self._reconnecting = False
            return False
        self._reconnecting = True
        self.connection_log.append(f"Reconnecting in {delay:.0f} s ({self.scheduler.progress()})")
        self._set_status(ConnectionStatus.CONNECTING, _("Connection lost: {reason} Reconnecting… (attempt {n} of {m})")
                         .format(reason=_(outcome.message), n=self.scheduler.attempts, m=self.scheduler.limit))
        self._sources.append(GLib.timeout_add(int(delay * 1000), self._reconnect_due))
        return True

    def _reconnect_due(self) -> bool:
        if self._closed or not self._reconnecting:
            return GLib.SOURCE_REMOVE
        monitor = Gio.NetworkMonitor.get_default()
        if monitor is not None and not monitor.get_network_available():
            self.connection_log.append("The network is down; waiting for it before reconnecting")
            self.scheduler.attempts = max(0, self.scheduler.attempts - 1)  # waiting is not an attempt
            if not self._network_handler:
                self._network_handler = monitor.connect("network-changed", self._on_network_changed)
            return GLib.SOURCE_REMOVE
        self._begin_connect()
        return GLib.SOURCE_REMOVE

    def _on_network_changed(self, monitor, available: bool) -> None:
        if not available or self._closed or not self._reconnecting:
            return
        self._stop_network_wait()
        self._begin_connect()

    def _stop_network_wait(self) -> None:
        if self._network_handler:
            monitor = Gio.NetworkMonitor.get_default()
            if monitor is not None:
                monitor.disconnect(self._network_handler)
            self._network_handler = 0

    def _fail(self, message: str, technical: str = "", *, kind: Kind = Kind.OTHER,
              outcome_connected: bool = False) -> None:
        if technical:
            self.connection_log.append(f"Failed: {technical}")
        self._close_tunnel()
        self.last_outcome = RdpOutcome(kind, technical or "failed", message, technical, outcome_connected)
        if self._reconnecting and kind is Kind.NETWORK and self._schedule_reconnect(self.last_outcome):
            return
        self._reconnecting = False
        self._set_status(ConnectionStatus.FAILED, message, banner=True, error=True)

    # --------------------------------------------------------- password
    def _ask_after_delay(self) -> bool:
        if not self._closed:
            self._ask_password(self._rejected_phrase())
        return GLib.SOURCE_REMOVE

    def _rejected_phrase(self) -> str:
        source = self._flow.source
        if source == self._resolution.source and self._password_service is not None:
            try:
                return self._password_service.describe_password_source(self._resolution)
            except Exception:
                pass
        return source_phrase(source) or source_phrase("stored")

    def _ask_password(self, rejected: str = "") -> None:
        hostname, port, user = host_connection_params(self._host)
        request = self._flow.next_request(f"{user}@{hostname}:{port}" if user else f"{hostname}:{port}", rejected)
        dialog = PasswordRequestDialog(self._window(), request,
                                       functools.partial(self._on_password_answer, retry=bool(rejected)))
        self._dialogs.append(dialog)

    def _on_password_answer(self, password: Optional[str], *, retry: bool) -> None:
        if self._closed:
            return
        if not password:
            self._reconnecting = False
            self._set_status(ConnectionStatus.FAILED if retry else ConnectionStatus.UNKNOWN,
                             _("Sign-in failed") if retry else _("Connection cancelled"), banner=True, error=retry)
            return
        self._start(password, "retry" if retry else "prompt")

    def _offer_save(self, password: str) -> None:
        if self._password_service is None:
            return
        try:
            context = self._password_service.password_save_context(self._host)
        except Exception:
            context = SaveContext(can_save=False, reason=_("Saved passwords are unavailable right now."))
        offer, notice = self._flow.on_authenticated("sentinel", context)
        if notice:
            self._log(_(notice), Level.INFO)
        if offer is not None:
            self._show_save_dialog(offer, password)

    def _show_save_dialog(self, offer: SaveOffer, password: str) -> None:
        dialog = SavePasswordDialog(self._window(), offer, functools.partial(self._on_save_decision,
                                                                             password=password))
        self._dialogs.append(dialog)

    def _on_save_decision(self, decision: Decision, *, password: str) -> Optional[str]:
        self._flow.decide(decision)
        if decision is not Decision.SAVE or self._password_service is None:
            return None
        try:
            status, message = self._password_service.save_host_password(self._host, password)
        except Exception as exc:
            LOGGER.warning("saving the host password failed: %s", type(exc).__name__)
            status, message = "error", _("Could not save the password.")
        if status == "error":
            self._log(message, Level.WARNING)
            return message
        self._log(message, Level.SUCCESS if status == "saved" else Level.WARNING)
        return None

    # -------------------------------------------------------------- log
    def _on_log_line(self, _line: str) -> None:
        if not self._log_flush_pending:
            self._log_flush_pending = True
            GLib.idle_add(self._flush_log)

    def _flush_log(self) -> bool:
        self._log_flush_pending = False
        if self._closed or not self.log_expander.get_expanded():
            return GLib.SOURCE_REMOVE
        buffer = self.log_view.get_buffer()
        buffer.set_text(self.connection_log.text())
        buffer.place_cursor(buffer.get_end_iter())
        self.log_view.scroll_mark_onscreen(buffer.get_insert())
        return GLib.SOURCE_REMOVE

    # -------------------------------------------------------- lifecycle
    def _on_destroy(self) -> None:
        self._closed = True
        self._reconnecting = False
        self._stop_network_wait()
        context = GLib.MainContext.default()
        for source in self._sources:
            if context.find_source_by_id(source) is not None:  # finished sources are already gone
                GLib.source_remove(source)
        self._sources.clear()
        for dialog in self._dialogs:
            dismiss = getattr(dialog, "dismiss", None)
            try:
                dismiss() if callable(dismiss) else dialog.destroy()
            except Exception:
                pass
        self._dialogs.clear()
        process = self.process
        if process is not None and process.running:
            process.stop(user=True)  # the whole process group; SIGKILL after the grace period
        if isinstance(self.surface, FrdpSurface):
            self.surface.close()
        self._close_tunnel()
        self._runtime.limiter.release(self)
        self._typed_password = self._attempt_password = None


__all__ = ["RdpSessionTab", "HEURISTIC_S"]
