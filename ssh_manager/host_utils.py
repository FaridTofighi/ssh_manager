"""Shared helpers for interpreting HostEntry connection identity."""

from __future__ import annotations

import getpass
import copy
from typing import Any, List, Optional, Tuple

from .config import HostEntry
from .ssh_command import build_connection_option_args


def host_option(host: HostEntry, *names: str) -> Optional[str]:
    """Return a host option value using case-insensitive key lookup."""

    return host.option(*names)


def _port_number(value: Any) -> int:
    try:
        port = int(str(value).strip())
    except (TypeError, ValueError):
        return 22
    return port if 1 <= port <= 65535 else 22


def host_connection_params(host: HostEntry, *, default_user: str | None = None) -> Tuple[str, int, str]:
    """Return hostname, port, and username for a host entry."""

    hostname = host_option(host, "hostname", "host") or host.alias
    username = host_option(host, "user") or default_user or getpass.getuser()
    port = _port_number(host_option(host, "port"))
    return hostname, port, username


def host_config_key(host: HostEntry) -> str:
    """Return the stable UI binding key for a host entry."""

    return f"{host.parent_group}::{host.child_group}::{host.alias}"


def build_scp_command(host: HostEntry, remote_path: str = "/remote/path", local_path: str = "/local/path") -> List[str]:
    """Return an argv list for a simple SCP download command."""

    hostname = host_option(host, "hostname", "host") or host.alias
    user = host_option(host, "user")
    target = f"{hostname}:{remote_path}"
    if user:
        target = f"{user}@{target}"
    command = ["scp", *build_connection_option_args(host.options, port_flag="-P")]
    command.extend([target, local_path])
    return command


def with_user(host: HostEntry, username: str) -> HostEntry:
    """Return a copy of host with a resolved username."""

    options = {
        key: value
        for key, value in host.options.items()
        if str(key).strip().lower() != "user"
    }
    options["user"] = username
    return HostEntry(
        alias=host.alias,
        options=options,
        metadata=copy.deepcopy(host.metadata),
        parent_group=host.parent_group,
        child_group=host.child_group,
        source_file=host.source_file,
        id=host.id,  # credential bindings follow the stable id
    )
