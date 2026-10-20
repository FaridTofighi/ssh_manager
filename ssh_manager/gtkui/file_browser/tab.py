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
from ...transfer.local import LocalBackend  # noqa: E402
from ...transfer.names import UnsafePath, contained_path  # noqa: E402
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
            pane.on_transfer = functools.partial(self.transfer, pane, other)
            pane.on_paste = functools.partial(self._paste_into, pane)
            pane.on_error = self._on_pane_error
            pane.on_navigated = functools.partial(self._remember_dir, pane)
            pane.on_properties = functools.partial(self._properties, pane)
        self.remote_pane.extra_menu = self._remote_extra_menu
        self.paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self.paned.set_wide_handle(True)
        self.paned.pack1(self.local_pane, True, False)
        self.paned.pack2(self.remote_pane, True, False)
        self.pack_start(self.paned, True, True, 0)

        self._statusbar = SessionStatusBar()
        self._statusbar.size_label.set_tooltip_text("")
        self.pack_start(self._statusbar, False, False, 0)
        self._set_status(ConnectionStatus.CONNECTING, _("Connecting…"))

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
        return make_backend(self._host, self._user(), password, host_key_policy=policy, settings=self.settings)

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
        start = self.settings.initial_local_dir or remembered or self.local_backend.home()
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
    def transfer(self, source: FilePane, target: FilePane, entries: List[RemoteEntry],
                 destination: Optional[str] = None) -> None:
        """Copy ``entries`` from ``source`` into ``target``'s current folder (simple sequential copy).

        Phase F3 replaces this with the transfer queue (folders, conflicts,
        progress); remote names are already made safe for the local disk.
        """

        if self.backend is None or target.current_path is None:
            return
        if target.side == "remote" and target.read_only:
            self._log(_("This host is read-only: uploads are disabled."), Level.WARNING)
            return
        files = [e for e in entries if e.kind is EntryKind.FILE]
        skipped = len(entries) - len(files)
        if skipped:
            self._log(_("Folders and links are transferred by the transfer queue; {n} item(s) skipped.").format(
                n=skipped), Level.INFO)
        if not files:
            return
        directory = destination or target.current_path
        src_backend, dst_backend = source.backend(), target.backend()
        if src_backend is None or dst_backend is None:
            return
        token = CancelToken()
        self._tokens.append(token)
        atomic = self.settings.upload_atomic

        def work() -> int:
            for entry in files:
                token.raise_if_cancelled()
                if target.side == "local":
                    path = str(contained_path(Path(directory), entry.name))
                else:
                    path = target.child_path(entry.name) if destination is None else os.path.join(directory,
                                                                                                    entry.name)
                copy_file(src_backend, entry.path, dst_backend, path, size=entry.size, cancel=token,
                          atomic=atomic if target.side == "remote" else True)
            return len(files)

        def finished(count: int) -> None:
            self._set_status(ConnectionStatus.CONNECTED, _("Transferred {n} file(s).").format(n=count))
            target.refresh()

        def failed(exc: BaseException) -> None:
            self._report(exc)
            self._set_status(ConnectionStatus.CONNECTED, _("Transfer failed."))
            target.refresh()

        self._set_status(ConnectionStatus.CONNECTED, _("Transferring {n} file(s)…").format(n=len(files)))
        self.worker.submit(work, finished, failed)

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
        backend, self.backend = self.backend, None
        if backend is not None:
            self.worker.submit(backend.close)
        self.worker.shutdown()
        self.local_worker.shutdown()
        if self._temp_dir and bool(getattr(self.prefs, "clean_temp_on_exit", True)):
            shutil.rmtree(self._temp_dir, ignore_errors=True)
