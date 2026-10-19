"""Import MobaXterm session exports (``*.mxtsessions``) — pure Python, no GTK.

Pipeline (each step is a pure function except :func:`commit`):

1. :func:`parse_mxtsessions` — bytes/path → :class:`MxtParseResult` (folders,
   sessions, problems). Hand-rolled, line-based, O(n); never raises on bad
   lines (fatal file-level problems raise :class:`MxtFileError`).
2. :func:`plan_import` — folders → parent/child groups, SSH sessions → hosts
   (sanitized, de-duplicated, collision-free), plus the usernames found.
3. :func:`apply_credential_mapping` — the user's username → credential choices
   (+ conflict policy against the existing inventory) → :class:`ImportJob`.
4. :func:`commit` — one bulk write (``ConfigManager.import_groups``), one
   ``CredentialStore.bind_many``, one ``refresh()``; rolls everything back on
   any failure.

Nothing here logs, prints or stores raw session lines; issues carry line
numbers, codes and the (user-visible) folder/session names only. Passwords are
never part of a MobaXterm export.

Format notes (see ``docs/import-mobaxterm.md``): every ``[Bookmarks]`` /
``[Bookmarks_N]`` section is one folder whose full path is ``SubRep=``
(``\\``-separated); every other ``name=value`` line in it is a session,
``#<icon>#<type>%f0%f1%…``. Type 0 is SSH: ``f0`` host, ``f1`` port, ``f2``
username (``[label]`` = a MobaXterm credential reference), ``f7``/``f8``/``f9``
the jump host, its port and user.
"""

from __future__ import annotations

import codecs
import hashlib
import re
import time
import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple, Union

from ..config import GroupConfig, HostEntry, ImportCancelled, ImportConflictError, ImportWriteError

# ----------------------------------------------------------------- constants
MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_LINE_CHARS = 64 * 1024
MAX_DEPTH = 32
MAX_SEGMENT = 64
ROOT_CHILD_NAME = "General"  # child group for sessions directly in a top-level folder
OWN_HOSTS_CHILD_NAME = "_hosts"  # a folder's own hosts when it also has subfolders
EMPTY_SUBREP_PARENT = "MobaXterm"  # folder for sessions of a section with an empty SubRep
NO_USERNAME = ""  # username-entry key for sessions without a username
SSH_TYPE = 0
TYPE_NAMES = {
    0: "SSH", 1: "Telnet", 2: "Rsh", 3: "Xdmcp", 4: "RDP", 5: "VNC", 6: "FTP", 7: "SFTP",
    8: "Serial", 9: "Mosh", 10: "AWS S3", 11: "WSL", 12: "Shell",
}
_HIDDEN_SUFFIXES = (".bak", ".tmp", ".swp", ".pyc")  # load_config_tree skips these names
_SESSION_VALUE = re.compile(r"^\s*#(\d+)#(\d+)%(.*)$", re.DOTALL)
_SECTION = re.compile(r"^\s*\[(.*)\]\s*$")
_BOOKMARKS = re.compile(r"^bookmarks(_\d+)?$", re.IGNORECASE)
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
_DASHES = re.compile(r"-{2,}")
_WINDOWS_PATH = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\|_[A-Za-z]+_\\)|\\")
_KEY_FILE = re.compile(r"(?:\.(?:pem|ppk|key)$)|(?:[\\/]\.ssh[\\/][^\\/]+$)", re.IGNORECASE)
_USER_OK = re.compile(r"^[^\s@:/\x00-\x1f\x7f]+$")


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class IssueCode(str, Enum):
    UNPARSEABLE_LINE = "unparseable_line"
    UNSUPPORTED_TYPE = "unsupported_type"
    EMPTY_HOSTNAME = "empty_hostname"
    INVALID_HOSTNAME = "invalid_hostname"
    INVALID_PORT = "invalid_port"
    INVALID_GATEWAY = "invalid_gateway"
    NAME_SANITIZED = "name_sanitized"
    NAME_TRUNCATED = "name_truncated"
    ALIAS_DEDUPLICATED = "alias_deduplicated"
    FOLDER_COLLISION_RESOLVED = "folder_collision_resolved"
    WINDOWS_KEY_PATH = "windows_key_path"
    DUPLICATE_EXISTING_HOST = "duplicate_existing_host"
    DUPLICATE_HOST = "duplicate_host"
    EXISTING_HOST_SKIPPED = "existing_host_skipped"
    EXISTING_HOST_RENAMED = "existing_host_renamed"
    EXISTING_HOST_OVERWRITTEN = "existing_host_overwritten"
    ORPHAN_SESSION = "orphan_session"
    DUPLICATE_FOLDER_SECTION = "duplicate_folder_section"
    EMPTY_SUBREP = "empty_subrep"
    FOLDER_TOO_DEEP = "folder_too_deep"
    LINE_TOO_LONG = "line_too_long"
    INVALID_FIELD = "invalid_field"


@dataclass(frozen=True)
class ImportIssue:
    severity: Severity
    code: IssueCode
    message: str  # never contains a raw session line or anything from the value fields but names
    line: Optional[int] = None


class MxtFileError(Exception):
    """A fatal, file-level problem (unreadable, too large, not a MobaXterm export…)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ------------------------------------------------------------------- parsing
@dataclass(frozen=True)
class MxtFolder:
    path: Tuple[str, ...]
    img_num: Optional[int]
    line: int


@dataclass(frozen=True)
class MxtSession:
    name: str
    type_code: int
    icon: Optional[int]
    fields: Tuple[str, ...]
    folder: Tuple[str, ...]
    line: int

    def field(self, index: int) -> str:
        return self.fields[index] if 0 <= index < len(self.fields) else ""


@dataclass
class MxtParseResult:
    folders: List[MxtFolder]
    sessions: List[MxtSession]
    problems: List[ImportIssue]
    encoding: str
    size: int = 0
    lines: int = 0


def decode_mxt_bytes(data: bytes) -> Tuple[str, str]:
    """``(text, encoding)``: BOM → UTF-8/UTF-16; else UTF-16 by NUL pattern, UTF-8, cp1252, latin-1."""

    if data.startswith(codecs.BOM_UTF8):
        return data[len(codecs.BOM_UTF8):].decode("utf-8", errors="replace"), "utf-8-sig"
    if data.startswith(codecs.BOM_UTF16_LE) or data.startswith(codecs.BOM_UTF16_BE):
        return data.decode("utf-16", errors="replace"), "utf-16"
    head = data[:4096]
    if len(head) >= 4:
        even = head[0::2].count(0)
        odd = head[1::2].count(0)
        half = len(head) / 2.0
        if odd > half * 0.3 and even < half * 0.05:
            return data.decode("utf-16-le", errors="replace"), "utf-16-le"
        if even > half * 0.3 and odd < half * 0.05:
            return data.decode("utf-16-be", errors="replace"), "utf-16-be"
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    try:
        return data.decode("cp1252"), "cp1252"
    except UnicodeDecodeError:
        return data.decode("latin-1", errors="replace"), "latin-1"


def _read_source(source: Union[bytes, bytearray, Path, str]) -> bytes:
    if isinstance(source, (bytes, bytearray)):
        data = bytes(source)
        if len(data) > MAX_FILE_BYTES:
            raise MxtFileError("too_large", _too_large())
        return data
    path = Path(source)
    try:
        if not path.exists():
            raise MxtFileError("missing", "The file does not exist.")
        if path.is_dir():
            raise MxtFileError("is_directory", "This is a folder, not a file.")
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            raise MxtFileError("too_large", _too_large())
        return path.read_bytes()
    except PermissionError as exc:
        raise MxtFileError("permission", "You do not have permission to read this file.") from exc
    except MxtFileError:
        raise
    except OSError as exc:
        raise MxtFileError("unreadable", f"The file could not be read ({exc.strerror or exc}).") from exc


def _too_large() -> str:
    return f"The file is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB, which is not a MobaXterm sessions export."


def parse_mxtsessions(source: Union[bytes, bytearray, Path, str]) -> MxtParseResult:
    """Parse a MobaXterm sessions export. Bad lines become problems; bad files raise :class:`MxtFileError`."""

    data = _read_source(source)
    if not data.strip(b" \t\r\n\x00"):
        raise MxtFileError("empty", "The file is empty.")
    text, encoding = decode_mxt_bytes(data)
    text = text.replace("\x00", "")
    sample = text[:8192]
    controls = sum(1 for ch in sample if ord(ch) < 32 and ch not in "\t\r\n")
    if sample and (controls > len(sample) * 0.05 or sample.count("�") > len(sample) * 0.1):
        raise MxtFileError("binary", "This looks like a binary file, not a MobaXterm sessions export.")

    folders: Dict[Tuple[str, ...], MxtFolder] = {}
    sessions: List[MxtSession] = []
    problems: List[ImportIssue] = []
    seen_bookmarks = False
    in_bookmarks = False
    folder: Optional[Tuple[str, ...]] = None
    pending: List[Tuple[str, str, int]] = []  # session lines seen before the section's SubRep
    section_line = 0
    img_num: Optional[int] = None
    lines = text.splitlines()

    def close_section() -> None:
        nonlocal pending
        if in_bookmarks and folder is None and pending:
            for _name, _value, number in pending:
                problems.append(ImportIssue(Severity.WARNING, IssueCode.ORPHAN_SESSION,
                                            "Session in a section without a SubRep line; skipped.", number))
        pending = []

    def add_session(name: str, value: str, number: int, target: Tuple[str, ...]) -> None:
        match = _SESSION_VALUE.match(value)
        if match is None:
            problems.append(ImportIssue(Severity.WARNING, IssueCode.UNPARSEABLE_LINE,
                                        f"Session '{_safe_label(name)}' could not be read; skipped.", number))
            return
        icon_text, type_text, rest = match.groups()
        sessions.append(MxtSession(name=name, type_code=int(type_text), icon=int(icon_text),
                                   fields=tuple(rest.split("%")), folder=target, line=number))

    for number, raw in enumerate(lines, start=1):
        if len(raw) > MAX_LINE_CHARS:
            problems.append(ImportIssue(Severity.WARNING, IssueCode.LINE_TOO_LONG, "Line is too long; skipped.", number))
            continue
        line = raw.strip()
        if not line or line.startswith(";"):
            continue
        section = _SECTION.match(line)
        if section is not None:
            close_section()
            in_bookmarks = bool(_BOOKMARKS.match(section.group(1).strip()))
            seen_bookmarks = seen_bookmarks or in_bookmarks
            folder = None
            img_num = None
            section_line = number
            continue
        key, sep, value = line.partition("=")
        key = key.strip()
        if not in_bookmarks:
            if not seen_bookmarks and sep and _SESSION_VALUE.match(value):
                problems.append(ImportIssue(Severity.WARNING, IssueCode.ORPHAN_SESSION,
                                            "Session line before any folder section; skipped.", number))
            continue
        lowered = key.lower()
        if lowered == "subrep" and sep:
            if folder is not None:
                continue  # the first SubRep of a section defines the folder
            segments = tuple(part.strip() for part in value.split("\\") if part.strip())
            if not segments:
                problems.append(ImportIssue(Severity.INFO, IssueCode.EMPTY_SUBREP,
                                            f"Empty folder path; its sessions go to '{EMPTY_SUBREP_PARENT}'.",
                                            number))
                segments = (EMPTY_SUBREP_PARENT,)
            if len(segments) > MAX_DEPTH:
                problems.append(ImportIssue(Severity.WARNING, IssueCode.FOLDER_TOO_DEEP,
                                            f"Folder nested deeper than {MAX_DEPTH} levels; skipped.", number))
                folder = ()
                pending = []
                continue
            folder = segments
            if segments in folders:
                problems.append(ImportIssue(Severity.INFO, IssueCode.DUPLICATE_FOLDER_SECTION,
                                            f"Folder '{_folder_text(segments)}' appears twice; merged.", number))
            else:
                folders[segments] = MxtFolder(segments, img_num, section_line or number)
            for name, session_value, line_number in pending:
                add_session(name, session_value, line_number, segments)
            pending = []
            continue
        if lowered == "imgnum" and sep:
            try:
                img_num = int(value.strip())
            except ValueError:
                img_num = None
            continue
        if not sep:
            problems.append(ImportIssue(Severity.WARNING, IssueCode.UNPARSEABLE_LINE,
                                        "Line is not 'name=value'; skipped.", number))
            continue
        if folder is None:
            pending.append((key, value, number))
        elif folder:  # () = a refused (too deep) folder
            add_session(key, value, number, folder)
    close_section()
    if not seen_bookmarks:
        raise MxtFileError("not_mobaxterm", "This does not look like a MobaXterm sessions file.")
    return MxtParseResult(list(folders.values()), sessions, problems, encoding, len(data), len(lines))


# ----------------------------------------------------------------- sanitizing
def _short_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()[:4]


def clean_label(text: str) -> str:
    """Original name for display/metadata: no control, format (bidi, zero-width) chars or line breaks."""

    kept = "".join(" " if ch in "\t\r\n" else ch for ch in unicodedata.normalize("NFC", text or "")
                   if unicodedata.category(ch) not in ("Cc", "Cf") or ch in "\t\r\n")
    return " ".join(kept.split())


def _safe_label(text: str, limit: int = 80) -> str:
    label = clean_label(text)
    return label if len(label) <= limit else label[: limit - 1] + "…"


def _folder_text(path: Sequence[str]) -> str:
    return "\\".join(clean_label(part) for part in path)


def sanitize_name(original: str) -> Tuple[str, List[IssueCode]]:
    """ASCII-only on-disk name ``[A-Za-z0-9._-]`` (≤ 64 chars) and the issues it raised.

    Empty results come back as ``""`` (the caller numbers them ``unnamed-<n>``).
    """

    text = unicodedata.normalize("NFC", original or "").strip()
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    name = _DASHES.sub("-", _UNSAFE.sub("-", ascii_text)).strip("-.")
    issues: List[IssueCode] = []
    if name in (".", ".."):
        name = ""
    lowered = name.lower()
    if lowered.endswith(_HIDDEN_SUFFIXES):
        name = name[: name.rfind(".")] + "-" + name[name.rfind(".") + 1:]
    if lowered == "__pycache__":
        name = "pycache"
    if len(name) > MAX_SEGMENT:
        name = name[: MAX_SEGMENT - 5].rstrip("-.") + "-" + _short_hash(original)
        issues.append(IssueCode.NAME_TRUNCATED)
    if name != text:
        issues.append(IssueCode.NAME_SANITIZED)
    return name, issues


def _unique(name: str, taken: Set[str]) -> str:
    """``name``, else ``name-2``, ``name-3``… (case-insensitive against ``taken``)."""

    if name.lower() not in taken:
        return name
    counter = 2
    while True:
        suffix = f"-{counter}"
        candidate = name[: MAX_SEGMENT - len(suffix)] + suffix
        if candidate.lower() not in taken:
            return candidate
        counter += 1


# ------------------------------------------------------------------ SSH fields
@dataclass(frozen=True)
class SshTarget:
    hostname: str
    port: int
    user_raw: str  # f2 as written
    user_key: str  # normalized: no brackets, trimmed ("" = no username)
    user_is_reference: bool  # [label] = a MobaXterm credential reference, not a login name
    proxy_jump: str  # "" when none
    identity_file: str  # "" when none


def _valid_hostname(host: str) -> bool:
    return bool(host) and not host.startswith("-") and not any(
        ch.isspace() or ord(ch) < 32 or ord(ch) == 127 or ch in "\"'`\\" for ch in host)


def _parse_port(text: str) -> Optional[int]:
    try:
        port = int(text.strip())
    except ValueError:
        return None
    return port if 1 <= port <= 65535 else None


def normalize_username(raw: str) -> Tuple[str, bool]:
    """``("alice", True)`` for ``[alice]``, ``("alice", False)`` for ``alice``, ``("", False)`` for empty."""

    text = (raw or "").strip()
    if len(text) >= 2 and text.startswith("[") and text.endswith("]"):
        return text[1:-1].strip(), True
    return text, False


def ssh_target(session: MxtSession, issues: List[ImportIssue]) -> Optional[SshTarget]:
    """The SSH fields of a type-0 session (``None`` when it cannot be imported; issues appended)."""

    label = _safe_label(session.name)
    host = session.field(0).strip()
    port_text = session.field(1).strip()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]  # [IPv6]
    elif host.count(":") == 1:  # host:port (an IPv6 literal has several colons)
        name, _, maybe_port = host.partition(":")
        if maybe_port.isdigit() and not port_text:
            host, port_text = name, maybe_port
        else:
            issues.append(ImportIssue(Severity.WARNING, IssueCode.INVALID_HOSTNAME,
                                      f"'{label}' has an invalid host name; skipped.", session.line))
            return None
    if not host:
        issues.append(ImportIssue(Severity.WARNING, IssueCode.EMPTY_HOSTNAME,
                                  f"'{label}' has no host name; skipped.", session.line))
        return None
    if not _valid_hostname(host):
        issues.append(ImportIssue(Severity.WARNING, IssueCode.INVALID_HOSTNAME,
                                  f"'{label}' has an invalid host name; skipped.", session.line))
        return None
    port = _parse_port(port_text)
    if port is None:
        issues.append(ImportIssue(Severity.WARNING, IssueCode.INVALID_PORT,
                                  f"'{label}' has no valid port; using 22.", session.line))
        port = 22
    user_raw = session.field(2)
    user_key, reference = normalize_username(user_raw)
    if user_key and (user_key.startswith("-") or any(ord(ch) < 32 or ord(ch) == 127 for ch in user_key)):
        issues.append(ImportIssue(Severity.WARNING, IssueCode.INVALID_FIELD,
                                  f"'{label}' has an unusable username; it is treated as empty.", session.line))
        user_key, reference = "", False
    proxy = _proxy_jump(session, label, issues)
    identity = _identity_file(session, label, issues)
    return SshTarget(host, port, user_raw, user_key, reference, proxy, identity)


def _proxy_jump(session: MxtSession, label: str, issues: List[ImportIssue]) -> str:
    gateway = session.field(7).strip()
    if not gateway:
        return ""  # f8 / f9 mean nothing without a gateway host
    if gateway.startswith("[") and gateway.endswith("]"):
        gateway = gateway[1:-1]
    port_text = session.field(8).strip()
    port = _parse_port(port_text) if port_text else 22
    if not _valid_hostname(gateway) or port is None:
        issues.append(ImportIssue(Severity.WARNING, IssueCode.INVALID_GATEWAY,
                                  f"'{label}' has an invalid jump host; imported without it.", session.line))
        return ""
    user, reference = normalize_username(session.field(9))
    if reference:
        issues.append(ImportIssue(Severity.INFO, IssueCode.INVALID_GATEWAY,
                                  f"'{label}': the jump-host user was a MobaXterm credential label; "
                                  "used as the login name.", session.line))
    if user and (user.startswith("-") or not _USER_OK.match(user)):
        issues.append(ImportIssue(Severity.WARNING, IssueCode.INVALID_GATEWAY,
                                  f"'{label}': the jump-host user is invalid; left out.", session.line))
        user = ""
    host_text = f"[{gateway}]" if ":" in gateway else gateway
    target = f"{user}@{host_text}" if user else host_text
    return target if port == 22 else f"{target}:{port}"


def _identity_file(session: MxtSession, label: str, issues: List[ImportIssue]) -> str:
    """Best effort: a field that clearly is a key file path (never guessed otherwise)."""

    for index, value in enumerate(session.fields):
        if index in (0, 2, 7, 9):
            continue
        text = value.strip()
        if not text or not _KEY_FILE.search(text):
            continue
        if any(ch.isspace() or ord(ch) < 32 for ch in text):
            issues.append(ImportIssue(Severity.WARNING, IssueCode.WINDOWS_KEY_PATH,
                                      f"'{label}': the private key path contains spaces; please select the key "
                                      "again.", session.line))
            return ""
        if _WINDOWS_PATH.search(text):
            issues.append(ImportIssue(Severity.WARNING, IssueCode.WINDOWS_KEY_PATH,
                                      f"'{label}': the private key path is a Windows path; please select the key "
                                      "again.", session.line))
        return text
    return ""


# ------------------------------------------------------------------ planning
class Destination(str, Enum):
    NEW_PARENTS = "new_parents"  # one parent group per top-level folder
    EXISTING_PARENT = "existing_parent"  # under one existing parent; the top folder becomes a child prefix


class ConflictPolicy(str, Enum):
    SKIP = "skip"
    RENAME = "rename"
    OVERWRITE = "overwrite"


@dataclass(frozen=True)
class ImportOptions:
    destination: Destination = Destination.NEW_PARENTS
    target_parent: str = ""
    conflict_policy: ConflictPolicy = ConflictPolicy.SKIP
    create_empty_folders: bool = False
    skip_duplicates: bool = True


@dataclass
class ExistingInventory:
    """What is already on disk under ``base_dir`` (captured when planning)."""

    base_dir: Path
    tree: Mapping[str, Mapping[str, GroupConfig]] = field(default_factory=dict)
    disk: bool = True  # False: plan without looking at the disk (tests, previews of a bare file)

    @classmethod
    def from_manager(cls, manager) -> "ExistingInventory":
        return cls(Path(manager.base_dir), manager.tree)

    def path(self, parent: str, child: Sequence[str] = ()) -> Path:
        return self.base_dir.joinpath(parent, *child)

    def exists(self, parent: str, child: Sequence[str] = ()) -> bool:
        return self.disk and self.path(parent, child).exists()

    def is_file(self, parent: str, child: Sequence[str] = ()) -> bool:
        return self.disk and self.path(parent, child).is_file()

    def is_dir(self, parent: str, child: Sequence[str] = ()) -> bool:
        return self.disk and self.path(parent, child).is_dir()


@dataclass
class FolderNode:
    """A folder of the export for the preview tree (counts include descendants)."""

    path: Tuple[str, ...]
    label: str
    own_hosts: int = 0
    total_hosts: int = 0
    children: List["FolderNode"] = field(default_factory=list)


@dataclass
class PlannedHost:
    session: MxtSession
    target: SshTarget
    folder: Tuple[str, ...]
    parent: str
    child: str
    alias: str
    original_name: str

    @property
    def host_key(self) -> str:
        return f"{self.parent}::{self.child}::{self.alias}"


@dataclass
class PlannedGroup:
    parent: str
    child: str
    folder: Tuple[str, ...]
    hosts: List[PlannedHost] = field(default_factory=list)


@dataclass
class UsernameEntry:
    key: str  # normalized username ("" = no username)
    count: int = 0
    raw_forms: Set[str] = field(default_factory=set)
    references: int = 0  # how many were [bracketed] credential labels
    literals: int = 0
    case_variants: List[str] = field(default_factory=list)


@dataclass
class ImportPlan:
    options: ImportOptions
    groups: List[PlannedGroup]
    hosts: List[PlannedHost]
    issues: List[ImportIssue]
    usernames: List[UsernameEntry]
    folder_tree: List[FolderNode]
    skipped_types: Dict[int, int]
    renamed_folders: List[Tuple[str, str]]  # (original folder path, parent/child target)
    existing: ExistingInventory
    excluded_hosts: int = 0

    @property
    def parents(self) -> List[str]:
        seen: List[str] = []
        for group in self.groups:
            if group.parent not in seen:
                seen.append(group.parent)
        return seen


def build_folder_tree(result: MxtParseResult) -> List[FolderNode]:
    """Preview tree of every folder (implicit ancestors included) with SSH host counts."""

    nodes: Dict[Tuple[str, ...], FolderNode] = {}
    roots: List[FolderNode] = []

    def node(path: Tuple[str, ...]) -> FolderNode:
        found = nodes.get(path)
        if found is None:
            found = nodes[path] = FolderNode(path, clean_label(path[-1]) or "?")
            if len(path) == 1:
                roots.append(found)
            else:
                node(path[:-1]).children.append(found)
        return found

    for folder in result.folders:
        node(folder.path)
    for session in result.sessions:
        if session.type_code != SSH_TYPE or not session.folder:
            continue
        own = node(session.folder)
        own.own_hosts += 1
        for depth in range(1, len(session.folder) + 1):
            nodes[session.folder[:depth]].total_hosts += 1
    return roots


def _excluded(path: Tuple[str, ...], excluded: Set[Tuple[str, ...]]) -> bool:
    return any(path[:depth] in excluded for depth in range(1, len(path) + 1))


def plan_import(
    result: MxtParseResult,
    options: ImportOptions = ImportOptions(),
    excluded_folders: Iterable[Tuple[str, ...]] = (),
    existing: Optional[ExistingInventory] = None,
) -> ImportPlan:
    """Map folders → parent/child groups and SSH sessions → hosts (no credentials yet).

    Deterministic: the same input always gives the same names (section order,
    then line order). ``excluded_folders`` drops those folders and their
    descendants (the wizard's unticked branches).
    """

    existing = existing or ExistingInventory(Path("."), disk=False)
    excluded = {tuple(path) for path in excluded_folders}
    issues: List[ImportIssue] = list(result.problems)
    skipped_types: Dict[int, int] = {}
    targets: List[Tuple[MxtSession, SshTarget]] = []
    excluded_hosts = 0
    for session in result.sessions:
        if session.type_code != SSH_TYPE:
            skipped_types[session.type_code] = skipped_types.get(session.type_code, 0) + 1
            continue
        if _excluded(session.folder, excluded):
            excluded_hosts += 1
            continue
        target = ssh_target(session, issues)
        if target is not None:
            targets.append((session, target))
    for type_code, count in sorted(skipped_types.items()):
        name = TYPE_NAMES.get(type_code, f"type {type_code}")
        issues.append(ImportIssue(Severity.INFO, IssueCode.UNSUPPORTED_TYPE,
                                  f"{count} {name} session(s) skipped (only SSH is imported)."))

    # Which folders are needed: those with hosts (or every selected one with the empty-folder option).
    host_folders: Dict[Tuple[str, ...], List[Tuple[MxtSession, SshTarget]]] = {}
    for session, target in targets:
        host_folders.setdefault(session.folder, []).append((session, target))
    order: Dict[Tuple[str, ...], int] = {}
    for index, folder in enumerate(result.folders):
        order.setdefault(folder.path, index)
    for session, _target in targets:
        order.setdefault(session.folder, len(order) + 10**6)
    needed: Set[Tuple[str, ...]] = set(host_folders)
    if options.create_empty_folders:
        needed |= {f.path for f in result.folders if not _excluded(f.path, excluded)}
    with_descendants: Set[Tuple[str, ...]] = set()
    for path in needed:
        for depth in range(1, len(path)):
            with_descendants.add(path[:depth])

    # Sanitize every needed path top-down; siblings stay unique (case-insensitive).
    seg_map: Dict[Tuple[str, ...], str] = {}
    siblings: Dict[Tuple[str, ...], Set[str]] = {}  # sanitized parent path → names taken
    unnamed = [0]
    renamed: List[Tuple[str, str]] = []
    reserved = {OWN_HOSTS_CHILD_NAME.lower()}

    def mapped(path: Tuple[str, ...]) -> Tuple[str, ...]:
        out: List[str] = []
        for depth in range(1, len(path) + 1):
            prefix = path[:depth]
            if prefix not in seg_map:
                name, codes = sanitize_name(prefix[-1])
                if not name:
                    unnamed[0] += 1
                    name = f"unnamed-{unnamed[0]}"
                parent_key = tuple(out)
                taken = siblings.setdefault(parent_key, set(reserved) if depth > 1 else set())
                unique = _unique(name, taken)
                if unique != name:
                    codes = codes + [IssueCode.FOLDER_COLLISION_RESOLVED]
                taken.add(unique.lower())
                seg_map[prefix] = unique
                for code in codes:
                    issues.append(ImportIssue(Severity.INFO, code,
                                              f"Folder '{_folder_text(prefix)}' is stored as '{'/'.join(out + [unique])}'."))
            out.append(seg_map[prefix])
        return tuple(out)

    for path in sorted(needed, key=lambda p: (order.get(p, 10**9), p)):
        mapped(path)  # every sibling name is known before any target is chosen

    def group_target(path: Tuple[str, ...]) -> Tuple[str, List[str]]:
        segs = list(mapped(path))
        if options.destination is Destination.EXISTING_PARENT and options.target_parent:
            parent, child = options.target_parent, segs
        else:
            parent, child = segs[0], segs[1:]
        if not child:
            taken = siblings.get((segs[0],), set()) if options.destination is Destination.NEW_PARENTS else set()
            child = [OWN_HOSTS_CHILD_NAME if ROOT_CHILD_NAME.lower() in taken else ROOT_CHILD_NAME]
        elif path in with_descendants:
            child = child + [OWN_HOSTS_CHILD_NAME]
        return parent, child

    # Resolve clashes with what already exists on disk (a file where a folder is needed, …).
    def resolve_disk(parent: str, child: List[str], folder: Tuple[str, ...]) -> Tuple[str, List[str]]:
        if existing.exists(parent) and not existing.is_dir(parent):
            new_parent = _unique(parent, {parent.lower()})
            issues.append(ImportIssue(Severity.WARNING, IssueCode.FOLDER_COLLISION_RESOLVED,
                                      f"'{parent}' exists and is not a folder; using '{new_parent}'."))
            parent = new_parent
        for depth in range(1, len(child)):
            prefix = child[:depth]
            if existing.is_file(parent, prefix):
                renamed_seg = _unique(prefix[-1], {prefix[-1].lower()})
                issues.append(ImportIssue(Severity.WARNING, IssueCode.FOLDER_COLLISION_RESOLVED,
                                          f"'{parent}/{'/'.join(prefix)}' is an existing group file; "
                                          f"the folder is stored as '{renamed_seg}'."))
                child = prefix[:-1] + [renamed_seg] + child[depth:]
        if existing.is_dir(parent, child):
            issues.append(ImportIssue(Severity.INFO, IssueCode.FOLDER_COLLISION_RESOLVED,
                                      f"'{parent}/{'/'.join(child)}' is an existing folder; "
                                      f"its hosts go to '{OWN_HOSTS_CHILD_NAME}'."))
            child = child + [OWN_HOSTS_CHILD_NAME]
        return parent, child

    groups: Dict[Tuple[str, str], PlannedGroup] = {}
    folder_target: Dict[Tuple[str, ...], Tuple[str, str]] = {}
    for path in sorted(needed, key=lambda p: (order.get(p, 10**9), p)):
        if not path:
            continue
        parent, child = resolve_disk(*group_target(path), path)
        key = folder_target[path] = (parent, "/".join(child))
        group = groups.get(key)
        if group is None:
            group = groups[key] = PlannedGroup(parent, key[1], path)
            original = _folder_text(path)
            target_text = f"{parent}/{key[1]}"
            if original.replace("\\", "/") != target_text:
                renamed.append((original, target_text))

    # Hosts: sanitized, unique per group (case-insensitive), deterministic order.
    hosts: List[PlannedHost] = []
    taken_aliases: Dict[Tuple[str, str], Set[str]] = {}
    for session, target in sorted(targets, key=lambda item: item[0].line):
        key = folder_target[session.folder]
        group = groups[key]
        name, codes = sanitize_name(session.name)
        if not name:
            unnamed[0] += 1
            name = f"unnamed-{unnamed[0]}"
        taken = taken_aliases.setdefault(key, set())
        alias = _unique(name, taken)
        taken.add(alias.lower())
        label = _safe_label(session.name)
        if alias != name:
            codes = codes + [IssueCode.ALIAS_DEDUPLICATED]
        for code in codes:
            issues.append(ImportIssue(Severity.INFO, code, f"Session '{label}' is stored as '{alias}'.", session.line))
        planned = PlannedHost(session, target, session.folder, key[0], key[1], alias, clean_label(session.name))
        group.hosts.append(planned)
        hosts.append(planned)

    usernames = discover_usernames(hosts)
    return ImportPlan(options, list(groups.values()), hosts, issues, usernames, build_folder_tree(result),
                      skipped_types, renamed, existing, excluded_hosts)


def discover_usernames(hosts: Sequence[PlannedHost]) -> List[UsernameEntry]:
    """Distinct usernames of the hosts to import (bracket + plain forms merged), most used first."""

    entries: Dict[str, UsernameEntry] = {}
    for host in hosts:
        key = host.target.user_key
        entry = entries.setdefault(key, UsernameEntry(key))
        entry.count += 1
        if key:
            entry.raw_forms.add(host.target.user_raw.strip())
        if host.target.user_is_reference:
            entry.references += 1
        elif key:
            entry.literals += 1
    lowered: Dict[str, List[str]] = {}
    for key in entries:
        if key:
            lowered.setdefault(key.lower(), []).append(key)
    for variants in lowered.values():
        if len(variants) > 1:
            for key in variants:
                entries[key].case_variants = sorted(v for v in variants if v != key)
    return sorted(entries.values(), key=lambda e: (-e.count, e.key == NO_USERNAME, e.key.lower(), e.key))


# ------------------------------------------------------- credential mapping
class CredentialMissingError(Exception):
    """A credential chosen in the wizard no longer exists."""


@dataclass
class GroupWrite:
    parent: str
    child: str
    group: GroupConfig
    existed: bool
    new_hosts: int = 0
    overwritten_hosts: int = 0


@dataclass(frozen=True)
class ImportedHost:
    planned: PlannedHost
    alias: str  # final alias (the Rename policy may change it)

    @property
    def host_key(self) -> str:
        return f"{self.planned.parent}::{self.planned.child}::{self.alias}"


@dataclass
class ImportJob:
    plan: ImportPlan
    writes: List[GroupWrite]
    bindings: Dict[str, Optional[str]]  # host_key → credential id (None = unbind, for overwritten hosts)
    expected: Dict[Path, Optional[bytes]]  # target file → bytes when planned (None = absent)
    issues: List[ImportIssue]
    imported: List[ImportedHost]
    skipped_existing: int = 0
    skipped_duplicates: int = 0
    overwritten: int = 0
    mapping: Dict[str, Optional[str]] = field(default_factory=dict)
    credential_names: Dict[str, str] = field(default_factory=dict)

    @property
    def host_count(self) -> int:
        return len(self.imported)


def _host_user(host: PlannedHost, cred_id: Optional[str], credentials: Mapping[str, object]) -> Optional[str]:
    if cred_id:
        cred = credentials.get(cred_id)
        if cred is None:
            raise CredentialMissingError(cred_id)
        return getattr(cred, "username", None) or None
    if host.target.user_key and not host.target.user_is_reference:
        return host.target.user_key  # a literal username is real data
    return None  # a [label] is only MobaXterm's credential name


def host_entry(host: PlannedHost, user: Optional[str], base_dir: Path) -> HostEntry:
    options: Dict[str, str] = {"hostname": host.target.hostname, "port": str(host.target.port)}
    if user:
        options["user"] = user
    if host.target.proxy_jump:
        options["proxyjump"] = host.target.proxy_jump
    if host.target.identity_file:
        options["identityfile"] = host.target.identity_file
    metadata: Dict[str, object] = {
        "display_name": host.original_name or host.alias,
        "imported_from": "mobaxterm",
        "mobaxterm_folder": _folder_text(host.folder),
    }
    return HostEntry(alias=host.alias, options=options, metadata=metadata,
                     source_file=base_dir.joinpath(host.parent, *host.child.split("/")),
                     parent_group=host.parent, child_group=host.child)


def _identity(entry: HostEntry) -> Tuple[str, str, str]:
    return ((entry.option("hostname", "host") or "").lower(), entry.option("port") or "22", entry.option("user") or "")


def apply_credential_mapping(
    plan: ImportPlan,
    mapping: Mapping[str, Optional[str]],
    credentials: Mapping[str, object],
    *,
    default: Optional[str] = None,
) -> ImportJob:
    """Final hosts (``User`` from the chosen credential) and what to write/bind.

    ``mapping`` maps a username entry key (``""`` = no username) to a credential
    id or ``None``; keys missing from it use ``default``. Applies the conflict
    policy and the duplicate option against the existing inventory.
    """

    base_dir = plan.existing.base_dir
    issues: List[ImportIssue] = []
    resolved = {entry.key: mapping.get(entry.key, default) for entry in plan.usernames}
    for cred_id in resolved.values():
        if cred_id and cred_id not in credentials:
            raise CredentialMissingError(cred_id)
    writes: List[GroupWrite] = []
    bindings: Dict[str, Optional[str]] = {}
    expected: Dict[Path, Optional[bytes]] = {}
    imported: List[ImportedHost] = []
    skipped_existing = skipped_duplicates = overwritten = 0
    policy = plan.options.conflict_policy

    for group in plan.groups:
        path = base_dir.joinpath(group.parent, *group.child.split("/"))
        existing_group = plan.existing.tree.get(group.parent, {}).get(group.child)
        existed = path.is_file()
        current: List[HostEntry] = [
            HostEntry(h.alias, dict(h.options), dict(h.metadata), h.source_file, h.parent_group, h.child_group)
            for h in (existing_group.hosts if existing_group is not None else [])
        ]
        by_alias = {h.alias.lower(): index for index, h in enumerate(current)}
        identities = {_identity(h) for h in current}
        write = GroupWrite(group.parent, group.child, GroupConfig(group.parent, group.child, path, current), existed)
        for host in group.hosts:
            cred_id = resolved.get(host.target.user_key)
            entry = host_entry(host, _host_user(host, cred_id, credentials), base_dir)
            label = _safe_label(host.original_name)
            if plan.options.skip_duplicates and _identity(entry) in identities:
                skipped_duplicates += 1
                issues.append(ImportIssue(Severity.INFO, IssueCode.DUPLICATE_EXISTING_HOST,
                                          f"'{label}': the same host name, port and user is already in "
                                          f"'{group.parent}/{group.child}'; skipped.", host.session.line))
                continue
            clash = by_alias.get(entry.alias.lower())
            if clash is not None:
                if policy is ConflictPolicy.SKIP:
                    skipped_existing += 1
                    issues.append(ImportIssue(Severity.INFO, IssueCode.EXISTING_HOST_SKIPPED,
                                              f"'{entry.alias}' already exists in '{group.parent}/{group.child}'; "
                                              "skipped.", host.session.line))
                    continue
                if policy is ConflictPolicy.RENAME:
                    new_alias = _unique(entry.alias, set(by_alias))
                    issues.append(ImportIssue(Severity.INFO, IssueCode.EXISTING_HOST_RENAMED,
                                              f"'{entry.alias}' already exists; imported as '{new_alias}'.",
                                              host.session.line))
                    entry.alias = new_alias
                    current.append(entry)
                    by_alias[new_alias.lower()] = len(current) - 1
                    write.new_hosts += 1
                else:
                    current[clash] = entry
                    overwritten += 1
                    write.overwritten_hosts += 1
                    issues.append(ImportIssue(Severity.WARNING, IssueCode.EXISTING_HOST_OVERWRITTEN,
                                              f"'{entry.alias}' in '{group.parent}/{group.child}' is replaced.",
                                              host.session.line))
            else:
                current.append(entry)
                by_alias[entry.alias.lower()] = len(current) - 1
                write.new_hosts += 1
            identities.add(_identity(entry))
            final = ImportedHost(host, entry.alias)
            imported.append(final)
            overwrote = clash is not None and policy is ConflictPolicy.OVERWRITE
            if cred_id or overwrote:  # an overwritten host drops its old binding when mapped to None
                bindings[final.host_key] = cred_id
        current.sort(key=lambda h: h.alias.lower())
        changed = write.new_hosts or write.overwritten_hosts or (not existed and plan.options.create_empty_folders)
        if changed:
            writes.append(write)
            try:
                expected[path] = path.read_bytes() if existed else None
            except OSError:
                expected[path] = None
    names = {cid: getattr(credentials[cid], "name", cid) for cid in resolved.values() if cid}
    return ImportJob(plan, writes, bindings, expected, issues, imported, skipped_existing, skipped_duplicates,
                     overwritten, dict(resolved), names)


# --------------------------------------------------------------------- commit
class ImportStatus(str, Enum):
    SUCCESS = "success"
    NOTHING = "nothing"  # every host already existed / was skipped
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class ImportReport:
    status: ImportStatus
    message: str
    hosts: int = 0
    groups: int = 0
    parents: List[str] = field(default_factory=list)
    log_path: Optional[Path] = None


def commit(
    job: ImportJob,
    manager,
    credential_store,
    *,
    progress: Optional[Callable[[int, int], None]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
) -> ImportReport:
    """Write the job (one bulk write, one binding save, one refresh) or change nothing.

    Credential ids are re-checked right before writing. Any failure — a write
    error, a target changed on disk, a credential deleted meanwhile, a failed
    binding save, a cancel — rolls back every file and binding.
    """

    parents = []
    for write in job.writes:
        if write.parent not in parents:
            parents.append(write.parent)
    if not job.writes:
        return ImportReport(ImportStatus.NOTHING, "Nothing new to import: every host already exists.")
    missing = sorted({cid for cid in job.bindings.values() if cid and credential_store.get(cid) is None})
    if missing:
        return ImportReport(ImportStatus.FAILED,
                            "Import failed, no changes were made: a chosen credential was deleted meanwhile.")
    try:
        txn = manager.import_groups([w.group for w in job.writes], expected=job.expected,
                                    progress=progress, should_cancel=should_cancel)
    except ImportCancelled:
        manager.refresh()
        return ImportReport(ImportStatus.CANCELLED, "Import cancelled, no changes were made.")
    except ImportConflictError as exc:
        return ImportReport(ImportStatus.FAILED, f"Import failed, no changes were made: {exc}")
    except ImportWriteError as exc:
        manager.refresh()
        return ImportReport(ImportStatus.FAILED, f"Import failed, no changes were made: {exc}")
    try:
        if should_cancel is not None and should_cancel():
            raise ImportCancelled("Import cancelled.")
        credential_store.bind_many(job.bindings)
    except ImportCancelled:
        txn.rollback()
        manager.refresh()
        return ImportReport(ImportStatus.CANCELLED, "Import cancelled, no changes were made.")
    except Exception as exc:  # SecureStoreError, KeyError (credential gone) …
        problems = txn.rollback()
        manager.refresh()
        detail = " Some files could not be restored." if problems else ""
        return ImportReport(ImportStatus.FAILED,
                            f"Import failed, no changes were made: credential bindings could not be saved "
                            f"({type(exc).__name__}).{detail}")
    manager.refresh()
    return ImportReport(ImportStatus.SUCCESS, f"Imported {job.host_count} host(s) into {len(job.writes)} group(s).",
                        job.host_count, len(job.writes), parents)


# --------------------------------------------------------------------- report
def render_report(plan: ImportPlan, job: Optional[ImportJob] = None, report: Optional[ImportReport] = None,
                  *, source_name: str = "") -> str:
    """Plain-text import report: counts, names, issue codes — no secrets, no raw session lines."""

    lines = ["SSH Manager — MobaXterm import report", time.strftime("%Y-%m-%d %H:%M:%S")]
    if source_name:
        lines.append(f"File: {source_name}")
    if report is not None:
        lines += ["", f"Result: {report.status.value} — {report.message}"]
    lines += ["", "Summary",
              f"  Parent groups: {len(plan.parents)}",
              f"  Child groups: {len(plan.groups)}",
              f"  SSH hosts planned: {len(plan.hosts)}"]
    if job is not None:
        lines += [f"  Hosts to import: {job.host_count}",
                  f"  Skipped (already exist): {job.skipped_existing}",
                  f"  Skipped (same host, port and user): {job.skipped_duplicates}",
                  f"  Overwritten: {job.overwritten}"]
    if plan.excluded_hosts:
        lines.append(f"  Excluded by unticked folders: {plan.excluded_hosts}")
    for type_code, count in sorted(plan.skipped_types.items()):
        lines.append(f"  Skipped {TYPE_NAMES.get(type_code, f'type {type_code}')}: {count}")
    counts: Dict[str, int] = {}
    all_issues = plan.issues + (job.issues if job is not None else [])
    for issue in all_issues:
        counts[issue.code.value] = counts.get(issue.code.value, 0) + 1
    if counts:
        lines += ["", "Issues by code"] + [f"  {code}: {count}" for code, count in sorted(counts.items())]
    if job is not None and plan.usernames:
        lines += ["", "Credential mapping (username in file → credential)"]
        for entry in plan.usernames:
            cred_id = job.mapping.get(entry.key)
            target = job.credential_names.get(cred_id, cred_id) if cred_id else "None"
            label = entry.key or "(no username)"
            lines.append(f"  {label} ({entry.count} host(s)) → {target}")
    if plan.renamed_folders:
        lines += ["", "Folders stored under a different name (original → group)"]
        lines += [f"  {original} → {target}" for original, target in plan.renamed_folders]
    if all_issues:
        lines += ["", "Details"]
        for issue in all_issues:
            where = f"line {issue.line}: " if issue.line else ""
            lines.append(f"  [{issue.severity.value}] {issue.code.value}: {where}{issue.message}")
    return "\n".join(lines) + "\n"


def save_report(text: str, directory: Path) -> Path:
    """Write the report as ``mobaxterm-<timestamp>.log`` (mode 0600) and return its path."""

    from ..persistence import write_text_atomic

    directory.mkdir(parents=True, exist_ok=True)
    path = directory / time.strftime("mobaxterm-%Y%m%d-%H%M%S.log")
    counter = 2
    while path.exists():
        path = directory / time.strftime(f"mobaxterm-%Y%m%d-%H%M%S-{counter}.log")
        counter += 1
    write_text_atomic(path, text, mode=0o600)
    return path
