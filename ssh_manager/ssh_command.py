"""Helper utilities for constructing SSH command arguments."""

from __future__ import annotations

from typing import Any, List, Mapping, Optional

from .config import HostEntry
from .legacy_vault import LEGACY_METADATA_KEYS


_CONNECTION_OPTIONS = {
    "hostname",
    "host",
    "user",
    "port",
    "identityfile",
    "proxyjump",
    "proxy_jump",
    "jump_host",
    "proxycommand",
    "proxy_command",
}

_APP_ONLY_OPTIONS = {
    "tags",
    "tag",
    "environment",
    "owner",
    "service",
    "notes",
    *LEGACY_METADATA_KEYS,
    "credential_id",
    "default_session_profile_id",
    "session_profile_id",
    "permissions",
}

_SENSITIVE_APP_OPTIONS = {
    "password",
    "plain_password",
    "plaintext_password",
    "plain_text_password",
    "ssh_password",
    "sshpass",
    "passphrase",
}

_EXCLUDED_OPTIONS = _CONNECTION_OPTIONS | _APP_ONLY_OPTIONS | _SENSITIVE_APP_OPTIONS


def _option_key(key: Any) -> str:
    return str(key).strip()


def _option_value(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, bool):
        return "yes" if value else "no"
    text = str(value).strip()
    return text or None


def _lookup_option(options: Mapping[str, Any], *names: str) -> Optional[str]:
    wanted = {name.lower() for name in names}
    for key, value in options.items():
        if _option_key(key).lower() in wanted:
            return _option_value(value)
    return None


def _valid_port(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    try:
        port = int(value)
    except ValueError:
        return None
    if 1 <= port <= 65535:
        return str(port)
    return None


def build_connection_option_args(options: Mapping[str, Any], *, port_flag: str = "-p") -> List[str]:
    """Return SSH-compatible option argv entries for a host option mapping."""

    command: List[str] = []

    port = _valid_port(_lookup_option(options, "port"))
    if port:
        command.extend([port_flag, port])

    identity = _lookup_option(options, "identityfile")
    if identity:
        command.extend(["-i", identity])

    proxy_command = _lookup_option(options, "proxycommand", "proxy_command")
    if proxy_command:
        command.extend(["-o", f"ProxyCommand={proxy_command}"])
    else:
        proxy_jump = _lookup_option(options, "proxyjump", "proxy_jump", "jump_host")
        if proxy_jump:
            command.extend(["-J", proxy_jump])

    for key, value in options.items():
        option_name = _option_key(key)
        if not option_name:
            continue
        lowered = option_name.lower()
        if lowered in _EXCLUDED_OPTIONS:
            continue
        option_value = _option_value(value)
        if option_value is None:
            continue
        command.extend(["-o", f"{option_name}={option_value}"])

    return command


def build_ssh_command(host: HostEntry) -> List[str]:
    """Return a command argv list for launching `ssh` for the given host."""

    protocol = (getattr(host, "protocol", "ssh") or "ssh")
    if protocol != "ssh":
        raise ValueError(f"'{host.alias}' is a {protocol} host, not an SSH host.")
    options = host.options
    command: List[str] = ["ssh", *build_connection_option_args(options, port_flag="-p")]

    hostname = _lookup_option(options, "hostname", "host") or host.alias
    user = _lookup_option(options, "user")
    if user:
        command.append(f"{user}@{hostname}")
    else:
        command.append(hostname)

    return command
