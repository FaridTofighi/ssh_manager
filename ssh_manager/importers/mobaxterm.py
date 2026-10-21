"""Import MobaXterm session exports (``*.mxtsessions``) into the app inventory — pure Python, no GTK.

Pipeline (each step is a pure function except :func:`commit`):

1. :func:`parse_mxtsessions` — bytes/path → :class:`MxtParseResult` (folders,
   sessions, problems). Hand-rolled, line-based, O(n); never raises on bad
   lines (fatal file-level problems raise :class:`MxtFileError`).
2. :func:`plan_import` — every MobaXterm folder becomes a group 1:1 (any
   depth; a group holds hosts and subgroups), SSH sessions become hosts; names
   are kept verbatim; plus the usernames found.
3. :func:`apply_credential_mapping` — the user's username → credential choices
   and the conflict policy against the existing inventory → :class:`ImportJob`.
4. :func:`commit` — **one** ``InventoryStore.transaction()``: groups
   (``create_group_path``), hosts (``add_hosts_bulk``), updates, and
   ``CredentialStore.bind_many`` by the new host ids; one refresh. Any error or
   a cancel rolls the transaction back: nothing changes. Nothing is written to
   disk outside the database.

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
import re
import time
import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple, Union

from ..config import HostEntry

# ----------------------------------------------------------------- constants
MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_LINE_CHARS = 64 * 1024
MAX_DEPTH = 32
MAX_NAME = 128  # the store's limit for group names and host aliases
UNNAMED = "Unnamed"
SLASH_REPLACEMENT = "\u2215"  # DIVISION SLASH: '/' separates group paths in the store
EMPTY_SUBREP_PARENT = "MobaXterm"  # folder for sessions of a section with an empty SubRep
NO_USERNAME = ""  # username-entry key for sessions without a username
SSH_TYPE = 0
RDP_TYPE = 4
FTP_TYPE = 6
SFTP_TYPE = 7
TRANSFER_TYPES = {FTP_TYPE: "ftp", SFTP_TYPE: "sftp"}  # MobaXterm type → protocol id
OTHER_TYPES = {RDP_TYPE: "rdp", **TRANSFER_TYPES}
DEFAULT_PORTS = {"ssh": 22, "sftp": 22, "ftp": 21, "rdp": 3389}
RDP_NOTE = ("RDP display and redirection options (resolution, colors, clipboard, drives …) were not imported: "
            "their positions in the MobaXterm export are not documented. The defaults are used.")
TYPE_NAMES = {
    0: "SSH", 1: "Telnet", 2: "Rsh", 3: "Xdmcp", 4: "RDP", 5: "VNC", 6: "FTP", 7: "SFTP",
    8: "Serial", 9: "Mosh", 10: "AWS S3", 11: "WSL", 12: "Shell",
}
_SESSION_VALUE = re.compile(r"^\s*#(\d+)#(\d+)%(.*)$", re.DOTALL)
_SECTION = re.compile(r"^\s*\[(.*)\]\s*$")
_BOOKMARKS = re.compile(r"^bookmarks(_\d+)?$", re.IGNORECASE)
_LINE_BREAKS = re.compile(r"[\r\n\u0085\u2028\u2029]+")
_BIDI_OVERRIDES = set("\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")
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
    INVALID_FIELD = "invalid_field"
    WINDOWS_KEY_PATH = "windows_key_path"
    SLASH_REPLACED = "slash_replaced"
    NAME_TRUNCATED = "name_truncated"
    NAME_EMPTY = "name_empty"
    NAME_DEDUPLICATED = "name_deduplicated"
    EXISTING_HOST_SKIPPED = "existing_host_skipped"
    EXISTING_HOST_RENAMED = "existing_host_renamed"
    EXISTING_HOST_OVERWRITTEN = "existing_host_overwritten"
    DESTINATION_MISSING = "destination_missing"
    ORPHAN_SESSION = "orphan_session"
    DUPLICATE_FOLDER_SECTION = "duplicate_folder_section"
    EMPTY_SUBREP = "empty_subrep"
    FOLDER_TOO_DEEP = "folder_too_deep"
    LINE_TOO_LONG = "line_too_long"
    FTPS_UNKNOWN = "ftps_unknown"


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


# ---------------------------------------------------------------------- names
def clean_label(text: str) -> str:
    """Original name for issue messages: no control, format (bidi, zero-width) chars or line breaks."""

    kept = "".join(" " if ch in "\t\r\n" else ch for ch in unicodedata.normalize("NFC", text or "")
                   if unicodedata.category(ch) not in ("Cc", "Cf") or ch in "\t\r\n")
    return " ".join(kept.split())


def _safe_label(text: str, limit: int = 80) -> str:
    label = clean_label(text)
    return label if len(label) <= limit else label[: limit - 1] + "…"


def _folder_text(path: Sequence[str]) -> str:
    return "\\".join(clean_label(part) for part in path)


def verbatim_name(original: str) -> Tuple[str, List["IssueCode"]]:
    """The store name for a MobaXterm folder/session name, kept as written.

    NFC; line breaks (and tabs) become one space; control and bidi-override
    characters are dropped; trimmed. Spaces, ``+``, parentheses, Persian text
    and zero-width joiners stay. ``/`` becomes ``∕`` (U+2215) and names longer
    than 128 characters are cut — both reported. ``""`` means "unnamed" (the
    caller numbers it).
    """

    text = _LINE_BREAKS.sub(" ", unicodedata.normalize("NFC", original or "")).replace("\t", " ")
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Cc" and ch not in _BIDI_OVERRIDES).strip()
    issues: List[IssueCode] = []
    if "/" in text:
        text = text.replace("/", SLASH_REPLACEMENT)
        issues.append(IssueCode.SLASH_REPLACED)
    if len(text) > MAX_NAME:
        text = text[:MAX_NAME].rstrip()
        issues.append(IssueCode.NAME_TRUNCATED)
    return text, issues


def unique_name(name: str, taken: Set[str]) -> str:
    """``name``, else ``name (2)``, ``name (3)``… unique case-insensitively against ``taken`` (≤ 128 chars)."""

    if name.casefold() not in taken:
        return name
    counter = 2
    while True:
        suffix = f" ({counter})"
        candidate = name[: MAX_NAME - len(suffix)].rstrip() + suffix
        if candidate.casefold() not in taken:
            return candidate
        counter += 1


class _Namer:
    """Names in one scope (a group's subgroups, or its hosts): verbatim, numbered when empty, unique."""

    def __init__(self, taken: Iterable[str] = ()) -> None:
        self.taken: Set[str] = {name.casefold() for name in taken}
        self.unnamed = 0

    def name(self, original: str) -> Tuple[str, List["IssueCode"]]:
        text, issues = verbatim_name(original)
        if not text:
            self.unnamed += 1
            text = UNNAMED if self.unnamed == 1 else f"{UNNAMED} {self.unnamed}"
            issues.append(IssueCode.NAME_EMPTY)
        unique = unique_name(text, self.taken)
        if unique != text:
            issues.append(IssueCode.NAME_DEDUPLICATED)
        self.taken.add(unique.casefold())
        return unique, issues


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
    protocol: str = "ssh"  # ssh, or ftp / sftp / rdp for MobaXterm FTP (6), SFTP (7) and RDP (4) sessions
    domain: str = ""  # RDP: the DOMAIN of a literal DOMAIN\user in the file

    @property
    def anonymous(self) -> bool:
        """FTP without a user, or the user ``anonymous``."""

        return self.protocol == "ftp" and self.user_key.casefold() in ("", "anonymous")


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
    """The SSH fields of a type-0 session (``None`` when it cannot be imported; issues appended).

    FTP (6) and SFTP (7) sessions use the same first three fields (host, port,
    user); their other fields are not verified, so nothing else is read from
    them. Whether an FTP server wants FTPS is unknown: it is imported as plain
    FTP with a warning to check.
    """

    protocol = OTHER_TYPES.get(session.type_code, "ssh")
    default_port = DEFAULT_PORTS[protocol]
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
                                  f"'{label}' has no valid port; using {default_port}.", session.line))
        port = default_port
    user_raw = session.field(2)
    user_key, reference = normalize_username(user_raw)
    domain = ""
    if protocol == "rdp" and not reference and "\\" in user_key:  # a literal DOMAIN\user (not a label)
        domain, _sep, user_key = user_key.partition("\\")
        domain, user_key = domain.strip(), user_key.strip()
        if "\\" in user_key or not domain:
            issues.append(ImportIssue(Severity.WARNING, IssueCode.INVALID_FIELD,
                                      f"'{label}' has an unusable DOMAIN\\user; it is treated as empty.",
                                      session.line))
            domain, user_key = "", ""
    if user_key and (user_key.startswith("-") or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127
                                                     for ch in user_key)):
        issues.append(ImportIssue(Severity.WARNING, IssueCode.INVALID_FIELD,
                                  f"'{label}' has an unusable username; it is treated as empty.", session.line))
        user_key, reference = "", False
    if protocol != "ssh":
        if protocol == "ftp":
            issues.append(ImportIssue(Severity.WARNING, IssueCode.FTPS_UNKNOWN,
                                      f"'{label}' is imported as plain FTP; check whether this server uses FTPS.",
                                      session.line))
        return SshTarget(host, port, user_raw, user_key, reference, "", "", protocol, domain)
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
    NEW_TOP_LEVEL = "new_top_level"  # every top-level MobaXterm folder becomes a top-level group
    EXISTING_GROUP = "existing_group"  # the whole MobaXterm tree goes beneath one existing group


class ConflictPolicy(str, Enum):
    SKIP = "skip"
    RENAME = "rename"
    OVERWRITE = "overwrite"


@dataclass(frozen=True)
class ImportOptions:
    destination: Destination = Destination.NEW_TOP_LEVEL
    target_group: Tuple[str, ...] = ()  # EXISTING_GROUP: the group path (as shown in the tree)
    conflict_policy: ConflictPolicy = ConflictPolicy.SKIP
    skip_empty_folders: bool = False  # empty MobaXterm folders are created by default
    include_transfer: bool = True  # FTP (6) / SFTP (7) sessions become ftp / sftp hosts
    include_rdp: bool = True  # RDP (4) sessions become rdp hosts


@dataclass(frozen=True)
class ExistingHost:
    id: str
    alias: str
    hostname: str
    port: Optional[int]
    user: str
    protocol: str = "ssh"


def _cf(path: Sequence[str]) -> Tuple[str, ...]:
    return tuple(unicodedata.normalize("NFC", part).strip().casefold() for part in path)


@dataclass
class ExistingInventory:
    """The store's groups and hosts when the import was planned (paths compared case-insensitively)."""

    group_ids: Dict[Tuple[str, ...], str] = field(default_factory=dict)  # folded path → group id
    paths: Dict[Tuple[str, ...], Tuple[str, ...]] = field(default_factory=dict)  # folded path → stored names
    hosts: Dict[str, List[ExistingHost]] = field(default_factory=dict)  # group id → hosts

    @classmethod
    def from_store(cls, store) -> "ExistingInventory":
        groups = {g.id: g for g in store.list_groups()}
        cache: Dict[str, Tuple[str, ...]] = {}

        def path_of(group_id: str) -> Tuple[str, ...]:
            if group_id not in cache:
                group = groups[group_id]
                cache[group_id] = (path_of(group.parent_id) if group.parent_id in groups else ()) + (group.name,)
            return cache[group_id]

        inventory = cls()
        for group_id in groups:
            path = path_of(group_id)
            inventory.group_ids[_cf(path)] = group_id
            inventory.paths[_cf(path)] = path
        for host in store.list_hosts():
            inventory.hosts.setdefault(host.group_id, []).append(
                ExistingHost(host.id, host.alias, host.hostname, host.port, host.user,
                             getattr(host, "protocol", "ssh") or "ssh"))
        return inventory

    @classmethod
    def from_manager(cls, manager) -> "ExistingInventory":
        store = getattr(manager, "store", None)
        if store is None:
            raise TypeError("The MobaXterm import needs the SSH Manager inventory (InventoryStore).")
        return cls.from_store(store)

    def group_id(self, path: Sequence[str]) -> Optional[str]:
        return self.group_ids.get(_cf(path))

    def stored_name(self, parent: Sequence[str], name: str) -> Optional[str]:
        """The existing sibling's spelling of ``name`` under ``parent``, if there is one."""

        path = self.paths.get(_cf(tuple(parent) + (name,)))
        return path[-1] if path else None

    def hosts_in(self, path: Sequence[str]) -> List[ExistingHost]:
        group_id = self.group_id(path)
        return list(self.hosts.get(group_id, [])) if group_id else []


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
    folder: Tuple[str, ...]  # MobaXterm folder path (as written)
    group: Tuple[str, ...]  # store group path
    alias: str
    original_name: str


@dataclass
class PlannedGroup:
    path: Tuple[str, ...]  # store group path
    folder: Tuple[str, ...]  # MobaXterm folder path (() for an implicit / mount ancestor)
    exists: bool
    hosts: List[PlannedHost] = field(default_factory=list)


@dataclass
class UsernameEntry:
    key: str  # normalized username ("" = no username)
    count: int = 0
    raw_forms: Set[str] = field(default_factory=set)
    references: int = 0  # how many were [bracketed] credential labels
    literals: int = 0
    case_variants: List[str] = field(default_factory=list)
    protocols: Set[str] = field(default_factory=set)  # ssh, sftp, ftp, rdp: shown as tags in the wizard


@dataclass
class ImportPlan:
    options: ImportOptions
    groups: List[PlannedGroup]
    hosts: List[PlannedHost]
    issues: List[ImportIssue]
    usernames: List[UsernameEntry]
    folder_tree: List[FolderNode]
    skipped_types: Dict[int, int]
    renamed: List[Tuple[str, str]]  # (MobaXterm name/path, stored name/path) where they differ
    existing: ExistingInventory
    excluded_hosts: int = 0
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


def _importable_type(type_code: int, include_transfer: bool, include_rdp: bool = True) -> bool:
    return (type_code == SSH_TYPE or (include_transfer and type_code in TRANSFER_TYPES)
            or (include_rdp and type_code == RDP_TYPE))


def count_types(result: MxtParseResult) -> Dict[int, int]:
    """Sessions per MobaXterm type code (the wizard shows SSH and FTP / SFTP separately)."""

    counts: Dict[int, int] = {}
    for session in result.sessions:
        counts[session.type_code] = counts.get(session.type_code, 0) + 1
    return counts


def build_folder_tree(result: MxtParseResult, include_transfer: bool = True,
                      include_rdp: bool = True) -> List[FolderNode]:
    """Preview tree of every folder (implicit ancestors included) with importable host counts."""

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
        if not _importable_type(session.type_code, include_transfer, include_rdp) or not session.folder:
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
    """Map MobaXterm folders 1:1 to groups and SSH sessions to hosts (no credentials yet).

    Deterministic (file order). Names are verbatim; only names the store would
    reject are changed (``/``, length, empty) and only collisions it forbids are
    numbered (``Name (2)``) — both reported. A folder whose name matches an
    existing group at the same place (case-insensitively) is merged into it.
    """

    existing = existing or ExistingInventory()
    excluded = {tuple(path) for path in excluded_folders}
    issues: List[ImportIssue] = list(result.problems)
    skipped_types: Dict[int, int] = {}
    targets: List[Tuple[MxtSession, SshTarget]] = []
    excluded_hosts = 0
    for session in result.sessions:
        if not _importable_type(session.type_code, options.include_transfer, options.include_rdp):
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
                                  f"{count} {name} session(s) skipped (not imported)."))

    # Folders to create: every selected folder (and implicit ancestors), or only those leading to hosts.
    order: Dict[Tuple[str, ...], int] = {}
    for folder in result.folders:
        for depth in range(1, len(folder.path) + 1):
            order.setdefault(folder.path[:depth], len(order))
    for session, _target in targets:
        for depth in range(1, len(session.folder) + 1):
            order.setdefault(session.folder[:depth], len(order))
    with_hosts = {session.folder for session, _target in targets}
    leading_to_hosts = {folder[:depth] for folder in with_hosts for depth in range(1, len(folder) + 1)}
    folders = [path for path in order if path and not _excluded(path, excluded)
               and (not options.skip_empty_folders or path in leading_to_hosts)]
    folders.sort(key=lambda path: (order[path], len(path)))

    # The mount point.
    base: Tuple[str, ...] = ()
    destination_missing = False
    if options.destination is Destination.EXISTING_GROUP:
        stored = existing.paths.get(_cf(options.target_group))
        if stored is None:
            destination_missing = True
            issues.append(ImportIssue(Severity.ERROR, IssueCode.DESTINATION_MISSING,
                                      "The destination group does not exist any more; choose it again."))
            base = tuple(options.target_group)
        else:
            base = stored

    # Folder → group path, top-down; names verbatim, merged with existing siblings, unique among imported ones.
    mapped: Dict[Tuple[str, ...], Tuple[str, ...]] = {(): base}
    namers: Dict[Tuple[str, ...], _Namer] = {}
    renamed: List[Tuple[str, str]] = []
    groups: Dict[Tuple[str, ...], PlannedGroup] = {}
    for depth in range(1, len(base) + 1):  # the mount point and its ancestors already exist
        groups[_cf(base[:depth])] = PlannedGroup(base[:depth], (), not destination_missing)
    for path in folders:
        parent = mapped[path[:-1]]
        namer = namers.setdefault(_cf(parent), _Namer())
        name, codes = namer.name(path[-1])
        existing_name = existing.stored_name(parent, name)
        if existing_name is not None and not codes:
            name = existing_name  # merge into the existing group (its spelling)
        group_path = parent + (name,)
        mapped[path] = group_path
        for code in codes:
            issues.append(ImportIssue(Severity.INFO, code,
                                      f"Folder '{_folder_text(path)}' is stored as '{name}'."))
        if codes:
            renamed.append((_folder_text(path), " / ".join(group_path)))
        if _cf(group_path) not in groups:
            groups[_cf(group_path)] = PlannedGroup(group_path, path, existing.group_id(group_path) is not None)

    # Sessions → hosts; aliases unique among the imported hosts of a group (clashes with existing ones: apply).
    hosts: List[PlannedHost] = []
    alias_namers: Dict[Tuple[str, ...], _Namer] = {}
    for session, target in sorted(targets, key=lambda item: item[0].line):
        group_path = mapped[session.folder]
        namer = alias_namers.setdefault(_cf(group_path), _Namer())
        alias, codes = namer.name(session.name)
        for code in codes:
            issues.append(ImportIssue(Severity.INFO, code,
                                      f"Session '{_safe_label(session.name)}' is stored as '{alias}'.", session.line))
        if codes:
            renamed.append((_safe_label(session.name), alias))
        planned = PlannedHost(session, target, session.folder, group_path, alias, clean_label(session.name))
        groups[_cf(group_path)].hosts.append(planned)
        hosts.append(planned)

    usernames = discover_usernames(hosts)
    return ImportPlan(options, list(groups.values()), hosts, issues, usernames,
                      build_folder_tree(result, options.include_transfer, options.include_rdp),
                      skipped_types, renamed, existing, excluded_hosts, destination_missing)


def discover_usernames(hosts: Sequence[PlannedHost]) -> List[UsernameEntry]:
    """Distinct usernames of the hosts to import (bracket + plain forms merged), most used first."""

    entries: Dict[str, UsernameEntry] = {}
    for host in hosts:
        if host.target.anonymous:
            continue  # anonymous FTP logs in without a credential
        key = host.target.user_key
        entry = entries.setdefault(key, UsernameEntry(key))
        entry.count += 1
        entry.protocols.add(host.target.protocol)
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


class ImportCancelled(Exception):
    """The user cancelled while the import was being written (the transaction is rolled back)."""


@dataclass
class ImportJob:
    plan: ImportPlan
    inserts: List[Tuple[Tuple[str, ...], HostEntry]]  # (group path, new host)
    updates: List[HostEntry]  # Overwrite: entry.id = the matched existing host
    bindings: List[Tuple[HostEntry, Optional[str]]]  # (host, credential id or None = unbind), ids known at commit
    issues: List[ImportIssue]
    skipped_existing: int = 0
    renamed: int = 0
    mapping: Dict[str, Optional[str]] = field(default_factory=dict)
    credential_names: Dict[str, str] = field(default_factory=dict)

    @property
    def host_count(self) -> int:
        return len(self.inserts)

    @property
    def overwritten(self) -> int:
        return len(self.updates)

    @property
    def nothing_to_do(self) -> bool:
        return not self.inserts and not self.updates and not self.plan.new_groups


def _host_user(host: PlannedHost, cred_id: Optional[str], credentials: Mapping[str, object]) -> Optional[str]:
    if cred_id:
        cred = credentials.get(cred_id)
        if cred is None:
            raise CredentialMissingError(cred_id)
        return getattr(cred, "username", None) or None
    if host.target.user_key and not host.target.user_is_reference:
        return host.target.user_key  # a literal username is real data
    return None  # a [label] is only MobaXterm's credential name


def host_entry(host: PlannedHost, user: Optional[str], alias: Optional[str] = None) -> HostEntry:
    options: Dict[str, str] = {"hostname": host.target.hostname, "port": str(host.target.port)}
    if user:
        options["user"] = user
    if host.target.proxy_jump:
        options["proxyjump"] = host.target.proxy_jump
    if host.target.identity_file:
        options["identityfile"] = host.target.identity_file
    metadata: Dict[str, object] = {"imported_from": "mobaxterm", "mobaxterm_folder": _folder_text(host.folder)}
    if host.target.anonymous:
        options.pop("user", None)
        metadata["transfer"] = {"v": 1, "anonymous": True}
    if host.target.protocol == "rdp":
        metadata["rdp"] = {"v": 1, "domain": host.target.domain} if host.target.domain else {"v": 1}
    group = host.group
    return HostEntry(alias=alias or host.alias, options=options, metadata=metadata,
                     parent_group=group[0] if group else "", child_group="/".join(group[1:]),
                     protocol=host.target.protocol)


def identity_key(hostname: str, port: Optional[int], user: Optional[str],
                 protocol: str = "ssh") -> Tuple[str, int, str, str]:
    """``(host name, port, user, protocol)`` as compared for "the same host" (case-insensitive name and user).

    The protocol is part of it: an SFTP session never matches the SSH host on the same ``host:22``.
    """

    protocol = (protocol or "ssh").lower()
    return ((hostname or "").strip().lower(), int(port or DEFAULT_PORTS.get(protocol, 22)),
            (user or "").strip().casefold(), protocol)


def apply_credential_mapping(
    plan: ImportPlan,
    mapping: Mapping[str, Optional[str]],
    credentials: Mapping[str, object],
    *,
    default: Optional[str] = None,
) -> ImportJob:
    """Final hosts (``User`` from the chosen credential), matched against the existing inventory.

    ``mapping`` maps a username entry key (``""`` = no username) to a credential
    id or ``None``; keys missing from it use ``default``. A host matching an
    existing one in the same group (host name, port, normalized user) is
    skipped, imported as ``Alias (2)`` or overwrites it in place, per policy.
    """

    resolved = {entry.key: mapping.get(entry.key, default) for entry in plan.usernames}
    for cred_id in resolved.values():
        if cred_id and cred_id not in credentials:
            raise CredentialMissingError(cred_id)
    issues: List[ImportIssue] = []
    inserts: List[Tuple[Tuple[str, ...], HostEntry]] = []
    updates: List[HostEntry] = []
    bindings: List[Tuple[HostEntry, Optional[str]]] = []
    skipped = renamed = 0
    policy = plan.options.conflict_policy
    for group in plan.groups:
        existing = plan.existing.hosts_in(group.path) if group.exists else []
        by_identity: Dict[Tuple[str, int, str, str], List[ExistingHost]] = {}
        for existing_host in existing:
            by_identity.setdefault(identity_key(existing_host.hostname or existing_host.alias, existing_host.port,
                                                existing_host.user, existing_host.protocol),
                                   []).append(existing_host)
        taken = {h.alias.casefold() for h in existing}
        for host in group.hosts:
            cred_id = None if host.target.anonymous else resolved.get(host.target.user_key)
            user = None if host.target.anonymous else _host_user(host, cred_id, credentials)
            label = _safe_label(host.original_name)
            candidates = by_identity.get(identity_key(host.target.hostname, host.target.port, user,
                                                      host.target.protocol), [])
            # The same host twice (an earlier "Rename" copy): prefer the one with this alias, else the first.
            match = next((h for h in candidates if h.alias.casefold() == host.alias.casefold()),
                         candidates[0] if candidates else None)
            alias = host.alias
            if match is not None:
                if policy is ConflictPolicy.SKIP:
                    skipped += 1
                    issues.append(ImportIssue(Severity.INFO, IssueCode.EXISTING_HOST_SKIPPED,
                                              f"'{label}' already exists as '{match.alias}' in "
                                              f"'{' / '.join(group.path)}'; skipped.", host.session.line))
                    continue
                if policy is ConflictPolicy.OVERWRITE:
                    entry = host_entry(host, user, alias=match.alias)  # the alias stays
                    entry.id = match.id
                    updates.append(entry)
                    bindings.append((entry, cred_id))  # None drops an old binding
                    issues.append(ImportIssue(Severity.WARNING, IssueCode.EXISTING_HOST_OVERWRITTEN,
                                              f"'{match.alias}' in '{' / '.join(group.path)}' is updated.",
                                              host.session.line))
                    continue
                alias = unique_name(alias, taken | {alias.casefold()})  # Rename: always a numbered copy
                renamed += 1
                issues.append(ImportIssue(Severity.INFO, IssueCode.EXISTING_HOST_RENAMED,
                                          f"'{label}' already exists; imported as '{alias}'.", host.session.line))
            elif alias.casefold() in taken:
                alias = unique_name(alias, taken)
                issues.append(ImportIssue(Severity.INFO, IssueCode.NAME_DEDUPLICATED,
                                          f"Another host is already called '{host.alias}' here; '{label}' is "
                                          f"stored as '{alias}'.", host.session.line))
            taken.add(alias.casefold())
            entry = host_entry(host, user, alias=alias)
            inserts.append((group.path, entry))
            if cred_id:
                bindings.append((entry, cred_id))
    names = {cid: getattr(credentials[cid], "name", cid) for cid in resolved.values() if cid}
    return ImportJob(plan, inserts, updates, bindings, issues, skipped, renamed, dict(resolved), names)


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
    parents: List[str] = field(default_factory=list)  # top-level groups touched (to select afterwards)
    log_path: Optional[Path] = None


FAILED_PREFIX = "Import failed, no changes were made"


def commit(
    job: ImportJob,
    manager,
    credential_store,
    *,
    progress: Optional[Callable[[int, int], None]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
) -> ImportReport:
    """Write the job in **one** store transaction (groups, hosts, updates, bindings) or change nothing.

    Credential ids are re-checked right before writing; inside the transaction
    the destination group must still exist. Any error or a cancel rolls back.
    One ``refresh()`` at the end.
    """

    plan = job.plan
    parents = plan.top_level
    if job.nothing_to_do:
        return ImportReport(ImportStatus.NOTHING, "Nothing new to import: every host already exists.")
    if plan.destination_missing:
        return ImportReport(ImportStatus.FAILED, f"{FAILED_PREFIX}: the destination group does not exist.")
    if any(cid and credential_store.get(cid) is None for _entry, cid in job.bindings):
        return ImportReport(ImportStatus.FAILED, f"{FAILED_PREFIX}: a chosen credential was deleted meanwhile.")
    store = manager.store
    if job.updates:
        manager.backup_before("before-import-overwrite")
    total = len(plan.groups) + 1

    def check_cancel() -> None:
        if should_cancel is not None and should_cancel():
            raise ImportCancelled()

    from ..inventory.manager import host_columns

    try:
        with store.transaction():
            group_ids: Dict[Tuple[str, ...], str] = {}
            if plan.options.destination is Destination.EXISTING_GROUP:
                target_id = plan.existing.group_id(plan.options.target_group)
                if target_id is None or store.get_group(target_id) is None:
                    raise LookupError("the destination group was deleted meanwhile")
            for index, group in enumerate(plan.groups):
                check_cancel()
                group_ids[_cf(group.path)] = store.create_group_path(list(group.path)).id
                if progress is not None:
                    progress(index + 1, total)
            check_cancel()
            new_rows = store.add_hosts_bulk(manager._new_host(group_ids[_cf(path)], entry) for path, entry in job.inserts)
            for (_path, entry), row in zip(job.inserts, new_rows):
                entry.id = row.id
            for entry in job.updates:
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
        return ImportReport(ImportStatus.CANCELLED, "Import cancelled, no changes were made.")
    except Exception as exc:  # sqlite3.Error, InventoryError, SecureStoreError, KeyError, LookupError…
        manager.refresh()
        return ImportReport(ImportStatus.FAILED, f"{FAILED_PREFIX}: {exc}" if str(exc) else FAILED_PREFIX)
    manager.refresh()
    new_groups = len(plan.new_groups)
    message = f"Imported {job.host_count} host(s)"
    if job.overwritten:
        message += f", updated {job.overwritten}"
    message += f"; {new_groups} new group(s)."
    return ImportReport(ImportStatus.SUCCESS, message, job.host_count, new_groups, parents)


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
              f"  Top-level groups: {len(plan.top_level)}",
              f"  Groups: {len(plan.groups)} ({len(plan.new_groups)} new)",
              f"  SSH hosts planned: {sum(1 for h in plan.hosts if h.target.protocol == 'ssh')}",
              f"  FTP / SFTP hosts planned: {sum(1 for h in plan.hosts if h.target.protocol in ('ftp', 'sftp'))}",
              f"  RDP hosts planned: {sum(1 for h in plan.hosts if h.target.protocol == 'rdp')}"]
    if any(h.target.protocol == "rdp" for h in plan.hosts):
        lines.append("  Note: " + RDP_NOTE)
    if job is not None:
        lines += [f"  Hosts to import: {job.host_count}",
                  f"  Skipped (already exist): {job.skipped_existing}",
                  f"  Imported as a numbered copy: {job.renamed}",
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
    if plan.renamed:
        lines += ["", "Names stored differently (MobaXterm → SSH Manager)"]
        lines += [f"  {original} → {stored}" for original, stored in plan.renamed]
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
