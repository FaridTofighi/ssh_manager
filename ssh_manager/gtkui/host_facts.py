"""GTK-free facts shown on the host page (header stats, info bar, captions)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, List, Optional, Tuple
from urllib.parse import quote

from .. import protocols
from ..host_utils import host_option
from .constants import RowKind
from .i18n import _, ngettext

NEVER = _("Never")
UNKNOWN = "—"

# --------------------------------------------------------------- relative time
_MINUTE, _HOUR, _DAY = 60, 3600, 86400


def format_relative_time(timestamp: Optional[float], now: float) -> str:
    """``"just now"``, ``"5 minutes ago"``, ``"2 hours ago"``, ``"yesterday"``,
    ``"3 days ago"``, ``"4 weeks ago"`` … or ``"Never"`` for ``None``."""

    if timestamp is None or timestamp <= 0:
        return NEVER
    delta = max(0.0, now - timestamp)
    if delta < 45:
        return _("just now")
    if delta < _HOUR:
        minutes = max(1, int(round(delta / _MINUTE)))
        return ngettext("{n} minute ago", "{n} minutes ago", minutes).format(n=minutes)
    if delta < _DAY:
        hours = max(1, int(delta // _HOUR))
        return ngettext("{n} hour ago", "{n} hours ago", hours).format(n=hours)
    days = int(delta // _DAY)
    if days == 1:
        return _("yesterday")
    if days < 14:
        return ngettext("{n} day ago", "{n} days ago", days).format(n=days)
    if days < 60:
        weeks = days // 7
        return ngettext("{n} week ago", "{n} weeks ago", weeks).format(n=weeks)
    if days < 365:
        months = days // 30
        return ngettext("{n} month ago", "{n} months ago", months).format(n=months)
    years = days // 365
    return ngettext("{n} year ago", "{n} years ago", years).format(n=years)


# -------------------------------------------------------------------- latency
class LatencyLevel(str, Enum):
    GOOD = "good"  # < 100 ms
    WARN = "warn"  # < 300 ms
    BAD = "bad"  # slower, or timed out / unreachable
    UNKNOWN = "unknown"  # not measured (proxied host, probing off, pending)


def latency_level(ms: Optional[float], *, failed: bool = False) -> LatencyLevel:
    if failed:
        return LatencyLevel.BAD
    if ms is None:
        return LatencyLevel.UNKNOWN
    if ms < 100:
        return LatencyLevel.GOOD
    if ms < 300:
        return LatencyLevel.WARN
    return LatencyLevel.BAD


def format_latency(ms: Optional[float], *, failed: bool = False) -> str:
    if failed:
        return _("Timeout")
    if ms is None:
        return UNKNOWN
    return _("{ms} ms").format(ms=max(1, int(round(ms))))


# ----------------------------------------------------------------------- auth
class AuthKind(str, Enum):
    KEY = "key"
    PASSWORD = "password"
    VAULT = "vault"
    CREDENTIAL = "credential"
    AGENT = "agent"


@dataclass(frozen=True)
class AuthSummary:
    kind: AuthKind
    label: str  # info bar value, e.g. "SSH key"
    detail: str = ""  # e.g. "ed25519"
    caption: str = ""  # empty-state caption, e.g. "Using ed25519 authentication"


_KEY_TYPES = {
    "ssh-ed25519": "ed25519",
    "sk-ssh-ed25519@openssh.com": "ed25519-sk",
    "ssh-rsa": "RSA",
    "ssh-dss": "DSA",
    "ecdsa-sha2-nistp256": "ECDSA",
    "ecdsa-sha2-nistp384": "ECDSA",
    "ecdsa-sha2-nistp521": "ECDSA",
    "sk-ecdsa-sha2-nistp256@openssh.com": "ECDSA-sk",
}
_NAME_TYPES = (("ed25519", "ed25519"), ("ecdsa", "ECDSA"), ("rsa", "RSA"), ("dsa", "DSA"))


def key_type_for(identity_file: str, *, read_text: Optional[Callable[[Path], str]] = None) -> str:
    """Key algorithm of ``identity_file``: from its ``.pub`` file, else its name."""

    if not identity_file:
        return ""
    path = Path(identity_file).expanduser()
    public = path if path.name.endswith(".pub") else Path(f"{path}.pub")
    reader = read_text or (lambda p: p.read_text(encoding="utf-8", errors="replace"))
    try:
        algorithm = reader(public).split(None, 1)[0]
    except (OSError, IndexError):
        algorithm = ""
    if algorithm in _KEY_TYPES:
        return _KEY_TYPES[algorithm]
    name = path.name.lower()
    for token, label in _NAME_TYPES:
        if token in name:
            return label
    return ""


def auth_summary(
    host,
    *,
    credential_name: str = "",
    vault: bool = False,
    has_password: bool = False,
    key_type: Optional[str] = None,
    host_specific: bool = False,
) -> AuthSummary:
    """How a connection to ``host`` authenticates, in the order SSH Manager tries it.

    ``host_specific``: the bound credential is the host's own (private) password.
    """

    if vault:
        return AuthSummary(AuthKind.VAULT, _("Vault"), caption=_("Using vault credential"))
    if host_specific:
        return AuthSummary(AuthKind.CREDENTIAL, _("Host-specific password"),
                           caption=_("Using a password specific to this host"))
    if credential_name:
        return AuthSummary(
            AuthKind.CREDENTIAL,
            _("Credential: {name}").format(name=credential_name),
            credential_name,
            _("Using credential {name}").format(name=credential_name),
        )
    identity = host_option(host, "identityfile") or ""
    if identity:
        kind = key_type if key_type is not None else key_type_for(identity)
        caption = _("Using {type} authentication").format(type=kind) if kind else _("Using SSH key")
        return AuthSummary(AuthKind.KEY, _("SSH key"), kind, caption)
    if has_password:
        return AuthSummary(AuthKind.PASSWORD, _("Password"), caption=_("Using password authentication"))
    return AuthSummary(AuthKind.AGENT, _("Agent"), caption=_("Using SSH agent"))


# ------------------------------------------------------------ connection text
def _port(host) -> int:
    raw = host_option(host, "port") or ""
    return int(raw) if raw.isdigit() else protocols.default_port(protocols.protocol_of(host))


def address_text(host, user: str) -> str:
    """``user@hostname`` (the host header's monospace line)."""

    target = host_option(host, "hostname", "host") or host.alias
    return f"{user}@{target}" if user else target


def connection_text(host, user: str) -> str:
    """``user@hostname:port`` (the info bar's CONNECTION field)."""

    return f"{address_text(host, user)}:{_port(host)}"


def ssh_command_text(host, user: str) -> str:
    """``ssh user@host -p N`` for SSH hosts; ``ftp://user@host:port`` for other protocols (never a password)."""

    target = host_option(host, "hostname", "host") or host.alias
    if not protocols.is_ssh(host):
        scheme = protocols.protocol_of(host)
        port = _port(host)
        netloc = f"{quote(user, safe='')}@{target}" if user else target
        if port != protocols.default_port(scheme):
            netloc += f":{port}"
        return f"{scheme}://{netloc}"
    command = f"ssh {user}@{target}" if user else f"ssh {target}"
    port = _port(host)
    return f"{command} -p {port}" if port != 22 else command


def protocol_text(host) -> str:
    """The protocol's label for the details page ("SFTP (SSH file transfer)" …)."""

    kind = protocols.kind_for(host)
    return _(kind.label) if kind else protocols.protocol_of(host)


def proxy_text(host) -> str:
    jump = host_option(host, "proxyjump", "proxy_jump", "jump_host") or ""
    if jump:
        return jump
    if host_option(host, "proxycommand"):
        return _("ProxyCommand")
    return _("None")


def uses_proxy(host) -> bool:
    return bool(host_option(host, "proxyjump", "proxy_jump", "jump_host", "proxycommand"))


# ---------------------------------------------------------------- breadcrumbs
@dataclass(frozen=True)
class Crumb:
    label: str
    target: Tuple[str, ...] = ()  # () = all hosts; ("parent", p) ; ("child", p, c) ; ("host", p, c, alias)
    current: bool = False


_CONF_SUFFIX = re.compile(r"\.conf$", re.IGNORECASE)


def breadcrumb_for(kind: Optional[RowKind], parent: str = "", child: str = "", alias: str = "") -> List[Crumb]:
    """``Hosts › Production › Core services`` style trail for a selection."""

    crumbs = [Crumb(_("Hosts"), ())]
    if kind is None:
        return [Crumb(_("Hosts"), (), current=True)]
    if kind is RowKind.HOST:
        return crumbs + [Crumb(alias, ("host", parent, child, alias), current=True)]
    crumbs.append(Crumb(parent, ("parent", parent), current=kind is RowKind.PARENT))
    if kind is not RowKind.PARENT and child:
        parts = [p for p in child.split("/") if p]
        for index, part in enumerate(parts):
            last = index == len(parts) - 1
            crumbs.append(Crumb(part, ("child", parent, "/".join(parts[: index + 1])), current=last))
    return crumbs


# ------------------------------------------------------------------- activity
@dataclass
class ActivityEvent:
    timestamp: float
    level: str  # Level value: info / success / warning / error
    text: str


@dataclass
class HostActivity:
    last_connected: Optional[float] = None
    events: List[ActivityEvent] = field(default_factory=list)
