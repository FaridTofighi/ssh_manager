"""An RDP session tab. In phase R1 the remote desktop runs in FreeRDP's own window, so the tab is a
**session card**: status, server, engine, security, size, time connected, the connection log, and
**Focus Window**, **Disconnect** and **Reconnect**.

One connection attempt goes like this (``docs/rdp.md``):

1. **Password:** the stored one (vault → credential → host password), or the
   password dialog ("Ask every time", or nothing stored).
2. **Probe** (worker thread): is the server reachable, which security does it
   select, and what certificate does it present?
3. **Trust** (:mod:`ssh_manager.rdp.trust_flow`): a pinned certificate
   connects, a new one asks (once / remember), a changed one warns (Cancel is
   the default), "Do not check" asks every time.
4. **Launch** (:mod:`ssh_manager.rdp.command` / :mod:`ssh_manager.rdp.process`):
   the password reaches FreeRDP only through its stdin pipe.
5. **Verified success:** FreeRDP's "Local framebuffer format" log line (the
   session is active), or after :data:`HEURISTIC_S` seconds without errors.
   Only a log-verified NLA login offers "Save this password for this host?".
6. **End:** the exit is classified; only "wrong password" asks again (at most
   three times, at least a second apart). Account, network, certificate and
   security problems show their own message and keep **Reconnect**.

Threads never touch GTK: results come back through ``GLib.idle_add``, and
every timeout source is removed when the tab is destroyed. Closing the tab
stops FreeRDP's process group.
"""

from __future__ import annotations

import functools
import logging
import threading
import time
from typing import Any, Callable, List, Optional

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk, Pango  # type: ignore  # noqa: E402

from ...auth_flow import (  # noqa: E402
    NO_PASSWORD, RETRY_DELAY_S, AuthOutcome, Decision, FailureAction, PasswordFlow, PasswordResolution, SaveContext,
    SaveOffer, source_phrase,
)
from ...host_utils import host_connection_params  # noqa: E402
from ...rdp.command import LaunchError, build_launch  # noqa: E402
from ...rdp.engines import Engine, EngineReport, cached_report, choose_engine, install_hint  # noqa: E402
from ...rdp.outcome import Kind, RdpOutcome, classify_rdp_exit, connected_line  # noqa: E402
from ...rdp.probe import ProbeResult, probe  # noqa: E402
from ...rdp.process import RdpProcess  # noqa: E402
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
from .runtime import RdpRuntime  # noqa: E402

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
                 process_factory: Optional[Callable[..., RdpProcess]] = None, autostart: bool = True) -> None:
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
        self.settings, warnings = RdpSettings.for_host(host)
        self.connection_log = ConnectionLog(host.alias)
        self._flow = PasswordFlow(host.alias)
        self._resolution: PasswordResolution = NO_PASSWORD
        self._typed_password: Optional[str] = None
        self._attempt_password: Optional[str] = None
        self.process: Optional[RdpProcess] = None
        self.engine: Optional[Engine] = None
        self.security = ""  # what the probe saw the server select
        self.certificate_checked = True
        self.connected_method = ""  # "log" | "heuristic" | ""
        self.last_outcome: Optional[RdpOutcome] = None
        self._connected = False
        self._connected_at = 0.0
        self._reconnect_after_exit = False
        self._sources: List[int] = []
        self._dialogs: List[Gtk.Widget] = []
        self._closed = False
        self._log_flush_pending = False
        self.connection_log.listeners.append(self._on_log_line)
        self._build()
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

        grid = Gtk.Grid(column_spacing=Space.M, row_spacing=Space.XS)
        grid.set_halign(Gtk.Align.CENTER)
        self.facts = {}
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
        for button in (self.focus_button, self.disconnect_button, self.reconnect_button):
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
        self.pack_start(scroller, True, True, 0)

        self._statusbar = SessionStatusBar()
        self._statusbar.size_label.set_tooltip_text(_("Remote screen size"))
        self.pack_start(self._statusbar, False, False, 0)
        self._set_status(ConnectionStatus.CONNECTING, _("Starting…"))
        self._sync_buttons()

    def _size_text(self) -> str:
        s = self.settings
        if s.size_mode == "fullscreen":
            return _("Full screen")
        if s.size_mode == "fixed":
            return f"{s.width} × {s.height}"
        return _("{w} × {h}, follows the window").format(w=s.width, h=s.height)

    def _window(self) -> Optional[Gtk.Window]:
        toplevel = self.get_toplevel()
        return toplevel if isinstance(toplevel, Gtk.Window) else None

    # ------------------------------------------------------------ status
    def _set_status(self, state: ConnectionStatus, message: str, *, banner: bool = False,
                    error: bool = False) -> None:
        self._status = state
        self._statusbar.set_status(state, message)
        self.status_label.set_text(message)
        if not self._closed:
            if banner:
                self._banner_label.set_text(message)
                self._banner.set_message_type(Gtk.MessageType.ERROR if error else Gtk.MessageType.INFO)
                self._banner.show()
            self._banner.set_revealed(banner)
            self.emit("status-changed", self._host.alias, state is ConnectionStatus.CONNECTED)
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        running = self.process is not None and self.process.running
        tool = focus_tool()
        self.focus_button.set_sensitive(running and bool(tool))
        self.focus_button.set_tooltip_text(
            _("Bring the remote desktop window to the front") if tool else
            _("Not possible here: Wayland does not let programs raise other windows, and on X11 it needs "
              "xdotool or wmctrl."))
        self.disconnect_button.set_sensitive(running)
        self.reconnect_button.set_sensitive(self._status is not ConnectionStatus.CONNECTING or running)

    def _on_banner_response(self, _bar, response: int) -> None:
        if response == _RESPONSE_RECONNECT:
            self.reconnect()
        elif response == _RESPONSE_CLOSE:
            self.emit("close-requested")

    # ------------------------------------------------------- public API
    def focus_terminal(self) -> None:
        if self.process is not None and self.process.running:
            self.focus_window()
        else:
            self.reconnect_button.grab_focus()

    def focus_window(self) -> bool:
        tool = focus_tool()
        if self.process is None or not tool:
            return False
        pid = self.process.pid
        threading.Thread(target=focus_pid, args=(pid, tool), daemon=True, name="sshm-rdp-focus").start()
        return True

    def disconnect(self) -> None:
        if self.process is not None and self.process.running:
            self.connection_log.append("Disconnect requested")
            self.process.stop(user=True)

    def reconnect(self) -> None:
        if self.process is not None and self.process.running:
            self._reconnect_after_exit = True
            self.process.stop(user=True)
            return
        if self.status is ConnectionStatus.CONNECTING and not self._banner.get_revealed():
            return
        self._begin_connect()

    def rdp_connected(self) -> bool:
        return self.process is not None and self.process.running

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

    # -------------------------------------------------------- connecting
    def _begin_connect(self) -> bool:
        if self._closed:
            return GLib.SOURCE_REMOVE
        self._flow.begin_connect()
        self._start(None, "stored")
        return GLib.SOURCE_REMOVE

    def _choose_engine(self) -> Optional[Engine]:
        report = self._report_provider()
        if report is None:
            self._fail(_("FreeRDP is still being looked for; try again in a moment."), "no report")
            return None
        prefs = getattr(self._app_settings, "remote_desktop", None)
        engine, note = choose_engine(report.engines, getattr(prefs, "preferred_engine", "auto"),
                                     self.settings.engine)
        if engine is None:
            self._fail(_("No FreeRDP client was found. {hint}").format(hint=install_hint(report.platform)),
                       "no engine")
            return None
        if engine.embedded:  # phase R1: every engine runs FreeRDP in its own window
            external = next((e for e in report.engines if e.id == "external" and e.available), None)
            if external is None:
                self._fail(_("Only an embedded engine is available, which this version cannot use yet."), "engine")
                return None
            self.connection_log.append(f"{engine.label} is not available yet; using the external window")
            engine = external
        if note:
            self.connection_log.append(note)
        return engine

    def _start(self, typed: Optional[str], origin: str) -> None:
        self._banner.set_revealed(False)
        engine = self._choose_engine()
        if engine is None or engine.binary is None:
            return
        self.engine = engine
        self.facts["engine"].set_text(f"{engine.label} · {engine.binary.name} {engine.binary.version_text}")
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
        self._set_status(ConnectionStatus.CONNECTING, _("Checking the server…"))
        hostname, port, _user = host_connection_params(self._host)
        settings = self.settings
        pins = tuple(settings.pinned_sha256) + tuple(p for p in (self._stored_pin(),) if p)

        def work() -> None:
            try:
                result = self._probe(hostname, port, timeout=float(settings.connect_timeout_s), pinned=pins,
                                     configured_security=settings.security)
            except Exception as exc:  # never let a worker die silently
                result = ProbeResult(hostname, port, error=f"probe failed: {type(exc).__name__}")
            GLib.idle_add(self._after_probe, result)

        threading.Thread(target=work, daemon=True, name="sshm-rdp-probe").start()

    def _pin_key(self) -> str:
        hostname, port, _user = host_connection_params(self._host)
        return pin_key(getattr(self._host, "id", "") or self._host.alias, hostname, port, scope="rdp")

    def _stored_pin(self) -> str:
        try:
            return self._runtime.trust.get(self._pin_key())
        except Exception:
            return ""

    def _remember(self, fingerprint: str, subject: str) -> None:
        try:
            self._runtime.trust.set(self._pin_key(), fingerprint, subject)
            self.connection_log.append(f"Certificate remembered: {fingerprint}")
        except (OSError, ValueError) as exc:
            self._log(_("Could not remember the certificate: {error}").format(error=type(exc).__name__),
                      Level.WARNING)

    def _after_probe(self, result: ProbeResult) -> bool:
        if self._closed:
            self._runtime.limiter.release(self)
            return GLib.SOURCE_REMOVE
        for line in result.lines():
            self.connection_log.append("probe: " + line)
        if not result.reachable:
            self._runtime.limiter.release(self)
            self._fail(_("The server could not be reached: {error}").format(error=result.error or _("no answer")),
                       "probe: unreachable", kind=Kind.NETWORK)
            return GLib.SOURCE_REMOVE
        security = "rdp" if (self.settings.security == "rdp" or result.security == "rdp") else result.security
        self.security = security
        self.facts["security"].set_text(result.security_label or _("unknown"))
        if not result.security and result.failure:
            self._runtime.limiter.release(self)
            self._fail(_("The server refused the security request: {error}").format(error=result.error),
                       result.failure, kind=Kind.SECURITY_MISMATCH)
            return GLib.SOURCE_REMOVE
        pins = list(self.settings.pinned_sha256) + [p for p in (self._stored_pin(),) if p]
        decision = decide(self.settings.cert_policy, result.certificate, pins, security=security)
        cert = result.certificate
        alias = self._host.alias
        fingerprints: List[str] = []
        ignore = False
        if decision.action is Action.CONNECT:
            fingerprints = [decision.fingerprint]
        elif decision.action is Action.REMEMBER and cert is not None:
            self._remember(decision.fingerprint, cert.subject)
            fingerprints = [decision.fingerprint]
        elif decision.action is Action.ASK and cert is not None:
            choice = rdp_trust_choice(self._window(), alias, cert)
            if choice == "cancel":
                return self._cancelled(_("The certificate was not trusted."))
            if choice == "remember":
                self._remember(decision.fingerprint, cert.subject)
            fingerprints = [decision.fingerprint]
        elif decision.action is Action.CHANGED and cert is not None:
            if not trust_certificate(self._window(), alias, cert, changed=True, expected=decision.expected):
                return self._cancelled(_("The server's certificate has changed; not connected."), error=True)
            self._remember(decision.fingerprint, cert.subject)
            fingerprints = [decision.fingerprint]
        elif decision.action is Action.REFUSE:
            self._runtime.limiter.release(self)
            self._fail(_("The server's certificate is not a trusted one, and this host refuses unknown "
                         "certificates."), "certificate refused", kind=Kind.CERT_REJECTED)
            return GLib.SOURCE_REMOVE
        elif decision.action is Action.CONFIRM_IGNORE:
            if not confirm_unchecked_certificate(self._window(), alias):
                return self._cancelled(_("Connection cancelled."))
            ignore = True
        elif decision.action is Action.UNREADABLE:
            self._runtime.limiter.release(self)
            self._fail(_("The server's certificate could not be read: {error}").format(
                error=result.error or _("no TLS answer")), "certificate unreadable", kind=Kind.TLS)
            return GLib.SOURCE_REMOVE
        self.certificate_checked = not ignore and security != "rdp"
        self.facts["certificate"].set_text(
            _("Not verified") if ignore else (_("Not used (standard RDP security)") if security == "rdp"
                                              else _("Trusted · {fp}").format(fp=fingerprints[0][:23] + "…")))
        self._launch(fingerprints, ignore)
        return GLib.SOURCE_REMOVE

    def _cancelled(self, message: str, *, error: bool = False) -> bool:
        self._runtime.limiter.release(self)
        self._set_status(ConnectionStatus.FAILED if error else ConnectionStatus.UNKNOWN, message, banner=True,
                         error=error)
        return GLib.SOURCE_REMOVE

    def _launch(self, fingerprints: List[str], ignore: bool) -> None:
        engine = self.engine
        assert engine is not None and engine.binary is not None
        hostname, port, user = host_connection_params(self._host)
        identity = resolve_identity(self._host.option("user") or "", self.settings)
        try:
            plan = build_launch(hostname=hostname, port=port, settings=self.settings, identity=identity,
                                password=self._attempt_password, client=engine.binary, title=self._host.alias,
                                accepted_fingerprints=fingerprints, ignore_certificate=ignore)
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

    # ------------------------------------------------------------ output
    def _on_output(self, line: str) -> None:
        """Worker thread: log the (already masked) line; a "connected" line goes to the GTK thread."""

        self.connection_log.append(line)
        if connected_line(line):
            GLib.idle_add(self._mark_connected, "log")

    def _tick(self) -> bool:
        process = self.process
        if self._closed or process is None or not process.running:
            return GLib.SOURCE_REMOVE
        if self._connected:
            seconds = int(time.monotonic() - self._connected_at)
            self.facts["elapsed"].set_text(f"{seconds // 3600}:{seconds // 60 % 60:02d}:{seconds % 60:02d}")
        elif process.alive_s() >= HEURISTIC_S and not any("[ERROR]" in line for line in process.output()):
            self._mark_connected("heuristic")
        return GLib.SOURCE_CONTINUE

    def _mark_connected(self, method: str) -> bool:
        if self._closed or self._connected or self.process is None:
            return GLib.SOURCE_REMOVE
        self._connected = True
        self._connected_at = time.monotonic()
        self.connected_method = method
        self.connection_log.append("Connected (verified by FreeRDP's log)" if method == "log"
                                   else f"Probably connected (no error for {HEURISTIC_S} s; FreeRDP did not log "
                                        "the connection)")
        state = _("Connected") if self.certificate_checked else _("Connected · certificate not verified")
        self._set_status(ConnectionStatus.CONNECTED, state + " · " + _("the remote desktop is in its own window"))
        verified_login = method == "log" and self.security in ("nla", "nla_ext")
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
        self.last_outcome = outcome
        self.process = None
        self.connection_log.append(f"Ended: {outcome.kind.value} ({outcome.technical})")
        if self._closed:
            return GLib.SOURCE_REMOVE
        if self._reconnect_after_exit:
            self._reconnect_after_exit = False
            self._begin_connect()
            return GLib.SOURCE_REMOVE
        if outcome.retry_password and not self._connected:
            self._typed_password = None
            action = self._flow.on_failure(AuthOutcome.AUTH_FAILED)
            if action is FailureAction.PROMPT:
                self._set_status(ConnectionStatus.FAILED, _(outcome.message))
                self._sources.append(GLib.timeout_add_seconds(RETRY_DELAY_S, self._ask_after_delay))
                return GLib.SOURCE_REMOVE
            self._fail(_("Sign-in failed after {n} attempts. Use Reconnect to try again.").format(
                n=self._flow.max_attempts), outcome.technical, kind=Kind.AUTH_FAILED)
            return GLib.SOURCE_REMOVE
        if outcome.kind is Kind.OK_DISCONNECT:
            self._set_status(ConnectionStatus.UNKNOWN, _("Disconnected: {reason}").format(reason=_(outcome.message)),
                             banner=outcome.reason != "user_disconnect")
        else:
            self._fail(_(outcome.message), outcome.technical, kind=outcome.kind)
        self._connected = False
        self.emit("session-closed", self._host.alias, not outcome.is_error)  # (alias, exited normally)
        return GLib.SOURCE_REMOVE

    def _fail(self, message: str, technical: str = "", *, kind: Kind = Kind.OTHER) -> None:
        if technical:
            self.connection_log.append(f"Failed: {technical}")
        self.last_outcome = RdpOutcome(kind, technical or "failed", message, technical)
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
        self._runtime.limiter.release(self)
        self._typed_password = self._attempt_password = None


__all__ = ["RdpSessionTab", "HEURISTIC_S"]
