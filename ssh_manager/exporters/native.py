"""Export the inventory as an "SSH Manager export" (``*.sshm``) document — pure Python, no GTK.

:func:`build_export` walks the store's tree (groups in sidebar order, a group's
hosts before its subgroups) and returns ``(document, report)``. Database ids
never appear in the file: hosts get sequential ``ref`` values (``h1``, ``h2`` …)
and so do the shared credentials they use (``c1`` …); an RDP host's SSH tunnel
(``rdp.via_ssh``, an inventory id) becomes ``via_ssh_ref``.

**No secrets.** Passwords live in the keyring and are never read. The secret
audit (:func:`audit_options` / :func:`audit_metadata`) also removes anything in
a host's own data that could be one: option and metadata keys named like a
password, token or secret; ``SSHPASS`` / ``sshpass -p`` / ``--password`` values
inside ``ProxyCommand`` / ``LocalCommand`` (replaced by :data:`REDACTED`, the
command itself stays); TLS certificate pins; the RDP gateway's local
credential id; links left by removed features. Every removal is counted in
the :class:`ExportReport`, which names fields and hosts, never values.

Colours come from the UI state: groups by ``"<top>"`` or ``"<top>/<child>/<…>"``,
hosts by ``"<top>::<child path>::<alias>"`` (``ColorController``).
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from ..legacy_vault import LEGACY_METADATA_KEYS
from ..native_format import FORMAT, VERSION, integrity_hash

REDACTED = "<removed>"
KNOWN_PROTOCOLS = ("ssh", "rdp", "ftp", "ftps", "sftp")
COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?$")
SECRET_KEY = re.compile(r"password|passwd|pwd|secret|token|passphrase", re.IGNORECASE)
NOT_SECRET = {("transfer", "anonymous_password")}  # the "anonymous@" convention of anonymous FTP
_COMMAND_KEYS = {"proxycommand", "localcommand"}
# sshpass's -p (not ssh's -p port flag), --password and SSHPASS=: the value after them is replaced.
_COMMAND_SECRET = re.compile(
    r"(?P<prefix>\bsshpass\b[^|;&]*?\s-p\s*|--password[=\s]+|\bSSHPASS=)(?P<secret>\"[^\"]*\"|'[^']*'|\S+)",
    re.IGNORECASE)


@dataclass(frozen=True)
class ExportOptions:
    colors: bool = True
    notes: bool = True  # notes and tags
    credential_refs: bool = True  # shared credentials: name and username only
    smart_groups: bool = False


@dataclass
class ExportReport:
    groups: int = 0
    hosts: int = 0
    by_protocol: Dict[str, int] = field(default_factory=dict)
    colors: int = 0
    colors_invalid: int = 0
    credentials: int = 0
    host_specific: int = 0
    removed: Dict[str, int] = field(default_factory=dict)  # field description → count
    flagged: List[str] = field(default_factory=list)  # "alias: ProxyCommand" (a secret token was removed)
    identity_files: List[str] = field(default_factory=list)  # "alias: path" (the keys themselves are not exported)
    warnings: List[str] = field(default_factory=list)

    def removal(self, what: str) -> None:
        self.removed[what] = self.removed.get(what, 0) + 1


# ------------------------------------------------------------------- colours
def normalize_color(value: Any) -> Optional[str]:
    """``#RRGGBB`` / ``#RRGGBBAA`` (upper case) from what the app stores (``#rrggbb``, ``rgb(…)``, ``rgba(…)``)."""

    if not isinstance(value, str):
        return None
    text = value.strip()
    if COLOR_RE.match(text):
        return text.upper()
    if re.fullmatch(r"#[0-9A-Fa-f]{3}", text):
        return "#" + "".join(ch * 2 for ch in text[1:]).upper()
    match = re.fullmatch(r"rgba?\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*(?:,\s*([0-9.]+)\s*)?\)", text)
    if match:
        r, g, b = (int(match.group(i)) for i in (1, 2, 3))
        if max(r, g, b) > 255:
            return None
        out = f"#{r:02X}{g:02X}{b:02X}"
        if match.group(4) is not None:
            try:
                alpha = float(match.group(4))
            except ValueError:
                return None
            if not 0.0 <= alpha <= 1.0:
                return None
            if alpha < 1.0:
                out += f"{int(round(alpha * 255)):02X}"
        return out
    return None


def group_color_key(path: Sequence[str]) -> str:
    return "/".join(path)


def host_color_key(path: Sequence[str], alias: str) -> str:
    return f"{path[0]}::{'/'.join(path[1:])}::{alias}"


# --------------------------------------------------------------- secret audit
def _redact_command(value: str) -> Tuple[str, bool]:
    redacted, count = _COMMAND_SECRET.subn(lambda m: m.group("prefix") + REDACTED, value)
    return redacted, count > 0


def audit_options(options: Mapping[str, Any], alias: str, report: ExportReport) -> Dict[str, Any]:
    """The host's options without anything that could be a password."""

    kept: Dict[str, Any] = {}
    for key, value in options.items():
        name = str(key)
        lowered = name.lower()
        if SECRET_KEY.search(name):
            report.removal(f"option {lowered}")
            continue
        if isinstance(value, str):
            if lowered in _COMMAND_KEYS or "sshpass" in value.lower() or "--password" in value.lower():
                value, changed = _redact_command(value)
                if changed:
                    report.flagged.append(f"{alias}: {name}")
                    report.removal(f"secret inside option {lowered}")
            if lowered == "identityfile" and value:
                report.identity_files.append(f"{alias}: {value}")
        kept[name] = value
    return kept


def _strip_secret_keys(value: Any, where: str, report: ExportReport, section: str = "") -> Any:
    if isinstance(value, dict):
        kept = {}
        for key, item in value.items():
            name = str(key)
            if SECRET_KEY.search(name) and (section, name) not in NOT_SECRET:
                report.removal(f"{where}.{name}")
                continue
            kept[name] = _strip_secret_keys(item, f"{where}.{name}", report, section)
        return kept
    if isinstance(value, list):
        return [_strip_secret_keys(item, where, report, section) for item in value]
    if isinstance(value, str) and "sshpass" in value.lower():
        redacted, changed = _redact_command(value)
        if changed:
            report.removal(f"secret inside {where}")
        return redacted
    return value


def audit_metadata(metadata: Mapping[str, Any], alias: str, report: ExportReport, *, notes: bool,
                   tunnel_ref: Callable[[str], Optional[str]]) -> Dict[str, Any]:
    """The metadata without secrets (and without links of removed features)."""

    kept: Dict[str, Any] = {}
    for key, value in metadata.items():
        name = str(key)
        if name in LEGACY_METADATA_KEYS:
            report.removal("link to a removed feature")
            continue
        if not notes and name in ("notes", "tags"):
            continue
        if name == "transfer" and isinstance(value, dict):
            value = dict(value)
            if value.pop("pinned_sha256", None):
                report.removal("FTPS certificate pin")
            value = _strip_secret_keys(value, "transfer", report, "transfer")
        elif name == "rdp" and isinstance(value, dict):
            value = dict(value)
            if value.pop("pinned_sha256", None):
                report.removal("RDP certificate pin")
            gateway = value.get("gateway")
            if isinstance(gateway, dict) and gateway.get("credential_id"):
                gateway = dict(gateway)
                gateway.pop("credential_id")
                value["gateway"] = gateway
                report.removal("RDP gateway credential (choose it again after the import)")
            via = value.pop("via_ssh", None)
            if via:
                ref = tunnel_ref(str(via))
                if ref:
                    value["via_ssh_ref"] = ref
                else:
                    report.warnings.append(f"'{alias}': its SSH tunnel host is not part of the export; "
                                           "the tunnel was left out.")
            value = _strip_secret_keys(value, "rdp", report, "rdp")
        elif SECRET_KEY.search(name):
            report.removal(f"metadata {name}")
            continue
        else:
            value = _strip_secret_keys(value, name, report, name)
        kept[name] = value
    return kept


# --------------------------------------------------------------------- export
def build_export(
    store,
    *,
    scope: str = "all",
    group_id: Optional[str] = None,
    host_ids: Optional[Sequence[str]] = None,
    options: ExportOptions = ExportOptions(),
    group_colors: Optional[Mapping[str, str]] = None,
    host_colors: Optional[Mapping[str, str]] = None,
    credential_for: Callable[[str], Optional[object]] = lambda _host_id: None,
    smart_groups: Sequence[Mapping[str, str]] = (),
    app_version: str = "",
    now: Optional[float] = None,
) -> Tuple[Dict[str, Any], ExportReport]:
    """The document for ``scope`` ``"all"`` / ``"groups"`` (``group_id`` and below) / ``"hosts"`` (``host_ids``)."""

    report = ExportReport()
    groups = store.list_groups()
    by_parent: Dict[Optional[str], List[Any]] = {}
    for group in groups:
        by_parent.setdefault(group.parent_id, []).append(group)
    for children in by_parent.values():
        children.sort(key=lambda g: (g.position, g.name.casefold()))
    by_id = {g.id: g for g in groups}
    hosts_by_group: Dict[str, List[Any]] = {}
    for host in store.list_hosts():
        hosts_by_group.setdefault(host.group_id, []).append(host)
    for hosts in hosts_by_group.values():
        hosts.sort(key=lambda h: (h.position, h.alias.casefold()))

    def path_of(gid: str) -> Tuple[str, ...]:
        names: List[str] = []
        current = by_id.get(gid)
        while current is not None:
            names.append(current.name)
            current = by_id.get(current.parent_id) if current.parent_id else None
        return tuple(reversed(names))

    selected_hosts: Optional[Set[str]] = set(host_ids or ()) if scope == "hosts" else None
    if scope == "groups":
        if group_id not in by_id:
            raise LookupError("The group to export does not exist any more.")
        roots = [by_id[group_id]]
    else:
        roots = list(by_parent.get(None, []))

    def wanted(gid: str) -> bool:
        if selected_hosts is None:
            return True
        if any(h.id in selected_hosts for h in hosts_by_group.get(gid, [])):
            return True
        return any(wanted(child.id) for child in by_parent.get(gid, []))

    # First pass: refs for every exported host (an RDP tunnel may point to a later one).
    refs: Dict[str, str] = {}

    def assign(gid: str) -> None:
        for host in hosts_by_group.get(gid, []):
            if selected_hosts is None or host.id in selected_hosts:
                refs[host.id] = f"h{len(refs) + 1}"
        for child in by_parent.get(gid, []):
            if wanted(child.id):
                assign(child.id)

    for root in roots:
        if wanted(root.id):
            assign(root.id)

    credentials: Dict[str, Tuple[str, Dict[str, str]]] = {}  # credential id → (ref, entry)
    group_colors = group_colors or {}
    host_colors = host_colors or {}

    def color_for(key: str, colors: Mapping[str, str]) -> str:
        raw = colors.get(key)
        if not raw or not options.colors:
            return ""
        normalized = normalize_color(raw)
        if normalized is None:
            report.colors_invalid += 1
            return ""
        report.colors += 1
        return normalized

    def host_node(host: Any, path: Tuple[str, ...]) -> Dict[str, Any]:
        protocol = host.protocol or "ssh"
        report.hosts += 1
        report.by_protocol[protocol] = report.by_protocol.get(protocol, 0) + 1
        host_options = audit_options(host.options, host.alias, report)
        metadata = audit_metadata(host.metadata, host.alias, report, notes=options.notes,
                                            tunnel_ref=refs.get)
        node: Dict[str, Any] = {"ref": refs[host.id], "alias": host.alias, "protocol": protocol}
        if host.hostname:
            node["hostname"] = host.hostname
        if host.port is not None:
            node["port"] = host.port
        if host.user:
            node["user"] = host.user
        if host_options:
            node["options"] = host_options
        if metadata:
            node["metadata"] = metadata
        node["color"] = color_for(host_color_key(path, host.alias), host_colors)
        node["position"] = host.position
        if options.credential_refs:
            credential = credential_for(host.id)
            if credential is not None and getattr(credential, "private", False):
                node["credential"] = {"mode": "host-specific"}
                report.host_specific += 1
            elif credential is not None:
                cred_id = getattr(credential, "id", "")
                if cred_id not in credentials:
                    credentials[cred_id] = (f"c{len(credentials) + 1}", {
                        "name": str(getattr(credential, "name", "")),
                        "username": str(getattr(credential, "username", ""))})
                node["credential"] = {"ref": credentials[cred_id][0], "mode": "shared"}
        return node

    def group_node(group: Any) -> Dict[str, Any]:
        path = path_of(group.id)
        report.groups += 1
        node: Dict[str, Any] = {"name": group.name, "note": group.note if options.notes else "",
                                "color": color_for(group_color_key(path), group_colors), "position": group.position,
                                "hosts": [host_node(h, path) for h in hosts_by_group.get(group.id, [])
                                          if selected_hosts is None or h.id in selected_hosts]}
        node["children"] = [group_node(child) for child in by_parent.get(group.id, []) if wanted(child.id)]
        return node

    tree = [group_node(root) for root in roots if wanted(root.id)]
    by_protocol = {name: report.by_protocol.get(name, 0) for name in KNOWN_PROTOCOLS}
    by_protocol.update({name: count for name, count in sorted(report.by_protocol.items()) if name not in by_protocol})
    report.credentials = len(credentials)
    document: Dict[str, Any] = {
        "format": FORMAT,
        "version": VERSION,
        "app_version": app_version,
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "scope": scope,
        "counts": {"groups": report.groups, "hosts": report.hosts, "by_protocol": by_protocol},
        "options": {"colors": options.colors, "notes": options.notes, "credential_refs": options.credential_refs,
                    "smart_groups": options.smart_groups},
        "groups": tree,
        "credentials": [{"ref": ref, **entry} for ref, entry in credentials.values()],
        "smart_groups": [{"name": str(g.get("name", "")), "query": str(g.get("query", ""))}
                         for g in smart_groups] if options.smart_groups else [],
    }
    document["integrity"] = {"sha256": integrity_hash(document)}
    return document, report


def render_export_report(report: ExportReport) -> str:
    """Plain text for the report dialog: counts, field names and host names — never a value that was removed."""

    lines = ["SSH Manager — export report", "",
             f"Groups: {report.groups}", f"Hosts: {report.hosts}"]
    lines += [f"  {name}: {count}" for name, count in sorted(report.by_protocol.items())]
    lines += [f"Colours: {report.colors}" + (f" ({report.colors_invalid} unreadable, left out)"
                                            if report.colors_invalid else ""),
              f"Credentials referenced (name and username only): {report.credentials}",
              f"Hosts with a host-specific password (not exported): {report.host_specific}"]
    if report.removed:
        lines += ["", "Removed (could hold a secret)"] + [f"  {what}: {count}"
                                                         for what, count in sorted(report.removed.items())]
    if report.flagged:
        lines += ["", "Commands that contained a password (the password was replaced by " + REDACTED + ")"]
        lines += [f"  {item}" for item in report.flagged]
    if report.identity_files:
        lines += ["", "Private key files (paths only; copy the keys yourself)"]
        lines += [f"  {item}" for item in report.identity_files]
    if report.warnings:
        lines += ["", "Warnings"] + [f"  {item}" for item in report.warnings]
    lines += ["", "Not exported: passwords, private keys, certificate pins, macros, "
                  "the master password, application settings, history and window state."]
    return "\n".join(lines) + "\n"


__all__ = ["ExportOptions", "ExportReport", "build_export", "render_export_report", "normalize_color",
           "group_color_key", "host_color_key", "audit_options", "audit_metadata", "REDACTED"]
