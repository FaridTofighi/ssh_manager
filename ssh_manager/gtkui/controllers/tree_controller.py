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

from ...config import GroupConfig, HostEntry
from ..constants import Icons, RowKind
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


def filter_host(host: HostEntry, terms: Sequence[Tuple[str, str]]) -> bool:
    """True when ``host`` satisfies every filter term."""

    for key, val in terms:
        if key in {"tag", "tags"}:
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
    children: List["TreeRow"] = field(default_factory=list)

    @property
    def icon(self) -> str:
        if self.kind is RowKind.HOST:
            return Icons.HOST
        if self.kind is RowKind.PARENT:
            return Icons.GROUP
        return Icons.SUBGROUP if self.kind is RowKind.CHILD else Icons.FOLDER

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


def _host_tooltip(host: HostEntry, user: str, status: str) -> str:
    target = _option(host, "hostname", "host") or host.alias
    port = _option(host, "port")
    address = f"{user}@{target}" if user else target
    if port and port != "22":
        address += f":{port}"
    return f"{host.alias}\n{address}\n{status_text(status)}"


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

    for child_name, group in sorted(children.items(), key=lambda kv: kv[0].lower()):
        name_match = parent_matches or (bool(query) and query in child_name.lower())
        host_rows = _host_rows(label_parent, child_name, group, query, terms, name_match and bool(query), callbacks, shared)
        keep_empty = not filtered or (name_match and bool(query) and not terms)
        if not host_rows and not keep_empty:
            continue
        segments = child_name.split("/")
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
            children=host_rows,
        )
        folder_for(segments[:-1]).append(child_row)

    for row in top:
        _fill_folder_counts(row)
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
        if terms and not filter_host(host, terms):
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
                tooltip=_host_tooltip(host, user, status),
                shared=shared,
            )
        )
    return rows


def _fill_folder_counts(row: TreeRow) -> int:
    """Set folder host counts to the sum of their descendants."""

    if row.kind is RowKind.CHILD_FOLDER:
        row.count = sum(_fill_folder_counts(child) for child in row.children)
        row.tooltip = _count_tooltip(row.count)
    return row.count


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
            count = sum(row.count for row in child_rows)
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
