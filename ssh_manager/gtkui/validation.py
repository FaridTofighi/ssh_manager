"""Pure validation helpers used by forms for inline feedback."""

from __future__ import annotations

import ipaddress
import re
from typing import Callable, Optional

from .i18n import _

Validator = Callable[[str], Optional[str]]

_HOST_LABEL = re.compile(r"^(?!-)[A-Za-z0-9_-]{1,63}(?<!-)$")


def validate_required(value: str, field: str = "") -> Optional[str]:
    if value.strip():
        return None
    return _("{field} is required.").format(field=field) if field else _("This field is required.")


def validate_alias(value: str) -> Optional[str]:
    """SSH ``Host`` alias: required, no whitespace/control characters, not a comment."""

    text = value.strip()
    if not text:
        return _("Alias is required.")
    if text != value or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in value):
        return _("Alias must not contain spaces or control characters.")
    if text.startswith("#"):
        return _("Alias must not start with '#'.")
    return None


def validate_port(value: str, *, required: bool = False) -> Optional[str]:
    text = value.strip()
    if not text:
        return _("Port is required.") if required else None
    if not text.isdigit():
        return _("Port must be a number.")
    port = int(text)
    if not 1 <= port <= 65535:
        return _("Port must be between 1 and 65535.")
    return None


def validate_hostname(value: str, *, required: bool = False) -> Optional[str]:
    """Hostname, IPv4/IPv6 address, or an ssh_config token such as ``%h``."""

    text = value.strip()
    if not text:
        return _("Host name is required.") if required else None
    if any(ch.isspace() for ch in text):
        return _("Host name must not contain spaces.")
    if "%" in text:
        return None  # ssh_config tokens are expanded by ssh itself
    try:
        ipaddress.ip_address(text.strip("[]"))
        return None
    except ValueError:
        pass
    if len(text) > 253:
        return _("Host name is too long.")
    labels = text.rstrip(".").split(".")
    if not all(_HOST_LABEL.match(label) for label in labels):
        return _("Host name may only contain letters, digits, '-', '_' and dots.")
    return None


def validate_group_name(value: str) -> Optional[str]:
    text = value.strip()
    if not text:
        return _("Name is required.")
    if text in {".", ".."}:
        return _("Name cannot be '.' or '..'.")
    if "/" in text or "\\" in text:
        return _("Name must not contain path separators.")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", text):
        return _("Use letters, numbers, '-', '_' or '.'.")
    return None


def validate_child_path(value: str) -> Optional[str]:
    """Child group path: one or more ``/``-separated group names."""

    text = value.strip().strip("/")
    if not text:
        return _("Name is required.")
    for part in text.split("/"):
        error = validate_group_name(part)
        if error:
            return error
    return None
