"""Opening, connecting and auditing terminal sessions."""

from __future__ import annotations

import datetime
import logging
import shlex
import threading
from pathlib import Path
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Dict, List, Optional

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gio, Gtk  # type: ignore  # noqa: E402

from ...config import HostEntry  # noqa: E402
from ...connection_state import ConnectionStateMachine  # noqa: E402
from ...connector import Connector  # noqa: E402
from ...host_utils import build_scp_command, host_option  # noqa: E402
from ...persistence import append_text  # noqa: E402
from ...profiles import SessionProfile  # noqa: E402
from ...ssh_command import build_ssh_command  # noqa: E402
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
    remember_password_default: bool
    parent_window: Gtk.Window
    options: Dict[str, str] = field(default_factory=dict)



class SessionController:
    """Opening, connecting and auditing terminal sessions."""

    def __init__(self, win: "SSHManagerWindow") -> None:
        self.win = win
        self.paramiko_connectors: Dict[object, Connector] = {}
        self.audit_lock = threading.Lock()

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
            resolved = self.win.creds.resolve_host_credentials(host_obj)
            if not isinstance(resolved, HostEntry):
                self.win._notify(_("Unable to resolve host entry; skipping."), Level.WARNING)
                continue
            profile = self.win.creds.profile_for_host(resolved)
            if self.win._settings.use_paramiko_vte:
                self.open_host_paramiko(resolved, profile=profile)
            else:
                self.win._notify(_("Opening {alias}").format(alias=resolved.alias), Level.INFO)
                tab = self.win.terminal_tabs.new_terminal(resolved, profile=profile)
                self.bind_tab_status(tab, resolved)
            opened.append(resolved.alias)

        if not opened:
            return
        if len(opened) == 1:
            self.win._notify(_("Opened {alias}.").format(alias=opened[0]), Level.SUCCESS)
        else:
            suffix = f" from {source}" if source else ""
            self.win._notify(_("Opened {count} connections{suffix}.").format(count=len(opened), suffix=suffix), Level.SUCCESS)

    def open_host_paramiko(self, host: HostEntry, profile: Optional[SessionProfile] = None) -> None:
        tab = self.win.terminal_tabs.new_terminal(host, autostart=False, profile=profile)
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
        tab.connect("destroy", lambda *_: self.cleanup_paramiko_connector(tab))
        connector.connect_to_host(host_cfg)

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
            remember_password_default=self.win._settings.remember_password_default,
            parent_window=self.win,
            options=dict(host.options),
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
        self.open_hosts([tab.host])

    def quick_connect(self) -> None:
        from ssh_manager.gtkui.dialogs.prompt import PromptDialog

        def validate(text: str) -> Optional[str]:
            if self.win.tree.find_host_by_alias(text.strip()) is not None:
                return None
            if details_presenter.parse_quick_connect(text) is None:
                return _("Use [user@]host[:port] or the alias of a saved host.")
            return None

        value = PromptDialog(
            self.win,
            _("Quick Connect"),
            _("Connect to a saved host by alias, or to any server as [user@]host[:port]."),
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
        parsed = details_presenter.parse_quick_connect(value)
        if parsed is None:
            return
        user, host, port = parsed
        options = {"hostname": host}
        if user:
            options["user"] = user
        if port:
            options["port"] = port
        adhoc = HostEntry(alias=value, options=options, parent_group=_("Quick Connect"), child_group="")
        self.open_hosts([adhoc])

    def copy_ssh_command(self) -> None:
        host = self.win.tree.selected_host()
        if not host:
            self.win._notify_info(_("Select a host to copy its SSH command."))
            return
        resolved = self.win.creds.host_with_resolved_identity(host)
        command = shlex.join(build_ssh_command(resolved))
        Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(command, -1)
        self.win._notify(_("SSH command copied to the clipboard."), Level.SUCCESS)

    def copy_scp_command(self) -> None:
        host = self.win.tree.selected_host()
        if not host:
            self.win._notify(_("Select a host to copy SCP command."), Level.INFO)
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
