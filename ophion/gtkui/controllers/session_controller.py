"""Opening, connecting and auditing terminal sessions."""

from __future__ import annotations

import copy
import dataclasses
import datetime
import logging
import shlex
import threading
from functools import partial
from pathlib import Path
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Dict, List, Optional

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gio, Gtk  # type: ignore  # noqa: E402

from ... import protocols  # noqa: E402
from ...config import HostEntry  # noqa: E402
from ...connection_state import ConnectionStateMachine  # noqa: E402
from ...connector import Connector  # noqa: E402
from ...host_utils import build_scp_command, host_option  # noqa: E402
from ...persistence import append_text  # noqa: E402
from ...profiles import SessionProfile  # noqa: E402
from ...ssh_command import build_ssh_command  # noqa: E402
from ..host_facts import ssh_command_text  # noqa: E402
from ..dialogs.confirm import confirm  # noqa: E402
from ..constants import RowKind  # noqa: E402
from ..i18n import _  # noqa: E402
from ..theme.tokens import Level  # noqa: E402
from . import details_presenter  # noqa: E402

if TYPE_CHECKING:  # pragma: no cover
    from ...gtk_app import SSHManagerWindow


LOGGER = logging.getLogger(__name__)


def audit_log_key(value: object) -> str:
    """Normalise an audit-log field name to ``[a-z0-9_-]+``."""

    text = str(value).strip().lower()
    cleaned = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in text)
    return cleaned.strip("_") or "field"


def audit_log_value(value: object, *, default: str = "") -> str:
    """Collapse whitespace so a value stays one token in the audit log."""

    text = " ".join(str(value).split())
    return text.replace(" ", "_") or default


@dataclass
class UIHostConfig:
    """Connection parameters handed to the Paramiko connector."""

    host: str
    port: int
    username: str
    timeout: float
    parent_window: Gtk.Window
    options: Dict[str, str] = field(default_factory=dict)
    alias: str = ""  # names the host in the password dialogs



# Openers by protocol: a terminal for SSH, the file browser for the FTP family.
protocols.set_opener("ssh", lambda win, host: win.sessions.open_ssh_host(host))
protocols.set_opener("rdp", lambda win, host: win.sessions.open_rdp_host(host))
for _transfer_protocol in ("ftp", "ftps", "sftp"):
    protocols.set_opener(_transfer_protocol, lambda win, host: win.sessions.open_transfer_host(host))


class SessionController:
    """Opening, connecting and auditing terminal sessions."""

    def __init__(self, win: "SSHManagerWindow") -> None:
        self.win = win
        self.paramiko_connectors: Dict[object, Connector] = {}
        self.audit_lock = threading.Lock()
        self._rdp_runtime = None  # created on first use (child registry, limits, trust store)
        self.rdp_discovery: Optional[threading.Thread] = None  # the running FreeRDP discovery, if any

    @property
    def rdp_runtime(self):
        from ..rdp.runtime import RdpRuntime

        if self._rdp_runtime is None:
            self._rdp_runtime = RdpRuntime(max_sessions=self.win._settings.remote_desktop.max_sessions)
        return self._rdp_runtime

    def bind_tab_status(self, tab, host: HostEntry) -> None:
        host_key = self.win.creds.host_key(host)
        notified = {"connected": False}

        def on_status(tab, _alias, connected: bool) -> None:
            status = getattr(tab, "status", None)
            state = status.value if status is not None else ("connected" if connected else "unknown")
            self.win.tree.set_host_status_for_key(host_key, state)
            if connected and not notified["connected"]:
                notified["connected"] = True
                self.notify_connection_success(host)

        def on_closed(_tab, _alias, exited_normally: bool) -> None:
            self.win.tree.set_host_status_for_key(host_key, "unknown" if exited_normally else "failed")

        def on_destroy(*_args) -> None:
            if self.win.tree.host_status.get(host_key) != "failed":
                self.win.tree.set_host_status_for_key(host_key, "unknown")

        try:
            tab.connect("status-changed", on_status)
            tab.connect("session-closed", on_closed)
            tab.connect("destroy", on_destroy)
        except Exception:
            pass

    def open_hosts(self, hosts: List[HostEntry], source: Optional[str] = None) -> None:
        if not hosts:
            if source:
                self.win._notify(_("No hosts found in {source}.").format(source=source), Level.INFO)
            else:
                self.win._notify(_("No hosts to connect."), Level.INFO)
            return

        opened: List[str] = []
        for host in hosts:
            host_obj = self.win.tree.coerce_host(host)
            if not host_obj:
                self.win._notify(_("Selected item is not a host; skipping."), Level.INFO)
                continue
            # Each host opens in its own kind of tab (terminal for SSH, file browser for FTP/SFTP …).
            protocol = protocols.protocol_of(host_obj)
            kind = protocols.kind_for(host_obj)
            opener = protocols.opener_for(protocol)
            if kind is None or opener is None:
                self.win._notify(_("“{alias}” uses a protocol this version cannot open ({protocol}).").format(
                    alias=host_obj.alias, protocol=kind.label if kind else protocol), Level.WARNING)
                continue
            problems = kind.validate(host_obj) if not kind.terminal else []
            if problems:
                self.win._notify(_("Cannot open “{alias}”: {problem}").format(alias=host_obj.alias,
                                                                              problem=_(problems[0])), Level.WARNING)
                continue
            self.audit_log("connect", host_obj)
            perms = self.win.creds.permissions_for_host(host_obj)
            if not perms.get("can_connect", True):
                if not confirm(
                    self.win,
                    _("Connect to “{alias}” anyway?").format(alias=host_obj.alias),
                    _("Connecting is disabled for this host by its local permissions."),
                    confirm_label=_("C_onnect Anyway"),
                    destructive=True,
                ):
                    continue
            alias = opener(self.win, host_obj)
            if alias:
                opened.append(alias)

        if not opened:
            return
        if len(opened) == 1:
            self.win._notify(_("Opened {alias}.").format(alias=opened[0]), Level.SUCCESS)
        else:
            suffix = f" from {source}" if source else ""
            self.win._notify(_("Opened {count} connections{suffix}.").format(count=len(opened), suffix=suffix), Level.SUCCESS)

    def open_ssh_host(self, host_obj: HostEntry) -> Optional[str]:
        """The SSH opener (protocol registry): a terminal tab; returns the alias, or ``None``."""

        resolved, _resolution = self.win.creds.resolve_host_credentials(host_obj)
        provider = partial(self.win.creds.resolve_password, host_obj)  # re-resolved at every (re)connect
        if not isinstance(resolved, HostEntry):
            self.win._notify(_("Unable to resolve host entry; skipping."), Level.WARNING)
            return None
        profile = self.win.creds.profile_for_host(resolved)
        if self.win._settings.use_paramiko_vte:
            tab = self.open_host_paramiko(resolved, profile=profile, password_provider=provider)
        else:
            self.win._notify(_("Opening {alias}").format(alias=resolved.alias), Level.INFO)
            tab = self.win.terminal_tabs.new_terminal(resolved, profile=profile, password_provider=provider,
                                                      password_service=self.win.creds)
            self.bind_tab_status(tab, resolved)
        keys = getattr(self.win, "ssh_keys", None)
        if tab is not None and keys is not None:
            tab.passphrase_provider = keys.saved_passphrase  # ssh's "Enter passphrase for key" prompt
        self.start_session_tunnels(host_obj, tab)
        return resolved.alias

    def start_session_tunnels(self, host: HostEntry, tab) -> None:
        """Forwards set to start with a session run beside the terminal (the engine, not ``ssh -L``)."""

        tunnels = getattr(self.win, "tunnels", None)
        if tunnels is None or tab is None:
            return
        tunnels.session_opened(host)
        key = self.win.creds.credential_key(host)

        def on_destroy(*_args) -> None:
            others = [t for t in self.win.terminal_tabs.tabs() if t is not tab and getattr(t, "is_terminal", False)
                      and getattr(t, "_host", None) is not None
                      and self.win.creds.credential_key(t._host) == key]
            if not others:
                tunnels.last_session_closed(host)

        try:
            tab.connect("destroy", on_destroy)
        except Exception:
            pass

    def open_transfer_host(self, host_obj: HostEntry, bind_status: bool = True) -> Optional[str]:
        """The FTP / FTPS / SFTP opener: a file-browser tab; returns the alias.

        ``bind_status=False`` leaves the tree status alone (the "Files" view of an SSH host).
        """

        from ..file_browser.tab import FileBrowserTab

        creds = self.win.creds
        resolved = creds.host_with_resolved_identity(host_obj)
        read_only = not creds.permissions_for_host(host_obj).get("can_edit", True) or creds.host_is_read_only(host_obj)
        self.win._notify(_("Opening {alias}").format(alias=resolved.alias), Level.INFO)
        tab = FileBrowserTab(resolved, self.win._log_event, password_provider=partial(creds.resolve_password, host_obj),
                             password_service=creds, app_settings=self.win._settings, read_only=read_only)
        self.win.terminal_tabs.add_page(tab)
        if bind_status:
            self.bind_tab_status(tab, resolved)
        return resolved.alias

    # ------------------------------------------------------------- RDP
    def refresh_rdp_engines(self) -> None:
        """Look for FreeRDP clients again (off the GTK thread); dialogs read the cached report."""

        from ...rdp.engines import discover_async

        prefs = self.win._settings.remote_desktop
        self.rdp_discovery = discover_async(lambda _report: None, preference=prefs.preferred_engine,
                                            override=prefs.binary_path)
        self.rdp_runtime.set_limit(prefs.max_sessions)

    def open_rdp_host(self, host_obj: HostEntry) -> Optional[str]:
        """The RDP opener: a session tab (it explains a missing FreeRDP itself, never a traceback)."""

        from ...rdp.engines import cached_report, install_hint
        from ..rdp.tab import RdpSessionTab

        report = cached_report()
        if report is not None and not report.any_available:
            self.win._notify(_("“{alias}” cannot be opened: no FreeRDP client was found. {hint}").format(
                alias=host_obj.alias, hint=install_hint(report.platform)), Level.WARNING)
            return None
        creds = self.win.creds
        resolved = creds.host_with_resolved_identity(host_obj)
        self.win._notify(_("Opening {alias}").format(alias=resolved.alias), Level.INFO)
        tab = RdpSessionTab(resolved, self.win._log_event, runtime=self.rdp_runtime, app_settings=self.win._settings,
                            password_provider=partial(creds.resolve_password, host_obj), password_service=creds,
                            tunnel_factory=self.rdp_tunnel_backend, gateway_password_provider=self.credential_password)
        tab.on_fullscreen = self.win.layout.set_maximized
        self.win.terminal_tabs.add_page(tab)
        self.bind_tab_status(tab, resolved)
        return resolved.alias

    def inventory_host(self, host_id: str) -> Optional[HostEntry]:
        for children in self.win.manager.tree.values():
            for group in children.values():
                for host in group.hosts:
                    if getattr(host, "id", "") == host_id:
                        return host
        return None

    def rdp_tunnel_backend(self, host_id: str):
        """An SSH connection (not connected yet) to the inventory host an RDP host tunnels through."""

        from ...transfer.sftp import backend_for

        host = self.inventory_host(host_id)
        if host is None or not protocols.is_ssh(host):
            raise LookupError("the SSH host of the tunnel no longer exists")
        creds = self.win.creds
        resolved = creds.host_with_resolved_identity(host)
        resolution = creds.resolve_password(host)
        return backend_for(resolved, resolved.option("user") or "", resolution.password or "",
                           host_key_policy=getattr(self.win._settings, "host_key_policy", "reject"),
                           passphrase=self.saved_key_passphrase(resolved))

    def saved_key_passphrase(self, host) -> str:
        """The passphrase saved for the host's identity file ('' if none): Paramiko connections use it."""

        keys = getattr(self.win, "ssh_keys", None)
        return keys.passphrase_for_host(host) if keys is not None else ""

    def credential_password(self, credential_id: str) -> Optional[str]:
        return self.win._credential_store.get_password(credential_id) or None

    def open_host_paramiko(self, host: HostEntry, profile: Optional[SessionProfile] = None,
                           password_provider=None):
        tab = self.win.terminal_tabs.new_terminal(host, autostart=False, profile=profile,
                                                  password_provider=password_provider, password_service=self.win.creds)
        self.bind_tab_status(tab, host)
        host_cfg = self.build_host_config(host)
        state_machine = ConnectionStateMachine()
        connector = Connector(
            self.win._password_dialog,
            self.win._secure_store,
            self.win._settings,
            state_machine,
        )
        self.paramiko_connectors[tab] = connector
        connector.add_connect_success_callback(lambda session, tab=tab: self.on_paramiko_success(tab, session))
        connector.add_aborted_callback(lambda reason, tab=tab: self.on_paramiko_aborted(tab, reason))
        connector.add_error_callback(lambda message, tab=tab: self.win._notify_error(message))
        connector.add_warning_callback(lambda message, tab=tab: self.win._notify_warning(message))
        # A typed password that logged in: the tab asks "Save this password for this host?" (nothing auto-saved).
        connector.add_password_verified_callback(tab.offer_save_password)
        tab.connect("destroy", lambda *_: self.cleanup_paramiko_connector(tab))
        connector.connect_to_host(host_cfg, password_provider=password_provider,
                                  describe_source=self.win.creds.describe_password_source)
        return tab

    def cleanup_paramiko_connector(self, tab) -> None:
        self.paramiko_connectors.pop(tab, None)

    def on_paramiko_success(self, tab, session) -> None:
        self.cleanup_paramiko_connector(tab)
        if not tab.get_parent():
            try:
                session.close()
            except Exception:
                pass
            return
        tab.attach_session(session)
        try:
            self.win.tree.set_host_status(tab.host, "connected")
        except Exception:
            pass

    def on_paramiko_aborted(self, tab, reason: str) -> None:
        self.cleanup_paramiko_connector(tab)
        self.win._log_event(reason or "Authentication cancelled.", False)
        try:
            self.win.tree.set_host_status(tab.host, "failed")
        except Exception:
            pass
        self.win.terminal_tabs.close_tab(tab)

    def build_host_config(self, host: HostEntry) -> UIHostConfig:
        hostname, port, username = self.win.creds.host_connection_params(host)
        return UIHostConfig(
            host=hostname,
            port=port,
            username=username,
            timeout=self.win._settings.connect_timeout,
            parent_window=self.win,
            options=dict(host.options),
            alias=host.alias,
        )

    def notify_connection_success(self, host: HostEntry) -> None:
        message = f"Connected to {host.alias}."
        app = self.win.get_application()
        if app and hasattr(app, "send_notification"):
            notification = Gio.Notification.new("SSH connected")
            notification.set_body(message)
            app.send_notification(f"ssh-manager-connected-{self.win.creds.host_key(host)}", notification)
            return
        self.win._log_event(message, True)

    def audit_log(self, action: str, host: HostEntry, *, extra: Optional[Dict[str, str]] = None) -> None:
        if not getattr(self.win._settings, "enable_audit_logging", False):
            return
        path_text = getattr(self.win._settings, "audit_log_path", "")
        if not path_text:
            return
        timestamp = datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z"
        host_display = audit_log_value(host.alias or host_option(host, "hostname") or "", default="unknown")
        username = audit_log_value(host_option(host, "user") or "")
        fields: List[str] = [f"{timestamp} [{action}] host={host_display}"]
        if username:
            fields.append(f"user={username}")
        fields.append(f"protocol={audit_log_value(protocols.protocol_of(host))}")
        if extra:
            for k, v in extra.items():
                if v:
                    fields.append(f"{audit_log_key(k)}={audit_log_value(v)}")
        line = " ".join(fields) + "\n"

        def _write_line() -> None:
            try:
                path = Path(path_text).expanduser()
                with self.audit_lock:
                    append_text(path, line, mode=0o600)
            except Exception:
                pass

        threading.Thread(target=_write_line, daemon=True).start()

    def open_selected_host(self) -> None:
        hosts = self.win.tree.selected_hosts()
        if not hosts:
            host = self.win.tree.selected_host()
            if not host:
                self.win._notify(_("Select a host to connect."), Level.INFO)
                return
            hosts = [host]
        self.open_hosts(hosts)

    def connect_selected(self) -> None:
        if self.win.tree.current_item_is_read_only():
            self.win._notify(_("Shared hosts are read-only; connect is allowed."), Level.INFO)
        self.open_selected_host()

    def connect_child_group(self) -> None:
        kind, data = self.win.tree.current_item()
        if kind not in {RowKind.CHILD.value, RowKind.CHILD_FOLDER.value}:
            return
        parent = data["parent"]
        child = data["child"]
        hosts = self.win.tree.hosts_in_child_group(parent, child)
        self.open_hosts(hosts, f"group {parent}/{child}")

    def connect_parent_group(self) -> None:
        kind, data = self.win.tree.current_item()
        if kind != RowKind.PARENT.value:
            return
        parent = data["parent"]
        hosts = self.win.tree.hosts_in_parent_group(parent)
        self.open_hosts(hosts, f"parent {parent}")

    def connect_current_item(self) -> None:
        kind, _ = self.win.tree.current_item()
        if kind == RowKind.CHILD.value:
            self.connect_child_group()
        elif kind == RowKind.PARENT.value:
            self.connect_parent_group()
        else:
            self.open_selected_host()

    def reconnect_selected(self) -> None:
        host = self.win.tree.selected_host()
        tab = self.win.terminal_tabs.find_tab(host) if host else None
        if tab is None:
            self.connect_selected()
            return
        self.win.terminal_tabs.focus_tab(tab)
        tab.reconnect()

    def duplicate_current_session(self) -> None:
        tab = self.win.terminal_tabs.current_tab()
        if tab is None:
            self.win._notify_info(_("Open a session first."))
            return
        if getattr(tab, "is_local", False):
            self.win.terminal_tabs.duplicate_tab(tab)
            return
        self.open_hosts([tab.host])

    def quick_connect(self) -> None:
        from ophion.gtkui.dialogs.prompt import PromptDialog
        from ophion.protocols.urls import looks_like_url, parse_connection_url

        def validate(text: str) -> Optional[str]:
            if self.win.tree.find_host_by_alias(text.strip()) is not None:
                return None
            if looks_like_url(text):
                _url, reason = parse_connection_url(text)
                return _(reason) if reason else None
            if details_presenter.parse_quick_connect(text) is None:
                return _("Use [user@]host[:port], a URL such as sftp://user@host or rdp://user@host, or the alias "
                         "of a saved host.")
            return None

        value = PromptDialog(
            self.win,
            _("Quick Connect"),
            _("Connect to a saved host by alias, to any SSH server as [user@]host[:port], open sftp://, "
              "ftps:// or ftp:// URLs in a file browser, or rdp:// URLs (and host:3389) as a remote desktop."),
            placeholder=_("deploy@web-01.example.com:22"),
            accept_label=_("C_onnect"),
            validator=validate,
        ).ask()
        if not value:
            return
        saved = self.win.tree.find_host_by_alias(value)
        if saved is not None:
            self.open_hosts([saved])
            return
        adhoc = quick_connect_host(value)
        if adhoc is None:
            return
        self.open_unsaved(adhoc)

    def open_unsaved(self, adhoc: HostEntry) -> None:
        """Open a host that is not in the inventory (Quick Connect, --rdp) and offer to save it."""

        self.open_hosts([adhoc])
        self.win._notify(_("“{alias}” is not saved.").format(alias=adhoc.alias), Level.INFO,
                         action_label=_("Save to Inventory…"),
                         action=lambda host=adhoc: self.win.hosts.save_to_inventory(host))

    def open_files_selected(self) -> None:
        host = self.win.tree.selected_host()
        if not host:
            self.win._notify_info(_("Select an SSH host to browse its files."))
            return
        self.open_files_for_host(host)

    def open_files_for_host(self, host: HostEntry) -> Optional[str]:
        """An SFTP file browser for an SSH host: same address, user, key, jump host and saved password.

        Nothing is stored: the SSH host stays an SSH host; the tab is a view of it over SFTP.
        """

        if not protocols.is_ssh(host):
            return self.open_transfer_host(host) if protocols.protocol_of(host) == "sftp" else None
        view = files_view_of(host)
        if not self.win.creds.permissions_for_host(host).get("can_connect", True) and not confirm(
            self.win,
            _("Connect to “{alias}” anyway?").format(alias=host.alias),
            _("Connecting is disabled for this host by its local permissions."),
            confirm_label=_("C_onnect Anyway"),
            destructive=True,
        ):
            return None
        self.audit_log("connect", view)
        return self.open_transfer_host(view, bind_status=False)

    def copy_ssh_command(self) -> None:
        host = self.win.tree.selected_host()
        if not host:
            self.win._notify_info(_("Select a host to copy its SSH command."))
            return
        resolved = self.win.creds.host_with_resolved_identity(host)
        if not protocols.is_ssh(resolved):  # an FTP / SFTP host: its address (never a password)
            user = self.win.creds.display_username_for_host(host)
            Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(ssh_command_text(resolved, user), -1)
            self.win._notify(_("Address copied to the clipboard."), Level.SUCCESS)
            return
        command = shlex.join(build_ssh_command(resolved))
        Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(command, -1)
        self.win._notify(_("SSH command copied to the clipboard."), Level.SUCCESS)

    def copy_scp_command(self) -> None:
        host = self.win.tree.selected_host()
        if not host:
            self.win._notify(_("Select a host to copy SCP command."), Level.INFO)
            return
        if not protocols.is_ssh(host):
            self.win._notify(_("“{alias}” is not an SSH host; there is no SCP command.").format(alias=host.alias),
                             Level.INFO)
            return
        command = shlex.join(build_scp_command(host))
        try:
            clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
            clipboard.set_text(command, -1)
            self.win._notify(_("SCP command copied to clipboard."), Level.SUCCESS)
        except Exception as exc:
            self.win._notify(_("Unable to copy command: {error}").format(error=exc), Level.ERROR)

    def bulk_connect_selected(self) -> None:
        hosts = self.win.tree.selected_hosts()
        if len(hosts) < 2:
            return
        for host in hosts:
            self.open_hosts([host])


def quick_connect_host(value: str) -> Optional[HostEntry]:
    """An unsaved host for Quick Connect: ``[user@]host[:port]`` (SSH) or an ssh/sftp/ftp/ftps URL."""

    from ophion.protocols.urls import looks_like_url, parse_connection_url

    group = _("Quick Connect")
    if looks_like_url(value):
        url, _reason = parse_connection_url(value)
        if url is None:
            return None
        transfer: Dict[str, object] = {"initial_remote_dir": url.path} if url.path else {}
        if url.protocol == "ftps" and url.port == 990:
            transfer["tls_mode"] = "implicit"  # the implicit-FTPS port
        metadata: Dict[str, object] = {"transfer": {"v": 1, **transfer}} if transfer else {}
        if url.protocol == "rdp":
            metadata = {"rdp": {"v": 1}}
        return HostEntry(alias=url.label(), options=url.options(), metadata=metadata, parent_group=group,
                         child_group="", protocol=url.protocol)
    parsed = details_presenter.parse_quick_connect(value)
    if parsed is None:
        return None
    user, host, port = parsed
    if port == "3389":  # the RDP port: a Remote Desktop, not SSH
        rdp_options = {"hostname": host, **({"user": user} if user else {})}
        return HostEntry(alias=value, options=rdp_options, metadata={"rdp": {"v": 1}}, parent_group=group,
                         child_group="", protocol="rdp")
    options = {"hostname": host}
    if user:
        options["user"] = user
    if port:
        options["port"] = port
    return HostEntry(alias=value, options=options, parent_group=group, child_group="")


def files_view_of(host: HostEntry) -> HostEntry:
    """The SFTP view of an SSH host (same id, so its credential and saved password apply)."""

    return dataclasses.replace(host, options=dict(host.options), metadata=copy.deepcopy(host.metadata),
                               protocol="sftp")
