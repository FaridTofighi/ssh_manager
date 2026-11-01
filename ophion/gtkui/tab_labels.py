"""Session tab labels: the label policy, as pure functions (no GTK).

A tab shows what identifies its session:

* ``name`` (default): the host alias, or the IP / host name when the host has
  no alias;
* ``ip``: the IP / host name, ``ip:port`` when the port is not 22 (IPv6
  literals become ``[addr]:port``);
* ``name_ip``: ``alias · ip`` (the IP part as in ``ip`` mode).

A name the user gave the tab (Rename, F2) always wins and is shown as typed.

When two open tabs would show the same automatic label, later ones get a
counter: ``web-01``, ``web-01 (2)``, ``web-01 (3)``. Counters are per label and
**stable**: a tab keeps its counter while it is open (closing ``web-01`` does
not rename ``web-01 (2)``), and a freed counter is reused by the next tab that
needs one. The caller keeps the counters between calls (``assigned``).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, Hashable, List, Mapping, Optional, Sequence, Tuple


class TabLabelMode(str, Enum):
    NAME = "name"
    IP = "ip"
    NAME_IP = "name_ip"

    @classmethod
    def coerce(cls, value: object) -> "TabLabelMode":
        raw = value.value if isinstance(value, Enum) else value
        try:
            return cls(str(raw).strip().lower())
        except ValueError:
            return cls.NAME


DEFAULT_PORT = 22
NAME_IP_SEPARATOR = " · "


@dataclass(frozen=True)
class SessionLabelInfo:
    """What the label policy needs to know about one open session."""

    key: Hashable  # identifies the session (the tab object, an id, …)
    alias: str = ""
    hostname: str = ""
    port: int = DEFAULT_PORT
    custom_name: str = ""  # from Rename; overrides everything


def address_text(hostname: str, port: int = DEFAULT_PORT) -> str:
    """``10.0.0.5``, ``10.0.0.5:2222``, ``[fe80::1]:2222`` (port only when not 22)."""

    host = (hostname or "").strip()
    if not port or int(port) == DEFAULT_PORT:
        return host
    if ":" in host and not host.startswith("["):  # IPv6 literal
        host = f"[{host}]"
    return f"{host}:{int(port)}"


def base_label(info: SessionLabelInfo, mode: object = TabLabelMode.NAME) -> str:
    """The automatic label of one session, before disambiguation."""

    mode = TabLabelMode.coerce(mode)
    alias = (info.alias or "").strip()
    hostname = (info.hostname or "").strip()
    address = address_text(hostname or alias, info.port)
    if mode is TabLabelMode.IP:
        return address or alias
    if mode is TabLabelMode.NAME_IP:
        if alias and address and alias != address and alias != hostname:
            return f"{alias}{NAME_IP_SEPARATOR}{address}"
        return alias or address
    return alias or address


def session_tab_labels(
    sessions: Sequence[SessionLabelInfo],
    mode: object = TabLabelMode.NAME,
    assigned: Optional[Mapping[Hashable, int]] = None,
) -> Tuple[List[str], Dict[Hashable, int]]:
    """Labels for ``sessions`` (in the given order) and the counters to keep.

    ``assigned`` maps session keys to the counter they had on the previous call
    (``1`` = no suffix). Pass the returned mapping back next time so counters
    stay stable. Sessions should be given in the order they were opened, so a
    new duplicate never takes the plain label from an older tab.
    """

    previous = dict(assigned or {})
    bases = [base_label(info, mode) for info in sessions]
    counters: Dict[Hashable, int] = {}
    used: Dict[str, set] = {}

    # Keep the counters of tabs that already had one (and still need it).
    for info, base in zip(sessions, bases):
        if info.custom_name:
            continue
        counter = previous.get(info.key)
        taken = used.setdefault(base, set())
        if counter and counter not in taken:
            counters[info.key] = counter
            taken.add(counter)
    # New tabs (or tabs whose counter clashes) take the smallest free counter.
    for info, base in zip(sessions, bases):
        if info.custom_name or info.key in counters:
            continue
        taken = used.setdefault(base, set())
        counter = 1
        while counter in taken:
            counter += 1
        counters[info.key] = counter
        taken.add(counter)

    labels = []
    for info, base in zip(sessions, bases):
        if info.custom_name:
            labels.append(info.custom_name)
            continue
        counter = counters[info.key]
        labels.append(base if counter <= 1 else f"{base} ({counter})")
    return labels, counters


def is_duplicate(counter: int) -> bool:
    """Whether a session with this counter duplicates an already open one."""

    return counter > 1


def pane_labels(label: str, count: int) -> List[str]:
    """Labels for the panes of one tab: ``web-01 · pane 1``, ``web-01 · pane 2`` (one pane: the label)."""

    if count <= 1:
        return [label]
    return [f"{label}{NAME_IP_SEPARATOR}pane {index}" for index in range(1, count + 1)]
