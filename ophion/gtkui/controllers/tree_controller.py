"""Session-tree logic: search, advanced filter and row building (GTK-free).

:func:`build_tree_rows` turns the parsed inventory into a nested list of
:class:`TreeRow` objects; the view only copies them into a ``Gtk.TreeStore``.

Matching rules (unchanged from the original implementation):

* the quick search matches parent names, child-group names and host search
  text (alias, host name, user, tags, jump host); a group-name match shows
  every host inside it;
* the advanced filter (``key:value AND …``) is applied to hosts.

Two deliberate improvements over the old tree:

* hosts found by search keep their full child-group path (they used to be
  attached directly to the parent group);
* while a search or filter is active, groups without matches are hidden so
  the sidebar can show a "No matching hosts" state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from ... import protocols
from ...config import GroupConfig, HostEntry
from ..constants import RowKind
from ..tree_icons import tree_icon_for
from ..i18n import _, ngettext
from ..theme.tokens import ConnectionStatus
from ..view_models import status_text

SHARED_PREFIX = "[Shared] "
FilterTerms = List[Tuple[str, str]]
Inventory = Dict[str, Dict[str, GroupConfig]]


# ------------------------------------------------------------------- filter
def parse_filter(text: str) -> Tuple[Optional[FilterTerms], str]:
    """Parse ``key:value AND key:value``. Returns ``(terms, error)``."""

    terms: FilterTerms = []
    if not text.strip():
        return terms, ""
    for chunk in (part.strip() for part in text.split("AND")):
        if not chunk:
            continue
        if ":" not in chunk:
            return None, _("Invalid term “{term}”. Use key:value.").format(term=chunk)
        key, value = chunk.split(":", 1)
        key = key.strip().lower()
        value = value.strip().lower()
        if not key or not value:
            return None, _("Invalid term “{term}”.").format(term=chunk)
        terms.append((key, value))
    return terms, ""


def _option(host: HostEntry, *names: str) -> str:
    return host.option(*names) or ""


FAVORITE_VALUES = {"favorite", "favourite", "fav"}


def filter_host(host: HostEntry, terms: Sequence[Tuple[str, str]],
                favorite: Optional[Callable[[HostEntry], bool]] = None) -> bool:
    """True when ``host`` satisfies every filter term (``is:favorite`` asks ``favorite``)."""

    for key, val in terms:
        if key == "is" and val in FAVORITE_VALUES:
            matched = favorite is not None and bool(favorite(host))
        elif key in {"proto", "protocol"}:  # proto:ftp, protocol:sftp
            matched = (getattr(host, "protocol", "ssh") or "ssh").lower() == val
        elif key in {"tag", "tags"}:
            tags = [t.strip().lower() for t in _option(host, "tags").split(",") if t.strip()]
            matched = val in tags
        elif key in {"environment", "owner", "service"}:
            matched = str(host.metadata.get(key, "")).lower() == val
        else:
            matched = _option(host, key).lower() == val or str(host.metadata.get(key, "")).lower() == val
        if not matched:
            return False
    return True


def search_text_for_host(host: HostEntry, display_user: str = "") -> str:
    parts = [
        host.alias.lower(),
        _option(host, "hostname").lower(),
        (display_user or "").lower(),
        _option(host, "tags").lower(),
        _option(host, "proxyjump").lower(),
        "" if protocols.is_ssh(host) else protocols.protocol_of(host),  # "sftp" finds SFTP hosts
    ]
    return " ".join(filter(None, parts))


def host_key(parent: str, child: str, alias: str) -> str:
    return f"{parent}::{child}::{alias}"


# --------------------------------------------------------------------- rows
@dataclass
class TreeRow:
    """One row of the session tree (and its children)."""

    kind: RowKind
    text: str
    parent: str
    child: str = ""
    alias: str = ""
    host: Optional[HostEntry] = None
    search: str = ""
    color: str = ""
    status: str = ConnectionStatus.UNKNOWN.value
    count: int = 0
    tooltip: str = ""
    shared: bool = False
    read_only: bool = False  # hosts: shared inventory or no edit permission (lock overlay)
    file_backed: bool = True  # a *.conf group of a folder inventory; app-store groups are folders
    children: List["TreeRow"] = field(default_factory=list)

    @property
    def icon(self) -> str:
        """The row's icon at rest (collapsed); the tree swaps in the open folder when expanded."""

        file_backed = self.file_backed if self.kind is RowKind.CHILD else None
        return tree_icon_for(self.kind, read_only=self.read_only, file_backed_group=file_backed,
                             has_children=bool(self.children),
                             protocol=protocols.protocol_of(self.host) if self.host is not None else "").name

    @property
    def key(self) -> str:
        return host_key(self.parent, self.child, self.alias)

    def walk(self) -> Iterable["TreeRow"]:
        yield self
        for child in self.children:
            yield from child.walk()


@dataclass
class TreeCallbacks:
    """Lookups the row builder needs from the window."""

    display_user: Callable[[HostEntry], str] = lambda _h: ""
    status: Callable[[str], str] = lambda _key: ConnectionStatus.UNKNOWN.value
    color: Callable[..., str] = lambda _parent, _child, _alias="": ""
    read_only: Callable[[HostEntry], bool] = lambda _host: False  # besides shared inventories
    password: Callable[[HostEntry], str] = lambda _host: ""  # "credential ‘X’" / "host-specific" (never a value)
    favorite: Callable[[HostEntry], bool] = lambda _host: False  # is:favorite


def host_tooltip(host: HostEntry, user: str, status: str, password: str = "") -> str:
    target = _option(host, "hostname", "host") or host.alias
    port = _option(host, "port")
    address = f"{user}@{target}" if user else target
    if port and port != str(protocols.default_port(protocols.protocol_of(host))):
        address += f":{port}"
    text = f"{host.alias}\n{address}\n{status_text(status)}"
    if not protocols.is_ssh(host):
        kind = protocols.kind_for(host)
        text += "\n" + _("Protocol: {name}").format(name=_(kind.label) if kind else protocols.protocol_of(host))
    if password:
        text += "\n" + _("Password: {source}").format(source=password)
    return text


def _count_tooltip(count: int) -> str:
    return ngettext("{n} host", "{n} hosts", count).format(n=count)


def _child_rows(
    label_parent: str,
    children: Dict[str, GroupConfig],
    query: str,
    terms: Optional[FilterTerms],
    parent_matches: bool,
    callbacks: TreeCallbacks,
    shared: bool,
) -> List[TreeRow]:
    filtered = bool(query) or bool(terms)
    top: List[TreeRow] = []
    folders: Dict[str, TreeRow] = {}

    def folder_for(path: List[str]) -> List[TreeRow]:
        container = top
        for depth in range(len(path)):
            folder_path = "/".join(path[: depth + 1])
            folder = folders.get(folder_path)
            if folder is None:
                folder = TreeRow(
                    RowKind.CHILD_FOLDER,
                    path[depth],
                    label_parent,
                    child=folder_path,
                    search=f"{label_parent.lower()} {folder_path.lower()}",
                    color=callbacks.color(label_parent, folder_path),
                    shared=shared,
                )
                folders[folder_path] = folder
                container.append(folder)
            container = folder.children
        return container

    for child_name, group in sorted(children.items(), key=lambda kv: [p.lower() for p in kv[0].split("/")]):
        name_match = parent_matches or (bool(query) and query in child_name.lower())
        host_rows = _host_rows(label_parent, child_name, group, query, terms, name_match and bool(query), callbacks, shared)
        keep_empty = not filtered or (name_match and bool(query) and not terms)
        if not host_rows and not keep_empty:
            continue
        if not child_name:  # app store: hosts placed directly in the top-level group
            top.extend(host_rows)
            continue
        segments = child_name.split("/")
        existing = folders.get(child_name)
        if existing is not None:  # an app-store group seen first as the holder of a subgroup
            existing.kind = RowKind.CHILD
            existing.color = callbacks.color(label_parent, child_name)
            existing.file_backed = group.file_backed
            existing.children = host_rows + existing.children
            continue
        child_row = TreeRow(
            RowKind.CHILD,
            segments[-1],
            label_parent,
            child=child_name,
            search=f"{label_parent.lower()} {child_name.lower()}",
            color=callbacks.color(label_parent, child_name),
            count=len(host_rows),
            tooltip=_count_tooltip(len(host_rows)),
            shared=shared,
            file_backed=group.file_backed,
            children=host_rows,
        )
        folder_for(segments[:-1]).append(child_row)
        if not group.file_backed:
            folders[child_name] = child_row  # its subgroups nest inside it

    if filtered:
        top = [row for row in top if _keep_filtered(row)]
    for row in top:
        _fill_folder_counts(row)
        _groups_first(row)
    top.sort(key=lambda row: row.kind is RowKind.HOST)
    return top


def _host_rows(
    label_parent: str,
    child_name: str,
    group: GroupConfig,
    query: str,
    terms: Optional[FilterTerms],
    include_all_hosts: bool,
    callbacks: TreeCallbacks,
    shared: bool,
) -> List[TreeRow]:
    """Rows for the hosts of one child group that pass the search and filter."""

    rows: List[TreeRow] = []
    for host in sorted(group.hosts, key=lambda h: h.alias.lower()):
        user = callbacks.display_user(host)
        search = search_text_for_host(host, user)
        if query and not (include_all_hosts or query in search):
            continue
        if terms and not filter_host(host, terms, None if shared else callbacks.favorite):
            continue
        key = host_key(label_parent, child_name, host.alias)
        status = ConnectionStatus.coerce(callbacks.status(key)).value
        rows.append(
            TreeRow(
                RowKind.HOST,
                host.alias,
                label_parent,
                child=child_name,
                alias=host.alias,
                host=host,
                search=search,
                color=callbacks.color(label_parent, child_name, host.alias),
                status=status,
                tooltip=host_tooltip(host, user, status, callbacks.password(host)),
                shared=shared,
                read_only=shared or bool(callbacks.read_only(host)),
            )
        )
    return rows


def _fill_folder_counts(row: TreeRow) -> int:
    """Set group host counts to their hosts plus everything below them."""

    if row.kind is RowKind.HOST:
        return 1
    if row.kind in (RowKind.CHILD_FOLDER, RowKind.CHILD):
        row.count = sum(_fill_folder_counts(child) for child in row.children)
        row.tooltip = _count_tooltip(row.count)
    return row.count


def _groups_first(row: TreeRow) -> None:
    """Subgroups above hosts (a group may hold both in the app store)."""

    row.children.sort(key=lambda child: child.kind is RowKind.HOST)
    for child in row.children:
        _groups_first(child)


def _keep_filtered(row: TreeRow) -> bool:
    """While filtering, drop holder rows whose whole branch has no matching host."""

    if row.kind is RowKind.HOST:
        return True
    row.children = [child for child in row.children if _keep_filtered(child)]
    return bool(row.children) or row.kind is RowKind.CHILD


# Top-level groups named after a deployment stage are listed in pipeline
# order (as in the reference design); every other group follows alphabetically.
STAGE_ORDER = {
    "production": 0, "prod": 0, "live": 0,
    "staging": 1, "stage": 1, "preprod": 1, "uat": 1,
    "development": 2, "dev": 2,
    "test": 3, "testing": 3, "qa": 3,
}


def group_sort_key(name: str) -> Tuple[int, str]:
    lowered = (name or "").strip().lower()
    return STAGE_ORDER.get(lowered, len(set(STAGE_ORDER.values()))), lowered


def build_tree_rows(
    inventories: Sequence[Tuple[Inventory, bool]],
    query: str = "",
    terms: Optional[FilterTerms] = None,
    callbacks: Optional[TreeCallbacks] = None,
) -> List[TreeRow]:
    """Build the nested rows for ``inventories`` (``[(tree, shared), …]``)."""

    callbacks = callbacks or TreeCallbacks()
    query = (query or "").strip().lower()
    filtered = bool(query) or bool(terms)
    rows: List[TreeRow] = []
    for tree, shared in inventories:
        for parent_name, children in sorted(tree.items(), key=lambda kv: group_sort_key(kv[0])):
            label_parent = f"{SHARED_PREFIX}{parent_name}" if shared else parent_name
            parent_matches = not query or query in label_parent.lower()
            child_rows = _child_rows(label_parent, children, query, terms, parent_matches, callbacks, shared)
            keep_empty = not filtered or (parent_matches and bool(query) and not terms)
            if not child_rows and not keep_empty:
                continue
            count = sum(_fill_folder_counts(row) for row in child_rows)
            rows.append(
                TreeRow(
                    RowKind.PARENT,
                    label_parent,
                    label_parent,
                    search=label_parent.lower(),
                    color=callbacks.color(label_parent, ""),
                    count=count,
                    tooltip=_count_tooltip(count),
                    shared=shared,
                    children=child_rows,
                )
            )
    return rows


def has_inventory(inventories: Sequence[Tuple[Inventory, bool]]) -> bool:
    return any(bool(tree) for tree, _shared in inventories)


def sidebar_page_for(inventories: Sequence[Tuple[Inventory, bool]], rows: Sequence[TreeRow], filtered: bool) -> str:
    """``"empty"`` (no inventory), ``"no-results"`` (filtered to nothing) or ``"tree"``."""

    if not has_inventory(inventories):
        return "empty"
    if not rows and filtered:
        return "no-results"
    return "tree"
