"""Parsers for FTP directory listings: ``MLSD`` (RFC 3659) and ``LIST`` (pure Python).

``LIST`` has no standard format. Supported:

* Unix ``ls -l`` (with or without the group column, symlinks ``name -> target``,
  names with spaces, ``HH:MM`` dates without a year — the year is inferred);
* Windows / IIS "DOS" style (``01-02-24  03:04PM  <DIR>  name``);
* NetWare and VMS, best effort.

A line nothing understands is kept as an entry of kind ``other`` with the
whole line as its name — never dropped silently. ``total N`` lines and
``.`` / ``..`` are skipped. Times are UTC plus the host's server time offset.
"""

from __future__ import annotations

import calendar
import re
import time
from typing import Optional

from .backend import EntryKind, RemoteEntry, join

_MONTHS = {name: index for index, name in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), start=1)}

_UNIX = re.compile(
    r"^(?P<type>[-dlbcpsD])(?P<perms>[rwxsStTl-]{9})[+@.]?\s+(?P<links>\d+)\s+"
    r"(?P<owner>\S+)\s+(?:(?P<group>\S+)\s+)?(?P<size>\d+)\s+"
    r"(?P<month>[A-Za-z]{3})\s+(?P<day>\d{1,2})\s+(?P<when>\d{1,2}:\d{2}|\d{4})\s(?P<name>.+)$")
_UNIX_DEVICE = re.compile(  # block / character devices: "major, minor" instead of a size
    r"^(?P<type>[bc])(?P<perms>[rwxsStTl-]{9})[+@.]?\s+\d+\s+(?P<owner>\S+)\s+(?P<group>\S+)\s+\d+,\s*\d+\s+"
    r"(?P<month>[A-Za-z]{3})\s+(?P<day>\d{1,2})\s+(?P<when>\d{1,2}:\d{2}|\d{4})\s(?P<name>.+)$")
_DOS = re.compile(
    r"^(?P<m>\d{2})-(?P<d>\d{2})-(?P<y>\d{2,4})\s+(?P<h>\d{1,2}):(?P<mi>\d{2})\s*(?P<ampm>[AaPp][Mm])?\s+"
    r"(?P<size><DIR>|\d+)\s+(?P<name>.+)$")
_NETWARE = re.compile(
    r"^(?P<type>[d-])\s+\[(?P<perms>[RWCEAFMS-]+)\]\s+(?P<owner>\S+)\s+(?P<size>\d+)\s+"
    r"(?P<month>[A-Za-z]{3})\s+(?P<day>\d{1,2})\s+(?P<when>\d{1,2}:\d{2}|\d{4})\s+(?P<name>.+)$")
_VMS = re.compile(r"^(?P<name>[A-Za-z0-9_$\-.]+);(?P<version>\d+)\s+(?P<size>\d+)(?:/\d+)?\s+(?P<rest>.*)$")
_MLSD = re.compile(r"^(?P<facts>(?:[^=;\s]+=[^;]*;)*) (?P<name>.*)$")


def _mode_from_perms(perms: str) -> Optional[int]:
    if len(perms) != 9:
        return None
    mode = 0
    for index, char in enumerate(perms):
        if char not in "-lST":
            mode |= 1 << (8 - index)
    for index, char, bit in ((2, perms[2], 0o4000), (5, perms[5], 0o2000), (8, perms[8], 0o1000)):
        if char in "sStT":
            mode |= bit
            if char in "ST":  # set-id / sticky without execute
                mode &= ~(1 << (8 - index))
    return mode


def _infer_time(month: str, day: str, when: str, now: float) -> Optional[float]:
    month_number = _MONTHS.get(month.lower())
    if month_number is None:
        return None
    try:
        day_number = int(day)
        if ":" in when:
            hour, minute = (int(part) for part in when.split(":"))
            year = time.gmtime(now).tm_year
            stamp = calendar.timegm((year, month_number, day_number, hour, minute, 0))
            if stamp > now + 86400:  # "Dec 31 10:00" seen in January: last year
                stamp = calendar.timegm((year - 1, month_number, day_number, hour, minute, 0))
            return float(stamp)
        return float(calendar.timegm((int(when), month_number, day_number, 0, 0, 0)))
    except (ValueError, OverflowError):
        return None


def parse_mlsd_line(line: str, directory: str) -> Optional[RemoteEntry]:
    """One ``MLSD`` / ``MLST`` line; ``None`` for ``.``, ``..`` and empty lines."""

    line = line.rstrip("\r\n")
    if not line.strip():
        return None
    match = _MLSD.match(line)
    if match is None:
        return RemoteEntry(line.strip(), join(directory, line.strip()), EntryKind.OTHER)
    facts = {}
    for fact in match.group("facts").split(";"):
        if "=" in fact:
            key, value = fact.split("=", 1)
            facts[key.strip().lower()] = value
    name = match.group("name")
    raw_type = facts.get("type", "")
    kind_fact = raw_type.lower()
    if kind_fact in ("cdir", "pdir") or name in (".", ".."):
        return None
    link_target = ""
    if kind_fact == "dir":
        kind = EntryKind.DIR
    elif kind_fact == "file":
        kind = EntryKind.FILE
    elif kind_fact.startswith("os.unix=slink") or kind_fact == "os.unix=symlink":
        kind = EntryKind.SYMLINK
        link_target = raw_type.split(":", 1)[1] if ":" in raw_type else ""  # the target keeps its case
    else:
        kind = EntryKind.OTHER
    size = facts.get("size") or facts.get("sizd")
    mtime = None
    modify = facts.get("modify", "")
    if re.fullmatch(r"\d{14}(\.\d+)?", modify):
        try:
            mtime = float(calendar.timegm(time.strptime(modify[:14], "%Y%m%d%H%M%S")))
        except ValueError:
            mtime = None
    mode = None
    unix_mode = facts.get("unix.mode", "")
    if re.fullmatch(r"0?[0-7]{3,4}", unix_mode):
        mode = int(unix_mode, 8)
    return RemoteEntry(
        name, join(directory, name), kind,
        size=int(size) if size and size.isdigit() else None,
        mtime=mtime, mode=mode, perms=facts.get("perm", ""),
        owner=facts.get("unix.owner", facts.get("unix.uid", "")),
        group=facts.get("unix.group", facts.get("unix.gid", "")),
        link_target=link_target,
    )


def parse_list_line(line: str, directory: str, *, now: Optional[float] = None,
                    offset_min: int = 0) -> Optional[RemoteEntry]:
    """One ``LIST`` line; ``None`` for ``total N``, ``.``, ``..`` and empty lines."""

    line = line.rstrip("\r\n")
    if not line.strip() or re.match(r"^total\s+\d+\s*$", line.strip(), re.IGNORECASE):
        return None
    now = time.time() if now is None else now
    shift = offset_min * 60.0
    entry = (_parse_unix(line, directory, now) or _parse_dos(line, directory) or _parse_netware(line, directory, now)
             or _parse_vms(line, directory))
    if entry is None:
        name = line.strip()
        return RemoteEntry(name, join(directory, name), EntryKind.OTHER)  # kept, never dropped
    if entry.name in (".", ".."):
        return None
    if entry.mtime is not None and shift:
        entry = RemoteEntry(entry.name, entry.path, entry.kind, entry.size, entry.mtime - shift, entry.mode,
                            entry.perms, entry.owner, entry.group, entry.link_target)
    return entry


def _parse_unix(line: str, directory: str, now: float) -> Optional[RemoteEntry]:
    match = _UNIX.match(line) or _UNIX_DEVICE.match(line)
    if match is None:
        return None
    kind_char = match.group("type")
    name = match.group("name")
    link_target = ""
    if kind_char == "l":
        kind = EntryKind.SYMLINK
        if " -> " in name:
            name, link_target = name.split(" -> ", 1)
    elif kind_char in "dD":
        kind = EntryKind.DIR
    elif kind_char == "-":
        kind = EntryKind.FILE
    else:
        kind = EntryKind.OTHER
    groups = match.groupdict()
    size = groups.get("size")
    return RemoteEntry(
        name, join(directory, name), kind,
        size=int(size) if size else None,
        mtime=_infer_time(match.group("month"), match.group("day"), match.group("when"), now),
        mode=_mode_from_perms(match.group("perms")),
        perms=match.group("perms"),
        owner=match.group("owner") or "",
        group=groups.get("group") or "",
        link_target=link_target,
    )


def _parse_dos(line: str, directory: str) -> Optional[RemoteEntry]:
    match = _DOS.match(line)
    if match is None:
        return None
    year = int(match.group("y"))
    if year < 100:
        year += 2000 if year < 70 else 1900
    hour = int(match.group("h"))
    ampm = (match.group("ampm") or "").lower()
    if ampm == "pm" and hour < 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    try:
        mtime: Optional[float] = float(calendar.timegm((year, int(match.group("m")), int(match.group("d")), hour,
                                                        int(match.group("mi")), 0)))
    except (ValueError, OverflowError):
        mtime = None
    name = match.group("name")
    is_dir = match.group("size") == "<DIR>"
    return RemoteEntry(name, join(directory, name), EntryKind.DIR if is_dir else EntryKind.FILE,
                       size=None if is_dir else int(match.group("size")), mtime=mtime)


def _parse_netware(line: str, directory: str, now: float) -> Optional[RemoteEntry]:
    match = _NETWARE.match(line)
    if match is None:
        return None
    name = match.group("name")
    is_dir = match.group("type") == "d"
    return RemoteEntry(name, join(directory, name), EntryKind.DIR if is_dir else EntryKind.FILE,
                       size=int(match.group("size")),
                       mtime=_infer_time(match.group("month"), match.group("day"), match.group("when"), now),
                       perms=match.group("perms"), owner=match.group("owner"))


def _parse_vms(line: str, directory: str) -> Optional[RemoteEntry]:
    match = _VMS.match(line)
    if match is None:
        return None
    name = match.group("name")
    is_dir = name.upper().endswith(".DIR")
    if is_dir:
        name = name[:-4]
    return RemoteEntry(name, join(directory, name), EntryKind.DIR if is_dir else EntryKind.FILE)
