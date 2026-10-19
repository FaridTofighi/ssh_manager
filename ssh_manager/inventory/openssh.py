"""OpenSSH config files as import sources and export targets (never the live store).

* :func:`plan_openssh_import` — read ``~/.ssh/config`` style files or a
  ``conf.d`` style folder (folder = group, file = group, the layout older
  SSH Manager versions wrote) into a dry-run plan. ``Host *`` and other
  wildcard stanzas are skipped with a note, ``Match`` blocks with a warning;
  ``Include`` is followed (loops are cut by the parser); files over 20 MB are
  refused.
* :func:`commit_openssh_import` — one store transaction: groups, hosts, and
  the old ``parent::child::alias`` credential bindings translated to host ids.
  Re-importing is idempotent (same group + host name + port + user = skip).
* :func:`render_export` / :func:`write_export` — a generated config with
  unique, safe ``Host`` aliases; :func:`include_line_change` /
  :func:`apply_include_line` add one idempotent ``Include`` line to
  ``~/.ssh/config`` after a timestamped backup (only on explicit request).

Source files are only ever read.
"""

from __future__ import annotations

import difflib
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from ..config import GroupConfig, HostEntry, load_config_tree, parse_ssh_config_file
from ..persistence import write_text_atomic

MAX_SOURCE_BYTES = 20 * 1024 * 1024
DEFAULT_EXPORT = Path("~/.config/ssh-manager/ssh_config")
OPENSSH_CONFIG_GROUP = "OpenSSH config"
INCLUDE_MARK = "# added by SSH Manager"
_WILDCARD = re.compile(r"[*?!]")
_SAFE_ALIAS = re.compile(r"[^A-Za-z0-9._@:+-]+")


class Policy(str, Enum):
    SKIP = "skip"
    RENAME = "rename"
    OVERWRITE = "overwrite"


@dataclass(frozen=True)
class Note:
    level: str  # "info" | "warning"
    message: str


@dataclass
class PlannedHost:
    group: Tuple[str, ...]  # display path in the store
    entry: HostEntry
    legacy_key: str  # parent::child::alias of the old folder inventory (binding migration)


@dataclass
class OpenSSHPlan:
    sources: List[Path]
    hosts: List[PlannedHost] = field(default_factory=list)
    groups: List[Tuple[str, ...]] = field(default_factory=list)  # every group to create (empty ones too)
    notes: List[Note] = field(default_factory=list)
    skipped: int = 0

    @property
    def host_count(self) -> int:
        return len(self.hosts)


def _strip_quotes(value: str) -> str:
    text = str(value).strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    return text


def _clean_entry(host: HostEntry) -> HostEntry:
    options = {key: _strip_quotes(value) for key, value in host.options.items()}
    return HostEntry(host.alias, options, dict(host.metadata), host.source_file, host.parent_group, host.child_group)


def _too_big(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > MAX_SOURCE_BYTES
    except OSError:
        return False


def _has_match_block(path: Path) -> bool:
    try:
        return any(line.strip().lower().startswith("match ") for line in path.read_text("utf-8-sig", "ignore").splitlines())
    except OSError:
        return False


def plan_openssh_import(sources: Sequence[Path], *, exclude_under: Sequence[Path] = ()) -> OpenSSHPlan:
    """Plan an import of OpenSSH config files / folders (nothing is written)."""

    plan = OpenSSHPlan([Path(s).expanduser() for s in sources])
    excluded = [Path(p).expanduser().resolve() for p in exclude_under]
    seen_groups: Set[Tuple[str, ...]] = set()
    seen_hosts: Dict[Tuple[Tuple[str, ...], str], PlannedHost] = {}

    def add_group(path: Tuple[str, ...]) -> None:
        for depth in range(1, len(path) + 1):
            prefix = path[:depth]
            if prefix not in seen_groups:
                seen_groups.add(prefix)
                plan.groups.append(prefix)

    def take(host: HostEntry, group: Tuple[str, ...], legacy_key: str) -> None:
        if _WILDCARD.search(host.alias):
            plan.skipped += 1
            plan.notes.append(Note("info", f"'Host {host.alias}' is a pattern, not a host; skipped."))
            return
        try:
            resolved = host.source_file.resolve()
            if any(root == resolved or root in resolved.parents for root in excluded):
                return  # imported from its folder already
        except OSError:
            pass
        add_group(group)
        entry = _clean_entry(host)
        key = (group, host.alias.lower())
        first = seen_hosts.get(key)
        if first is not None:  # OpenSSH: the first value obtained for each option wins
            for option, value in entry.options.items():
                first.entry.options.setdefault(option, value)
            plan.notes.append(Note("info", f"'Host {host.alias}' appears more than once; merged (first value wins)."))
            return
        planned = PlannedHost(group, entry, legacy_key)
        seen_hosts[key] = planned
        plan.hosts.append(planned)

    for source in plan.sources:
        if not source.exists():
            plan.notes.append(Note("warning", f"{source} does not exist."))
            continue
        if source.is_dir():
            files = [p for p in source.rglob("*") if p.is_file()]
            big = [p for p in files if _too_big(p)]
            for path in big:
                plan.notes.append(Note("warning", f"{path} is larger than 20 MB; skipped."))
            for path in files:
                if path not in big and _has_match_block(path):
                    plan.notes.append(Note("warning", f"{path.name}: 'Match' blocks are not imported."))
            tree = load_config_tree(source)
            for parent, children in tree.items():
                for child_name, group in children.items():
                    if group.path in big:
                        continue
                    path = (parent, *[part for part in child_name.split("/") if part])
                    if not group.hosts:
                        add_group(path)
                    for host in group.hosts:
                        take(host, path, f"{parent}::{child_name}::{host.alias}")
            continue
        if _too_big(source):
            plan.notes.append(Note("warning", f"{source} is larger than 20 MB; skipped."))
            continue
        if _has_match_block(source):
            plan.notes.append(Note("warning", f"{source.name}: 'Match' blocks are not imported."))
        name = OPENSSH_CONFIG_GROUP if source.name == "config" else source.name
        for host in parse_ssh_config_file(source, name, "", follow_includes=True):
            take(host, (name,), "")
    return plan


@dataclass
class OpenSSHImportReport:
    added: int = 0
    skipped_existing: int = 0
    renamed: int = 0
    overwritten: int = 0
    groups_created: int = 0
    bindings_carried: int = 0
    bindings_unmatched: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    first_group: Optional[str] = None

    def summary(self) -> str:
        parts = [f"{self.added} host(s) imported"]
        if self.skipped_existing:
            parts.append(f"{self.skipped_existing} already present")
        if self.renamed:
            parts.append(f"{self.renamed} renamed")
        if self.overwritten:
            parts.append(f"{self.overwritten} overwritten")
        if self.bindings_carried:
            parts.append(f"{self.bindings_carried} credential binding(s) carried over")
        if self.bindings_unmatched:
            parts.append(f"{len(self.bindings_unmatched)} old binding(s) without a host")
        if self.errors:
            parts.append(f"{len(self.errors)} host(s) could not be imported")
        return ", ".join(parts) + "."


def _identity(entry: HostEntry) -> Tuple[str, str, str]:
    return ((entry.option("hostname", "host") or entry.alias).lower(), entry.option("port") or "22",
            entry.option("user") or "")


def commit_openssh_import(plan: OpenSSHPlan, manager, credential_store=None, *, policy: Policy = Policy.SKIP,
                          legacy_bindings: Optional[Dict[str, str]] = None) -> OpenSSHImportReport:
    """Write the plan into the app store in one transaction (all or nothing); refresh once.

    ``legacy_bindings`` (``{parent::child::alias: credential id}``) are re-keyed
    to the new host ids; keys that match no imported host are reported.
    """

    from .store import InventoryError, ValidationError, _fold

    report = OpenSSHImportReport()
    store = manager.store
    new_bindings: Dict[str, str] = {}
    matched_keys: Set[str] = set()
    legacy = dict(legacy_bindings or {})
    with store.transaction():
        group_ids: Dict[Tuple[str, ...], str] = {}

        def group_id(path: Tuple[str, ...]) -> str:
            if path not in group_ids:
                before = store.find_group(path)
                group_ids[path] = (before or store.create_group_path(list(path))).id
                if before is None:
                    report.groups_created += 1
            return group_ids[path]

        for path in plan.groups:
            group_id(path)
            report.first_group = report.first_group or path[0]
        existing: Dict[str, Dict[str, object]] = {}
        for planned in plan.hosts:
            gid = group_id(planned.group)
            if gid not in existing:
                existing[gid] = {h.alias.casefold(): h for h in store.list_hosts(gid)}
            rows = existing[gid]
            identities = {(r.hostname.lower() or r.alias.lower(), str(r.port or 22), r.user) for r in rows.values()}
            entry = planned.entry
            if _identity(entry) in identities:
                report.skipped_existing += 1
                matched = next((r for r in rows.values()
                                if (r.hostname.lower() or r.alias.lower(), str(r.port or 22), r.user) == _identity(entry)),
                               None)
                if planned.legacy_key in legacy and matched is not None:
                    new_bindings[matched.id] = legacy[planned.legacy_key]
                    matched_keys.add(planned.legacy_key)
                continue
            clash = rows.get(_fold(entry.alias))
            try:
                if clash is not None and policy is Policy.SKIP:
                    report.skipped_existing += 1
                    continue
                if clash is not None and policy is Policy.OVERWRITE:
                    from .manager import host_columns

                    hostname, port, user, rest = host_columns(entry.options)
                    row = store.update_host(clash.id, hostname=hostname, port=port, user=user, options=rest,
                                            metadata=dict(entry.metadata))
                    report.overwritten += 1
                else:
                    if clash is not None:  # rename with a suffix
                        base, counter = entry.alias, 2
                        while _fold(f"{base}-{counter}") in rows:
                            counter += 1
                        entry = HostEntry(f"{base}-{counter}", entry.options, entry.metadata)
                        report.renamed += 1
                    row = store.add_host(manager._new_host(gid, entry))
                    report.added += 1
            except ValidationError as exc:
                report.errors.append(f"{entry.alias}: {exc}")
                continue
            rows[row.alias.casefold()] = row
            if planned.legacy_key in legacy:
                new_bindings[row.id] = legacy[planned.legacy_key]
                matched_keys.add(planned.legacy_key)
        if new_bindings and credential_store is not None:
            valid = {k: v for k, v in new_bindings.items() if credential_store.get(v) is not None}
            credential_store.bind_many(valid)  # a failure rolls the whole import back
            report.bindings_carried = len(valid)
    report.bindings_unmatched = sorted(set(legacy) - matched_keys)
    manager.refresh()
    return report


def count_hosts(path: Path) -> int:
    """How many concrete hosts a file/folder holds (first-run prompt)."""

    try:
        return plan_openssh_import([path]).host_count
    except (OSError, ValueError):
        return 0


# --------------------------------------------------------------------- export
def safe_alias(alias: str, taken: Set[str]) -> str:
    base = _SAFE_ALIAS.sub("-", alias.strip()).strip("-.") or "host"
    if base.startswith("#"):
        base = base.lstrip("#") or "host"
    candidate, counter = base, 2
    while candidate.lower() in taken:
        candidate = f"{base}-{counter}"
        counter += 1
    taken.add(candidate.lower())
    return candidate


_EXPORT_ORDER = ("hostname", "user", "port", "identityfile", "proxyjump", "proxycommand")
_APP_ONLY = {"tags", "tag", "environment", "owner", "service", "notes", "vault_id", "credential_id",
             "default_session_profile_id", "session_profile_id", "permissions", "password", "passphrase"}


def render_export(tree: Dict[str, Dict[str, GroupConfig]]) -> str:
    """OpenSSH config text for every host (aliases made unique and safe)."""

    lines = [
        "# Generated by SSH Manager on " + time.strftime("%Y-%m-%d %H:%M:%S"),
        "# Do not edit: changes are lost on the next export. Edit hosts in SSH Manager instead.",
        "",
    ]
    taken: Set[str] = set()
    for parent, children in tree.items():
        for child, group in children.items():
            for host in group.hosts:
                alias = safe_alias(host.alias, taken)
                where = "/".join(filter(None, (parent, child)))
                lines.append(f"# {where} · {host.alias}".replace("\n", " "))
                lines.append(f"Host {alias}")
                options = {str(k).lower(): v for k, v in host.options.items()}
                if "hostname" not in options:
                    options["hostname"] = host.alias
                for key in list(_EXPORT_ORDER) + sorted(k for k in options if k not in _EXPORT_ORDER):
                    value = options.get(key)
                    if value in (None, "") or key in _APP_ONLY:
                        continue
                    text = str(value)
                    if any(ch.isspace() for ch in text) and key != "proxycommand":
                        text = f'"{text}"'
                    lines.append(f"    {key} {text}")
                lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def protected_targets() -> List[Path]:
    return [Path("~/.ssh/config").expanduser()]


def write_export(tree, target: Path) -> Path:
    target = Path(target).expanduser()
    if any(target.resolve() == p.resolve() for p in protected_targets()):
        raise ValueError("Exporting over ~/.ssh/config is not allowed; choose another file and use Include.")
    write_text_atomic(target, render_export(tree), mode=0o600)
    return target


def include_directive(target: Path) -> str:
    try:
        text = "~/" + str(Path(target).expanduser().relative_to(Path.home()))
    except ValueError:
        text = str(target)
    if " " in text:
        text = f'"{text}"'
    return f"Include {text}  {INCLUDE_MARK}"


def include_line_change(config: Path, target: Path) -> Tuple[Optional[str], str]:
    """``(new text or None when already included, unified diff)`` for adding the Include line.

    The line goes at the top: OpenSSH applies an ``Include`` placed after a
    ``Host`` line only to that host.
    """

    config = Path(config).expanduser()
    old = config.read_text(encoding="utf-8") if config.exists() else ""
    directive = include_directive(target)
    wanted = directive.split("  #")[0].split(None, 1)[1].strip('"')
    for line in old.splitlines():
        parts = line.split("#", 1)[0].split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "include":
            if Path(parts[1].strip().strip('"')).expanduser() == Path(wanted).expanduser():
                return None, ""
    new = directive + "\n" + old
    diff = "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), str(config), str(config) + " (new)"))
    return new, diff


def apply_include_line(config: Path, target: Path) -> Optional[Path]:
    """Add the Include line (idempotent); returns the backup made, or ``None`` when nothing changed."""

    config = Path(config).expanduser()
    new, _diff = include_line_change(config, target)
    if new is None:
        return None
    backup = None
    if config.exists():
        backup = config.with_name(f"{config.name}.sshm-backup-{time.strftime('%Y%m%d-%H%M%S')}")
        backup.write_bytes(config.read_bytes())
        backup.chmod(0o600)
    config.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    write_text_atomic(config, new, mode=0o600)
    return backup


def import_sources_found(home: Optional[Path] = None) -> List[Tuple[Path, int]]:
    """``[(path, host count)]`` for ``~/.ssh/conf.d`` and ``~/.ssh/config`` (first-run prompt)."""

    home = home or Path.home()
    found = []
    confd, config = home / ".ssh" / "conf.d", home / ".ssh" / "config"
    if confd.is_dir():
        count = count_hosts(confd)
        if count:
            found.append((confd, count))
    if config.is_file():
        excluded = [confd] if found else []
        count = plan_openssh_import([config], exclude_under=excluded).host_count
        if count:
            found.append((config, count))
    return found


def iter_hosts(tree) -> Iterable[HostEntry]:
    for children in tree.values():
        for group in children.values():
            yield from group.hosts
