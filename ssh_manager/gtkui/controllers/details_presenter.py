"""Build :class:`DetailContent` for the details drawer (GTK-free).

The presenter turns the current selection into display data plus action
callbacks. The callbacks object is the window (or anything implementing the
methods named in :class:`DetailCallbacks`).
"""

from __future__ import annotations

from typing import List, Optional, Protocol, Sequence

from ...config import HostEntry
from ..constants import Icons, RowKind
from ..i18n import _, ngettext
from ..theme.tokens import ConnectionStatus
from ..view_models import DetailAction, DetailContent, placeholder_content


class DetailCallbacks(Protocol):
    def _connect_selected(self) -> None: ...
    def _edit_selected(self) -> None: ...
    def _duplicate_selected(self) -> None: ...
    def _copy_ssh_command(self) -> None: ...
    def _copy_scp_command(self) -> None: ...
    def _move_selected(self) -> None: ...
    def _forget_password_for_current_host(self) -> None: ...
    def _delete_selected(self) -> None: ...
    def _connect_child_group(self) -> None: ...
    def _connect_parent_group(self) -> None: ...
    def _create_host_in_current_group(self) -> None: ...
    def _on_edit_clicked(self) -> None: ...
    def _on_delete_clicked(self) -> None: ...
    def _bulk_connect_selected(self) -> None: ...
    def _bulk_move_selected(self) -> None: ...
    def _bulk_tag_selected(self) -> None: ...


def _opt(host: HostEntry, *names: str) -> str:
    return host.option(*names) or ""


def host_tags(host: HostEntry) -> List[str]:
    return [tag.strip() for tag in _opt(host, "tags").split(",") if tag.strip()]


def host_subtitle(host: HostEntry, display_user: str) -> str:
    target = _opt(host, "hostname", "host") or host.alias
    port = _opt(host, "port")
    text = f"{display_user}@{target}" if display_user else target
    if port and port != "22":
        text = f"{text}:{port}"
    return text


def host_detail(
    host: HostEntry,
    callbacks: DetailCallbacks,
    *,
    display_user: str = "",
    status: object = ConnectionStatus.UNKNOWN,
    profile_name: str = "",
    permissions_text: str = "",
    read_only: bool = False,
    can_view: bool = True,
    can_connect: bool = True,
) -> DetailContent:
    """Details for a single host."""

    if not can_view:
        return DetailContent(
            title=host.alias,
            subtitle=_("Viewing details is disabled for this host."),
            status=ConnectionStatus.coerce(status),
        )
    environment = str(host.metadata.get("environment", "") or "")
    chips = host_tags(host)
    accent = []
    if environment and environment not in chips:
        chips.insert(0, environment)
        accent.append(environment)
    facts = [
        (_("Group"), f"{host.parent_group}/{host.child_group}"),
        (_("Port"), _opt(host, "port") or "22"),
        (_("Jump host"), _opt(host, "proxyjump", "proxy_jump", "jump_host", "proxycommand")),
        (_("Identity file"), _opt(host, "identityfile")),
        (_("Environment"), environment),
        (_("Owner"), str(host.metadata.get("owner", "") or "")),
        (_("Service"), str(host.metadata.get("service", "") or "")),
        (_("Session profile"), profile_name),
        (_("Permissions"), permissions_text or (_("Read-only (shared)") if read_only else "")),
        (_("Notes"), str(host.metadata.get("notes", "") or "")),
    ]
    editable = not read_only
    actions = [
        DetailAction(_("_Connect"), callbacks._connect_selected, Icons.CONNECT, primary=True,
                     tooltip=_("Open a session (Enter)") if can_connect else _("Connecting is disabled; you will be asked to confirm")),
        DetailAction(_("Edit"), callbacks._edit_selected, Icons.EDIT, sensitive=editable, tooltip=_("Edit host")),
        DetailAction(_("Duplicate"), callbacks._duplicate_selected, Icons.DUPLICATE, sensitive=editable, tooltip=_("Duplicate host")),
        DetailAction(_("Copy SSH Command"), callbacks._copy_ssh_command, Icons.TERMINAL, tooltip=_("Copy SSH command")),
        DetailAction(_("Copy SCP Command"), callbacks._copy_scp_command, Icons.COPY, overflow=True),
        DetailAction(_("Move to Group…"), callbacks._move_selected, Icons.MOVE, overflow=True, sensitive=editable),
        DetailAction(_("Forget Password"), callbacks._forget_password_for_current_host, Icons.FORGET, overflow=True),
        DetailAction(_("Delete Host…"), callbacks._delete_selected, Icons.DELETE, overflow=True, destructive=True, sensitive=editable),
    ]
    return DetailContent(
        title=host.alias,
        subtitle=host_subtitle(host, display_user),
        status=ConnectionStatus.coerce(status),
        chips=chips,
        accent_chips=accent,
        facts=facts,
        actions=actions,
    )


def group_detail(
    kind: RowKind,
    parent: str,
    child: str,
    callbacks: DetailCallbacks,
    *,
    host_count: int,
    child_count: int = 0,
    path: str = "",
    read_only: bool = False,
) -> DetailContent:
    """Details for a parent group, child group or nested folder."""

    title = parent if kind is RowKind.PARENT else f"{parent}/{child}"
    hosts_text = ngettext("{n} host", "{n} hosts", host_count).format(n=host_count)
    if kind is RowKind.PARENT:
        subtitle = ngettext("{n} child group", "{n} child groups", child_count).format(n=child_count) + " · " + hosts_text
        connect = callbacks._connect_parent_group
    else:
        subtitle = hosts_text
        connect = callbacks._connect_child_group
    facts = [(_("Location"), path), (_("Hosts"), str(host_count))]
    if kind is RowKind.PARENT:
        facts.append((_("Child groups"), str(child_count)))
    editable = not read_only
    actions = [
        DetailAction(_("Connect _All"), connect, Icons.CONNECT, primary=True, sensitive=host_count > 0,
                     tooltip=_("Open a session for every host in this group")),
        DetailAction(_("New Host…"), callbacks._create_host_in_current_group, Icons.NEW, sensitive=editable,
                     tooltip=_("Add a host to this group")),
    ]
    if kind is not RowKind.CHILD_FOLDER:
        actions.append(DetailAction(_("Rename…"), callbacks._on_edit_clicked, Icons.EDIT, overflow=True, sensitive=editable))
        actions.append(DetailAction(_("Delete Group…"), callbacks._on_delete_clicked, Icons.DELETE, overflow=True,
                                    destructive=True, sensitive=editable))
    return DetailContent(title=title, subtitle=subtitle, facts=facts, actions=actions)


def multi_detail(hosts: Sequence[HostEntry], callbacks: DetailCallbacks) -> DetailContent:
    """Details for a multi-host selection (bulk actions)."""

    names = ", ".join(host.alias for host in hosts[:4]) + ("…" if len(hosts) > 4 else "")
    count = len(hosts)
    return DetailContent(
        title=ngettext("{n} host selected", "{n} hosts selected", count).format(n=count),
        subtitle=names,
        actions=[
            DetailAction(_("Connect _Selected"), callbacks._bulk_connect_selected, Icons.CONNECT, primary=True),
            DetailAction(_("Move…"), callbacks._bulk_move_selected, Icons.MOVE, tooltip=_("Move selected hosts")),
            DetailAction(_("Tag or Color…"), callbacks._bulk_tag_selected, Icons.TAG, tooltip=_("Tag or color selected hosts")),
        ],
    )


def empty_detail() -> DetailContent:
    return placeholder_content()


def parse_quick_connect(text: str) -> Optional[tuple]:
    """Parse ``[user@]host[:port]`` into ``(user, host, port)``; ``None`` if invalid."""

    value = text.strip()
    if not value or any(ch.isspace() for ch in value):
        return None
    user = ""
    if "@" in value:
        user, value = value.rsplit("@", 1)
        if not user:
            return None
    port = ""
    if value.startswith("["):
        end = value.find("]")
        if end == -1:
            return None
        host = value[1:end]
        rest = value[end + 1:]
        if rest.startswith(":"):
            port = rest[1:]
        elif rest:
            return None
    elif value.count(":") == 1:
        host, port = value.split(":", 1)
    else:
        host = value
    if not host:
        return None
    if port and (not port.isdigit() or not 1 <= int(port) <= 65535):
        return None
    return user, host, port
