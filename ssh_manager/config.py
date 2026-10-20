"""Configuration parsing and persistence helpers for the SSH manager."""

from __future__ import annotations

import shlex
import shutil
import string
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple, Set
import glob
import os
import copy

from .persistence import write_text_atomic


###############################################################################
# Data classes                                                                #
###############################################################################


@dataclass
class HostEntry:
    """Represents a single `Host` stanza inside a configuration file."""

    alias: str
    options: Dict[str, str] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)
    source_file: Path = Path("")  # legacy (file-based inventories); empty for the app store
    parent_group: str = ""
    child_group: str = ""
    id: str = ""  # stable host id (app store); "" for file-based inventories

    def option(self, *names: str) -> Optional[str]:
        wanted = {name.lower() for name in names}
        for key, value in self.options.items():
            if str(key).strip().lower() in wanted:
                text = str(value).strip()
                return text or None
        return None

    def display_title(self) -> str:
        hostname = self.option("hostname", "host")
        return hostname or self.alias

    def summary_pairs(self) -> Iterable[Tuple[str, str]]:
        for key in ("hostname", "user", "port", "proxycommand", "identityfile"):
            value = self.option(key)
            if value:
                yield key.capitalize(), value


@dataclass
class GroupConfig:
    parent: str
    name: str
    path: Path  # legacy: the child-group file; empty for the app store
    hosts: List[HostEntry]
    id: str = ""  # stable group id (app store)
    file_backed: bool = True  # a *.conf file of a folder inventory (icon); app-store groups are folders


###############################################################################
# Parsing                                                                     #
###############################################################################


def split_config_directive(line: str) -> Tuple[str, Optional[str]]:
    """Split an ssh_config directive, accepting whitespace or equals syntax."""

    stripped = line.strip()
    if not stripped:
        return "", None
    if "=" in stripped:
        before, after = stripped.split("=", 1)
        before = before.strip()
        if before and not any(ch.isspace() for ch in before):
            return before, after.strip()
    parts = stripped.split(None, 1)
    if len(parts) == 1:
        return parts[0], None
    key, value = parts
    value = value.strip()
    if value.startswith("="):
        value = value[1:].strip()
    return key, value


_split_config_directive = split_config_directive


def parse_ssh_config_file(
    path: Path,
    parent_group: str,
    child_group: str,
    *,
    visited: Optional[Set[Path]] = None,
    follow_includes: bool = True,
) -> List[HostEntry]:
    """Parse an OpenSSH config file into a list of HostEntry objects."""

    entries: List[HostEntry] = []
    if not path.exists():
        return entries

    try:
        resolved = path.resolve()
    except OSError:
        resolved = path.absolute()
    visited = visited or set()
    if resolved in visited:
        return entries
    visited.add(resolved)

    current_aliases: List[str] = []
    current_options: Dict[str, str] = {}
    current_meta: Dict[str, Any] = {}

    def append_entries() -> None:
        nonlocal current_aliases, current_options, current_meta
        if not current_aliases:
            return
        lowered = {k.lower(): v for k, v in current_options.items()}
        for alias in current_aliases:
            entries.append(
                HostEntry(
                    alias=alias,
                    options=dict(lowered),
                    metadata=copy.deepcopy(current_meta),
                    source_file=path,
                    parent_group=parent_group,
                    child_group=child_group,
                )
            )
        current_aliases = []
        current_options = {}
        current_meta = {}

    try:
        raw_text = path.read_text(encoding="utf-8-sig", errors="ignore")  # a BOM must not hide the first Host
    except OSError:
        return entries

    for line in raw_text.splitlines():
        stripped = line.strip()
        key, value = split_config_directive(stripped)
        lower_key = key.lower()
        if stripped.startswith("#"):
            # Accept lines like "# meta key=value" inside a Host stanza.
            if current_aliases:
                meta_line = stripped.lstrip("#").strip()
                parts = meta_line.split(None, 1)
                if len(parts) == 2 and parts[0].lower() == "meta":
                    meta_key, meta_value = split_config_directive(parts[1])
                    if meta_value is not None and meta_key.lower() == "permissions":
                        perms: Dict[str, str] = {}
                        for chunk in meta_value.split():
                            if "=" in chunk:
                                k, v = chunk.split("=", 1)
                                perms[k.strip()] = v.strip()
                        current_meta["permissions"] = {
                            "can_view": perms.get("can_view", "true").lower() != "false",
                            "can_connect": perms.get("can_connect", "true").lower() != "false",
                            "can_edit": perms.get("can_edit", "true").lower() != "false",
                        }
                    elif meta_value is not None and meta_key:
                        current_meta[meta_key.strip()] = meta_value.strip()
            continue
        if not stripped:
            continue
        if lower_key == "include":
            if not follow_includes or not value:
                continue
            append_entries()
            try:
                tokens = shlex.split(value)
            except ValueError:
                tokens = value.split()
            for pattern in tokens:
                expanded = os.path.expandvars(os.path.expanduser(pattern))
                candidates = []
                if Path(expanded).is_absolute():
                    candidates.append(Path(expanded))
                else:
                    candidates.append(path.parent / expanded)

                for candidate in candidates:
                    for match in glob.glob(str(candidate), recursive=True):
                        match_path = Path(match)
                        if match_path.is_file():
                            entries.extend(
                                parse_ssh_config_file(
                                    match_path,
                                    parent_group,
                                    child_group,
                                    visited=visited,
                                    follow_includes=follow_includes,
                                )
                            )
            continue
        if lower_key == "match":
            append_entries()
            continue
        if lower_key == "host" and value is not None:
            append_entries()
            try:
                tokens = shlex.split(value)
            except ValueError:
                tokens = value.split()
            current_aliases = [tok for tok in tokens if tok != "="]
            current_options = {}
            continue
        if value is not None:
            current_options[key.lower()] = value.strip()

    append_entries()
    return entries


def load_config_tree(base_dir: Path) -> Dict[str, Dict[str, GroupConfig]]:
    """Return nested dict {parent_group: {child_group: GroupConfig}}."""

    tree: Dict[str, Dict[str, GroupConfig]] = {}
    if not base_dir.exists():
        return tree

    # Allow pointing directly at a single ssh config file.
    if base_dir.is_file():
        parent_name = base_dir.parent.name or "default"
        child_map: Dict[str, GroupConfig] = {}
        hosts = parse_ssh_config_file(base_dir, parent_name, base_dir.name, follow_includes=True)
        child_map[base_dir.name] = GroupConfig(
            parent=parent_name,
            name=base_dir.name,
            path=base_dir,
            hosts=hosts,
        )
        tree[parent_name] = child_map
        return tree

    def safe_iterdir(directory: Path) -> List[Path]:
        try:
            return list(directory.iterdir())
        except OSError:
            return []

    # Config files placed directly in base_dir (not inside parent folders)
    root_children: Dict[str, GroupConfig] = {}
    root_parent_name = base_dir.name or "default"
    for entry in sorted(
        (p for p in safe_iterdir(base_dir) if p.is_file() and _is_config_candidate(p)),
        key=lambda p: p.name.lower(),
    ):
        hosts = parse_ssh_config_file(
            entry,
            root_parent_name,
            entry.name,
            visited=set(),
            follow_includes=False,
        )
        root_children[entry.name] = GroupConfig(
            parent=root_parent_name,
            name=entry.name,
            path=entry,
            hosts=hosts,
        )
    if root_children:
        tree[root_parent_name] = root_children

    for parent_dir in sorted(
        (p for p in safe_iterdir(base_dir) if p.is_dir() and _is_config_candidate(p)),
        key=lambda p: p.name.lower(),
    ):
        parent_name = parent_dir.name
        child_map: Dict[str, GroupConfig] = {}

        def collect(directory: Path, prefix: str = "") -> None:
            entries = sorted(safe_iterdir(directory), key=lambda p: p.name.lower())
            for entry in entries:
                if not _is_config_candidate(entry):
                    continue
                rel_name = f"{prefix}{entry.name}" if not prefix else f"{prefix}/{entry.name}"
                if entry.is_dir():
                    collect(entry, rel_name)
                    continue
                hosts = parse_ssh_config_file(
                    entry,
                    parent_name,
                    rel_name,
                    visited=set(),
                    follow_includes=False,
                )
                child_map[rel_name] = GroupConfig(
                    parent=parent_name,
                    name=rel_name,
                    path=entry,
                    hosts=hosts,
                )

        collect(parent_dir)
        tree.setdefault(parent_name, {}).update(child_map)
    return tree


###############################################################################
# Persistence                                                                 #
###############################################################################


def _validate_simple_name(name: str) -> Optional[str]:
    if not name:
        return "Name cannot be empty."
    if name in {".", ".."}:
        return "Name cannot be '.' or '..'."
    if any(ch in name for ch in ("/", "\\", os.sep)):
        return "Name must not contain path separators."
    allowed = set(string.ascii_letters + string.digits + "-_.")
    if not set(name) <= allowed:
        return "Name may only contain letters, numbers, hyphen, underscore, or dot."
    return None


def _validate_child_path(name: str) -> Optional[str]:
    parts = Path(name).parts if name else ()
    if not parts:
        return "Name cannot be empty."
    for part in parts:
        error = _validate_simple_name(part)
        if error:
            return error.replace("Name", "Segment")
    return None


def _validate_parent_name(name: str) -> Optional[str]:
    error = _validate_simple_name(name)
    return error.replace("Name", "Parent group") if error else None


def _has_line_break(value: object) -> bool:
    return "\n" in str(value) or "\r" in str(value)


def _has_control_or_space(value: object) -> bool:
    text = str(value)
    return any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in text)


def _validate_host_entry(host: HostEntry) -> Optional[str]:
    alias = str(host.alias)
    if not alias:
        return "Host alias cannot be empty."
    if alias != alias.strip() or _has_control_or_space(alias):
        return "Host alias must not contain whitespace or control characters."
    if alias.startswith("#"):
        return "Host alias must not start with '#'."

    for key, value in host.options.items():
        key_text = str(key).strip()
        if not key_text:
            return "SSH option key cannot be empty."
        if key_text != str(key) or _has_control_or_space(key_text):
            return f"SSH option key '{key}' must not contain whitespace or control characters."
        if _has_line_break(value):
            return f"SSH option '{key_text}' must not contain line breaks."

    for key, value in host.metadata.items():
        key_text = str(key).strip()
        if not key_text:
            return "Metadata key cannot be empty."
        if key_text != str(key) or _has_control_or_space(key_text) or "=" in key_text:
            return f"Metadata key '{key}' is invalid."
        if key_text == "permissions":
            if value is not None and not isinstance(value, dict):
                return "Permissions metadata must be a mapping."
            continue
        if _has_line_break(value):
            return f"Metadata value for '{key_text}' must not contain line breaks."

    return None


def render_group_file(group: GroupConfig) -> str:
    """The on-disk text of a child-group file (``Host`` stanzas with ``# meta`` lines)."""

    lines: List[str] = []
    for host in group.hosts:
        lines.append(f"Host {host.alias}")
        perms = host.metadata.get("permissions")
        if isinstance(perms, dict):
            meta_parts = []
            if perms.get("can_view") is False:
                meta_parts.append("can_view=false")
            if perms.get("can_connect") is False:
                meta_parts.append("can_connect=false")
            if perms.get("can_edit") is False:
                meta_parts.append("can_edit=false")
            if meta_parts:
                lines.append(f"# meta permissions={' '.join(meta_parts)}")
        for meta_key, meta_val in host.metadata.items():
            if meta_key == "permissions":
                continue
            if meta_val:
                lines.append(f"# meta {meta_key}={meta_val}")
        for key, value in host.options.items():
            lines.append(f"    {key} {value}")
        lines.append("")
    text = "\n".join(line.rstrip() for line in lines).strip()
    return (text + "\n") if text else ""


def _write_group_file(group: GroupConfig) -> None:
    write_text_atomic(group.path, render_group_file(group))


def _remove_empty_ancestor_dirs(root: Path, start: Path) -> None:
    try:
        root_resolved = root.resolve()
    except OSError:
        return
    current = start
    while True:
        try:
            current_resolved = current.resolve()
        except OSError:
            break
        if current_resolved == root_resolved:
            break
        if root_resolved not in current_resolved.parents:
            break
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def _is_config_candidate(path: Path) -> bool:
    name = path.name
    if name.startswith(".") or name == "__pycache__":
        return False
    lowered = name.lower()
    if lowered.endswith((".bak", ".tmp", ".swp", ".pyc", "~")):
        return False
    return True


class ConfigManager:
    """Handles reading and mutating SSH config groups on disk (legacy folder inventories).

    The app's own inventory is :class:`ssh_manager.inventory.manager.StoreConfigManager`
    (same API); this class remains for ``--base-dir`` folders, shared files and tests.
    """

    allows_display_names = False  # file names: [A-Za-z0-9._-] only

    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir
        self.tree: Dict[str, Dict[str, GroupConfig]] = {}
        self.refresh()

    # ------------------------------------------------------------------ helpers
    @property
    def read_only(self) -> bool:
        """True when this inventory is a single ssh config file: it can be shown, not edited."""

        return self.base_dir.exists() and not self.base_dir.is_dir()

    def refresh(self) -> None:
        """Reload the tree from ``base_dir`` (never switches to another path).

        An empty inventory stays empty and editable: there is no fallback to
        ``~/.ssh/config`` (that made the app adopt a *file* as its base, after
        which every create failed). A missing folder is created (mode 0700).
        """

        if not self.base_dir.exists():
            try:
                self.base_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            except OSError:
                pass  # mutations report the problem through _ensure_directory_base
        self.tree = load_config_tree(self.base_dir)

    def _get_child(self, parent_name: str, child_name: str) -> Optional[GroupConfig]:
        child = self.tree.get(parent_name, {}).get(child_name)
        if not child:
            return None
        return GroupConfig(
            parent=child.parent,
            name=child.name,
            path=child.path,
            hosts=[
                HostEntry(
                    alias=host.alias,
                    options=dict(host.options),
                    metadata=copy.deepcopy(host.metadata),
                    source_file=host.source_file,
                    parent_group=host.parent_group,
                    child_group=host.child_group,
                )
                for host in child.hosts
            ],
        )

    def _ensure_directory_base(self) -> Optional[str]:
        if self.base_dir.exists():
            if not self.base_dir.is_dir():
                return (
                    f"'{self.base_dir}' is a single file, so groups cannot be added to it. "
                    "SSH Manager keeps groups in its own inventory (or in a folder)."
                )
            return None
        try:
            self.base_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return f"Failed to create base directory '{self.base_dir}': {exc}"
        return None

    # ----------------------------------------------------------------- parents
    def create_parent_group(self, name: str) -> Tuple[bool, str]:
        base_error = self._ensure_directory_base()
        if base_error:
            return False, base_error
        error = _validate_parent_name(name)
        if error:
            return False, error
        target = self.base_dir / name
        if target.exists():
            return False, f"Parent group '{name}' already exists."
        try:
            target.mkdir(parents=False)
        except OSError as exc:
            return False, f"Failed to create parent group '{name}': {exc}"
        self.refresh()
        return True, f"Created parent group '{name}'."

    def rename_parent_group(self, old_name: str, new_name: str) -> Tuple[bool, str]:
        base_error = self._ensure_directory_base()
        if base_error:
            return False, base_error
        error = _validate_parent_name(old_name) or _validate_parent_name(new_name)
        if error:
            return False, error
        old_path = self.base_dir / old_name
        new_path = self.base_dir / new_name
        if not old_path.exists():
            return False, f"Parent group '{old_name}' does not exist."
        if not old_path.is_dir():
            return False, f"Source '{old_name}' is not a directory."
        if new_path.exists():
            return False, f"Target '{new_name}' already exists."
        try:
            old_path.rename(new_path)
        except OSError as exc:
            return False, f"Failed to rename parent '{old_name}': {exc}"
        self.refresh()
        return True, f"Renamed parent '{old_name}' to '{new_name}'."

    def duplicate_parent_group(self, source: str, target: str) -> Tuple[bool, str]:
        base_error = self._ensure_directory_base()
        if base_error:
            return False, base_error
        error = _validate_parent_name(source) or _validate_parent_name(target)
        if error:
            return False, error
        source_path = self.base_dir / source
        if not source_path.exists():
            return False, f"Parent group '{source}' does not exist."
        if not source_path.is_dir():
            return False, f"Source '{source}' is not a directory."
        target_path = self.base_dir / target
        if target_path.exists():
            return False, f"Target '{target}' already exists."
        try:
            shutil.copytree(source_path, target_path)
        except OSError as exc:
            return False, f"Failed to duplicate '{source}': {exc}"
        self.refresh()
        return True, f"Duplicated parent group '{source}' to '{target}'."

    # ------------------------------------------------------------------- child
    def create_child_group(
        self, parent_name: str, child_name: str
    ) -> Tuple[bool, str]:
        base_error = self._ensure_directory_base()
        if base_error:
            return False, base_error
        error = _validate_parent_name(parent_name) or _validate_child_path(child_name)
        if error:
            return False, error
        parts = Path(child_name).parts
        parent_path = self.base_dir / parent_name
        if not parent_path.exists():
            return False, f"Parent group '{parent_name}' does not exist."
        target_file = parent_path.joinpath(*parts)
        parent_dir = target_file.parent
        if target_file.exists():
            if target_file.is_dir():
                return False, f"A folder named '{child_name}' already exists."
            return False, f"Child group '{child_name}' already exists."
        if parent_dir.exists() and not parent_dir.is_dir():
            return False, "Cannot create subgroup at this path."  # parent must be directory
        try:
            parent_dir.mkdir(parents=True, exist_ok=True)
            write_text_atomic(target_file, "")
        except OSError as exc:
            return False, f"Failed to create child group '{child_name}': {exc}"
        self.refresh()
        return True, f"Created child group '{child_name}' under '{parent_name}'."

    def rename_child_group(
        self, parent_name: str, old_name: str, new_name: str
    ) -> Tuple[bool, str]:
        base_error = self._ensure_directory_base()
        if base_error:
            return False, base_error
        error = (
            _validate_parent_name(parent_name)
            or _validate_child_path(old_name)
            or _validate_child_path(new_name)
        )
        if error:
            return False, error
        parent_path = self.base_dir / parent_name
        old_file = parent_path / old_name
        new_file = parent_path / new_name
        if not old_file.exists():
            return False, f"Child group '{old_name}' not found."
        if not old_file.is_file():
            return False, f"Source '{old_name}' is not a file."
        if new_file.exists():
            return False, f"Target '{new_name}' already exists."
        try:
            new_file.parent.mkdir(parents=True, exist_ok=True)
            old_file.rename(new_file)
        except OSError as exc:
            return False, f"Failed to rename child group '{old_name}': {exc}"
        _remove_empty_ancestor_dirs(parent_path, old_file.parent)
        self.refresh()
        return True, f"Renamed child group to '{new_name}'."

    def duplicate_child_group(
        self, parent_name: str, child_name: str, new_name: str
    ) -> Tuple[bool, str]:
        base_error = self._ensure_directory_base()
        if base_error:
            return False, base_error
        error = (
            _validate_parent_name(parent_name)
            or _validate_child_path(child_name)
            or _validate_child_path(new_name)
        )
        if error:
            return False, error
        parent_path = self.base_dir / parent_name
        if not parent_path.exists():
            return False, f"Parent group '{parent_name}' does not exist."
        source_file = parent_path / child_name
        if not source_file.exists():
            return False, f"Child group '{child_name}' not found."
        if not source_file.is_file():
            return False, f"Source '{child_name}' is not a file."
        target_file = parent_path / new_name
        if target_file.exists():
            return False, f"Target '{new_name}' already exists."
        try:
            target_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_file, target_file)
        except OSError as exc:
            return False, f"Failed to duplicate '{child_name}': {exc}"
        self.refresh()
        return True, f"Duplicated child group to '{new_name}'."

    def delete_child_group(self, parent_name: str, child_name: str) -> Tuple[bool, str]:
        base_error = self._ensure_directory_base()
        if base_error:
            return False, base_error
        error = _validate_parent_name(parent_name) or _validate_child_path(child_name)
        if error:
            return False, error
        parent_path = self.base_dir / parent_name
        target_file = parent_path / child_name
        if not target_file.exists():
            return False, f"Child group '{child_name}' not found."
        if not target_file.is_file():
            return False, f"Target '{child_name}' is not a file."
        try:
            target_file.unlink()
        except OSError as exc:
            return False, f"Failed to remove child group '{child_name}': {exc}"
        _remove_empty_ancestor_dirs(parent_path, target_file.parent)
        self.refresh()
        return True, f"Removed child group '{child_name}'."

    def move_child_group(
        self,
        parent_name: str,
        child_name: str,
        target_parent: str,
        target_child_name: Optional[str] = None,
    ) -> Tuple[bool, str]:
        base_error = self._ensure_directory_base()
        if base_error:
            return False, base_error
        new_child_name = target_child_name or child_name
        error = (
            _validate_parent_name(parent_name)
            or _validate_child_path(child_name)
            or _validate_parent_name(target_parent)
            or _validate_child_path(new_child_name)
        )
        if error:
            return False, error
        if parent_name == target_parent and child_name == new_child_name:
            return False, "Child group is already in the target location."
        source_parent_path = self.base_dir / parent_name
        source_file = source_parent_path / child_name
        if not source_file.exists():
            return False, f"Child group '{parent_name}/{child_name}' not found."
        if not source_file.is_file():
            return False, f"Source '{parent_name}/{child_name}' is not a file."

        target_parent_path = self.base_dir / target_parent
        if not target_parent_path.exists():
            return False, f"Target parent '{target_parent}' does not exist."
        if not target_parent_path.is_dir():
            return False, f"Target '{target_parent}' is not a directory."

        destination = target_parent_path / new_child_name
        if destination.exists():
            return False, f"Destination '{target_parent}/{new_child_name}' already exists."

        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            source_file.rename(destination)
        except OSError as exc:
            return False, f"Failed to move '{child_name}': {exc}"
        _remove_empty_ancestor_dirs(source_parent_path, source_file.parent)

        self.refresh()
        return True, (
            f"Moved child group '{parent_name}/{child_name}' to "
            f"'{target_parent}/{new_child_name}'."
        )

    # --------------------------------------------------------------------- host
    def add_host(
        self,
        parent_name: str,
        child_name: str,
        host: HostEntry,
    ) -> Tuple[bool, str]:
        error = _validate_host_entry(host)
        if error:
            return False, error
        child = self._get_child(parent_name, child_name)
        if child is None:
            return False, "Child group not found."
        if any(existing.alias == host.alias for existing in child.hosts):
            return False, f"Host alias '{host.alias}' already exists in this group."
        child.hosts.append(host)
        child.hosts.sort(key=lambda h: h.alias.lower())
        try:
            _write_group_file(child)
        except OSError as exc:
            return False, f"Failed to write child group '{child_name}': {exc}"
        self.refresh()
        return True, f"Added host '{host.alias}'."

    def update_host(
        self,
        parent_name: str,
        child_name: str,
        original_alias: str,
        host: HostEntry,
    ) -> Tuple[bool, str]:
        error = _validate_host_entry(host)
        if error:
            return False, error
        child = self._get_child(parent_name, child_name)
        if child is None:
            return False, "Child group not found."
        if host.alias != original_alias and any(
            existing.alias == host.alias for existing in child.hosts
        ):
            return False, f"Host alias '{host.alias}' already exists."
        replaced = False
        for idx, existing in enumerate(child.hosts):
            if existing.alias == original_alias:
                child.hosts[idx] = host
                replaced = True
                break
        if not replaced:
            return False, f"Host '{original_alias}' not found."
        child.hosts.sort(key=lambda h: h.alias.lower())
        try:
            _write_group_file(child)
        except OSError as exc:
            return False, f"Failed to write child group '{child_name}': {exc}"
        self.refresh()
        return True, f"Updated host '{host.alias}'."

    def delete_host(
        self,
        parent_name: str,
        child_name: str,
        alias: str,
    ) -> Tuple[bool, str]:
        child = self._get_child(parent_name, child_name)
        if child is None:
            return False, "Child group not found."
        filtered = [host for host in child.hosts if host.alias != alias]
        if len(filtered) == len(child.hosts):
            return False, f"Host '{alias}' not found."
        child.hosts = filtered
        try:
            _write_group_file(child)
        except OSError as exc:
            return False, f"Failed to write child group '{child_name}': {exc}"
        self.refresh()
        return True, f"Removed host '{alias}'."

    def move_hosts(
        self,
        host_keys: Iterable[Tuple[str, str, str]],
        target_parent: str,
        target_child: str,
        create_child: bool = False,
    ) -> Tuple[bool, str]:
        base_error = self._ensure_directory_base()
        if base_error:
            return False, base_error
        key_list = list(host_keys)
        if not key_list:
            return False, "No hosts selected for move."
        error = _validate_parent_name(target_parent) or _validate_child_path(target_child)
        if error:
            return False, error

        for parent, child, _alias in key_list:
            error = _validate_parent_name(parent) or _validate_child_path(child)
            if error:
                return False, error

        target_parent_path = self.base_dir / target_parent
        if not target_parent_path.exists():
            return False, f"Target parent '{target_parent}' does not exist."
        if not target_parent_path.is_dir():
            return False, f"Target '{target_parent}' is not a directory."

        target_file = target_parent_path / target_child
        if target_file.exists() and not target_file.is_file():
            return False, f"Destination '{target_parent}/{target_child}' is not a file."
        if not target_file.exists():
            if not create_child:
                return False, (
                    f"Child group '{target_child}' does not exist under '{target_parent}'."
                )
            try:
                target_file.parent.mkdir(parents=True, exist_ok=True)
                write_text_atomic(target_file, "")
            except OSError as exc:
                return False, f"Failed to create child group '{target_child}': {exc}"

        dest_group = self._get_child(target_parent, target_child)
        if dest_group is None:
            dest_group = GroupConfig(
                parent=target_parent,
                name=target_child,
                path=target_file,
                hosts=[],
            )

        existing_aliases: Set[str] = {host.alias for host in dest_group.hosts}
        sources: Dict[Tuple[str, str], GroupConfig] = {}
        moves: List[Tuple[GroupConfig, HostEntry]] = []
        skipped_same_group = 0
        missing: List[str] = []

        for parent, child, alias in key_list:
            if parent == target_parent and child == target_child:
                skipped_same_group += 1
                continue

            source_group = sources.get((parent, child))
            if source_group is None:
                source_group = self._get_child(parent, child)
                if source_group is None:
                    missing.append(f"{parent}/{child}:{alias}")
                    continue
                sources[(parent, child)] = source_group

            host = next((h for h in source_group.hosts if h.alias == alias), None)
            if host is None:
                missing.append(f"{parent}/{child}:{alias}")
                continue

            if host.alias in existing_aliases:
                return False, (
                    f"Host alias '{alias}' already exists in "
                    f"{target_parent}/{target_child}."
                )

            existing_aliases.add(host.alias)
            moves.append((source_group, host))

        if not moves:
            if missing:
                return False, "Unable to resolve any of the selected hosts."
            if skipped_same_group == len(key_list):
                return False, "All selected hosts are already in the target group."
            return False, "Nothing to move."

        affected_sources: Set[Tuple[str, str]] = set()
        for source_group, host in moves:
            source_group.hosts = [h for h in source_group.hosts if h.alias != host.alias]
            affected_sources.add((source_group.parent, source_group.name))
        for _, host in moves:
            dest_group.hosts.append(
                HostEntry(
                    alias=host.alias,
                    options=dict(host.options),
                    metadata=copy.deepcopy(host.metadata),
                    source_file=target_file,
                    parent_group=target_parent,
                    child_group=target_child,
                )
            )

        dest_group.hosts.sort(key=lambda h: h.alias.lower())
        try:
            _write_group_file(dest_group)
            for key in sorted(affected_sources):
                _write_group_file(sources[key])
        except OSError as exc:
            return False, f"Failed to write moved hosts: {exc}"

        self.refresh()

        moved_count = len(moves)
        message = f"Moved {moved_count} host(s) to {target_parent}/{target_child}."
        if missing:
            message += f" Skipped {len(missing)} host(s) not found."
        if skipped_same_group:
            message += f" Skipped {skipped_same_group} already in destination."
        return True, message

    # ------------------------------------------------------------------ removal
    def delete_parent_group(self, name: str) -> Tuple[bool, str]:
        base_error = self._ensure_directory_base()
        if base_error:
            return False, base_error
        error = _validate_parent_name(name)
        if error:
            return False, error
        target = self.base_dir / name
        if not target.exists():
            return False, f"Parent group '{name}' does not exist."
        if not target.is_dir():
            return False, f"Target '{name}' is not a directory."
        try:
            shutil.rmtree(target)
        except OSError as exc:
            return False, f"Failed to remove parent group '{name}': {exc}"
        self.refresh()
        return True, f"Removed parent group '{name}'."
