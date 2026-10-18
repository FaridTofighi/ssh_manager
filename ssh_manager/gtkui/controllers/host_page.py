"""Host page: builds :class:`HostPageContent` from the selection and wires actions.

Owns the per-host history (last connected + activity), the latency probe and
the timers that keep the header fresh. Every timer and probe is cancelled in
:meth:`shutdown` (window destroy).
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Callable, List, Optional, Tuple

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # type: ignore  # noqa: E402

from ...host_utils import host_connection_params  # noqa: E402
from ..constants import Icons, RowKind  # noqa: E402
from ..host_facts import (  # noqa: E402
    address_text,
    auth_summary,
    breadcrumb_for,
    connection_text,
    format_latency,
    format_relative_time,
    latency_level,
    proxy_text,
    ssh_command_text,
    uses_proxy,
)
from ..host_history import HostHistory  # noqa: E402
from ..i18n import _, ngettext  # noqa: E402
from ..latency import REFRESH_SECONDS, LatencyProbe, LatencyResult  # noqa: E402
from ..theme.loader import current_palette  # noqa: E402
from ..theme.tokens import Level  # noqa: E402
from ..views.host_page import HostPageContent, HostPageView, PageTab, latency_css  # noqa: E402
from ..widgets.menus import build_menu, popup_menu  # noqa: E402
from ..widgets.ready_state import ReadyState  # noqa: E402

if TYPE_CHECKING:  # pragma: no cover
    from ...gtk_app import SSHManagerWindow

LOGGER = logging.getLogger(__name__)

CLOCK_SECONDS = 60  # relative times ("5 minutes ago") refresh


def _idle(fn: Callable[[], bool]) -> None:
    GLib.idle_add(fn)


class TerminalPage(Gtk.Stack):
    """Terminal tab content: the ready state, or the session workspace."""

    def __init__(self, notebook: Gtk.Widget) -> None:
        Gtk.Stack.__init__(self)
        self.set_hhomogeneous(False)
        self.set_vhomogeneous(False)
        self.get_style_context().add_class("page-canvas")
        self.ready = ReadyState()
        self.add_named(self.ready, "ready")
        self.add_named(notebook, "sessions")

    def show_sessions(self, sessions: bool) -> None:
        name = "sessions" if sessions else "ready"
        child = self.get_child_by_name(name)
        if child is not None:
            child.show()
        self.set_visible_child_name(name)


class HostPageController:
    def __init__(self, win: "SSHManagerWindow", *, history_path: Optional[Path] = None) -> None:
        self.win = win
        self.history = HostHistory(history_path)
        self.latency = LatencyProbe(dispatch=_idle)
        self.latency.enabled = bool(getattr(win._ui, "latency_probe", True))
        self.view: Optional[HostPageView] = None
        self.terminal_page: Optional[TerminalPage] = None
        self._timers: List[int] = []
        self._latency_key: Optional[Tuple[str, int]] = None
        self._chrome_visible = True

    # ------------------------------------------------------------ building
    def build(self, notebook: Gtk.Widget) -> HostPageView:
        self.terminal_page = TerminalPage(notebook)
        view = HostPageView(self.terminal_page)
        view.page_bar.on_crumb = self.on_crumb
        view.page_bar.on_primary = self.on_primary
        view.page_bar.on_more = self.on_more
        view.info_bar.on_copy = self.copy_command
        view.on_tab_changed = self._on_tab_changed
        view.on_strip_merged = self._on_strip_merged
        if hasattr(notebook, "set_strip_start"):
            notebook.set_strip_start(view.strip_tabs)
        if hasattr(notebook, "on_strip_changed"):
            notebook.on_strip_changed = self._sync_sessions_strip
        self.view = view
        self.history.add_listener(lambda _key: self.refresh())
        self._timers.append(GLib.timeout_add_seconds(REFRESH_SECONDS, self._refresh_latency_tick))
        self._timers.append(GLib.timeout_add_seconds(CLOCK_SECONDS, self._clock_tick))
        return view

    def shutdown(self) -> None:
        for source in self._timers:
            GLib.source_remove(source)
        self._timers.clear()
        self.latency.shutdown()

    # ---------------------------------------------------------------- tabs
    def _on_tab_changed(self, tab: PageTab) -> None:
        if self.view is not None:
            self.view._show_tab(tab)

    def select_tab(self, tab: PageTab) -> None:
        if self.view is not None:
            self.view.select_tab(PageTab(tab))

    def goto_page_tab(self, index: int) -> None:
        tabs = [PageTab.TERMINAL, PageTab.DETAILS, PageTab.ACTIVITY]
        if 1 <= index <= len(tabs):
            self.select_tab(tabs[index - 1])

    def toggle_details(self) -> None:
        """F9: switch between the Terminal and Details tabs."""

        if self.view is None:
            return
        self.select_tab(PageTab.TERMINAL if self.view.current_tab is PageTab.DETAILS else PageTab.DETAILS)

    def set_chrome_visible(self, visible: bool) -> None:
        """Focus mode: only the tabs / session row and the terminal stay visible."""

        self._chrome_visible = visible
        if self.view is None:
            return
        self.view.set_chrome_visible(visible)
        if not visible:
            self.select_tab(PageTab.TERMINAL)

    def apply_density(self) -> None:
        if self.view is None:
            return
        if self.terminal_page is not None:
            self.terminal_page.ready.apply_density()
        self.view.apply_density()
        self._sync_sessions_strip()
        self.refresh()

    def set_narrow(self, narrow: bool) -> None:
        if self.view is not None:
            self.view.set_narrow(narrow)

    def _sync_sessions_strip(self) -> None:
        notebook = self.win.terminal_tabs if hasattr(self.win, "terminal_tabs") else None
        if self.view is not None and notebook is not None and hasattr(notebook, "strip_visible"):
            self.view.set_sessions_strip(notebook.strip_visible)

    def _on_strip_merged(self, merged: bool) -> None:
        notebook = self.win.terminal_tabs if hasattr(self.win, "terminal_tabs") else None
        if notebook is not None and hasattr(notebook, "set_strip_merged"):
            notebook.set_strip_merged(merged)

    # ----------------------------------------------------------- selection
    def selection(self):
        tree = self.win.tree
        hosts = tree.selected_hosts()
        if len(hosts) > 1:
            return "multi", None, {}, hosts
        kind_value, data = tree.current_item()
        kind = RowKind.coerce(kind_value)
        return kind, tree.selected_host() if kind is RowKind.HOST else None, data or {}, hosts

    def refresh(self) -> None:
        if self.view is None or getattr(self.win, "closing", False):
            return
        content = self.build_content()
        self.view.set_content(content)
        self._refresh_ready_state()

    def build_content(self) -> HostPageContent:
        kind, host, data, hosts = self.selection()
        now = time.time()
        if kind == "multi":
            return self._multi_content(hosts, now)
        if kind is RowKind.HOST and host is not None:
            return self._host_content(host, data, now)
        if isinstance(kind, RowKind):
            return self._group_content(kind, data, now)
        return self._overview_content(now)

    def _host_content(self, host, data, now: float) -> HostPageContent:
        creds = self.win.creds
        key = creds.host_key(host)
        user = creds.display_username_for_host(host)
        perms = creds.permissions_for_host(host)
        if not perms.get("can_view", True):
            return HostPageContent(
                kind="host",
                crumbs=breadcrumb_for(RowKind.HOST, host.parent_group, host.child_group, host.alias),
                title=host.alias,
                address=_("Viewing details is disabled for this host."),
                primary_label=_("_Connect"),
                now=now,
            )
        auth = self.auth_for(host)
        environment = str(host.metadata.get("environment", "") or "")
        live = self.win.terminal_tabs.has_session_for(host) if hasattr(self.win.terminal_tabs, "has_session_for") else False
        latency_text, latency_level_css = self._latency_value(host)
        stats = [
            (_("Last connected"), format_relative_time(self.history.last_connected(key), now), ""),
            (_("Latency"), latency_text, latency_level_css),
        ]
        profile = creds.profile_for_host(host)
        facts = [
            (_("Host"), host.option("hostname", "host") or host.alias, True),
            (_("User"), user or "—", True),
            (_("Port"), host.option("port") or "22", True),
            (_("Group"), f"{host.parent_group} / {host.child_group}", False),
            (_("Jump host"), host.option("proxyjump", "proxy_jump", "jump_host", "proxycommand") or "", True),
            (_("Identity file"), host.option("identityfile") or "", True),
            (_("Authentication"), auth.label + (f" ({auth.detail})" if auth.detail else ""), False),
            (_("Environment"), environment, False),
            (_("Owner"), str(host.metadata.get("owner", "") or ""), False),
            (_("Service"), str(host.metadata.get("service", "") or ""), False),
            (_("Session profile"), profile.name if profile else "", False),
            (_("Permissions"), creds.render_permissions(host) or (_("Read-only (shared)") if creds.host_is_read_only(host) else ""), False),
            (_("Notes"), str(host.metadata.get("notes", "") or ""), False),
        ]
        tags = [t.strip() for t in (host.option("tags") or "").split(",") if t.strip()]
        if live:
            primary, outlined, tip = _("_Reconnect"), True, _("Reconnect the current session (Ctrl+Shift+R)")
        else:
            primary, outlined = _("_Connect"), False
            tip = _("Open a session (Enter)") if perms.get("can_connect", True) else _("Connecting is disabled; you will be asked to confirm")
        return HostPageContent(
            kind="host",
            crumbs=breadcrumb_for(RowKind.HOST, host.parent_group, host.child_group, host.alias),
            title=host.alias,
            environment=environment,
            address=address_text(host, user),
            icon=Icons.HOST,
            stats=stats,
            primary_label=primary,
            primary_outlined=outlined,
            primary_tooltip=tip,
            info=[
                (_("Connection"), connection_text(host, user), True),
                (_("Auth"), auth.label, False),
                (_("Proxy"), proxy_text(host), False),
            ],
            copy_text=ssh_command_text(host, user),
            facts=facts,
            chips=tags,
            activity=self.history.events(key),
            now=now,
        )

    def _group_content(self, kind: RowKind, data, now: float) -> HostPageContent:
        tree = self.win.tree
        parent = data.get("parent", "")
        child = data.get("child", "")
        if kind is RowKind.PARENT:
            hosts = [h for group in self.win.manager.tree.get(parent, {}).values() for h in group.hosts]
            title = parent
        else:
            hosts = tree.hosts_in_child_group(parent, child)
            title = child.split("/")[-1] if child else parent
        connected = sum(1 for h in hosts if tree.host_status.get(self.win.creds.host_key(h)) == "connected")
        count = len(hosts)
        read_only = parent.startswith("[Shared] ")
        return HostPageContent(
            kind="group",
            crumbs=breadcrumb_for(kind, parent, child),
            title=title,
            address=ngettext("{n} host", "{n} hosts", count).format(n=count),
            icon=Icons.GROUP,
            stats=[
                (_("Hosts"), str(count), ""),
                (_("Connected"), str(connected), "good" if connected else ""),
            ],
            primary_label=_("Connect _all"),
            primary_sensitive=count > 0,
            primary_tooltip=_("Open a session for every host in this group"),
            info=[
                (_("Location"), parent if kind is RowKind.PARENT else f"{parent}/{child}", True),
                (_("Access"), _("Read-only (shared)") if read_only else _("Editable"), False),
            ],
            facts=[
                (_("Location"), str(self.win.manager.base_dir / parent / child) if child else str(self.win.manager.base_dir / parent), True),
                (_("Hosts"), str(count), False),
                (_("Connected"), str(connected), False),
            ],
            now=now,
        )

    def _multi_content(self, hosts, now: float) -> HostPageContent:
        count = len(hosts)
        names = ", ".join(h.alias for h in hosts[:4]) + ("…" if count > 4 else "")
        return HostPageContent(
            kind="multi",
            crumbs=breadcrumb_for(None),
            title=ngettext("{n} host selected", "{n} hosts selected", count).format(n=count),
            address=names,
            icon=Icons.HOST,
            primary_label=_("Connect _selected"),
            primary_tooltip=_("Open a session for every selected host"),
            facts=[(h.alias, address_text(h, self.win.creds.display_username_for_host(h)), True) for h in hosts],
            now=now,
        )

    def _overview_content(self, now: float) -> HostPageContent:
        count = len(self.win.tree.host_lookup)
        return HostPageContent(
            kind="empty",
            crumbs=breadcrumb_for(None),
            title=_("All hosts"),
            address=ngettext("{n} host", "{n} hosts", count).format(n=count),
            icon=Icons.SERVER,
            primary_label=_("Add host"),
            primary_tooltip=_("Add a new host (Ctrl+N)"),
            now=now,
        )

    # ---------------------------------------------------------------- facts
    def auth_for(self, host):
        creds = self.win.creds
        credential = creds.credential_for_host(host)
        vault = bool(host.metadata.get("vault_id"))
        has_password = False
        if not credential and not vault:
            try:
                hostname, port, user = host_connection_params(host)
                has_password = bool(self.win._secure_store.get(hostname, port, user))
            except Exception:  # locked store, keyring errors
                has_password = False
        return auth_summary(host, credential_name=credential.name if credential else "", vault=vault, has_password=has_password)

    def latency_key(self, host) -> Optional[Tuple[str, int]]:
        if uses_proxy(host):
            return None
        hostname, port, _user = host_connection_params(host)
        return (hostname, port) if hostname else None

    def _latency_value(self, host) -> Tuple[str, str]:
        key = self.latency_key(host)
        if key is None or not self.latency.enabled:
            return format_latency(None), ""
        result = self.latency.cached(key)
        if result is None:
            self._request_latency(host)
            return format_latency(None), ""
        if not self.latency.is_fresh(key):
            self._request_latency(host)
        level = latency_level(result.ms, failed=result.failed)
        return format_latency(result.ms, failed=result.failed), latency_css(level)

    def _request_latency(self, host, *, force: bool = False) -> None:
        perms = self.win.creds.permissions_for_host(host)
        if not perms.get("can_connect", True):
            return
        key = self.latency_key(host)
        if key is None:
            return
        self._latency_key = key
        self.latency.request(key, self._on_latency, force=force)

    def _on_latency(self, result: LatencyResult) -> None:
        _kind, host, _data, _hosts = self.selection()
        if host is not None and self.latency_key(host) == result.key:
            self.refresh()

    def inject_latency(self, host, ms: float) -> None:
        key = self.latency_key(host)
        if key is not None:
            self.latency.inject(key, ms)

    def set_latency_enabled(self, enabled: bool) -> None:
        self.latency.enabled = bool(enabled)
        self.refresh()

    def _refresh_latency_tick(self) -> bool:
        _kind, host, _data, _hosts = self.selection()
        if host is not None:
            self._request_latency(host, force=True)
        return GLib.SOURCE_CONTINUE

    def _clock_tick(self) -> bool:
        self.refresh()
        return GLib.SOURCE_CONTINUE

    # --------------------------------------------------------- ready state
    def _refresh_ready_state(self) -> None:
        page = self.terminal_page
        if page is None:
            return
        notebook = self.win.terminal_tabs
        has_sessions = bool(notebook.tabs()) if hasattr(notebook, "tabs") else False
        page.show_sessions(has_sessions)
        if has_sessions:
            return
        kind, host, _data, _hosts = self.selection()
        palette = current_palette()

        def strong(text: str) -> str:
            return f'<span weight="600" foreground="{palette.text_primary}">{GLib.markup_escape_text(text)}</span>'

        actions = self.win.actions
        if kind is RowKind.HOST and host is not None:
            page.ready.set_content(
                _("Ready to connect"),
                _("Start a secure SSH session with {host}. Your session will open here in the workspace.").format(host=strong(host.alias)),
                _("Connect to host"),
                self.win.sessions.connect_current_item,
                caption=self.auth_for(host).caption,
            )
        elif not self.win.tree.host_lookup and not self.win.manager.tree:
            page.ready.set_content(
                _("Add your first host"),
                GLib.markup_escape_text(_("Add a host, or open a folder that contains SSH config files.")),
                _("Add host"),
                self.win.hosts.prompt_new_host,
                primary_icon=Icons.NEW,
                secondary_label=_("_Open config folder"),
                on_secondary=self.win.inventory.open_inventory_folder,
            )
        elif kind == "multi":
            page.ready.set_content(
                _("Several hosts selected"),
                GLib.markup_escape_text(_("Open a session for each selected host.")),
                _("Connect _selected"),
                actions.bulk_connect_selected,
            )
        elif isinstance(kind, RowKind):
            page.ready.set_content(
                _("Ready to connect"),
                GLib.markup_escape_text(_("Open a session for every host in this group, or pick a single host.")),
                _("Connect _all"),
                self.win.sessions.connect_current_item,
            )
        else:
            page.ready.set_content(
                _("Select a host"),
                GLib.markup_escape_text(_("Choose a host from the list, or press Ctrl+K to search.")),
                _("Add host"),
                self.win.hosts.prompt_new_host,
                primary_icon=Icons.NEW,
                secondary_label=_("_Quick connect"),
                on_secondary=self.win.sessions.quick_connect,
            )

    # -------------------------------------------------------------- actions
    def on_primary(self) -> None:
        kind, host, _data, _hosts = self.selection()
        if kind == "multi":
            self.win.actions.bulk_connect_selected()
        elif kind is RowKind.HOST and host is not None and self.win.terminal_tabs.has_session_for(host):
            self.win.terminal_tabs.focus_session_for(host)
            self.win.terminal_tabs.reconnect_current()
        elif isinstance(kind, RowKind):
            self.win.sessions.connect_current_item()
        else:
            self.win.hosts.prompt_new_host()

    def on_more(self, widget: Gtk.Widget) -> None:
        spec = self.win.tree.context_menu_spec()
        kind, host, _data, _hosts = self.selection()
        if kind is RowKind.HOST and host is not None and self.win.terminal_tabs.has_session_for(host):
            from ..menu_spec import MenuItemSpec, MenuSection

            spec = [MenuSection([MenuItemSpec(_("New _Session"), lambda h=host: self.win.sessions.open_hosts([h]), icon=Icons.NEW)])] + list(spec)
        menu = build_menu(spec)
        menu.attach_to_widget(widget, None)
        popup_menu(menu, None, widget=widget)

    def on_crumb(self, target: Tuple[str, ...]) -> None:
        tree = self.win.tree
        if not target:
            self.win.tree_view.get_selection().unselect_all()
        elif target[0] == "parent":
            tree.select_parent_group(target[1])
        elif target[0] == "child":
            tree.select_child_group(target[1], target[2])
        self.refresh()

    def copy_command(self) -> None:
        text = self.view.content.copy_text if self.view is not None else ""
        if not text:
            return
        clipboard = Gtk.Clipboard.get_default(self.win.get_display())
        clipboard.set_text(text, -1)
        self.win._notify(_("Copied “{command}”.").format(command=text), Level.SUCCESS)

    # ------------------------------------------------------------- history
    def on_host_status(self, key: str, old: str, new: str) -> None:
        if old == new:
            return
        if new == "connected":
            self.history.record_connected(key, text=_("Connected"))
        elif new == "failed":
            self.history.record_event(key, Level.ERROR.value, _("Connection failed"))
        elif old == "connected" and new == "unknown":
            self.history.record_event(key, Level.INFO.value, _("Disconnected"))

    def on_password_changed(self, host) -> None:
        self.history.record_event(self.win.creds.host_key(host), Level.INFO.value, _("Password updated"))
