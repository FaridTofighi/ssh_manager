"""Import an "SSH Manager export" (``*.sshm``) — pure Python, no GTK.

The same pipeline as the MobaXterm import (:mod:`ophion.importers.mobaxterm`,
whose naming, merge and conflict rules are reused, not copied):

1. :func:`read_native` — bytes/path (and a passphrase for an encrypted file) →
   :class:`NativeDocument`: the validated tree of groups and hosts. Limits are
   enforced; bad items are skipped and reported, never fatal; fatal file-level
   problems raise :class:`NativeFormatError`.
2. :func:`plan_native` — the file's groups become groups 1:1 (any depth) under
   the top level or an existing group; existing groups with the same path are
   merged; the users found (:func:`discover_users`).
3. :func:`apply_mapping` — the user → credential choices, the conflict policy
   and the colour policy → :class:`NativeJob`.
4. :func:`commit_native` — **one** ``InventoryStore.transaction()``; colours and
   smart groups are handed back to the caller to save after the commit.

Nothing from the file is executed, resolved or connected to, and nothing here
logs file content. The file holds no secrets; a credential in it is only a
name and a username used to suggest a match.
"""

from __future__ import annotations

import json
import os
import re
import time
import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple, Union

from ..config import HostEntry
from ..identity import APP_NAME
from ..native_format import (
    ENCRYPTED_FORMAT,
    FORMAT,
    VERSION,
    NativeFormatError,
    check_integrity,
    decrypt_document,
)
from .mobaxterm import (
    ConflictPolicy,
    CredentialMissingError,
    Destination,
    ExistingHost,
    ExistingInventory,
    ImportCancelled,
    ImportReport,
    ImportStatus,
    Severity,
    UsernameEntry,
    _cf,
    _Namer,
    _valid_hostname,
    clean_label,
    identity_key,
    unique_name,
)

MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_DEPTH = 32
MAX_HOSTS = 200_000
MAX_STRING = 4096
MAX_NOTE = 64 * 1024
KNOWN_PROTOCOLS = ("ssh", "rdp", "ftp", "ftps", "sftp")
TRANSFER_PROTOCOLS = ("ftp", "ftps", "sftp")
DEFAULT_PORTS = {"ssh": 22, "sftp": 22, "ftp": 21, "ftps": 21, "rdp": 3389}
COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?$")
_PROTOCOL_RE = re.compile(r"^[a-z0-9_-]{1,16}$")
_COLUMN_KEYS = {"hostname", "host", "port", "user"}
_BIDI = set("‪‫‬‭‮⁦⁧⁨⁩")
NO_USER = ""


class Code(str, Enum):
    INTEGRITY_MISMATCH = "integrity_mismatch"
    INTEGRITY_MISSING = "integrity_missing"
    TOO_DEEP = "group_too_deep"
    TOO_MANY_HOSTS = "too_many_hosts"
    FIELD_TOO_LONG = "field_too_long"
    NOTE_TOO_LONG = "note_too_long"
    INVALID_ITEM = "invalid_item"
    INVALID_PROTOCOL = "invalid_protocol"
    EMPTY_HOSTNAME = "empty_hostname"
    INVALID_HOSTNAME = "invalid_hostname"
    INVALID_PORT = "invalid_port"
    INVALID_USER = "invalid_user"
    INVALID_JUMP = "invalid_jump_host"
    INVALID_COLOR = "invalid_color"
    INVALID_CREDENTIAL = "invalid_credential_reference"
    NAME_CHANGED = "name_changed"
    NAME_DEDUPLICATED = "name_deduplicated"
    DESTINATION_MISSING = "destination_missing"
    EXCLUDED_PROTOCOL = "excluded_protocol"
    EXISTING_HOST_SKIPPED = "existing_host_skipped"
    EXISTING_HOST_RENAMED = "existing_host_renamed"
    EXISTING_HOST_OVERWRITTEN = "existing_host_overwritten"
    TUNNEL_DROPPED = "tunnel_dropped"
    KEY_FILE_MISSING = "key_file_missing"
    HOST_SPECIFIC = "host_specific_password"
    FIELD_CLEANED = "field_cleaned"


@dataclass(frozen=True)
class Issue:
    severity: Severity
    code: Code
    message: str  # names only, never values from host fields


# --------------------------------------------------------------------- model
@dataclass
class FileHost:
    ref: str
    alias: str
    protocol: str
    hostname: str = ""
    port: Optional[int] = None
    user: str = ""
    options: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)
    color: str = ""
    position: int = 0
    credential_ref: str = ""  # a shared credential of the file's "credentials" list
    host_specific: bool = False
    via_ssh_ref: str = ""


@dataclass
class FileGroup:
    key: Tuple[int, ...]  # index path in the file (unique even when names repeat)
    name: str
    note: str = ""
    color: str = ""
    position: int = 0
    hosts: List[FileHost] = field(default_factory=list)
    children: List["FileGroup"] = field(default_factory=list)

    def total_hosts(self) -> int:
        return len(self.hosts) + sum(child.total_hosts() for child in self.children)

    def walk(self) -> Iterable["FileGroup"]:
        yield self
        for child in self.children:
            yield from child.walk()


@dataclass(frozen=True)
class FileCredential:
    ref: str
    name: str
    username: str


@dataclass
class NativeDocument:
    version: int
    app_version: str
    exported_at: str
    scope: str
    groups: List[FileGroup]
    credentials: Dict[str, FileCredential]
    smart_groups: List[Dict[str, str]]
    integrity: str  # ok / missing / mismatch
    issues: List[Issue]
    encrypted: bool = False
    size: int = 0

    def walk(self) -> Iterable[FileGroup]:
        for group in self.groups:
            yield from group.walk()

    def hosts(self) -> List[FileHost]:
        return [host for group in self.walk() for host in group.hosts]

    @property
    def has_colors(self) -> bool:
        return any(g.color for g in self.walk()) or any(h.color for h in self.hosts())

    def protocol_counts(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for host in self.hosts():
            counts[host.protocol] = counts.get(host.protocol, 0) + 1
        return counts


# -------------------------------------------------------------------- reading
def _read_bytes(source: Union[bytes, bytearray, Path, str]) -> bytes:
    if isinstance(source, (bytes, bytearray)):
        data = bytes(source)
    else:
        path = Path(source)
        try:
            if not path.exists():
                raise NativeFormatError("missing", "The file does not exist.")
            if path.is_dir():
                raise NativeFormatError("is_directory", "This is a folder, not a file.")
            if path.stat().st_size > MAX_FILE_BYTES:
                raise NativeFormatError("too_large", "The file is larger than 50 MB.")
            data = path.read_bytes()
        except PermissionError as exc:
            raise NativeFormatError("permission", "You do not have permission to read this file.") from exc
        except NativeFormatError:
            raise
        except OSError as exc:
            raise NativeFormatError("unreadable", f"The file could not be read ({exc.strerror or exc}).") from exc
    if len(data) > MAX_FILE_BYTES:
        raise NativeFormatError("too_large", "The file is larger than 50 MB.")
    if not data.strip():
        raise NativeFormatError("empty", "The file is empty.")
    return data


def _load_json(data: bytes) -> Any:
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise NativeFormatError("not_utf8", "This is not an Ophion export (the text is not UTF-8).") from None
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        if exc.pos >= len(text.rstrip()) - 1:
            raise NativeFormatError("truncated", "The file is incomplete: it ends too early (was the copy cut off?)."
                                    ) from None
        raise NativeFormatError("json", f"The file is damaged: line {exc.lineno}, column {exc.colno}: {exc.msg}."
                                ) from None


def sniff(source: Union[bytes, bytearray, Path, str]) -> str:
    """``"plain"``, ``"encrypted"`` or ``""`` (not an SSH Manager export), by content."""

    try:
        value = _load_json(_read_bytes(source))
    except NativeFormatError:
        return ""
    if isinstance(value, dict) and value.get("format") == FORMAT:
        return "plain"
    if isinstance(value, dict) and value.get("format") == ENCRYPTED_FORMAT:
        return "encrypted"
    return ""


def read_native(source: Union[bytes, bytearray, Path, str], passphrase: Optional[str] = None) -> NativeDocument:
    """Validate an export. An encrypted file without (or with a wrong) passphrase raises ``encrypted`` / ``wrong_passphrase``."""

    data = _read_bytes(source)
    value = _load_json(data)
    encrypted = False
    if isinstance(value, dict) and value.get("format") == ENCRYPTED_FORMAT:
        if passphrase is None:
            raise NativeFormatError("encrypted", "This export is protected with a passphrase.")
        value = _load_json(decrypt_document(value, passphrase))
        encrypted = True
    if not isinstance(value, dict) or value.get("format") != FORMAT:
        raise NativeFormatError("not_native", "This is not an Ophion export.")
    version = value.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise NativeFormatError("damaged", "The export has no valid version number.")
    if version > VERSION:
        raise NativeFormatError("newer", "This file was created by a newer Ophion; update Ophion to "
                                         "import it.")
    issues: List[Issue] = []
    integrity = check_integrity(value)
    if integrity == "mismatch":
        issues.append(Issue(Severity.WARNING, Code.INTEGRITY_MISMATCH, "The file was edited after export."))
    elif integrity == "missing":
        issues.append(Issue(Severity.INFO, Code.INTEGRITY_MISSING, "The file has no integrity check."))
    credentials: Dict[str, FileCredential] = {}
    for item in value.get("credentials") or [] if isinstance(value.get("credentials"), list) else []:
        if isinstance(item, dict) and all(isinstance(item.get(k), str) and len(item[k]) <= MAX_STRING
                                          for k in ("ref", "name", "username")):
            credentials[item["ref"]] = FileCredential(item["ref"], _clean_text(item["name"]),
                                                      _clean_text(item["username"]))
        else:
            issues.append(Issue(Severity.WARNING, Code.INVALID_ITEM, "A credential reference is unreadable; left out."))
    smart = []
    for item in value.get("smart_groups") or [] if isinstance(value.get("smart_groups"), list) else []:
        if isinstance(item, dict) and isinstance(item.get("name"), str) and isinstance(item.get("query"), str) \
                and item["name"].strip() and len(item["name"]) <= MAX_STRING and len(item["query"]) <= MAX_STRING:
            smart.append({"name": _clean_text(item["name"]), "query": _clean_text(item["query"])})
    reader = _Reader(issues, credentials)
    groups = reader.groups(value.get("groups"), (), 1)
    return NativeDocument(version, _short(value.get("app_version")), _short(value.get("exported_at")),
                          _short(value.get("scope")) or "all", groups, credentials, smart, integrity, issues,
                          encrypted, len(data))


def _short(value: Any) -> str:
    return _clean_text(value)[:200] if isinstance(value, str) else ""


def _clean_text(text: str, *, keep_newlines: bool = False) -> str:
    """No control (except newlines/tabs in notes) or bidi-override characters."""

    text = unicodedata.normalize("NFC", text)
    if not keep_newlines:
        text = re.sub(r"[\r\n\t]+", " ", text)  # one line: line breaks become spaces
    return "".join(ch for ch in text if (unicodedata.category(ch) != "Cc" or ch in "\n\t") and ch not in _BIDI)


def _too_long(value: Any, limit: int = MAX_STRING) -> bool:
    if isinstance(value, str):
        return len(value) > limit
    if isinstance(value, dict):
        return any(_too_long(k, limit) or _too_long(v, limit) for k, v in value.items())
    if isinstance(value, list):
        return any(_too_long(v, limit) for v in value)
    return False


def _clean_values(value: Any) -> Any:
    if isinstance(value, str):
        return _clean_text(value)
    if isinstance(value, dict):
        return {str(k): _clean_values(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_clean_values(v) for v in value]
    return value


class _Reader:
    def __init__(self, issues: List[Issue], credentials: Mapping[str, FileCredential]) -> None:
        self.issues = issues
        self.credentials = credentials
        self.host_count = 0
        self.limit_reported = False

    def warn(self, code: Code, message: str, severity: Severity = Severity.WARNING) -> None:
        self.issues.append(Issue(severity, code, message))

    def groups(self, items: Any, parent: Tuple[int, ...], depth: int) -> List[FileGroup]:
        result: List[FileGroup] = []
        if not isinstance(items, list):
            return result
        for index, item in enumerate(items):
            key = parent + (index,)
            if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                self.warn(Code.INVALID_ITEM, "A group without a name was left out (with its content).")
                continue
            label = clean_label(item["name"])[:80]
            if depth > MAX_DEPTH:
                self.warn(Code.TOO_DEEP, f"Group '{label}' is nested deeper than {MAX_DEPTH} levels; left out.")
                continue
            if len(item["name"]) > MAX_STRING:
                self.warn(Code.FIELD_TOO_LONG, "A group name is too long; the group was left out.")
                continue
            note = item.get("note") if isinstance(item.get("note"), str) else ""
            if len(note) > MAX_NOTE:
                self.warn(Code.NOTE_TOO_LONG, f"The note of group '{label}' is too long; left out.")
                note = ""
            group = FileGroup(key, item["name"], _clean_text(note, keep_newlines=True),
                              self.color(item.get("color"), label), self.position(item.get("position")))
            for host_item in item.get("hosts") or [] if isinstance(item.get("hosts"), list) else []:
                host = self.host(host_item, label)
                if host is not None:
                    group.hosts.append(host)
            group.children = self.groups(item.get("children"), key, depth + 1)
            result.append(group)
        return result

    def color(self, value: Any, label: str) -> str:
        if value in (None, ""):
            return ""
        if isinstance(value, str) and COLOR_RE.match(value.strip()):
            return value.strip().upper()
        self.warn(Code.INVALID_COLOR, f"'{label}' has an invalid colour; left out.", Severity.INFO)
        return ""

    @staticmethod
    def position(value: Any) -> int:
        return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 2 ** 31 else 0

    def host(self, item: Any, group_label: str) -> Optional[FileHost]:
        if self.host_count >= MAX_HOSTS:
            if not self.limit_reported:
                self.warn(Code.TOO_MANY_HOSTS, f"The file has more than {MAX_HOSTS} hosts; the rest is left out.")
                self.limit_reported = True
            return None
        if not isinstance(item, dict) or not isinstance(item.get("alias"), str):
            self.warn(Code.INVALID_ITEM, f"A host without a name in '{group_label}' was left out.")
            return None
        label = clean_label(item["alias"])[:80] or "?"
        notes = item.get("metadata", {}).get("notes") if isinstance(item.get("metadata"), dict) else None
        rest = {k: v for k, v in item.items() if k != "metadata"}
        metadata_rest = {k: v for k, v in item["metadata"].items() if k != "notes"} \
            if isinstance(item.get("metadata"), dict) else {}
        if _too_long(rest) or _too_long(metadata_rest):
            self.warn(Code.FIELD_TOO_LONG, f"'{label}' has a field longer than {MAX_STRING} characters; left out.")
            return None
        protocol = item.get("protocol", "ssh")
        protocol = protocol.strip().lower() if isinstance(protocol, str) else ""
        if not _PROTOCOL_RE.match(protocol or "-"):
            self.warn(Code.INVALID_PROTOCOL, f"'{label}' has an invalid protocol; left out.")
            return None
        hostname = _clean_text(item.get("hostname") or "") if isinstance(item.get("hostname"), (str, type(None))) else None
        if hostname is None:
            self.warn(Code.INVALID_HOSTNAME, f"'{label}' has an invalid host name; left out.")
            return None
        hostname = hostname.strip()
        if hostname.startswith("[") and hostname.endswith("]"):
            hostname = hostname[1:-1]
        if not hostname and protocol in KNOWN_PROTOCOLS:
            self.warn(Code.EMPTY_HOSTNAME, f"'{label}' has no host name; left out.")
            return None
        if hostname and not _valid_hostname(hostname):
            self.warn(Code.INVALID_HOSTNAME, f"'{label}' has an invalid host name; left out.")
            return None
        port = item.get("port")
        if port is not None and (not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535):
            default = DEFAULT_PORTS.get(protocol)
            self.warn(Code.INVALID_PORT, f"'{label}' has no valid port; " +
                      (f"using {default}." if default else "the default is used."))
            port = default
        user = item.get("user") or ""
        user = _clean_text(user).strip() if isinstance(user, str) else ""
        if user and (user.startswith("-") or any(ch.isspace() for ch in user)):
            self.warn(Code.INVALID_USER, f"'{label}' has an unusable user name; it is left empty.")
            user = ""
        for where, values in (("options", item.get("options")), ("metadata", metadata_rest)):
            if isinstance(values, dict) and any(isinstance(v, str) and ("\n" in v or "\r" in v)
                                                for v in values.values()):
                self.warn(Code.FIELD_CLEANED, f"'{label}': line breaks in its {where} were replaced by spaces.",
                          Severity.INFO)
        options = _clean_values(item.get("options")) if isinstance(item.get("options"), dict) else {}
        options = self.options(options, label)
        cleaned = _clean_values(metadata_rest)
        metadata = {}
        for key in (item.get("metadata") or {}) if isinstance(item.get("metadata"), dict) else {}:
            if key != "notes":
                metadata[key] = cleaned[key]
            elif isinstance(notes, str) and len(notes) > MAX_NOTE:
                self.warn(Code.NOTE_TOO_LONG, f"The notes of '{label}' are too long; left out.")
            elif isinstance(notes, str):
                metadata["notes"] = _clean_text(notes)  # in its place: a round trip keeps the order
        via_ref = ""
        if isinstance(metadata.get("rdp"), dict):
            rdp = dict(metadata["rdp"])
            rdp.pop("via_ssh", None)  # an inventory id of another computer
            via = rdp.pop("via_ssh_ref", "")
            via_ref = via if isinstance(via, str) else ""
            metadata["rdp"] = rdp
        credential = item.get("credential")
        credential_ref, host_specific = "", False
        if isinstance(credential, dict):
            if credential.get("mode") == "host-specific":
                host_specific = True
            elif credential.get("mode") in ("shared", None) and credential.get("ref") in self.credentials:
                credential_ref = credential["ref"]
            else:
                self.warn(Code.INVALID_CREDENTIAL, f"'{label}' refers to a credential that is not in the file; "
                                                   "ignored.", Severity.INFO)
        ref = item.get("ref") if isinstance(item.get("ref"), str) else ""
        self.host_count += 1
        return FileHost(ref, item["alias"], protocol, hostname, port, user, options, metadata,
                        self.color(item.get("color"), label), self.position(item.get("position")), credential_ref,
                        host_specific, via_ref)

    def options(self, options: Dict[str, Any], label: str) -> Dict[str, Any]:
        kept: Dict[str, Any] = {}
        for key, value in options.items():
            lowered = key.lower()
            if lowered in _COLUMN_KEYS:
                continue  # host name, port and user are the host's own fields
            if lowered == "proxyjump" and isinstance(value, str) and \
                    any(part.strip().startswith("-") for part in value.split(",")):
                self.warn(Code.INVALID_JUMP, f"'{label}' has an invalid jump host; imported without it.")
                continue
            kept[key] = value
        return kept


# ------------------------------------------------------------------- planning
class ColorPolicy(str, Enum):
    IF_EMPTY = "if_empty"  # only where the group / host has no colour yet
    OVERWRITE = "overwrite"
    NONE = "none"


@dataclass(frozen=True)
class NativeImportOptions:
    destination: Destination = Destination.NEW_TOP_LEVEL
    target_group: Tuple[str, ...] = ()
    conflict_policy: ConflictPolicy = ConflictPolicy.SKIP
    skip_empty_groups: bool = False
    include_ssh: bool = True
    include_rdp: bool = True
    include_transfer: bool = True  # FTP, FTPS and SFTP
    import_colors: bool = True
    color_policy: ColorPolicy = ColorPolicy.IF_EMPTY
    import_notes: bool = True
    import_smart_groups: bool = False

    def includes(self, protocol: str) -> bool:
        if protocol == "ssh":
            return self.include_ssh
        if protocol == "rdp":
            return self.include_rdp
        if protocol in TRANSFER_PROTOCOLS:
            return self.include_transfer
        return True  # unknown protocols are kept as they are


@dataclass
class PlannedHost:
    file: FileHost
    group: Tuple[str, ...]  # store group path
    alias: str
    user_key: str  # the effective user (credential's username, else the host's user)
    credential: Optional[FileCredential] = None

    @property
    def anonymous(self) -> bool:
        transfer = self.file.metadata.get("transfer")
        return self.file.protocol in ("ftp", "ftps") and (
            (isinstance(transfer, dict) and transfer.get("anonymous") is True)
            or self.user_key.casefold() in ("", "anonymous"))


@dataclass
class PlannedGroup:
    path: Tuple[str, ...]
    key: Optional[Tuple[int, ...]]  # the file group (None: the mount point or an ancestor of it)
    exists: bool
    note: str = ""
    color: str = ""
    position: int = 0
    hosts: List[PlannedHost] = field(default_factory=list)


@dataclass
class NativePlan:
    document: NativeDocument
    options: NativeImportOptions
    groups: List[PlannedGroup]
    hosts: List[PlannedHost]
    issues: List[Issue]
    users: List[UsernameEntry]
    renamed: List[Tuple[str, str]]
    existing: ExistingInventory
    excluded_hosts: int = 0
    filtered_hosts: int = 0
    destination_missing: bool = False

    @property
    def top_level(self) -> List[str]:
        seen: List[str] = []
        for group in self.groups:
            if group.path and group.path[0] not in seen:
                seen.append(group.path[0])
        return seen

    @property
    def new_groups(self) -> List[Tuple[str, ...]]:
        return [g.path for g in self.groups if not g.exists]

    @property
    def merged_groups(self) -> List[Tuple[str, ...]]:
        return [g.path for g in self.groups if g.exists and g.key is not None]

    @property
    def host_specific(self) -> int:
        return sum(1 for h in self.hosts if h.file.host_specific)


def _excluded(key: Tuple[int, ...], excluded: Set[Tuple[int, ...]]) -> bool:
    return any(key[:depth] in excluded for depth in range(1, len(key) + 1))


def plan_native(document: NativeDocument, options: NativeImportOptions = NativeImportOptions(),
                excluded_groups: Iterable[Tuple[int, ...]] = (),
                existing: Optional[ExistingInventory] = None) -> NativePlan:
    """The file's tree mapped onto the inventory (deterministic, file order); no credentials yet."""

    from ..inventory.store import validate_alias, validate_target_fields

    existing = existing or ExistingInventory()
    excluded = {tuple(key) for key in excluded_groups}
    issues: List[Issue] = list(document.issues)
    base: Tuple[str, ...] = ()
    destination_missing = False
    if options.destination is Destination.EXISTING_GROUP:
        stored = existing.paths.get(_cf(options.target_group))
        if stored is None:
            destination_missing = True
            issues.append(Issue(Severity.ERROR, Code.DESTINATION_MISSING,
                                "The destination group does not exist any more; choose it again."))
            base = tuple(options.target_group)
        else:
            base = stored
    groups: Dict[Tuple[str, ...], PlannedGroup] = {}
    for depth in range(1, len(base) + 1):
        groups[_cf(base[:depth])] = PlannedGroup(base[:depth], None, not destination_missing)
    hosts: List[PlannedHost] = []
    renamed: List[Tuple[str, str]] = []
    excluded_hosts = filtered = 0
    group_namers: Dict[Tuple[str, ...], _Namer] = {}
    alias_namers: Dict[Tuple[str, ...], _Namer] = {}

    def keeps(group: FileGroup) -> bool:
        """Skip-empty: a group is kept when it (or a descendant) has a host that is imported."""

        return any(options.includes(h.protocol) for h in group.hosts) or any(keeps(c) for c in group.children)

    def place(group: FileGroup, parent: Tuple[str, ...]) -> None:
        nonlocal excluded_hosts, filtered
        if _excluded(group.key, excluded):
            excluded_hosts += group.total_hosts()
            return
        if options.skip_empty_groups and not keeps(group):
            return
        namer = group_namers.setdefault(_cf(parent), _Namer())
        name, codes = namer.name(group.name)
        existing_name = existing.stored_name(parent, name)
        if existing_name is not None and not codes:
            name = existing_name  # merged with the existing group (its spelling)
        path = parent + (name,)
        if codes:
            issues.append(Issue(Severity.INFO, Code.NAME_CHANGED,
                                f"Group '{clean_label(group.name)[:80]}' is stored as '{name}'."))
            renamed.append((clean_label(group.name), " / ".join(path)))
        planned = groups.get(_cf(path))
        if planned is None:
            planned = groups[_cf(path)] = PlannedGroup(path, group.key, existing.group_id(path) is not None,
                                                       group.note if options.import_notes else "", group.color,
                                                       group.position)
        alias_namer = alias_namers.setdefault(_cf(path), _Namer())
        for host in group.hosts:
            if not options.includes(host.protocol):
                filtered += 1
                continue
            alias, alias_codes = alias_namer.name(host.alias)
            label = clean_label(host.alias)[:80]
            error = validate_alias(alias, host.hostname) or validate_target_fields(host.hostname, host.port,
                                                                                   host.user)
            if error:
                issues.append(Issue(Severity.WARNING, Code.INVALID_HOSTNAME, f"'{label}': {error} Left out."))
                continue
            if alias_codes:
                issues.append(Issue(Severity.INFO, Code.NAME_CHANGED, f"Host '{label}' is stored as '{alias}'."))
                renamed.append((label, alias))
            credential = document.credentials.get(host.credential_ref) if host.credential_ref else None
            user_key = credential.username if credential is not None and credential.username else host.user
            planned_host = PlannedHost(host, path, alias, user_key, credential)
            planned.hosts.append(planned_host)
            hosts.append(planned_host)
        for child in group.children:
            place(child, path)

    for root in document.groups:
        place(root, base)
    if filtered:
        issues.append(Issue(Severity.INFO, Code.EXCLUDED_PROTOCOL,
                            f"{filtered} host(s) of protocols you left out are not imported."))
    return NativePlan(document, options, list(groups.values()), hosts, issues, discover_users(hosts), renamed,
                      existing, excluded_hosts, filtered, destination_missing)


def discover_users(hosts: Sequence[PlannedHost]) -> List[UsernameEntry]:
    """Every distinct effective user (a credential's username, else the host's user), most used first.

    Case-only variants are separate rows marked with each other; ``""`` is "(no user)"; anonymous FTP is
    left out (it logs in without a credential). ``raw_forms`` says where each came from.
    """

    entries: Dict[str, UsernameEntry] = {}
    for host in hosts:
        if host.anonymous:
            continue
        entry = entries.setdefault(host.user_key, UsernameEntry(host.user_key))
        entry.count += 1
        entry.protocols.add(host.file.protocol)
        if host.credential is not None:
            entry.references += 1
            entry.raw_forms.add(f"credential “{host.credential.name}” ({host.credential.username})")
        elif host.user_key:
            entry.literals += 1
            entry.raw_forms.add(host.user_key)
    lowered: Dict[str, List[str]] = {}
    for key in entries:
        if key:
            lowered.setdefault(key.casefold(), []).append(key)
    for variants in lowered.values():
        if len(variants) > 1:
            for key in variants:
                entries[key].case_variants = sorted(v for v in variants if v != key)
    return sorted(entries.values(), key=lambda e: (-e.count, e.key == NO_USER, e.key.casefold(), e.key))


def suggest_credentials(plan: NativePlan, credentials: Sequence[object]) -> Dict[str, str]:
    """``{user key: credential id}`` "matched" suggestions.

    A credential referenced in the file whose name **and** username match an existing credential; failing
    that, exactly one existing credential with that username.
    """

    by_name_user: Dict[Tuple[str, str], List[str]] = {}
    by_user: Dict[str, List[str]] = {}
    for cred in credentials:
        if getattr(cred, "private", False):
            continue
        name, user = str(getattr(cred, "name", "")), str(getattr(cred, "username", ""))
        by_name_user.setdefault((name.casefold(), user), []).append(cred.id)  # type: ignore[attr-defined]
        by_user.setdefault(user, []).append(cred.id)  # type: ignore[attr-defined]
    suggestions: Dict[str, str] = {}
    for entry in plan.users:
        if not entry.key:
            continue
        refs = {h.credential for h in plan.hosts if h.user_key == entry.key and h.credential is not None}
        exact = sorted({cid for ref in refs for cid in by_name_user.get((ref.name.casefold(), ref.username), [])})
        if len(exact) == 1:
            suggestions[entry.key] = exact[0]
        elif not exact and len(by_user.get(entry.key, [])) == 1:
            suggestions[entry.key] = by_user[entry.key][0]
    return suggestions


# ---------------------------------------------------------- credential mapping
@dataclass
class NativeJob:
    plan: NativePlan
    inserts: List[Tuple[Tuple[str, ...], HostEntry, PlannedHost]]
    updates: List[Tuple[HostEntry, PlannedHost]]
    bindings: List[Tuple[HostEntry, Optional[str]]]
    private: List[Tuple[HostEntry, str]]  # host-specific: a private credential without a password (asked at connect)
    tunnels: List[Tuple[HostEntry, str]]  # (RDP host, via_ssh_ref): resolved to the new ids at commit
    group_colors: Dict[str, str]
    host_colors: Dict[str, str]
    issues: List[Issue]
    skipped_existing: int = 0
    renamed: int = 0
    mapping: Dict[str, Optional[str]] = field(default_factory=dict)
    credential_names: Dict[str, str] = field(default_factory=dict)
    smart_groups: List[Dict[str, str]] = field(default_factory=list)

    @property
    def host_count(self) -> int:
        return len(self.inserts)

    @property
    def overwritten(self) -> int:
        return len(self.updates)

    @property
    def nothing_to_do(self) -> bool:
        return not self.inserts and not self.updates and not self.plan.new_groups


def group_color_key(path: Sequence[str]) -> str:
    return "/".join(path)


def host_color_key(path: Sequence[str], alias: str) -> str:
    return f"{path[0]}::{'/'.join(path[1:])}::{alias}"


def _store_color(color: str) -> str:
    return color.lower()  # the app writes #rrggbb


def host_entry(host: PlannedHost, user: str, alias: str, *, notes: bool) -> HostEntry:
    from ..inventory.manager import entry_options

    metadata = dict(host.file.metadata)
    if not notes:
        metadata.pop("notes", None)
        metadata.pop("tags", None)
    options = entry_options(host.file.hostname, host.file.port, user, dict(host.file.options))
    group = host.group
    return HostEntry(alias=alias, options=options, metadata=metadata, parent_group=group[0] if group else "",
                     child_group="/".join(group[1:]), protocol=host.file.protocol)


def apply_mapping(
    plan: NativePlan,
    mapping: Mapping[str, Optional[str]],
    credentials: Mapping[str, object],
    *,
    default: Optional[str] = None,
    existing_group_colors: Optional[Mapping[str, str]] = None,
    existing_host_colors: Optional[Mapping[str, str]] = None,
    key_exists: Callable[[str], bool] = lambda path: os.path.exists(os.path.expanduser(path)),
) -> NativeJob:
    """Final hosts, conflicts (the MobaXterm rules) and colours. ``mapping``: user key → credential id or ``None``."""

    resolved = {entry.key: mapping.get(entry.key, default) for entry in plan.users}
    for cred_id in resolved.values():
        if cred_id and cred_id not in credentials:
            raise CredentialMissingError(cred_id)
    options = plan.options
    issues: List[Issue] = []
    inserts: List[Tuple[Tuple[str, ...], HostEntry, PlannedHost]] = []
    updates: List[Tuple[HostEntry, PlannedHost]] = []
    bindings: List[Tuple[HostEntry, Optional[str]]] = []
    private: List[Tuple[HostEntry, str]] = []
    tunnels: List[Tuple[HostEntry, str]] = []
    group_colors: Dict[str, str] = {}
    host_colors: Dict[str, str] = {}
    old_group_colors = existing_group_colors or {}
    old_host_colors = existing_host_colors or {}
    skipped = renamed = 0
    policy = options.conflict_policy
    colors_on = options.import_colors and options.color_policy is not ColorPolicy.NONE

    def color(target: Dict[str, str], old: Mapping[str, str], key: str, value: str) -> None:
        if not colors_on or not value:
            return
        if options.color_policy is ColorPolicy.IF_EMPTY and old.get(key):
            return
        target[key] = _store_color(value)

    for group in plan.groups:
        if group.key is not None:
            color(group_colors, old_group_colors, group_color_key(group.path), group.color)
        existing = plan.existing.hosts_in(group.path) if group.exists else []
        by_identity: Dict[Tuple[str, int, str, str], List[ExistingHost]] = {}
        for existing_host in existing:
            by_identity.setdefault(identity_key(existing_host.hostname or existing_host.alias, existing_host.port,
                                                existing_host.user, existing_host.protocol), []).append(existing_host)
        taken = {h.alias.casefold() for h in existing}
        for host in group.hosts:
            cred_id = None if host.anonymous else resolved.get(host.user_key)
            if cred_id:
                cred = credentials[cred_id]
                user = str(getattr(cred, "username", "") or host.file.user)
            else:
                user = host.file.user
            label = clean_label(host.file.alias)[:80]
            candidates = by_identity.get(identity_key(host.file.hostname or host.alias, host.file.port, user,
                                                      host.file.protocol), [])
            match = next((h for h in candidates if h.alias.casefold() == host.alias.casefold()),
                         candidates[0] if candidates else None)
            alias = host.alias
            if match is not None:
                if policy is ConflictPolicy.SKIP:
                    skipped += 1
                    issues.append(Issue(Severity.INFO, Code.EXISTING_HOST_SKIPPED,
                                        f"'{label}' already exists as '{match.alias}' in "
                                        f"'{' / '.join(group.path)}'; skipped."))
                    continue
                if policy is ConflictPolicy.OVERWRITE:
                    entry = host_entry(host, user, match.alias, notes=options.import_notes)
                    entry.id = match.id
                    updates.append((entry, host))
                    bindings.append((entry, cred_id))
                    if host.file.host_specific and not cred_id:
                        private.append((entry, user))
                    if host.file.via_ssh_ref:
                        tunnels.append((entry, host.file.via_ssh_ref))
                    color(host_colors, old_host_colors, host_color_key(group.path, match.alias), host.file.color)
                    issues.append(Issue(Severity.WARNING, Code.EXISTING_HOST_OVERWRITTEN,
                                        f"'{match.alias}' in '{' / '.join(group.path)}' is updated."))
                    continue
                alias = unique_name(alias, taken | {alias.casefold()})
                renamed += 1
                issues.append(Issue(Severity.INFO, Code.EXISTING_HOST_RENAMED,
                                    f"'{label}' already exists; imported as '{alias}'."))
            elif alias.casefold() in taken:
                alias = unique_name(alias, taken)
                issues.append(Issue(Severity.INFO, Code.NAME_DEDUPLICATED,
                                    f"Another host is already called '{host.alias}' here; '{label}' is stored as "
                                    f"'{alias}'."))
            taken.add(alias.casefold())
            entry = host_entry(host, user, alias, notes=options.import_notes)
            inserts.append((group.path, entry, host))
            if cred_id:
                bindings.append((entry, cred_id))
            elif host.file.host_specific:
                private.append((entry, user))
            if host.file.via_ssh_ref:
                tunnels.append((entry, host.file.via_ssh_ref))
            color(host_colors, old_host_colors, host_color_key(group.path, alias), host.file.color)
            identity_file = str(host.file.options.get("identityfile", "") or "")
            if identity_file and not key_exists(identity_file):
                issues.append(Issue(Severity.INFO, Code.KEY_FILE_MISSING,
                                    f"'{alias}': its private key file is not on this computer."))
    # Tunnels to hosts that are not imported are dropped (resolved at commit for the others).
    imported_refs = {h.file.ref for _p, _e, h in inserts} | {h.file.ref for _e, h in updates}
    kept_tunnels = []
    for entry, ref in tunnels:
        if ref in imported_refs:
            kept_tunnels.append((entry, ref))
        else:
            issues.append(Issue(Severity.WARNING, Code.TUNNEL_DROPPED,
                                f"'{entry.alias}': its SSH tunnel host is not imported; the tunnel was left out."))
    if plan.host_specific:
        issues.append(Issue(Severity.INFO, Code.HOST_SPECIFIC,
                            f"{len(private)} host(s) will ask for their password on first connect."))
    names = {cid: str(getattr(credentials[cid], "name", cid)) for cid in resolved.values() if cid}
    smart = list(plan.document.smart_groups) if options.import_smart_groups else []
    return NativeJob(plan, inserts, updates, bindings, private, kept_tunnels, group_colors, host_colors, issues,
                     skipped, renamed, dict(resolved), names, smart)


__all__ = [
    "NativeDocument", "FileGroup", "FileHost", "FileCredential", "read_native", "sniff", "plan_native",
    "NativeImportOptions", "ColorPolicy", "NativePlan", "PlannedHost", "PlannedGroup", "discover_users",
    "suggest_credentials", "apply_mapping", "NativeJob", "Issue", "Code", "NativeFormatError",
    "CredentialMissingError", "ImportCancelled", "ImportReport", "ImportStatus", "ConflictPolicy", "Destination",
    "ExistingInventory", "group_color_key", "host_color_key", "MAX_HOSTS", "MAX_DEPTH", "MAX_FILE_BYTES",
    "render_native_report", "save_native_report", "commit_native", "NativeImportReport", "merge_smart_groups",
]


# --------------------------------------------------------------------- commit
FAILED_PREFIX = "Import failed, no changes were made"


@dataclass
class NativeImportReport(ImportReport):
    """:class:`ImportReport` plus what the caller saves after the database commit (UI state, smart groups)."""

    group_colors: Dict[str, str] = field(default_factory=dict)
    host_colors: Dict[str, str] = field(default_factory=dict)
    smart_groups: List[Dict[str, str]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


def commit_native(
    job: NativeJob,
    manager,
    credential_store,
    *,
    progress: Optional[Callable[[int, int], None]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
) -> NativeImportReport:
    """Write the job in **one** store transaction (groups, hosts, updates, tunnels, bindings) or change nothing.

    Host ids are chosen before writing, so an RDP host's ``via_ssh`` can point to a host of the same import.
    Credentials and the destination are re-checked first. Afterwards (outside the transaction) the
    host-specific credentials are created without a password; the colours and smart groups are returned for
    the caller to save — only after a successful commit, under the final names.
    """

    import uuid

    from ..inventory.manager import host_columns

    plan = job.plan
    parents = plan.top_level
    if job.nothing_to_do:
        return NativeImportReport(ImportStatus.NOTHING, "Nothing new to import: every host already exists.")
    if plan.destination_missing:
        return NativeImportReport(ImportStatus.FAILED, f"{FAILED_PREFIX}: the destination group does not exist.")
    if any(cid and credential_store.get(cid) is None for _entry, cid in job.bindings):
        return NativeImportReport(ImportStatus.FAILED, f"{FAILED_PREFIX}: a chosen credential was deleted meanwhile.")
    store = manager.store
    if job.updates:
        manager.backup_before("before-import-overwrite")
    total = len(plan.groups) + 1

    def check_cancel() -> None:
        if should_cancel is not None and should_cancel():
            raise ImportCancelled()

    for _path, entry, _host in job.inserts:
        entry.id = uuid.uuid4().hex  # known before writing: tunnels and bindings refer to it
    ids_by_ref = {host.file.ref: entry.id for _p, entry, host in job.inserts if host.file.ref}
    ids_by_ref.update({host.file.ref: entry.id for entry, host in job.updates if host.file.ref})
    for entry, ref in job.tunnels:
        rdp = dict(entry.metadata.get("rdp") or {})
        rdp["via_ssh"] = ids_by_ref[ref]
        entry.metadata["rdp"] = rdp
    try:
        with store.transaction():
            if plan.options.destination is Destination.EXISTING_GROUP:
                target_id = plan.existing.group_id(plan.options.target_group)
                if target_id is None or store.get_group(target_id) is None:
                    raise LookupError("the destination group was deleted meanwhile")
            group_ids: Dict[Tuple[str, ...], str] = {}
            for index, group in enumerate(plan.groups):
                check_cancel()
                parent_id = group_ids.get(_cf(group.path[:-1])) if len(group.path) > 1 else None
                found = store.find_child(parent_id, group.path[-1])
                if found is None:
                    if group.exists:
                        raise LookupError(f"the group '{' / '.join(group.path)}' was deleted meanwhile")
                    found = store.create_group(parent_id, group.path[-1], note=group.note)
                    if group.position:
                        store.set_position("group", found.id, group.position)
                group_ids[_cf(group.path)] = found.id
                if progress is not None:
                    progress(index + 1, total)
            check_cancel()
            store.add_hosts_bulk(manager._new_host(group_ids[_cf(path)], entry) for path, entry, _h in job.inserts)
            for _path, entry, host in job.inserts:
                if host.file.position:
                    store.set_position("host", entry.id, host.file.position)
            for entry, _host in job.updates:
                hostname, port, user, rest = host_columns(entry.options)
                store.update_host(entry.id, hostname=hostname, port=port, user=user, options=rest,
                                  metadata=dict(entry.metadata), protocol=entry.protocol)
            binding_map = {entry.id: cid for entry, cid in job.bindings if entry.id}
            if binding_map:
                credential_store.bind_many(binding_map)  # a failure rolls the database back as well
            check_cancel()
            if progress is not None:
                progress(total, total)
    except ImportCancelled:
        manager.refresh()
        return NativeImportReport(ImportStatus.CANCELLED, "Import cancelled, no changes were made.")
    except Exception as exc:  # sqlite3.Error, InventoryError, SecureStoreError, KeyError, LookupError…
        manager.refresh()
        return NativeImportReport(ImportStatus.FAILED, f"{FAILED_PREFIX}: {exc}" if str(exc) else FAILED_PREFIX)
    warnings: List[str] = []
    failed_private = 0
    for entry, username in job.private:
        try:
            credential_store.create_private(entry.id, username, None, label=entry.alias)
        except Exception:  # SecureStoreError: the host simply has no host-specific entry yet
            failed_private += 1
    if failed_private:
        warnings.append(f"{failed_private} host(s) could not be marked as using a host-specific password.")
    manager.refresh()
    new_groups = len(plan.new_groups)
    message = f"Imported {job.host_count} host(s)"
    if job.overwritten:
        message += f", updated {job.overwritten}"
    message += f"; {new_groups} new group(s)."
    return NativeImportReport(ImportStatus.SUCCESS, message, job.host_count, new_groups, parents,
                              group_colors=dict(job.group_colors), host_colors=dict(job.host_colors),
                              smart_groups=list(job.smart_groups), warnings=warnings)


def merge_smart_groups(existing: Sequence[object], incoming: Sequence[Mapping[str, str]]) -> Tuple[list, int]:
    """``(existing + the incoming ones whose name is new, how many were added)`` (names compared case-insensitively)."""

    from ..smart_groups import SmartGroup

    names = {str(getattr(g, "name", "")).casefold() for g in existing}
    merged = list(existing)
    added = 0
    for item in incoming:
        name = item.get("name", "").strip()
        if name and name.casefold() not in names:
            merged.append(SmartGroup(name=name, query=item.get("query", "").strip()))
            names.add(name.casefold())
            added += 1
    return merged, added


# --------------------------------------------------------------------- report
def render_native_report(plan: NativePlan, job: Optional[NativeJob] = None, report: Optional[ImportReport] = None,
                         *, source_name: str = "") -> str:
    """Plain-text import report: counts, names and issue codes — never a value from a host's fields."""

    document = plan.document
    lines = [f"{APP_NAME} — import report ({APP_NAME} export)", time.strftime("%Y-%m-%d %H:%M:%S")]
    if source_name:
        lines.append(f"File: {source_name}")
    lines.append(f"Exported: {document.exported_at or 'unknown'} by Ophion {document.app_version or '?'}"
                 + (" (encrypted)" if document.encrypted else ""))
    lines.append(f"Integrity: {document.integrity}")
    if report is not None:
        lines += ["", f"Result: {report.status.value} — {report.message}"]
    by_protocol: Dict[str, int] = {}
    for host in plan.hosts:
        by_protocol[host.file.protocol] = by_protocol.get(host.file.protocol, 0) + 1
    lines += ["", "Summary",
              f"  Groups: {len(plan.groups)} ({len(plan.new_groups)} new, {len(plan.merged_groups)} merged)",
              f"  Hosts planned: {len(plan.hosts)}"]
    lines += [f"    {name}: {count}" for name, count in sorted(by_protocol.items())]
    if plan.excluded_hosts:
        lines.append(f"  Excluded by unticked groups: {plan.excluded_hosts}")
    if plan.filtered_hosts:
        lines.append(f"  Left out by the protocol filters: {plan.filtered_hosts}")
    if job is not None:
        lines += [f"  Hosts to import: {job.host_count}",
                  f"  Skipped (already exist): {job.skipped_existing}",
                  f"  Imported as a numbered copy: {job.renamed}",
                  f"  Updated: {job.overwritten}",
                  f"  Colours to set: {len(job.group_colors)} group(s), {len(job.host_colors)} host(s)",
                  f"  Credential bindings: {sum(1 for _e, cid in job.bindings if cid)}",
                  f"  Host-specific passwords to enter on first connect: {len(job.private)}",
                  f"  SSH tunnels kept: {len(job.tunnels)}"]
        if job.smart_groups:
            lines.append(f"  Smart groups: {len(job.smart_groups)}")
    all_issues = plan.issues + (job.issues if job is not None else [])
    counts: Dict[str, int] = {}
    for issue in all_issues:
        counts[issue.code.value] = counts.get(issue.code.value, 0) + 1
    if counts:
        lines += ["", "Issues by code"] + [f"  {code}: {count}" for code, count in sorted(counts.items())]
    if job is not None and plan.users:
        lines += ["", "Credential mapping (user in file → credential)"]
        for entry in plan.users:
            cred_id = job.mapping.get(entry.key)
            target = job.credential_names.get(cred_id, cred_id) if cred_id else "None (keep the user as written)"
            lines.append(f"  {entry.key or '(no user)'} ({entry.count} host(s)) → {target}")
    if plan.renamed:
        lines += ["", "Names stored differently (file → Ophion)"] + [f"  {a} → {b}" for a, b in plan.renamed]
    if all_issues:
        lines += ["", "Details"] + [f"  [{i.severity.value}] {i.code.value}: {i.message}" for i in all_issues]
    return "\n".join(lines) + "\n"


def save_native_report(text: str, directory: Path) -> Path:
    """``sshm-import-<timestamp>.log`` (mode 0600)."""

    from ..persistence import write_text_atomic

    directory.mkdir(parents=True, exist_ok=True)
    path = directory / time.strftime("sshm-import-%Y%m%d-%H%M%S.log")
    counter = 2
    while path.exists():
        path = directory / time.strftime(f"sshm-import-%Y%m%d-%H%M%S-{counter}.log")
        counter += 1
    write_text_atomic(path, text, mode=0o600)
    return path
