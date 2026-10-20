"""Build the right backend for a host's protocol (no GTK)."""

from __future__ import annotations

from typing import Any, Callable, Optional

from .backend import RemoteFileBackend
from .pins import PinStore
from .settings import TransferSettings


def make_backend(host: Any, user: str, password: str, *, host_key_policy: str = "reject",
                 pins: Optional[PinStore] = None, passphrase: str = "",
                 log: Optional[Callable[[str], None]] = None,
                 settings: Optional[TransferSettings] = None) -> RemoteFileBackend:
    """A not-yet-connected backend for an ``ftp`` / ``ftps`` / ``sftp`` host."""

    protocol = (getattr(host, "protocol", "") or "").lower()
    settings = settings or TransferSettings.for_host(host)[0]
    if protocol in ("ftp", "ftps"):
        from .ftp import backend_for

        return backend_for(host, user, password, settings=settings, pins=pins, log=log)
    if protocol == "sftp":
        from .sftp import backend_for as sftp_backend_for

        return sftp_backend_for(host, user, password, settings=settings, host_key_policy=host_key_policy,
                                passphrase=passphrase, log=log)
    raise ValueError(f"'{protocol}' is not a file-transfer protocol")
