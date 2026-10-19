"""``StoreConfigManager``: the ``ConfigManager`` API the GTK layer uses, over :class:`InventoryStore`.

``manager.tree`` keeps its shape ``{top-level group: {child path: GroupConfig}}``
where the child path is the ``/``-joined names below the top-level group, and
**every group node is its own entry** — containers, empty groups and groups
that hold both hosts and subgroups. Hosts placed directly in a top-level group
live in the entry with the empty child path ``""`` (only present when there
are such hosts). ``GroupConfig.id`` / ``HostEntry.id`` carry the stable ids;
``path`` / ``source_file`` are legacy and empty here.

Every mutator returns ``(ok, message)`` like the file-based manager and calls
``refresh()`` once; store problems become messages, never exceptions.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from ..config import GroupConfig, HostEntry
from .store import (
    InventoryError,
    InventoryStore,
    NewHost,
    ValidationError,
    clean_name,
    validate_name,
)

_COLUMN_OPTIONS = ("hostname", "port", "user")
Result = Tuple[bool, str]


def split_child(child: str) -> List[str]:
    return [part for part in (child or "").split("/") if part.strip()]


def host_columns(options: Dict[str, Any]) -> Tuple[str, Optional[int], str, Dict[str, Any]]:
    """``(hostname, port, user, other options)`` from a ``HostEntry.options`` mapping."""

    hostname, port_text, user = "", "", ""
    rest: Dict[str, Any] = {}
    for key, value in options.items():
        lowered = str(key).strip().lower()
        text = "" if value is None else str(value).strip()
        if lowered in ("hostname", "host"):
            hostname = hostname or text
        elif lowered == "port":
            port_text = text
        elif lowered == "user":
            user = text
        else:
            rest[lowered] = value
    if port_text and not port_text.isdigit():
        raise ValidationError("Port must be a number.")
    return hostname, int(port_text) if port_text else None, user, rest


def entry_options(hostname: str, port: Optional[int], user: str, rest: Dict[str, Any]) -> Dict[str, Any]:
    options: Dict[str, Any] = {}
    if hostname:
        options["hostname"] = hostname
    if user:
        options["user"] = user
    if port is not None:
        options["port"] = str(port)
    options.update(rest)
    return options


class StoreConfigManager:
    """Inventory operations for the UI, backed by SQLite (see module docstring)."""

    allows_display_names = True  # any readable Unicode names, any depth (see gtkui.validation.validators_for)

    def __init__(self, store: InventoryStore) -> None:
        self.store = store
        self.tree: Dict[str, Dict[str, GroupConfig]] = {}
        self._top_ids: Dict[str, str] = {}
        self._group_ids: Dict[Tuple[str, str], str] = {}
        self._data_version = 0
        self.last_duplicate_map: Dict[str, str] = {}  # old host id → new host id of the last duplicate
        self.refresh()

    # ---------------------------------------------------------------- state
    @property
    def base_dir(self) -> Path:
        """Where the inventory lives (the database file); kept for the workspace switcher and UI state."""

        return self.store.path

    @property
    def read_only(self) -> bool:
        return self.store.read_only

    @property
    def read_only_reason(self) -> str:
        return getattr(self.store, "read_only_reason", "") or "The inventory is open read-only."

    def refresh(self) -> None:
        """Reload the whole tree from the database (one query for groups, one for hosts)."""

        groups = self.store.list_groups()
        hosts = self.store.list_hosts()
        by_id = {g.id: g for g in groups}
        paths: Dict[str, Tuple[str, ...]] = {}

        def path_of(group_id: str) -> Tuple[str, ...]:
            cached = paths.get(group_id)
            if cached is not None:
                return cached
            chain, current, seen = [], by_id.get(group_id), set()
            while current is not None and current.id not in seen:
                seen.add(current.id)
                chain.append(current)
                current = by_id.get(current.parent_id) if current.parent_id else None
            result = tuple(g.name for g in reversed(chain))
            paths[group_id] = result
            return result

        tree: Dict[str, Dict[str, GroupConfig]] = {}
        top_ids: Dict[str, str] = {}
        group_ids: Dict[Tuple[str, str], str] = {}
        for group in sorted(groups, key=lambda g: tuple(n.casefold() for n in path_of(g.id))):
            path = path_of(group.id)
            top = path[0]
            children = tree.setdefault(top, {})
            if len(path) == 1:
                top_ids[top] = group.id
                group_ids[(top, "")] = group.id
                continue
            child = "/".join(path[1:])
            children[child] = GroupConfig(top, child, Path(), [], id=group.id, file_backed=False)
            group_ids[(top, child)] = group.id
        for row in hosts:
            path = path_of(row.group_id)
            if not path:
                continue
            top, child = path[0], "/".join(path[1:])
            entry = tree[top].get(child)
            if entry is None:  # hosts directly in a top-level group
                entry = tree[top][child] = GroupConfig(top, child, Path(), [], id=row.group_id, file_backed=False)
            entry.hosts.append(HostEntry(
                alias=row.alias,
                options=entry_options(row.hostname, row.port, row.user, row.options),
                metadata=dict(row.metadata),
                source_file=Path(),
                parent_group=top,
                child_group=child,
                id=row.id,
            ))
        for children in tree.values():
            for entry in children.values():
                entry.hosts.sort(key=lambda h: h.alias.casefold())
            ordered = dict(sorted(children.items(), key=lambda kv: [p.casefold() for p in split_child(kv[0])]))
            children.clear()
            children.update(ordered)
        self.tree = dict(sorted(tree.items(), key=lambda kv: kv[0].casefold()))
        self._top_ids = top_ids
        self._group_ids = group_ids
        self._data_version = self.store.data_version()

    def changed_externally(self) -> bool:
        """True when another window/instance committed since the last refresh."""

        try:
            return self.store.data_version() != self._data_version
        except InventoryError:
            return False

    @contextmanager
    def transaction(self) -> Iterator[InventoryStore]:
        """Group several changes into one transaction and one refresh."""

        with self.store.transaction() as store:
            yield store
        self.refresh()

    # -------------------------------------------------------------- lookups
    def group_id(self, parent: str, child: str = "") -> Optional[str]:
        found = self._group_ids.get((parent, child or ""))
        if found and self.store.get_group(found) is not None:
            return found
        group = self.store.find_group([parent] + split_child(child))
        return group.id if group else None

    def host_id(self, parent: str, child: str, alias: str) -> Optional[str]:
        group_id = self.group_id(parent, child)
        if group_id is None:
            return None
        host = self.store.find_host(group_id, alias)
        return host.id if host else None

    def all_host_ids(self) -> set:
        return self.store.host_ids()

    def backup_before(self, reason: str, group_id: Optional[str] = None) -> None:
        """A snapshot before a destructive change (only when it would delete hosts); never blocks the change."""

        try:
            if group_id is None or self.store.count_hosts(group_id) > 0:
                self.store.backup(reason)
        except (OSError, InventoryError):
            pass

    def _run(self, action, success: str) -> Result:
        try:
            action()
        except ValidationError as exc:
            return False, str(exc)
        except InventoryError as exc:
            return False, str(exc)
        self.refresh()
        return True, success

    # -------------------------------------------------------------- parents
    def create_parent_group(self, name: str) -> Result:
        return self._run(lambda: self.store.create_group(None, name), f"Created group '{clean_name(name)}'.")

    def rename_parent_group(self, old_name: str, new_name: str) -> Result:
        group_id = self.group_id(old_name)
        if group_id is None:
            return False, f"Group '{old_name}' does not exist."
        return self._run(lambda: self.store.rename_group(group_id, new_name),
                         f"Renamed '{old_name}' to '{clean_name(new_name)}'.")

    def duplicate_parent_group(self, source: str, target: str) -> Result:
        group_id = self.group_id(source)
        if group_id is None:
            return False, f"Group '{source}' does not exist."

        def action() -> None:
            _copy, self.last_duplicate_map = self.store.duplicate_group(group_id, None, target)

        return self._run(action, f"Duplicated group '{source}' to '{clean_name(target)}'.")

    def delete_parent_group(self, name: str) -> Result:
        group_id = self.group_id(name)
        if group_id is None:
            return False, f"Group '{name}' does not exist."
        self.backup_before("before-delete", group_id)
        return self._run(lambda: self.store.delete_group(group_id), f"Removed group '{name}'.")

    # ---------------------------------------------------------------- child
    def create_child_group(self, parent_name: str, child_name: str) -> Result:
        parent_id = self.group_id(parent_name)
        if parent_id is None:
            return False, f"Group '{parent_name}' does not exist."
        parts = split_child(child_name)
        if not parts:
            return False, "Name cannot be empty."
        for part in parts:
            error = validate_name(part, "Group name")
            if error:
                return False, error

        def action() -> None:
            with self.store.transaction():
                holder = self.store.create_group_path(parts[:-1], parent_id).id if len(parts) > 1 else parent_id
                if self.store.find_child(holder, parts[-1]) is not None:
                    raise ValidationError(f"A group named '{clean_name(parts[-1])}' already exists here.")
                self.store.create_group(holder, parts[-1])

        return self._run(action, f"Created group '{child_name}' under '{parent_name}'.")

    def _resolve_target(self, parent_name: str, child_path: str) -> Tuple[Optional[str], str]:
        """``(holder group id, last name)`` for a destination path, creating missing holders."""

        parent_id = self.group_id(parent_name)
        if parent_id is None:
            raise ValidationError(f"Group '{parent_name}' does not exist.")
        parts = split_child(child_path)
        if not parts:
            raise ValidationError("Name cannot be empty.")
        holder = self.store.create_group_path(parts[:-1], parent_id).id if len(parts) > 1 else parent_id
        return holder, parts[-1]

    def rename_child_group(self, parent_name: str, old_name: str, new_name: str) -> Result:
        group_id = self.group_id(parent_name, old_name)
        if group_id is None:
            return False, f"Group '{old_name}' not found."

        def action() -> None:
            with self.store.transaction():
                holder, last = self._resolve_target(parent_name, new_name)
                self.store.move_group(group_id, holder, new_name=last)

        return self._run(action, f"Renamed group to '{new_name}'.")

    def duplicate_child_group(self, parent_name: str, child_name: str, new_name: str) -> Result:
        group_id = self.group_id(parent_name, child_name)
        if group_id is None:
            return False, f"Group '{child_name}' not found."

        def action() -> None:
            with self.store.transaction():
                holder, last = self._resolve_target(parent_name, new_name)
                _copy, self.last_duplicate_map = self.store.duplicate_group(group_id, holder, last)

        return self._run(action, f"Duplicated group to '{new_name}'.")

    def delete_child_group(self, parent_name: str, child_name: str) -> Result:
        group_id = self.group_id(parent_name, child_name) if child_name else None
        if group_id is None:
            return False, f"Group '{child_name}' not found."
        self.backup_before("before-delete", group_id)
        return self._run(lambda: self.store.delete_group(group_id), f"Removed group '{child_name}'.")

    def move_child_group(self, parent_name: str, child_name: str, target_parent: str,
                         target_child_name: Optional[str] = None) -> Result:
        group_id = self.group_id(parent_name, child_name)
        if group_id is None:
            return False, f"Group '{parent_name}/{child_name}' not found."
        new_child = target_child_name or split_child(child_name)[-1]
        if parent_name == target_parent and child_name == new_child:
            return False, "The group is already in the target location."

        def action() -> None:
            with self.store.transaction():
                holder, last = self._resolve_target(target_parent, new_child)
                self.store.move_group(group_id, holder, new_name=last)

        return self._run(action, f"Moved group '{parent_name}/{child_name}' to '{target_parent}/{new_child}'.")

    def child_host_count(self, parent_name: str, child_name: str = "") -> int:
        """Hosts in a group and all its subgroups (for delete confirmations)."""

        group_id = self.group_id(parent_name, child_name)
        return self.store.count_hosts(group_id) if group_id else 0

    # ----------------------------------------------------------------- host
    def _new_host(self, group_id: str, host: HostEntry) -> NewHost:
        hostname, port, user, rest = host_columns(host.options)
        return NewHost(group_id, host.alias, hostname, port, user, rest, dict(host.metadata), id=host.id or None)

    def add_host(self, parent_name: str, child_name: str, host: HostEntry) -> Result:
        group_id = self.group_id(parent_name, child_name)
        if group_id is None:
            return False, "Group not found."
        try:
            row = self.store.add_host(self._new_host(group_id, host))
        except InventoryError as exc:
            return False, str(exc)
        host.id = row.id  # the caller binds credentials by this id
        host.parent_group, host.child_group, host.alias = parent_name, child_name, row.alias
        self.refresh()
        return True, f"Added host '{row.alias}'."

    def update_host(self, parent_name: str, child_name: str, original_alias: str, host: HostEntry) -> Result:
        host_id = host.id or self.host_id(parent_name, child_name, original_alias)
        if host_id is None:
            return False, f"Host '{original_alias}' not found."
        try:
            hostname, port, user, rest = host_columns(host.options)
            self.store.update_host(host_id, alias=host.alias, hostname=hostname, port=port, user=user,
                                   options=rest, metadata=dict(host.metadata))
        except InventoryError as exc:
            return False, str(exc)
        host.id = host_id
        self.refresh()
        return True, f"Updated host '{clean_name(host.alias)}'."

    def delete_host(self, parent_name: str, child_name: str, alias: str) -> Result:
        host_id = self.host_id(parent_name, child_name, alias)
        if host_id is None:
            return False, f"Host '{alias}' not found."
        return self._run(lambda: self.store.delete_hosts([host_id]), f"Removed host '{alias}'.")

    def move_hosts(self, host_keys: Iterable[Tuple[str, str, str]], target_parent: str, target_child: str,
                   create_child: bool = False) -> Result:
        keys = list(host_keys)
        if not keys:
            return False, "No hosts selected for move."
        ids, missing, same = [], [], 0
        for parent, child, alias in keys:
            if parent == target_parent and child == target_child:
                same += 1
                continue
            host_id = self.host_id(parent, child, alias)
            (ids.append(host_id) if host_id else missing.append(f"{parent}/{child}:{alias}"))
        if not ids:
            if same == len(keys):
                return False, "All selected hosts are already in the target group."
            return False, "Unable to resolve any of the selected hosts."
        target_id = self.group_id(target_parent, target_child)
        if target_id is None and not create_child:
            return False, f"Group '{target_child}' does not exist under '{target_parent}'."

        def action() -> None:
            with self.store.transaction():
                destination = target_id
                if destination is None:
                    parent_id = self.group_id(target_parent)
                    if parent_id is None:
                        raise ValidationError(f"Group '{target_parent}' does not exist.")
                    destination = self.store.create_group_path(split_child(target_child), parent_id).id
                self.store.move_hosts(ids, destination)

        message = f"Moved {len(ids)} host(s) to {target_parent}/{target_child}."
        if missing:
            message += f" Skipped {len(missing)} host(s) not found."
        if same:
            message += f" Skipped {same} already in destination."
        return self._run(action, message)

    # ---------------------------------------------------------------- bulk
    def create_group_path(self, parent: str, child_path: str = "") -> str:
        """Get or create ``parent`` (top level) and ``child_path`` below it; returns the group id."""

        top = self.store.find_child(None, parent) or self.store.create_group(None, parent)
        if not split_child(child_path):
            return top.id
        return self.store.create_group_path(split_child(child_path), top.id).id

    def add_hosts_bulk(self, items: Sequence[Tuple[str, HostEntry]]) -> List[HostEntry]:
        """``[(group id, host), …]`` in one transaction; sets ``host.id``. Raises on any problem."""

        rows = self.store.add_hosts_bulk(self._new_host(group_id, host) for group_id, host in items)
        for (_group_id, host), row in zip(items, rows):
            host.id = row.id
            host.alias = row.alias
        return [host for _group_id, host in items]
