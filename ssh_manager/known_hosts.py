"""OpenSSH ``known_hosts``: read, match, fingerprint and edit without losing a byte (no GTK).

* **Parsing** keeps every line: entries become :class:`Entry`, comments, blank and
  malformed lines :class:`Unparsed` (shown, never rewritten). Joining the
  ``raw_line`` of all items gives the file back byte for byte (CRLF, odd spacing).
* **Matching** follows ``sshd``'s rules: plain names, ``[host]:port``, comma
  lists, ``*`` / ``?`` wildcards, ``!`` negation, and hashed entries
  (``|1|salt|hash``: HMAC-SHA1 of the name, compared in constant time).
* **Editing** (remove, replace, add, restore) locks the file (``fcntl.flock``),
  refuses when it changed since it was read, writes a timestamped backup first
  (``known_hosts.bak-YYYYmmdd-HHMMSS``, the 5 newest kept), and replaces it
  atomically (temporary file in the same folder, ``fsync``, ``os.replace``, 0600).
  Untouched lines keep their exact bytes.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import fcntl
import hashlib
import hmac
import os
import re
import struct
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple, Union

MARKERS = ("@cert-authority", "@revoked")
BACKUP_PREFIX = ".bak-"
KEEP_BACKUPS = 5
_ENCODING = ("utf-8", "surrogateescape")  # bytes that are not UTF-8 survive a round trip


class KnownHostsError(Exception):
    """A clear, user-facing reason (never a key or a secret)."""


class KnownHostsChanged(KnownHostsError):
    """The file changed since it was read (another program, another window): nothing was written."""


def default_known_hosts() -> Path:
    return Path("~/.ssh/known_hosts").expanduser()


def host_pattern(hostname: str, port: int) -> str:
    """How OpenSSH names a host in known_hosts: ``name`` on port 22, else ``[name]:port``."""

    return hostname if int(port or 22) == 22 else f"[{hostname}]:{port}"


# ------------------------------------------------------------------ keys
def _blob(key_b64: str) -> Optional[bytes]:
    try:
        return base64.b64decode(key_b64.encode("ascii"), validate=True)
    except (binascii.Error, ValueError, UnicodeEncodeError):
        return None


def fingerprint_sha256(key_b64: str) -> str:
    """``SHA256:…`` exactly like ``ssh-keygen -l`` (unpadded base64); ``""`` for a damaged key."""

    blob = _blob(key_b64)
    if not blob:
        return ""
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode("ascii").rstrip("=")


def fingerprint_md5(key_b64: str) -> str:
    blob = _blob(key_b64)
    if not blob:
        return ""
    digest = hashlib.md5(blob, usedforsecurity=False).hexdigest()
    return "MD5:" + ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))


def _strings(blob: bytes) -> List[bytes]:
    out, offset = [], 0
    while offset + 4 <= len(blob):
        (length,) = struct.unpack(">I", blob[offset:offset + 4])
        offset += 4
        if offset + length > len(blob):
            break
        out.append(blob[offset:offset + length])
        offset += length
    return out


def key_bits(key_type: str, key_b64: str) -> int:
    """The key size where the blob tells it (RSA modulus, ECDSA curve, Ed25519); 0 otherwise."""

    base = key_type.replace("-cert-v01@openssh.com", "")
    if "ed25519" in base:
        return 256
    if "ed448" in base:
        return 456
    blob = _blob(key_b64)
    if not blob:
        return 0
    parts = _strings(blob)
    if base.startswith("ecdsa-sha2-nistp") or base.startswith("sk-ecdsa-sha2-nistp"):
        match = re.search(r"nistp(\d+)", base)
        return int(match.group(1)) if match else 0
    if base in ("ssh-rsa", "rsa-sha2-256", "rsa-sha2-512") and len(parts) >= 3:
        return int.from_bytes(parts[2], "big").bit_length()
    if base == "ssh-dss" and len(parts) >= 2:
        return int.from_bytes(parts[1], "big").bit_length()
    return 0


def key_type_label(key_type: str) -> str:
    """``ECDSA``, ``ED25519``, ``RSA`` … as ssh prints them."""

    base = key_type.replace("-cert-v01@openssh.com", "")
    if base.startswith("sk-ssh-ed25519"):
        return "ED25519-SK"
    if base.startswith("sk-ecdsa"):
        return "ECDSA-SK"
    if base.startswith("ecdsa-"):
        return "ECDSA"
    if base == "ssh-ed25519":
        return "ED25519"
    if base == "ssh-ed448":
        return "ED448"
    if base in ("ssh-rsa", "rsa-sha2-256", "rsa-sha2-512"):
        return "RSA"
    if base == "ssh-dss":
        return "DSA"
    return key_type


# -------------------------------------------------------------- matching
def _glob(pattern: str) -> "re.Pattern[str]":
    return re.compile("".join(".*" if ch == "*" else "." if ch == "?" else re.escape(ch) for ch in pattern) + r"\Z",
                      re.IGNORECASE | re.DOTALL)


def _hash_name(salt: bytes, name: str) -> bytes:
    return hmac.new(salt, name.encode("utf-8"), hashlib.sha1).digest()


def hashed_host(name: str, port: int = 22, salt: Optional[bytes] = None) -> str:
    """``|1|salt|hash`` for ``name`` (``HashKnownHosts`` style)."""

    salt = salt if salt is not None else os.urandom(20)
    digest = _hash_name(salt, host_pattern(name, port).lower())
    return f"|1|{base64.b64encode(salt).decode()}|{base64.b64encode(digest).decode()}"


@dataclass(frozen=True)
class Entry:
    line_no: int
    marker: str
    hosts_field: str
    hashed: bool
    key_type: str
    key_b64: str
    comment: str
    raw_line: str

    @property
    def patterns(self) -> List[str]:
        return [] if self.hashed else [p for p in self.hosts_field.split(",") if p]

    @property
    def fingerprint(self) -> str:
        return fingerprint_sha256(self.key_b64)

    @property
    def wildcard(self) -> bool:
        return any(ch in self.hosts_field for ch in "*?!")

    def _hash_parts(self) -> Optional[Tuple[bytes, bytes]]:
        parts = self.hosts_field.split("|")
        if len(parts) != 4 or parts[1] != "1":
            return None
        try:
            return base64.b64decode(parts[2]), base64.b64decode(parts[3])
        except (binascii.Error, ValueError):
            return None

    def matches(self, name: str, port: int = 22) -> bool:
        candidate = host_pattern(name, port).lower()
        if self.hashed:
            parts = self._hash_parts()
            return bool(parts) and hmac.compare_digest(_hash_name(parts[0], candidate), parts[1])
        matched = False
        for pattern in self.patterns:
            negated = pattern.startswith("!")
            if _glob(pattern[1:] if negated else pattern).match(candidate):
                if negated:
                    return False  # a matching negation wins over everything
                matched = True
        return matched


@dataclass(frozen=True)
class Unparsed:
    """A comment, a blank line or a line that is not an entry: kept exactly as it is."""

    line_no: int
    raw_line: str


Item = Union[Entry, Unparsed]


def parse_line(line_no: int, raw_line: str) -> Item:
    text = raw_line.rstrip("\r\n").strip()
    if not text or text.startswith("#"):
        return Unparsed(line_no, raw_line)
    parts = text.split()
    marker = ""
    if parts[0].startswith("@"):
        marker = parts.pop(0)
        if marker not in MARKERS:
            return Unparsed(line_no, raw_line)
    if len(parts) < 3 or _blob(parts[2]) is None:
        return Unparsed(line_no, raw_line)
    hosts = parts[0]
    return Entry(line_no=line_no, marker=marker, hosts_field=hosts, hashed=hosts.startswith("|"),
                 key_type=parts[1], key_b64=parts[2], comment=" ".join(parts[3:]), raw_line=raw_line)


def parse(data: bytes) -> List[Item]:
    text = data.decode(*_ENCODING)
    return [parse_line(number, line) for number, line in enumerate(text.splitlines(keepends=True), start=1)]


def _ending(raw_line: str) -> str:
    return "\r\n" if raw_line.endswith("\r\n") else "\n"


def entry_line(hosts: str, key_type: str, key_b64: str, *, marker: str = "", comment: str = "",
               ending: str = "\n") -> str:
    return " ".join(part for part in (marker, hosts, key_type, key_b64, comment) if part) + ending


# ---------------------------------------------------------------- backups
def _stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def backups(path: Path) -> List[Path]:
    """Backups of ``path``, newest first."""

    path = Path(path)
    prefix = path.name + BACKUP_PREFIX
    if not path.parent.is_dir():
        return []
    return sorted((p for p in path.parent.iterdir() if p.name.startswith(prefix) and p.is_file()),
                  key=lambda p: p.name, reverse=True)


def _write_atomic(path: Path, data: bytes) -> None:
    folder = path.parent
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(folder))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp)
        raise
    with contextlib.suppress(OSError):
        dir_fd = os.open(folder, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)


def _backup(path: Path, data: bytes) -> Path:
    target = path.with_name(path.name + BACKUP_PREFIX + _stamp())
    counter = 1
    while target.exists():
        target = path.with_name(f"{path.name}{BACKUP_PREFIX}{_stamp()}-{counter}")
        counter += 1
    _write_atomic(target, data)
    for old in backups(path)[KEEP_BACKUPS:]:
        with contextlib.suppress(OSError):
            old.unlink()
    return target


def _file_stamp(path: Path) -> Optional[Tuple[int, int, int]]:
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    return info.st_mtime_ns, info.st_size, info.st_ino


@contextlib.contextmanager
def _locked(path: Path):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def restore_backup(path: Path, backup: Path) -> None:
    """Put a backup back in place (the "Undo" of a deletion)."""

    path = Path(path)
    data = Path(backup).read_bytes()
    with _locked(path):
        _write_atomic(path, data)


# ------------------------------------------------------------------- file
class KnownHostsFile:
    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path is not None else default_known_hosts()
        self._stamp: Optional[Tuple[int, int, int]] = None
        self._items: List[Item] = []

    def load(self) -> List[Item]:
        try:
            data = self.path.read_bytes()
        except FileNotFoundError:
            data = b""
        self._stamp = _file_stamp(self.path)
        self._items = parse(data)
        return list(self._items)

    def entries(self) -> List[Entry]:
        return [item for item in self.load() if isinstance(item, Entry)]

    @property
    def stamp(self) -> Optional[Tuple[int, int, int]]:
        return self._stamp

    def lookup(self, name: str, port: int = 22) -> List[Entry]:
        return [entry for entry in self.entries() if entry.matches(name, port)]

    def entry_at(self, line_no: int) -> Optional[Entry]:
        for item in self.load():
            if item.line_no == line_no:
                return item if isinstance(item, Entry) else None
        return None

    # ------------------------------------------------------------ editing
    def _edit(self, change: Callable[[List[str]], List[str]], *, backup: bool = True,
              check_stamp: bool = True) -> Optional[Path]:
        with _locked(self.path):
            current = self.path.read_bytes() if self.path.exists() else b""
            if check_stamp and self._stamp is not None and _file_stamp(self.path) != self._stamp and current:
                raise KnownHostsChanged("known_hosts was changed by another program. Reload it and try again.")
            lines = current.decode(*_ENCODING).splitlines(keepends=True)
            updated = change(lines)
            data = "".join(updated).encode(*_ENCODING)
            saved = _backup(self.path, current) if backup and current else None
            _write_atomic(self.path, data)
        self._stamp = _file_stamp(self.path)
        self._items = parse(data)
        return saved

    @staticmethod
    def _check(lines: List[str], entry: Entry) -> None:
        index = entry.line_no - 1
        if index >= len(lines) or lines[index] != entry.raw_line:
            raise KnownHostsChanged(f"Line {entry.line_no} of known_hosts is no longer the expected entry.")

    def remove_entries(self, entries: Sequence[Entry], *, backup: bool = True) -> Optional[Path]:
        """Delete exactly these lines (each checked against the file); returns the backup."""

        doomed = sorted({entry.line_no: entry for entry in entries}.values(), key=lambda e: e.line_no)

        def change(lines: List[str]) -> List[str]:
            for entry in doomed:
                self._check(lines, entry)
            numbers = {entry.line_no for entry in doomed}
            return [line for index, line in enumerate(lines, start=1) if index not in numbers]

        return self._edit(change, backup=backup)

    def replace_key(self, entry: Entry, key_type: str, key_b64: str) -> Optional[Path]:
        """The same hosts (and marker, comment), another key; the other lines are not touched."""

        if _blob(key_b64) is None:
            raise KnownHostsError("The new key is not valid.")

        def change(lines: List[str]) -> List[str]:
            self._check(lines, entry)
            lines[entry.line_no - 1] = entry_line(entry.hosts_field, key_type, key_b64, marker=entry.marker,
                                                  comment=entry.comment, ending=_ending(entry.raw_line))
            return lines

        return self._edit(change)

    def add_entry(self, host: str, port: int, key_type: str, key_b64: str, *, hashed: bool = False,
                  backup: bool = True) -> Optional[Path]:
        if _blob(key_b64) is None:
            raise KnownHostsError("The key is not valid.")
        hosts = hashed_host(host, port) if hashed else host_pattern(host, port)

        def change(lines: List[str]) -> List[str]:
            if lines and not lines[-1].endswith("\n"):
                lines[-1] += "\n"
            return lines + [entry_line(hosts, key_type, key_b64)]

        return self._edit(change, backup=backup, check_stamp=False)  # appending never loses another change

    def merge(self, other: Iterable[Entry]) -> Tuple[int, int]:
        """Append the entries of another file that are not here yet; ``(added, skipped)``."""

        other = list(other)
        present = {(e.marker, e.hosts_field.lower(), e.key_type, e.key_b64) for e in self.entries()}
        new = []
        for entry in other:
            key = (entry.marker, entry.hosts_field.lower(), entry.key_type, entry.key_b64)
            if key not in present:
                present.add(key)
                new.append(entry)
        if not new:
            return 0, len(other)

        def change(lines: List[str]) -> List[str]:
            if lines and not lines[-1].endswith("\n"):
                lines[-1] += "\n"
            return lines + [entry_line(e.hosts_field, e.key_type, e.key_b64, marker=e.marker, comment=e.comment)
                            for e in new]

        self._edit(change, check_stamp=False)
        return len(new), len(other) - len(new)


def merge_preview(target: Sequence[Entry], other: Sequence[Entry]) -> Tuple[int, int]:
    """``(new, duplicates)`` if ``other`` were merged into ``target``."""

    present = {(e.marker, e.hosts_field.lower(), e.key_type, e.key_b64) for e in target}
    new = 0
    for entry in other:
        key = (entry.marker, entry.hosts_field.lower(), entry.key_type, entry.key_b64)
        if key not in present:
            present.add(key)
            new += 1
    return new, len(other) - new


# --------------------------------------------------------------- inventory
InventoryHost = Tuple[str, str, str, int]  # (key, alias, hostname, port)


def map_inventory(entries: Sequence[Entry], hosts: Sequence[InventoryHost], *,
                  progress: Optional[Callable[[int, int], None]] = None) -> Dict[str, List[Entry]]:
    """The entries that match each inventory host (by host name and by alias), in one pass.

    Plain names are looked up in a dictionary; wildcard / negated patterns are tested per host; hashed
    entries are tried against every host only when the file has some (one HMAC per pair: run it in a
    worker thread).
    """

    plain: Dict[str, List[Entry]] = {}
    patterned: List[Entry] = []
    hashed: List[Tuple[Entry, bytes, bytes]] = []
    for entry in entries:
        if entry.hashed:
            parts = entry._hash_parts()
            if parts:
                hashed.append((entry, parts[0], parts[1]))
        elif entry.wildcard:
            patterned.append(entry)
        else:
            for pattern in entry.patterns:
                plain.setdefault(pattern.lower(), []).append(entry)
    result: Dict[str, List[Entry]] = {}
    names: Dict[str, List[str]] = {}
    for key, alias, hostname, port in hosts:
        candidates = []
        for name in (hostname, alias):
            if name and host_pattern(name, port).lower() not in candidates:
                candidates.append(host_pattern(name, port).lower())
        names[key] = candidates
        found = {e.line_no: e for c in candidates for e in plain.get(c, [])}
        for entry in patterned:
            if any(entry.matches(name, port) for name in (hostname, alias) if name):
                found[entry.line_no] = entry
        result[key] = list(found.values())
    total = len(hashed)
    for index, (entry, salt, digest) in enumerate(hashed, start=1):
        for key, candidates in names.items():
            if any(hmac.compare_digest(_hash_name(salt, c), digest) for c in candidates):
                result[key].append(entry)
        if progress is not None and (index % 200 == 0 or index == total):
            progress(index, total)
    if progress is not None and not hashed:
        progress(1, 1)
    for key in result:
        result[key].sort(key=lambda e: e.line_no)
    return result


def unmatched_hashed(entries: Sequence[Entry], mapping: Dict[str, List[Entry]]) -> List[Entry]:
    matched = {e.line_no for found in mapping.values() for e in found}
    return [e for e in entries if e.hashed and e.line_no not in matched]


_CACHE: Dict[tuple, Dict[str, List[Entry]]] = {}


def cached_map(file: KnownHostsFile, entries: Sequence[Entry], hosts: Sequence[InventoryHost], *,
               progress: Optional[Callable[[int, int], None]] = None) -> Dict[str, List[Entry]]:
    """:func:`map_inventory`, remembered per file version (mtime, size) and inventory."""

    key = (str(file.path), file.stamp, tuple(hosts))
    if key not in _CACHE:
        _CACHE.clear()  # one file, one inventory at a time
        _CACHE[key] = map_inventory(entries, hosts, progress=progress)
    elif progress is not None:
        progress(1, 1)
    return _CACHE[key]


def resolve_changed_entry(file: KnownHostsFile, line_no: int, names: Sequence[Tuple[str, int]],
                          new_fingerprint: str = "") -> Entry:
    """The entry ssh called "offending": line ``line_no`` if it really belongs to one of ``names``.

    When that line is something else now (the file changed), the host's entries are searched again:
    exactly one entry with another key than ``new_fingerprint`` is accepted; anything else is refused
    (never a removal by pattern).
    """

    names = [(name, int(port or 22)) for name, port in names if name]
    entry = file.entry_at(line_no) if line_no > 0 else None
    if entry is not None and not entry.marker and any(entry.matches(name, port) for name, port in names):
        return entry
    candidates = {e.line_no: e for e in file.entries() if not e.marker
                  and any(e.matches(name, port) for name, port in names)
                  and (not new_fingerprint or e.fingerprint != new_fingerprint)}
    if len(candidates) == 1:
        return next(iter(candidates.values()))
    if not candidates:
        raise KnownHostsError("No stored key for this host was found in known_hosts. Nothing was changed.")
    raise KnownHostsError("Several stored keys could be the old one ({lines}). Nothing was changed; use the "
                          "Known Hosts manager.".format(lines=", ".join(str(n) for n in sorted(candidates))))


# ------------------------------------------------------------- verifying
MATCH, DIFFERENT, NOT_STORED = "match", "different", "not_stored"


def probe_host_key(hostname: str, port: int = 22, *, prefer: Sequence[str] = (), timeout: float = 10.0
                   ) -> Tuple[str, str]:
    """The key the server presents ``(key_type, key_b64)``: key exchange only, no login, nothing written.

    ``prefer`` asks for these key types first (the stored ones), so a server with several keys shows
    the one known_hosts has.
    """

    import socket

    import paramiko

    sock = socket.create_connection((hostname, int(port or 22)), timeout=timeout)
    transport = paramiko.Transport(sock)
    try:
        if prefer:
            options = transport.get_security_options()
            wanted = [t for t in prefer if t in options.key_types]
            if wanted:
                options.key_types = wanted + [t for t in options.key_types if t not in wanted]
        transport.start_client(timeout=timeout)
        key = transport.get_remote_server_key()
        return key.get_name(), key.get_base64()
    finally:
        transport.close()


def compare_host_key(entries: Sequence[Entry], key_type: str, key_b64: str) -> str:
    """``match``, ``different`` (known with other keys) or ``not_stored``; ``@`` markers are ignored."""

    known = [e for e in entries if not e.marker]
    if any(e.key_type == key_type and e.key_b64 == key_b64 for e in known):
        return MATCH
    return DIFFERENT if known else NOT_STORED


__all__ = ["Entry", "Unparsed", "KnownHostsFile", "KnownHostsError", "KnownHostsChanged", "parse", "parse_line",
           "fingerprint_sha256", "fingerprint_md5", "key_bits", "key_type_label", "host_pattern", "hashed_host",
           "backups", "restore_backup", "map_inventory", "unmatched_hashed", "cached_map", "merge_preview",
           "default_known_hosts", "probe_host_key", "compare_host_key", "resolve_changed_entry", "MATCH", "DIFFERENT", "NOT_STORED"]
