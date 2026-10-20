"""Connection protocols: what kinds of connections a host can be (pure Python, no GTK).

A :class:`ConnectionKind` describes one protocol: its id (stored in
``hosts.protocol``), labels, tree badge, icon, default port, capabilities,
setting defaults and validation. The GTK layer attaches an *opener* with
:func:`set_opener` (``opener(window, host) -> session widget or None``).

Adding a protocol later (RDP, VNC …) needs only a module that calls
:func:`register` (and :func:`set_opener` from its UI code); nothing else in the
core changes. Unknown ids found in the inventory (written by a newer version)
are kept and reported as "cannot open", never an exception.

Protocol-specific settings that are not OpenSSH options live in
``host.metadata["transfer"]`` (file transfer) — never in ``host.options``,
which become ``ssh -o`` arguments.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

DEFAULT_PROTOCOL = "ssh"

Opener = Callable[[Any, Any], Any]  # (window, host) -> session widget or None


@dataclass(frozen=True)
class ConnectionKind:
    """One connection protocol."""

    id: str
    label: str  # "SSH", "FTP (unencrypted)" …
    badge: str  # short tree / tab badge text: "SSH", "FTP", "FTPS", "SFTP"
    icon: str  # bundled icon name (without "-symbolic")
    default_port: int
    supports_credentials: bool = True
    supports_keys: bool = False
    supports_jump_host: bool = False
    terminal: bool = False  # opens a terminal (broadcast, split input, stats … apply)
    encrypted: bool = True
    family: str = ""  # "shell" | "transfer" (UI grouping)
    defaults: Callable[[], Dict[str, Any]] = field(default=dict, compare=False)

    def settings_defaults(self) -> Dict[str, Any]:
        return dict(self.defaults())

    def validate(self, host: Any) -> List[str]:
        """Problems that keep ``host`` from connecting with this protocol (messages for the user)."""

        problems: List[str] = []
        options = getattr(host, "options", {}) or {}
        lowered = {str(key).lower(): value for key, value in options.items()}
        hostname = str(lowered.get("hostname") or lowered.get("host") or "").strip()
        if not hostname and self.id != "ssh":  # ssh may connect to the alias itself
            problems.append(f"{self.label} needs a host name.")
        port = str(lowered.get("port") or "").strip()
        if port and (not port.isdigit() or not 1 <= int(port) <= 65535):
            problems.append("The port must be a number between 1 and 65535.")
        return problems


_KINDS: Dict[str, ConnectionKind] = {}
_OPENERS: Dict[str, Opener] = {}


def register(kind: ConnectionKind) -> ConnectionKind:
    _KINDS[kind.id] = kind
    return kind


def get(protocol_id: Optional[str]) -> Optional[ConnectionKind]:
    return _KINDS.get((protocol_id or DEFAULT_PROTOCOL).strip().lower())


def all_kinds() -> List[ConnectionKind]:
    return list(_KINDS.values())


def protocol_of(host: Any) -> str:
    return (getattr(host, "protocol", "") or DEFAULT_PROTOCOL).strip().lower()


def kind_for(host: Any) -> Optional[ConnectionKind]:
    """The host's protocol, or ``None`` for an id this version does not know."""

    return get(protocol_of(host))


def is_ssh(host: Any) -> bool:
    return protocol_of(host) == DEFAULT_PROTOCOL


def default_port(protocol_id: Optional[str], fallback: int = 22) -> int:
    kind = get(protocol_id)
    return kind.default_port if kind else fallback


def badge_for(host: Any) -> str:
    """Tree / tab badge: "" for SSH (the default stays unmarked), the short name otherwise."""

    protocol = protocol_of(host)
    if protocol == DEFAULT_PROTOCOL:
        return ""
    kind = get(protocol)
    return kind.badge if kind else protocol.upper()[:5]


def set_opener(protocol_id: str, opener: Optional[Opener]) -> None:
    """Attach (or with ``None`` detach) the UI opener of a protocol."""

    if opener is None:
        _OPENERS.pop(protocol_id, None)
    else:
        _OPENERS[protocol_id] = opener


def opener_for(protocol_id: Optional[str]) -> Optional[Opener]:
    return _OPENERS.get((protocol_id or DEFAULT_PROTOCOL).strip().lower())


def can_open(host: Any) -> bool:
    return kind_for(host) is not None and opener_for(protocol_of(host)) is not None


# ------------------------------------------------------------------ built-ins
SSH = register(ConnectionKind(
    "ssh", "SSH", "SSH", "sshm-server", 22,
    supports_keys=True, supports_jump_host=True, terminal=True, family="shell",
))
FTP = register(ConnectionKind(
    "ftp", "FTP (unencrypted)", "FTP", "sshm-transfer", 21, encrypted=False, family="transfer",
))
FTPS = register(ConnectionKind(
    "ftps", "FTPS (FTP over TLS)", "FTPS", "sshm-transfer", 21, family="transfer",
))
SFTP = register(ConnectionKind(
    "sftp", "SFTP (SSH file transfer)", "SFTP", "sshm-transfer", 22,
    supports_keys=True, supports_jump_host=True, family="transfer",
))
