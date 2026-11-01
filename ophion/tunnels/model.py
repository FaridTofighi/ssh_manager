"""Port forwards of a host: the :class:`Forward` model, validation, metadata and ssh-option parsing (no GTK).

A host keeps its forwards in ``metadata["forwards"]`` (a list of dicts; keys
this version does not know are kept). Kinds:

* ``local`` — listen on this computer (``bind_host:bind_port``), connect to
  ``dest_host:dest_port`` *from the server* (``ssh -L``). ``dest_host`` is
  resolved on the server, so names of the server's network work;
* ``remote`` — the server listens (``bind_host:bind_port`` on the server) and
  connects back to ``dest_host:dest_port`` *from this computer* (``ssh -R``);
* ``dynamic`` — a SOCKS5 proxy on this computer; every connection goes out
  from the server (``ssh -D``).

Listeners bind to ``127.0.0.1`` unless ``allow_remote_bind`` is set
explicitly (then other machines can use the forward: a warning is shown).
"""

from __future__ import annotations

import ipaddress
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from ..gtkui.i18n import _
from ..gtkui.validation import validate_hostname

LOCAL, REMOTE, DYNAMIC = "local", "remote", "dynamic"
KINDS = (LOCAL, REMOTE, DYNAMIC)
LOOPBACK = "127.0.0.1"
OPTION_KINDS = {"localforward": LOCAL, "remoteforward": REMOTE, "dynamicforward": DYNAMIC}
_FIELDS = ("id", "name", "kind", "bind_host", "bind_port", "dest_host", "dest_port", "auto_start_with_session",
           "auto_start_with_app", "stop_with_last_session", "allow_remote_bind", "note")


def kind_label(kind: str) -> str:
    return {LOCAL: _("Local"), REMOTE: _("Remote"), DYNAMIC: _("Dynamic (SOCKS)")}.get(kind, kind)


def is_loopback(host: str) -> bool:
    text = (host or "").strip().strip("[]").lower()
    if text == "localhost":
        return True
    try:
        return ipaddress.ip_address(text).is_loopback
    except ValueError:
        return False


def _wildcard(host: str) -> bool:
    return (host or "").strip().strip("[]") in ("0.0.0.0", "::", "*", "")


def host_port(host: str, port: int) -> str:
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def _int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@dataclass
class Forward:
    name: str = ""
    kind: str = LOCAL
    bind_host: str = LOOPBACK
    bind_port: int = 0
    dest_host: str = ""
    dest_port: int = 0
    auto_start_with_session: bool = False
    auto_start_with_app: bool = False
    note: str = ""
    allow_remote_bind: bool = False  # the explicit "bind to all interfaces / another address" choice
    stop_with_last_session: Optional[bool] = None  # None: on for with-session forwards, off otherwise
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    extra: Dict[str, Any] = field(default_factory=dict)  # keys of newer versions, kept as they are

    def __post_init__(self) -> None:
        if self.stop_with_last_session is None:
            self.stop_with_last_session = bool(self.auto_start_with_session)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Forward":
        def text(key: str, default: str = "") -> str:
            value = data.get(key, default)
            return value if isinstance(value, str) else default

        stop = data.get("stop_with_last_session")
        forward = cls(name=text("name"), kind=text("kind", LOCAL), bind_host=text("bind_host", LOOPBACK) or LOOPBACK,
                      bind_port=_int(data.get("bind_port")), dest_host=text("dest_host"),
                      dest_port=_int(data.get("dest_port")),
                      auto_start_with_session=bool(data.get("auto_start_with_session", False)),
                      auto_start_with_app=bool(data.get("auto_start_with_app", False)), note=text("note"),
                      allow_remote_bind=bool(data.get("allow_remote_bind", False)),
                      stop_with_last_session=stop if isinstance(stop, bool) else None,
                      id=text("id") or uuid.uuid4().hex,
                      extra={k: v for k, v in data.items() if k not in _FIELDS})
        return forward

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = dict(self.extra)
        data.update(id=self.id, name=self.name, kind=self.kind, bind_host=self.bind_host, bind_port=self.bind_port,
                    dest_host=self.dest_host, dest_port=self.dest_port,
                    auto_start_with_session=self.auto_start_with_session,
                    auto_start_with_app=self.auto_start_with_app, stop_with_last_session=self.stop_with_last_session,
                    allow_remote_bind=self.allow_remote_bind, note=self.note)
        return data

    # ------------------------------------------------------------- display
    @property
    def listen_address(self) -> str:
        return host_port(self.bind_host or LOOPBACK, self.bind_port)

    @property
    def copy_address(self) -> str:
        return f"socks5://{self.listen_address}" if self.kind == DYNAMIC else self.listen_address

    @property
    def destination(self) -> str:
        return "" if self.kind == DYNAMIC else host_port(self.dest_host, self.dest_port)

    @property
    def title(self) -> str:
        return self.name or (f"{kind_label(self.kind)} {self.listen_address}")


def forwards_from_metadata(metadata: Optional[Mapping[str, Any]]) -> List[Forward]:
    """The host's forwards; anything that is not a forward is skipped (never an exception)."""

    items = metadata.get("forwards") if isinstance(metadata, Mapping) else None
    if not isinstance(items, list):
        return []
    forwards = []
    for item in items:
        if isinstance(item, Mapping) and item.get("kind", LOCAL) in KINDS:
            forwards.append(Forward.from_dict(item))
    return forwards


def set_forwards(metadata: Optional[Mapping[str, Any]], forwards: Sequence[Forward]) -> Dict[str, Any]:
    """A copy of ``metadata`` with these forwards (the key is dropped when there are none)."""

    updated = dict(metadata or {})
    if forwards:
        updated["forwards"] = [forward.to_dict() for forward in forwards]
    else:
        updated.pop("forwards", None)
    return updated


# -------------------------------------------------------------- validation
@dataclass
class Validation:
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _port_error(value: int, label: str) -> Optional[str]:
    if not isinstance(value, int) or not 1 <= value <= 65535:
        return _("{field} must be between 1 and 65535.").format(field=label)
    return None


def _clashes(a: str, b: str) -> bool:
    a, b = (a or LOOPBACK).strip("[]").lower(), (b or LOOPBACK).strip("[]").lower()
    return a == b or _wildcard(a) or _wildcard(b) or (a == "localhost" and is_loopback(b)) or \
        (b == "localhost" and is_loopback(a))


def validate(forward: Forward, *, host_id: str, others: Iterable[Tuple[str, Forward]] = ()) -> Validation:
    """``others``: ``(host id, forward)`` of the inventory (the forward itself is skipped by id)."""

    result = Validation()
    if forward.kind not in KINDS:
        result.errors.append(_("Choose Local, Remote or Dynamic."))
        return result
    error = _port_error(forward.bind_port, _("The listening port"))
    if error:
        result.errors.append(error)
    elif forward.bind_port < 1024:
        where = _("on the server") if forward.kind == REMOTE else _("on this computer")
        result.warnings.append(_("Ports below 1024 need administrator (root) rights {where}.").format(where=where))
    bind_error = validate_hostname(forward.bind_host or LOOPBACK, required=True)
    if bind_error:
        result.errors.append(_("Listening address: {error}").format(error=bind_error))
    elif not is_loopback(forward.bind_host or LOOPBACK):
        if not forward.allow_remote_bind:
            result.errors.append(_("Listening on {address} lets other computers use this forward. Turn on "
                                   "\"Bind to all interfaces\" to allow it.").format(address=forward.bind_host))
        else:
            result.warnings.append(_("Other computers on the network can use this forward (it has no password)."))
    if forward.kind != DYNAMIC:
        dest_error = validate_hostname(forward.dest_host, required=True)
        if dest_error:
            result.errors.append(_("Destination: {error}").format(error=dest_error))
        error = _port_error(forward.dest_port, _("The destination port"))
        if error:
            result.errors.append(error)
    for other_host, other in others:
        if other.id == forward.id or other.bind_port != forward.bind_port:
            continue
        if not _clashes(other.bind_host, forward.bind_host):
            continue
        if forward.kind == REMOTE:
            if other.kind == REMOTE and other_host == host_id:
                result.errors.append(_("Another remote forward of this host already listens on {address}.").format(
                    address=forward.listen_address))
                break
        elif other.kind in (LOCAL, DYNAMIC):
            result.errors.append(_("{address} is already used by the forward ‘{name}’.").format(
                address=forward.listen_address, name=other.title))
            break
    return result


# ------------------------------------------------- ssh's *Forward options
_ENDPOINT = re.compile(r"^(?:\[(?P<v6>[^\]]+)\]|(?P<host>[^:\s\[\]]+)):(?P<port>\d+)$")


def _split_endpoint(text: str) -> Optional[Tuple[str, int]]:
    match = _ENDPOINT.match(text)
    if not match:
        return None
    return (match.group("v6") or match.group("host"), int(match.group("port")))


def _listen(text: str) -> Optional[Tuple[str, int]]:
    if text.isdigit():
        return LOOPBACK, int(text)
    return _split_endpoint(text)


def parse_ssh_forward(key: str, value: str) -> Optional[Forward]:
    """``LocalForward 8080 db:5432`` & co. as a :class:`Forward`; ``None`` when it cannot be read (left alone)."""

    kind = OPTION_KINDS.get((key or "").lower())
    parts = str(value or "").split()
    if kind is None or not parts:
        return None
    listen = _listen(parts[0])
    if listen is None or not 1 <= listen[1] <= 65535:
        return None
    bind_host, bind_port = listen
    if kind == DYNAMIC:
        if len(parts) != 1:
            return None
        return Forward(name="", kind=DYNAMIC, bind_host=bind_host, bind_port=bind_port,
                       allow_remote_bind=not is_loopback(bind_host))
    if len(parts) != 2:
        return None
    dest = _split_endpoint(parts[1])
    if dest is None or not 1 <= dest[1] <= 65535:
        return None
    return Forward(name="", kind=kind, bind_host=bind_host, bind_port=bind_port, dest_host=dest[0],
                   dest_port=dest[1], allow_remote_bind=not is_loopback(bind_host))


def raw_forward_options(options: Mapping[str, Any]) -> List[Tuple[str, str, Optional[Forward]]]:
    """The host's own ``*Forward`` ssh options (``(key, value, parsed or None)``), in their order."""

    found = []
    for key, value in (options or {}).items():
        if str(key).lower() not in OPTION_KINDS:
            continue
        for item in value if isinstance(value, (list, tuple)) else [value]:
            text = str(item)
            found.append((str(key), text, parse_ssh_forward(str(key), text)))
    return found


def inventory_forwards(hosts: Iterable[Any], key) -> List[Tuple[str, Forward]]:
    """``(key(host), forward)`` for every forward of these hosts (validation of duplicates)."""

    return [(key(host), forward) for host in hosts for forward in forwards_from_metadata(getattr(host, "metadata", {}))]


__all__ = ["Forward", "Validation", "LOCAL", "REMOTE", "DYNAMIC", "KINDS", "LOOPBACK", "kind_label", "is_loopback",
           "forwards_from_metadata", "set_forwards", "validate", "parse_ssh_forward", "raw_forward_options",
           "inventory_forwards", "host_port"]
