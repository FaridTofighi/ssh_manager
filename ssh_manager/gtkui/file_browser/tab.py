"""``FileBrowserTab``: the session tab of an FTP, FTPS or SFTP host (a :class:`SessionPage`).

Layout: a reconnect banner (like the terminal), a header (security badge,
remote-only toggle, bookmarks), the **local** and **remote** panes side by side
and the status bar the window docks.

Connecting runs in the connection's worker. The password follows the SSH
rules (``docs/passwords.md``):
- the resolved password (vault → credential → host-specific → legacy) goes first;
- an ``AuthError`` opens the same retry dialog as a terminal (at most 3);
- a typed password that logged in is offered once: "Save this password for this host?";
- nothing is saved silently.

TLS certificates and SFTP host keys are confirmed by the user (pinned /
appended to ``known_hosts``). A changed certificate defaults to Cancel; a
changed host key is refused.

Plain FTP with a password asks once per host and keeps a "Not encrypted" badge.

Closing the tab cancels listings and transfers, closes the connection in the
background, removes every GLib source and deletes the private temp folder of
opened files.
"""

from __future__ import annotations

import functools
import logging
import os
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gio, GLib, Gtk  # type: ignore  # noqa: E402

from ... import protocols  # noqa: E402
from ...auth_flow import (  # noqa: E402
    NO_PASSWORD, AuthOutcome, Decision, PasswordFlow, PasswordResolution, SaveContext, SaveOffer, source_phrase,
)
from ...host_utils import host_connection_params  # noqa: E402
from ...transfer.backend import (  # noqa: E402
    AuthError, BackendError, CancelToken, CertificateChanged, Disconnected, EntryKind, HostKeyChanged, RemoteEntry,
    RemoteFileBackend, ServerInfo, UnknownHostKey, UntrustedCertificate,
)
from ...transfer.browse import remote_url  # noqa: E402
from ...transfer.copy import copy_file  # noqa: E402
from ...transfer.dnd import DropAction, into_itself, resolve_drop  # noqa: E402
from ...transfer.engine import (  # noqa: E402
    FINISHED, Conflict, ConflictInfo, Direction, Endpoint, State, TransferJob, TransferQueue, summarize,
)
from ...transfer.local import LocalBackend  # noqa: E402
from ...transfer.names import UnsafePath, contained_path  # noqa: E402
from ...transfer.protolog import ConnectionLog, RotatingLogFile, default_log_path  # noqa: E402
from ...transfer.settings import TransferSettings, default_port_for  # noqa: E402
from ...transfer.state import BrowserState, host_state_key  # noqa: E402
from ..constants import Icons  # noqa: E402
from ..dialogs.confirm import confirm  # noqa: E402
from ..dialogs.password import PasswordRequestDialog, SavePasswordDialog  # noqa: E402
from ..dialogs.trust import trust_certificate, trust_host_key  # noqa: E402
from ..i18n import _  # noqa: E402
from ..menu_spec import MenuItemSpec, MenuSection  # noqa: E402
from ..session_page import SessionPage  # noqa: E402
from ..theme.tokens import ConnectionStatus, Level, Size, Space  # noqa: E402
from ..views.statusbar import SessionStatusBar  # noqa: E402
from ..widgets.icon_button import IconMenuButton, IconToggleButton, set_accessible_name  # noqa: E402
from ..widgets.icons import make_icon  # noqa: E402
from ..widgets.menus import build_menu  # noqa: E402
from .pane import FilePane  # noqa: E402
from .transfers import ConflictDialog, TransferPanel  # noqa: E402
from .worker import Worker  # noqa: E402

LOGGER = logging.getLogger(__name__)
_RESPONSE_RECONNECT = 1
_RESPONSE_CLOSE = 2

BackendFactory = Callable[[Any, str, str], RemoteFileBackend]  # (host, user, password)
LogFn = Callable[[str, Any], None]


class FileBrowserTab(SessionPage):
    __gtype_name__ = "FileBrowserTab"
    kind = "files"

    def __init__(self, host: Any, log: LogFn, *, password_provider: Optional[Callable[[], PasswordResolution]] = None,
                 password_service: Any = None, backend_factory: Optional[BackendFactory] = None,
                 app_settings: Any = None, read_only: bool = False, state: Optional[BrowserState] = None,
                 autostart: bool = True, local_home: str = "") -> None:
        Gtk.Box.__init__(self, orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.get_style_context().add_class("file-browser")
        self._host = host
        self._log = log
        self._custom_title = ""
        self._status = ConnectionStatus.UNKNOWN
        self._password_provider = password_provider
        self._password_service = password_service
        self._backend_factory = backend_factory
        self._app_settings = app_settings
        self.prefs = getattr(app_settings, "file_transfer", None)
        self.settings, warnings = TransferSettings.for_host(host)
        self.protocol = protocols.protocol_of(host)
        self._state = state or BrowserState()
        self._state_key = host_state_key(host)
        self._flow = PasswordFlow(host.alias)
        self._resolution: PasswordResolution = NO_PASSWORD
        self._typed_password: Optional[str] = None
        self.backend: Optional[RemoteFileBackend] = None
        self.server: Optional[ServerInfo] = None
        self._sources: List[int] = []
        self._dialogs: List[Gtk.Widget] = []
        self._temp_dir: Optional[str] = None
        self._monitors: Dict[str, Gio.FileMonitor] = {}
        self._tokens: List[CancelToken] = []
        self._closed = False
        self.worker = Worker(f"remote-{host.alias}"[:15])
        self.local_worker = Worker("local")
        self.local_backend = LocalBackend(local_home)
        self.local_endpoint = Endpoint("local", lambda: LocalBackend(local_home), local=True,
                                       max_connections=4)
        self._endpoint: Optional[Endpoint] = None
        self._connected_password = ""
        self._finished: set = set()
        self._conflict_lock = threading.Lock()
        log_file = RotatingLogFile(default_log_path()) if bool(getattr(self.prefs, "protocol_log", False)) else None
        self.connection_log = ConnectionLog(host.alias, file=log_file)
        self._log_flush_pending = False
        self.connection_log.listeners.append(self._on_log_line)
        concurrency = int(getattr(self.prefs, "concurrency", 3) or 3)
        self.queue = TransferQueue(concurrency=concurrency, limit_kbps=int(getattr(self.prefs, "bandwidth_limit_kbps",
                                                                                   0) or 0),
                                   ask=self._ask_conflict)
        self.queue.listeners.append(lambda job: GLib.idle_add(self._on_job_update, job))
        self._build(read_only)
        self.connect("destroy", lambda *_: self._on_destroy())
        for warning in warnings:
            self._log(_("{alias}: {warning}").format(alias=host.alias, warning=_(warning)), Level.WARNING)
        GLib.idle_add(self._load_local)
        if autostart:
            GLib.idle_add(self._begin_connect)

    # ---------------------------------------------------------- building
    def _build(self, read_only: bool) -> None:
        self._banner = Gtk.InfoBar()
        self._banner.set_message_type(Gtk.MessageType.WARNING)
        self._banner_label = Gtk.Label()
        self._banner_label.set_xalign(0.0)
        self._banner_label.set_line_wrap(True)
        self._banner.get_content_area().pack_start(self._banner_label, True, True, 0)
        self._banner_label.show()  # the bar itself is no-show-all: its contents must be shown explicitly
        self._reconnect_button = self._banner.add_button(_("_Reconnect"), _RESPONSE_RECONNECT)
        self._banner.add_button(_("_Close Session"), _RESPONSE_CLOSE)
        self._banner.connect("response", self._on_banner_response)
        self._banner.set_revealed(False)
        self._banner.set_no_show_all(True)
        self.pack_start(self._banner, False, False, 0)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.S)
        header.get_style_context().add_class("file-browser-header")
        header.set_margin_start(Space.S)
        header.set_margin_end(Space.S)
        self.security_icon_box = Gtk.Box()
        self.security_label = Gtk.Label()
        self.security_label.set_xalign(0.0)
        header.pack_start(self.security_icon_box, False, False, 0)
        header.pack_start(self.security_label, False, False, 0)
        self.remote_only = IconToggleButton(Icons.SIDEBAR, _("Show only the server's files"), self._set_remote_only)
        self.bookmarks_button = IconMenuButton("user-bookmarks-symbolic", _("Bookmarks and recent folders"))
        self.bookmarks_button.connect("toggled", self._on_bookmarks_toggled)
        header.pack_end(self.remote_only, False, False, 0)
        self.transfers_toggle = IconToggleButton(Icons.SAVE, _("Transfers"),
                                                 lambda active: self.panel.set_reveal_child(active))
        header.pack_end(self.transfers_toggle, False, False, 0)
        self.log_toggle = IconToggleButton(Icons.ACTIVITY, _("Connection log"), self._set_log_visible)
        header.pack_end(self.log_toggle, False, False, 0)
        header.pack_end(self.bookmarks_button, False, False, 0)
        self.pack_start(header, False, False, 0)
        self._update_security_badge()

        prefs_hidden = bool(getattr(self.prefs, "show_hidden", False))
        transient = lambda: self._window()  # noqa: E731
        self.local_pane = FilePane("local", self.local_worker, lambda: self.local_backend,
                                   show_hidden=prefs_hidden, transient_for=transient)
        self.remote_pane = FilePane("remote", self.worker, lambda: self.backend, read_only=read_only,
                                    show_hidden=self.settings.show_hidden or prefs_hidden, owner_columns=True,
                                    transient_for=transient)
        for pane, other in ((self.local_pane, self.remote_pane), (self.remote_pane, self.local_pane)):
            pane.on_open_file = functools.partial(self._open_file, pane)
            pane.on_open_entry = functools.partial(self._open_link, pane)
            pane.on_transfer = functools.partial(self._transfer_selection, pane, other)
            pane.on_paste = functools.partial(self._paste_into, pane)
            pane.on_error = self._on_pane_error
            pane.on_navigated = functools.partial(self._remember_dir, pane)
            pane.on_properties = functools.partial(self._properties, pane)
            pane.on_drop = functools.partial(self._on_drop, pane)
            pane.owner = self
            pane.enable_dnd()
        self.remote_pane.extra_menu = self._remote_extra_menu
        self.paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self.paned.set_wide_handle(True)
        self.paned.pack1(self.local_pane, True, False)
        self.paned.pack2(self.remote_pane, True, False)
        self.pack_start(self.paned, True, True, 0)
        self.panel = TransferPanel()
        self.panel.on_pause = self.queue.pause
        self.panel.on_resume = self.queue.resume
        self.panel.on_cancel = self.queue.cancel
        self.panel.on_retry = self._on_retry
        self.panel.on_reveal = self._reveal
        self.panel.on_clear = self.queue.clear_finished
        self.panel.on_limit = self.queue.set_limit
        self.pack_start(self.panel, False, False, 0)
        self.pack_start(self._build_log_pane(), False, False, 0)
        # desktop files dropped in: a stand-in source pane for "this computer"
        self._desktop_pane = FilePane("local", self.local_worker, lambda: self.local_backend)
        self._desktop_pane.owner = self

        self._statusbar = SessionStatusBar()
        self._statusbar.size_label.set_tooltip_text("")
        self.pack_start(self._statusbar, False, False, 0)
        self._set_status(ConnectionStatus.CONNECTING, _("Connecting…"))

    def _build_log_pane(self) -> Gtk.Widget:
        """The connection log: server commands and replies (passwords masked), Copy Log, Copy Diagnostics."""

        self.log_revealer = Gtk.Revealer()
        self.log_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_UP)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=Space.XS)
        box.get_style_context().add_class("file-browser-log")
        for setter in (box.set_margin_start, box.set_margin_end, box.set_margin_bottom):
            setter(Space.S)
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.S)
        title = Gtk.Label(label=_("Connection log"))
        title.set_xalign(0.0)
        title.get_style_context().add_class("section-title")
        bar.pack_start(title, True, True, 0)
        copy_diag = Gtk.Button.new_with_mnemonic(_("Copy _Diagnostics"))
        copy_diag.set_tooltip_text(_("Server banner, features and TLS details (no passwords)"))
        copy_diag.connect("clicked", lambda *_a: self.copy_diagnostics())
        copy_log = Gtk.Button.new_with_mnemonic(_("Copy _Log"))
        copy_log.connect("clicked", lambda *_a: self.copy_log())
        bar.pack_end(copy_log, False, False, 0)
        bar.pack_end(copy_diag, False, False, 0)
        box.pack_start(bar, False, False, 0)
        self.log_view = Gtk.TextView()
        self.log_view.set_editable(False)
        self.log_view.set_cursor_visible(False)
        self.log_view.set_monospace(True)
        self.log_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        set_accessible_name(self.log_view, _("Connection log"))
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.set_min_content_height(6 * Size.TREE_ROW)  # about six lines
        scroller.set_shadow_type(Gtk.ShadowType.IN)
        scroller.add(self.log_view)
        box.pack_start(scroller, True, True, 0)
        self.log_revealer.add(box)
        self.log_revealer.set_reveal_child(False)
        return self.log_revealer

    def _set_log_visible(self, active: bool) -> None:
        self.log_revealer.set_reveal_child(active)
        if active:
            self._flush_log()

    def _on_log_line(self, _line: str) -> None:
        """Called on any thread: one coalesced refresh on the GTK thread."""

        if not self._log_flush_pending:
            self._log_flush_pending = True
            GLib.idle_add(self._flush_log)

    def _flush_log(self) -> bool:
        self._log_flush_pending = False
        if self._closed or not self.log_revealer.get_reveal_child():
            return GLib.SOURCE_REMOVE
        buffer = self.log_view.get_buffer()
        buffer.set_text(self.connection_log.text())
        buffer.place_cursor(buffer.get_end_iter())
        self.log_view.scroll_mark_onscreen(buffer.get_insert())
        return GLib.SOURCE_REMOVE

    def diagnostics_text(self) -> str:
        """What "Copy Diagnostics" copies: connection facts and the server's own details (never a password)."""

        hostname, port, user = host_connection_params(self._host)
        kind = protocols.kind_for(self._host)
        settings = self.settings
        lines = ["SSH Manager — file transfer diagnostics",
                 f"Protocol: {kind.badge if kind else self.protocol}",
                 f"Server: {hostname}:{port or default_port_for(self.protocol, settings)}",
                 f"User: {'(anonymous)' if settings.anonymous else (user or '(none)')}",
                 f"Status: {self._status.value}"]
        if self.protocol in ("ftp", "ftps"):
            lines.append(f"Mode: {'passive' if settings.passive else 'active'}, list {settings.list_command}, "
                         f"encoding {settings.encoding}")
        if self.protocol == "ftps":
            lines.append(f"TLS: {settings.tls_mode}, verify {settings.tls_verify}")
        if self.server is not None:
            lines += ["", "Server details"] + [f"  {line}" for line in self.server.lines()]
        return "\n".join(lines) + "\n"

    def copy_log(self) -> str:
        from gi.repository import Gdk

        text = self.connection_log.text()
        Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(text, -1)
        return text

    def copy_diagnostics(self) -> str:
        from gi.repository import Gdk

        text = self.diagnostics_text()
        Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(text, -1)
        return text

    def _window(self) -> Optional[Gtk.Window]:
        toplevel = self.get_toplevel()
        return toplevel if isinstance(toplevel, Gtk.Window) else None

    def focus_terminal(self) -> None:
        pane = self.remote_pane if self.remote_pane.connected else self.local_pane
        pane.view.grab_focus()

    # ------------------------------------------------------------ status
    def _set_status(self, state: ConnectionStatus, message: str) -> None:
        self._status = state
        self._statusbar.set_status(state, message)
        connected = state is ConnectionStatus.CONNECTED
        if not self._closed:
            reveal = state is ConnectionStatus.FAILED or state is ConnectionStatus.UNKNOWN
            self._banner_label.set_text(message)
            self._banner.set_message_type(Gtk.MessageType.ERROR if state is ConnectionStatus.FAILED
                                          else Gtk.MessageType.WARNING)
            if reveal:
                self._banner.show()
            self._banner.set_revealed(reveal)
            self.emit("status-changed", self._host.alias, connected)

    def _update_security_badge(self) -> None:
        if self.protocol == "ftp":
            text, icon, tip = _("Not encrypted"), Icons.WARNING, _("Plain FTP: passwords and files travel in clear text.")
        elif self.protocol == "ftps" and self.settings.tls_verify == "insecure":
            text, icon, tip = _("Certificate not verified"), Icons.WARNING, _("TLS without certificate verification.")
        else:
            detail = ""
            if self.server is not None:
                detail = self.server.tls_version or (self.server.host_key.split(" ", 1)[0] if self.server.host_key
                                                     else "")
            text = _("Encrypted") + (f" · {detail}" if detail else "")
            icon, tip = Icons.VAULT, _("The connection is encrypted.")
        self.security_label.set_text(text)
        self.security_label.set_tooltip_text(tip)
        self.security_kind = "plain" if icon == Icons.WARNING else "encrypted"
        for child in self.security_icon_box.get_children():
            self.security_icon_box.remove(child)
        image = make_icon(icon, Size.ICON)
        self.security_icon_box.pack_start(image, False, False, 0)
        image.show()
        set_accessible_name(self.security_label, tip)

    def _on_banner_response(self, _bar, response: int) -> None:
        if response == _RESPONSE_RECONNECT:
            self.reconnect()
        elif response == _RESPONSE_CLOSE:
            self.emit("close-requested")

    # -------------------------------------------------------- connecting
    def reconnect(self) -> None:
        if self.status is ConnectionStatus.CONNECTING:
            return
        self._begin_connect()

    def _begin_connect(self) -> bool:
        if self._closed:
            return GLib.SOURCE_REMOVE
        self._flow.begin_connect()
        self._connect(None, "stored")
        return GLib.SOURCE_REMOVE

    def _user(self) -> str:
        return self._host.option("user") or ""

    def _connect(self, typed: Optional[str], origin: str) -> None:
        if typed is None:
            try:
                self._resolution = self._password_provider() if self._password_provider else NO_PASSWORD
            except Exception as exc:  # storage problems never block a connect
                LOGGER.warning("password resolution failed: %s", type(exc).__name__)
                self._resolution = NO_PASSWORD
            self._flow.stored = self._resolution.password
            password = self._typed_password or self._resolution.password or ""
            source = "session" if self._typed_password else self._resolution.source
        else:
            password, source = typed, origin
        self._flow.attempt(source, password if source in ("prompt", "retry") else None)
        self.connection_log.set_secret("password", password)
        if not self._plain_ftp_ok(password):
            self._set_status(ConnectionStatus.FAILED, _("Connection cancelled: plain FTP was not confirmed."))
            return
        try:
            backend = self._make_backend(password)
        except Exception as exc:
            self._set_status(ConnectionStatus.FAILED, _("Cannot connect: {error}").format(error=exc))
            return
        self._set_status(ConnectionStatus.CONNECTING, _("Connecting…"))
        self.remote_pane.set_connected(False, _("Connecting…"))
        self.worker.submit(backend.connect, functools.partial(self._connected_cb, backend, password),
                           functools.partial(self._connect_failed_cb, backend, password))

    def _connected_cb(self, backend: RemoteFileBackend, password: str, info: ServerInfo) -> None:
        self._connected(backend, info, password)

    def _connect_failed_cb(self, backend: RemoteFileBackend, password: str, exc: BaseException) -> None:
        self._connect_failed(backend, exc, password)

    def _make_backend(self, password: str) -> RemoteFileBackend:
        if self._backend_factory is not None:
            return self._backend_factory(self._host, self._user(), password)
        from ...transfer.factory import make_backend

        policy = getattr(self._app_settings, "host_key_policy", "reject")
        return make_backend(self._host, self._user(), password, host_key_policy=policy, settings=self.settings,
                            log=self.connection_log)

    def _plain_ftp_ok(self, password: str) -> bool:
        """Plain FTP with a password: ask once per host (Preferences can turn the question off)."""

        if self.protocol != "ftp" or not password or self.settings.anonymous:
            return True
        if not bool(getattr(self.prefs, "warn_plain_ftp", True)):
            return True
        if self._state.get(self._state_key).get("plain_ftp_ok"):
            return True
        ok = confirm(
            self._window(),
            _("Send the password unencrypted?"),
            _("“{alias}” uses plain FTP: the password and every file travel in clear text and can be read on the "
              "network. Use FTPS or SFTP when the server offers it.").format(alias=self._host.alias),
            confirm_label=_("_Connect Anyway"),
            destructive=True,
        )
        if ok:
            self._state.update(self._state_key, plain_ftp_ok=True)
        return ok

    def _connected(self, backend: RemoteFileBackend, info: ServerInfo, password: str) -> None:
        if self._closed:
            self.worker.submit(backend.close)
            return
        old, self.backend = self.backend, backend
        if old is not None and old is not backend:
            self.worker.submit(old.close)
        self.server = info
        self.connection_log.append(f"Connected in {info.seconds:.2f} s" if info.seconds else "Connected")
        self._connected_password = password  # extra transfer connections log in the same way
        if self._endpoint is not None:
            self.worker.submit(self._endpoint.close)
            self._endpoint = None
        self._update_security_badge()
        detail = info.tls_version or (_("SFTP") if self.protocol == "sftp" else "")
        self._set_status(ConnectionStatus.CONNECTED, _("Connected") + (f" · {detail}" if detail else ""))
        self.remote_pane.set_connected(True)
        remembered = self._state.get(self._state_key).get("remote") if self._remember_dirs() else None
        start = self.settings.initial_remote_dir or remembered or info.home or backend.home()
        self.remote_pane.load(start)
        self._start_keepalive()
        self._offer_save(password)

    def _connect_failed(self, backend: RemoteFileBackend, exc: BaseException, password: str) -> None:
        if self._closed:
            return
        self.worker.submit(backend.close)
        alias = self._host.alias
        technical = getattr(exc, "technical", "") or ""
        self.connection_log.append(f"Connection failed: {getattr(exc, 'message', '') or type(exc).__name__}"
                                   + (f" ({technical})" if technical else ""))
        if isinstance(exc, AuthError):
            self._typed_password = None
            action = self._flow.on_failure(AuthOutcome.AUTH_FAILED)
            if action.value == "prompt":
                self._set_status(ConnectionStatus.FAILED, _("Authentication failed"))
                self._ask_password(self._rejected_phrase())
                return
            self._set_status(ConnectionStatus.FAILED,
                             _("Authentication failed after 3 attempts. Use Reconnect to try again."))
            return
        if isinstance(exc, CertificateChanged):
            if trust_certificate(self._window(), alias, exc.info, changed=True, expected=exc.expected):
                self._trust(backend, exc)
                return
            self._set_status(ConnectionStatus.FAILED, exc.message)
            return
        if isinstance(exc, UntrustedCertificate):
            if trust_certificate(self._window(), alias, exc.info):
                self._trust(backend, exc)
                return
            self._set_status(ConnectionStatus.FAILED, _("The certificate was not trusted."))
            return
        if isinstance(exc, UnknownHostKey):
            if trust_host_key(self._window(), alias, exc.key_type, exc.fingerprint):
                try:
                    backend.accept_host_key(exc)  # type: ignore[attr-defined]
                except OSError as os_error:
                    self._log(_("Could not update known_hosts: {error}").format(error=os_error), Level.WARNING)
                self._retry_same(password)
                return
            self._set_status(ConnectionStatus.FAILED, _("The host key was not accepted."))
            return
        if isinstance(exc, HostKeyChanged):
            self._set_status(ConnectionStatus.FAILED, exc.message)
            self._log(_("{alias}: the host key changed (now {new}, expected {old}).").format(
                alias=alias, new=exc.fingerprint, old=exc.expected), Level.ERROR)
            return
        error = exc if isinstance(exc, BackendError) else BackendError(technical=type(exc).__name__)
        self._set_status(ConnectionStatus.FAILED, error.message)
        if error.technical:
            LOGGER.info("%s: %s", alias, error.technical)

    def _trust(self, backend: RemoteFileBackend, exc: UntrustedCertificate) -> None:
        trust = getattr(backend, "trust_certificate", None)
        if callable(trust):
            trust(exc.info)
        self._retry_same(None)

    def _retry_same(self, password: Optional[str]) -> None:
        if password and self._flow.typed:
            self._connect(password, self._flow.source)
        else:
            self._connect(None, "stored")

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
        request = self._flow.next_request(f"{user}@{hostname}:{port}", rejected)
        dialog = PasswordRequestDialog(self._window(), request,
                                       functools.partial(self._on_password_answer, retry=bool(rejected)))
        self._dialogs.append(dialog)

    def _on_password_answer(self, password: Optional[str], *, retry: bool) -> None:
        if not password:
            self._set_status(ConnectionStatus.FAILED, _("Authentication failed") if retry
                             else _("Connection cancelled"))
            return
        self._connect(password, "retry" if retry else "prompt")

    def _offer_save(self, password: str) -> None:
        if self._password_service is None or self._flow.source not in ("prompt", "retry"):
            return
        self._typed_password = password  # this tab keeps using it (Reconnect)
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

    # --------------------------------------------------------- keepalive
    def _start_keepalive(self) -> None:
        seconds = self.settings.keepalive_s
        if seconds and not any(self._sources):
            self._sources.append(GLib.timeout_add_seconds(seconds, self._keepalive_tick))

    def _keepalive_tick(self) -> bool:
        backend = self.backend
        if self._closed or backend is None:
            return GLib.SOURCE_REMOVE
        if self.worker.pending == 0:
            self.worker.submit(backend.keepalive, None, self._on_keepalive_failed)
        return GLib.SOURCE_CONTINUE

    def _on_keepalive_failed(self, exc: BaseException) -> None:
        if isinstance(exc, (Disconnected, BackendError)) and self.status is ConnectionStatus.CONNECTED:
            self._disconnected(exc.message if isinstance(exc, BackendError) else str(exc))

    def _disconnected(self, message: str) -> None:
        self.remote_pane.set_connected(False, message)
        self._set_status(ConnectionStatus.FAILED, message or _("The connection was lost."))
        self.emit("session-closed", self._host.alias, False)

    def _report(self, exc: BaseException) -> None:
        self._on_pane_error(exc if isinstance(exc, BackendError) else BackendError(technical=type(exc).__name__))

    def _on_pane_error(self, error: BackendError) -> None:
        if isinstance(error, Disconnected):
            self._disconnected(error.message)
            return
        self._log(_("{alias}: {message}").format(alias=self._host.alias, message=error.message), Level.ERROR)

    # ------------------------------------------------------------ panes
    def _load_local(self) -> bool:
        if self._closed:
            return GLib.SOURCE_REMOVE
        remembered = self._state.get(self._state_key).get("local") if self._remember_dirs() else None
        download_dir = os.path.expanduser(str(getattr(self.prefs, "download_dir", "") or ""))
        start = self.settings.initial_local_dir or remembered or download_dir or self.local_backend.home()
        if not os.path.isdir(start):
            start = self.local_backend.home()
        self.local_pane.load(start)
        return GLib.SOURCE_REMOVE

    def _remember_dirs(self) -> bool:
        return bool(getattr(self.prefs, "remember_last_dirs", True))

    def _remember_dir(self, pane: FilePane, path: str) -> None:
        if self._remember_dirs():
            self._state.update(self._state_key, **{pane.side: path})

    def _set_remote_only(self, remote_only: bool) -> None:
        self.local_pane.set_visible(not remote_only)

    def _remote_extra_menu(self) -> List[MenuItemSpec]:
        selected = self.remote_pane.selected_entries()
        def copy_url() -> None:
            self.copy_remote_url()

        return [MenuItemSpec(_("Copy Remote _URL"), copy_url, Icons.COPY, sensitive=len(selected) <= 1)]

    def copy_remote_url(self) -> str:
        """``scheme://user@host:port/path`` of the selection (or the folder) — never with a password."""

        from gi.repository import Gdk

        hostname, port, user = host_connection_params(self._host)
        selected = self.remote_pane.selected_entries()
        path = selected[0].path if selected else (self.remote_pane.current_path or "/")
        url = remote_url(self.protocol, hostname, port, default_port_for(self.protocol, self.settings),
                         "" if self.settings.anonymous else self._user(), path)
        Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(url, -1)
        return url

    def _on_bookmarks_toggled(self, button: Gtk.MenuButton) -> None:
        if not button.get_active():
            return
        sections = []
        marks = self._state.bookmarks(self._state_key)
        items = [MenuItemSpec(f"{_('Server') if m['side'] == 'remote' else _('This computer')}: {m['path']}",
                              functools.partial(self._go_bookmark, m["side"], m["path"])) for m in marks]
        if items:
            sections.append(MenuSection("bookmarks", items))
        recent = [MenuItemSpec(path, functools.partial(self.remote_pane.load, path))
                  for path in self.remote_pane.history.recent[1:11]]
        if recent:
            sections.append(MenuSection("recent", recent))
        sections.append(MenuSection("add", [
            MenuItemSpec(_("Bookmark the Server Folder"), lambda: self.add_bookmark("remote"),
                         sensitive=self.remote_pane.current_path is not None),
            MenuItemSpec(_("Bookmark the Local Folder"), lambda: self.add_bookmark("local"),
                         sensitive=self.local_pane.current_path is not None),
        ]))
        button.set_popup(build_menu(sections))

    def add_bookmark(self, side: str) -> None:
        pane = self.remote_pane if side == "remote" else self.local_pane
        if pane.current_path:
            self._state.add_bookmark(self._state_key, side, pane.current_path)

    def _go_bookmark(self, side: str, path: str) -> None:
        (self.remote_pane if side == "remote" else self.local_pane).load(path)

    # ---------------------------------------------------------- opening
    def _open_link(self, pane: FilePane, entry: RemoteEntry) -> None:
        backend = pane.backend()
        if backend is None:
            return

        pane.worker.submit(lambda: backend.is_dir(entry.path),
                           lambda is_dir: pane.load(entry.path) if is_dir else self._open_file(pane, entry),
                           lambda exc: self._on_pane_error(exc if isinstance(exc, BackendError) else BackendError()))

    def _open_file(self, pane: FilePane, entry: RemoteEntry) -> None:
        if pane.side == "local":
            Gio.AppInfo.launch_default_for_uri(Gio.File.new_for_path(entry.path).get_uri(), None)
            return
        if self.backend is None:
            return
        try:
            target = contained_path(Path(self._private_temp()), entry.name)
        except UnsafePath:
            self._log(_("That file name cannot be opened safely."), Level.ERROR)
            return
        backend, local = self.backend, self.local_backend
        token = CancelToken()
        self._tokens.append(token)

        def work():
            copy_file(backend, entry.path, local, str(target), size=entry.size, cancel=token)
            os.chmod(target, 0o600)
            return str(target)

        def opened(path: str) -> None:
            if bool(getattr(self.prefs, "open_with_default_app", True)):
                try:
                    Gio.AppInfo.launch_default_for_uri(Gio.File.new_for_path(path).get_uri(), None)
                except GLib.Error as exc:
                    self._log(_("No application can open this file: {error}").format(error=exc.message), Level.WARNING)
            self._watch(path, entry.path)

        self.worker.submit(work, opened, lambda exc: self._on_pane_error(
            exc if isinstance(exc, BackendError) else BackendError(technical=type(exc).__name__)))

    def _private_temp(self) -> str:
        if self._temp_dir is None:
            self._temp_dir = tempfile.mkdtemp(prefix="sshm-open-")  # 0700
        return self._temp_dir

    def _watch(self, local_path: str, remote_path: str) -> None:
        """Offer to upload changes back when an opened file is saved."""

        if local_path in self._monitors:
            return
        monitor = Gio.File.new_for_path(local_path).monitor_file(Gio.FileMonitorFlags.NONE, None)
        monitor.connect("changed", functools.partial(self._on_opened_changed, local_path, remote_path))
        self._monitors[local_path] = monitor

    def _on_opened_changed(self, local_path: str, remote_path: str, _monitor, _file, _other, event) -> None:
        if event != Gio.FileMonitorEvent.CHANGES_DONE_HINT or self._closed or self.backend is None:
            return
        if self.remote_pane.read_only:
            self._log(_("This host is read-only; changes to {name} were not uploaded.").format(
                name=os.path.basename(remote_path)), Level.WARNING)
            return
        if not confirm(self._window(), _("Upload the changes?"),
                       _("“{name}” was changed on this computer. Upload it back to the server?").format(
                           name=os.path.basename(remote_path)), confirm_label=_("_Upload")):
            return
        backend, local = self.backend, self.local_backend
        size = os.path.getsize(local_path)
        def uploaded(_result: Any) -> None:
            self._log(_("Uploaded {name}.").format(name=os.path.basename(remote_path)), Level.SUCCESS)
            self.remote_pane.refresh()

        self.worker.submit(lambda: copy_file(local, local_path, backend, remote_path, size=size,
                                             atomic=self.settings.upload_atomic),
                           uploaded, self._report)

    def _properties(self, pane: FilePane, entry: RemoteEntry) -> None:
        from .properties import PropertiesDialog

        dialog = PropertiesDialog(self._window(), pane, entry)
        self._dialogs.append(dialog)
        dialog.present()

    # --------------------------------------------------------- transfers
    def _remote_endpoint(self) -> Optional[Endpoint]:
        """Connections for transfers (``max_connections``; FTP needs one per running transfer)."""

        if self.backend is None:
            return None
        if self._endpoint is None:
            password = self._connected_password
            self._endpoint = Endpoint(self._host.alias, lambda: self._make_backend(password),
                                      max_connections=self.settings.max_connections,
                                      limit_kbps=self.settings.bandwidth_limit_kbps)
        return self._endpoint

    def _second_endpoint(self) -> Optional[Endpoint]:
        """Another pool to the same server (a same-server copy reads and writes at once)."""

        if self.backend is None:
            return None
        password = self._connected_password
        return Endpoint(self._host.alias, lambda: self._make_backend(password), max_connections=1)

    def _transfer_selection(self, source: FilePane, target: FilePane, entries: List[RemoteEntry]) -> None:
        """Download / Upload (context menu): into the other pane's current folder."""

        self.transfer(source, target, entries)

    def endpoint_for(self, pane: FilePane) -> Optional[Endpoint]:
        return self.local_endpoint if pane.side == "local" else self._remote_endpoint()

    def transfer(self, source: FilePane, target: FilePane, entries: List[RemoteEntry],
                 destination: Optional[str] = None, *, move: bool = False) -> Optional[TransferJob]:
        """Queue ``entries`` from ``source`` (any tab's pane) into ``target`` (a pane of this tab).

        Upload / download / local copy follow from the sides; ``move`` within
        one server renames; a remote pane of another tab is copied server to
        server through a local streaming relay. Folders are planned
        recursively; downloaded names are made safe.
        """

        directory = destination or target.current_path
        if directory is None or not entries:
            return None
        if target.side == "remote" and target.read_only:
            self._log(_("This host is read-only: uploads are disabled."), Level.WARNING)
            return None
        if into_itself([e.path for e in entries if e.kind is EntryKind.DIR], directory) and source.side == target.side:
            self._log(_("A folder cannot be copied or moved into itself."), Level.WARNING)
            return None
        other_tab = source.owner if source.owner is not None else self
        source_endpoint = other_tab.endpoint_for(source) if hasattr(other_tab, "endpoint_for") else None
        target_endpoint = self.endpoint_for(target)
        if source_endpoint is None or target_endpoint is None:
            self._log(_("Not connected: the transfer was not started."), Level.WARNING)
            return None
        if source.side == "local" and target.side == "remote":
            direction = Direction.UPLOAD
        elif source.side == "remote" and target.side == "local":
            direction = Direction.DOWNLOAD
        elif source.side == "local":
            direction = Direction.LOCAL_COPY
        elif other_tab is self and move:
            direction = Direction.REMOTE_MOVE
        else:
            direction = Direction.REMOTE_COPY
            if other_tab is self:
                target_endpoint = self._second_endpoint() or target_endpoint
        prefs_policy = getattr(self.prefs, "conflict_policy", "ask")
        settings = self.settings
        job = TransferJob(
            direction, list(entries), directory, source_endpoint, target_endpoint,
            conflict=Conflict(prefs_policy) if prefs_policy in Conflict._value2member_map_ else Conflict.ASK,
            atomic=settings.upload_atomic if target.side == "remote" else True,
            preserve_mtime=settings.preserve_mtime, follow_symlinks=settings.follow_symlinks,
            ascii_for=settings.is_ascii_name if self.protocol in ("ftp", "ftps") and settings.transfer_mode == "auto"
            else None,
        )
        self.panel.set_reveal_child(True)
        self.transfers_toggle.set_active(True)
        return self.queue.add(job)

    def _on_drop(self, target: FilePane, source: Optional[FilePane], entries: List[RemoteEntry], folder: str,
                 copy: bool) -> bool:
        """A validated drop on ``target`` (``source`` is None for files from the desktop)."""

        if source is None:  # desktop files: always copied, from this computer
            source = self._desktop_pane
        same_pane = source is target
        same_server = source.side == target.side == "remote" and source.owner is target.owner
        action = resolve_drop(source.side, target.side, same_pane=same_pane, same_server=same_server,
                              copy_modifier=copy, target_is_file=False, read_only_target=target.read_only)
        if action is DropAction.REJECT:
            self._log(_("Files cannot be dropped here."), Level.WARNING)
            return False
        if action is DropAction.MOVE and source.side == "local":
            return self._local_move(entries, folder, target)
        job = self.transfer(source, target, entries, folder, move=action is DropAction.MOVE)
        return job is not None

    def _local_move(self, entries: List[RemoteEntry], folder: str, pane: FilePane) -> bool:
        if into_itself([e.path for e in entries if e.kind is EntryKind.DIR], folder):
            self._log(_("A folder cannot be moved into itself."), Level.WARNING)
            return False
        local = self.local_backend

        def work() -> None:
            for entry in entries:
                local.rename(entry.path, os.path.join(folder, os.path.basename(entry.path)))

        self.local_worker.submit(work, lambda _r: pane.refresh(), self._report)
        return True

    def _on_job_update(self, job: TransferJob) -> bool:
        if self._closed:
            return GLib.SOURCE_REMOVE
        self.panel.update(job)
        running, rate = summarize(self.queue.jobs)
        summary = self.panel.summary_text() if running else ""
        self._statusbar.size_label.set_text(summary)
        self.transfers_toggle.set_tooltip_text(summary or _("Transfers"))
        if job.state in FINISHED and job.id not in self._finished:
            self._finished.add(job.id)
            self._job_finished(job)
        return GLib.SOURCE_REMOVE

    def _job_finished(self, job: TransferJob) -> None:
        changed = self.local_pane if job.target.local else self.remote_pane  # where the files landed
        if changed.current_path is not None:
            changed.refresh()
        files = sum(1 for item in job.items if not item.is_dir)
        big = files >= 20 or job.total >= 100 * 1000 * 1000
        if job.state is State.DONE and big:
            self._log(_("{title}: {n} files transferred.").format(title=job.title, n=files), Level.SUCCESS)
        elif job.state is State.FAILED:
            self._log(_("{title}: {error}").format(title=job.title, error=job.error), Level.ERROR)
        if job.warnings:
            LOGGER.info("transfer %s: %d warning(s)", job.id, len(job.warnings))
        window = self._window()
        if big and window is not None and not window.is_active():
            app = window.get_application()
            if app is not None:  # the user is elsewhere: a desktop notification
                note = Gio.Notification.new(_("Transfer finished") if job.state is State.DONE
                                            else _("Transfer failed"))
                note.set_body(job.title)
                app.send_notification(f"sshm-transfer-{job.id}", note)

    def _ask_conflict(self, job: TransferJob, info: ConflictInfo):
        """Called from a transfer thread: show the conflict dialog on the GTK thread and wait for it."""

        with self._conflict_lock:  # one question at a time; "apply to all" answers the others
            if job.conflict is not Conflict.ASK:
                return job.conflict, False
            if self._closed or job.cancel.cancelled:
                return Conflict.SKIP, True
            done = threading.Event()
            answer: Dict[str, Any] = {}

            def show() -> bool:
                if self._closed:
                    answer["value"] = (None, False)
                else:
                    answer["value"] = ConflictDialog(self._window(), info).ask()
                done.set()
                return GLib.SOURCE_REMOVE

            GLib.idle_add(show)
            while not done.wait(0.2):
                if self._closed or job.cancel.cancelled:
                    return Conflict.SKIP, True
            decision, apply_all = answer["value"]
            if decision is None:  # "Cancel the transfer"
                self.queue.cancel(job)
                return Conflict.SKIP, True
            return decision, apply_all

    def _on_retry(self, job: TransferJob) -> None:
        self.queue.retry(job)

    def _reveal(self, job: TransferJob) -> None:
        if job.target.local:
            Gio.AppInfo.launch_default_for_uri(Gio.File.new_for_path(job.destination).get_uri(), None)

    def running_transfers(self) -> int:
        return len(self.queue.running())

    def confirm_close(self) -> bool:
        """Closing with transfers running asks first (the notebook calls this)."""

        count = self.running_transfers()
        if not count:
            return True
        return confirm(self._window(), _("Cancel the transfers?"),
                       _("{n} transfers are in progress. Cancel them and close?").format(n=count),
                       confirm_label=_("Cancel Transfers and _Close"), destructive=True)

    def _paste_into(self, pane: FilePane) -> None:
        entries = FilePane.clipboard
        side = getattr(FilePane, "clipboard_side", "")
        if not entries or side == pane.side:
            return
        source = self.local_pane if side == "local" else self.remote_pane
        self.transfer(source, pane, list(entries))

    # --------------------------------------------------------- lifecycle
    def _on_destroy(self) -> None:
        self._closed = True
        for token in self._tokens:
            token.cancel()
        for source in self._sources:
            GLib.source_remove(source)
        self._sources.clear()
        for dialog in self._dialogs:
            dismiss = getattr(dialog, "dismiss", None)
            try:
                dismiss() if callable(dismiss) else dialog.destroy()
            except Exception:
                pass
        self._dialogs.clear()
        for monitor in self._monitors.values():
            monitor.cancel()
        self._monitors.clear()
        self.queue.shutdown()  # cancels running transfers (partial files removed)
        self._desktop_pane.destroy()
        if self._endpoint is not None:
            endpoint = self._endpoint
            self.worker.submit(endpoint.close)
        backend, self.backend = self.backend, None
        if backend is not None:
            self.worker.submit(backend.close)
        self.worker.shutdown()
        self.local_worker.shutdown()
        if self._temp_dir and bool(getattr(self.prefs, "clean_temp_on_exit", True)):
            shutil.rmtree(self._temp_dir, ignore_errors=True)
