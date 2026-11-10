"""Is the server's host key trusted before a password session starts? (no GTK)

``sshpass -e ssh`` cannot answer ssh's first-connection question: when the
key is not in known_hosts yet, sshpass stops at "Are you sure you want to
continue connecting" and exits with status 6, without output. So before a
password session the app checks the key the way ssh will:

* :func:`check` asks ``ssh -G`` (with the session's own arguments) for the
  effective host name, port, ``HostKeyAlias``, known_hosts files,
  ``StrictHostKeyChecking``, ``HashKnownHosts`` and proxy, and looks the host
  up with :class:`~ophion.known_hosts.KnownHostsFile` (hashed entries and
  ``[host]:port`` included). Known → the session starts as before (a changed
  key is still ssh's to refuse). Unknown → the server's key is read with a key
  exchange only (:func:`~ophion.known_hosts.probe_host_key`: no login, no
  credential, short timeout) so the user can be asked.
* The user's own ``StrictHostKeyChecking`` (yes / no / accept-new) and hosts
  behind a jump host or ``ProxyCommand`` are left to ssh exactly as before.
* :func:`trust` appends the key the user accepted to the first known_hosts
  file ssh reads (hashed when ssh would hash it; 0600, folder 0700).

Nothing here accepts a key by itself.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .i18n import _

KNOWN, UNKNOWN, SKIP, PROXIED, UNREADABLE = "known", "unknown", "skip", "proxied", "unreadable"
PROBE_TIMEOUT_S = 5.0


def untrusted_text() -> str:
    """The one message of background commands (stats, Run Command, …) for a host key not trusted yet."""

    return _("Host key not trusted yet — open the session once to accept it.")


@dataclass
class Target:
    name: str  # the name ssh looks up (HostKeyAlias, else the host name)
    port: int  # 22 when HostKeyAlias is used (ssh looks the alias up without a port)
    hostname: str  # where the key is read from
    connect_port: int
    files: List[Path] = field(default_factory=list)  # UserKnownHostsFile then GlobalKnownHostsFile
    hashed: bool = False
    strict: str = "ask"
    proxied: bool = False


@dataclass
class Preflight:
    state: str
    target: Optional[Target] = None
    key_type: str = ""
    key_b64: str = ""
    error: str = ""

    @property
    def fingerprint(self) -> str:
        from .known_hosts import fingerprint_sha256

        return fingerprint_sha256(self.key_b64) if self.key_b64 else ""


def ssh_config(ssh_argv: Sequence[str]) -> Dict[str, str]:
    """``ssh -G`` for the session's own arguments (``ssh`` first, the destination last)."""

    program = shutil.which("ssh")
    if program is None or len(ssh_argv) < 2:
        return {}
    done = subprocess.run([program, "-G", *ssh_argv[1:]], capture_output=True, text=True, timeout=10,
                          stdin=subprocess.DEVNULL)
    values: Dict[str, str] = {}
    for line in done.stdout.splitlines():
        key, _sep, value = line.partition(" ")
        values.setdefault(key.lower(), value.strip())
    return values


def target_from(values: Dict[str, str]) -> Optional[Target]:
    hostname = values.get("hostname", "")
    if not hostname:
        return None
    port = int(values.get("port", "22") or 22)
    alias = values.get("hostkeyalias", "")
    if alias.lower() == "none":
        alias = ""
    files = []
    for key in ("userknownhostsfile", "globalknownhostsfile"):
        for item in values.get(key, "").split():
            if item.lower() != "none":
                files.append(Path(os.path.expanduser(item)))
    proxy = any(values.get(key, "none").lower() not in ("", "none") for key in ("proxyjump", "proxycommand"))
    return Target(name=alias or hostname, port=22 if alias else port, hostname=hostname, connect_port=port,
                  files=files, hashed=values.get("hashknownhosts", "no").lower() == "yes",
                  strict=values.get("stricthostkeychecking", "ask").lower(), proxied=proxy)


def is_known(target: Target) -> bool:
    """known_hosts holds an entry for the host (any key: a different one is ssh's "changed" case)."""

    from .known_hosts import KnownHostsError, KnownHostsFile

    for path in target.files:
        if not path.is_file():
            continue
        try:
            if any(not entry.marker for entry in KnownHostsFile(path).lookup(target.name, target.port)):
                return True
        except (OSError, KnownHostsError):
            continue
    return False


def check(ssh_argv: Sequence[str], *, probe: Optional[Callable[..., Tuple[str, str]]] = None) -> Preflight:
    """Run in a worker thread (``ssh -G`` and the key exchange may take a moment)."""

    from .known_hosts import probe_host_key

    try:
        target = target_from(ssh_config(ssh_argv))
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return Preflight(SKIP, error=type(exc).__name__)  # ssh then behaves as it always did
    if target is None or target.strict not in ("ask",):
        return Preflight(SKIP, target)  # the user's own StrictHostKeyChecking: ssh decides
    if is_known(target):
        return Preflight(KNOWN, target)
    if target.proxied:
        return Preflight(PROXIED, target)  # the key cannot be read past a jump host / ProxyCommand
    try:
        key_type, key_b64 = (probe or probe_host_key)(target.hostname, target.connect_port, timeout=PROBE_TIMEOUT_S)
    except Exception as exc:  # unreachable, refused, not SSH: say so, accept nothing
        return Preflight(UNREADABLE, target, error=str(exc) or type(exc).__name__)
    return Preflight(UNKNOWN, target, key_type=key_type, key_b64=key_b64)


def trust(result: Preflight) -> Optional[Path]:
    """Append the accepted key to the first known_hosts file ssh reads; returns that file."""

    from .known_hosts import KnownHostsFile

    target = result.target
    if result.state != UNKNOWN or target is None or not target.files or not result.key_b64:
        return None
    path = target.files[0]
    KnownHostsFile(path).add_entry(target.name, target.port, result.key_type, result.key_b64, hashed=target.hashed,
                                   backup=False)
    return path


__all__ = ["check", "trust", "is_known", "target_from", "ssh_config", "untrusted_text", "Preflight", "Target",
           "KNOWN", "UNKNOWN", "SKIP", "PROXIED", "UNREADABLE"]
