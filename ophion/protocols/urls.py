"""``ssh://``, ``sftp://``, ``ftp://``, ``ftps://`` and ``rdp://`` connection URLs (pure Python).

``rdp://`` accepts ``DOMAIN\\user`` written as ``DOMAIN%5Cuser``.

Quick Connect accepts them next to ``[user@]host[:port]``. A password in the
URL (``ftp://user:secret@host``) is refused, never used: it would end up in
the tab title, the history and the audit log. The program asks for it instead.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Optional, Tuple
from urllib.parse import unquote

from . import default_port

SCHEMES = ("ssh", "sftp", "ftp", "ftps", "rdp")
_URL = re.compile(r"^(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*)://(?P<rest>.*)$", re.DOTALL)
_BAD = re.compile(r"[\s\x00-\x1f\x7f\"'`\\]")


@dataclass(frozen=True)
class ConnectionUrl:
    protocol: str  # ssh, sftp, ftp, ftps
    host: str
    user: str = ""
    port: Optional[int] = None  # None: the protocol's default
    path: str = ""  # the remote folder to open first (file transfer only); "" = home

    @property
    def effective_port(self) -> int:
        return self.port or default_port(self.protocol)

    def label(self) -> str:
        """``protocol://[user@]host[:port][/path]`` (no password, ever)."""

        host = f"[{self.host}]" if ":" in self.host else self.host
        text = f"{self.protocol}://" + (f"{self.user}@" if self.user else "") + host
        if self.port:
            text += f":{self.port}"
        return text + (self.path if self.path not in ("", "/") else "")

    def options(self) -> Dict[str, str]:
        options = {"hostname": self.host}
        if self.user:
            options["user"] = self.user
        if self.port:
            options["port"] = str(self.port)
        return options


def looks_like_url(text: str) -> bool:
    return bool(_URL.match((text or "").strip()))


def parse_connection_url(text: str) -> Tuple[Optional[ConnectionUrl], str]:
    """``(url, "")`` or ``(None, reason)``; the reason is a sentence for the user (English, translate it)."""

    match = _URL.match((text or "").strip())
    if not match:
        return None, "Not a URL."
    scheme = match.group("scheme").lower()
    if scheme not in SCHEMES:
        return None, "Use ssh://, sftp://, ftp://, ftps:// or rdp://."
    rest = match.group("rest")
    authority, slash, path = rest.partition("/")
    path = slash + path
    if "?" in authority or "#" in authority or "?" in path or "#" in path:
        return None, "The address must not contain ? or #."
    user = ""
    if "@" in authority:
        userinfo, authority = authority.rsplit("@", 1)
        if ":" in userinfo:
            return None, "Do not put a password in the address; you will be asked for it."
        user = unquote(userinfo)
        checked = user.replace("\\", "", 1) if scheme == "rdp" else user  # rdp://DOMAIN%5Cuser@host
        if not user or _BAD.search(checked) or user.startswith("-"):
            return None, "The user name is not valid."
    port_text = ""
    if authority.startswith("["):
        end = authority.find("]")
        if end == -1:
            return None, "The host name is not valid."
        host, tail = authority[1:end], authority[end + 1:]
        if tail.startswith(":"):
            port_text = tail[1:]
        elif tail:
            return None, "The host name is not valid."
    elif authority.count(":") == 1:
        host, port_text = authority.split(":", 1)
    else:
        host = authority
    if not host or host.startswith("-") or _BAD.search(host) or "/" in host:
        return None, "The host name is not valid."
    port: Optional[int] = None
    if port_text:
        if not port_text.isdigit() or not 1 <= int(port_text) <= 65535:
            return None, "The port must be a number from 1 to 65535."
        port = int(port_text)
    path = unquote(path) if path else ""
    if path and any(ord(ch) < 32 or ord(ch) == 127 for ch in path):
        return None, "The folder is not valid."
    if scheme in ("ssh", "rdp"):
        path = ""  # a terminal or a desktop has no "folder to open"
    return ConnectionUrl(scheme, host, user, port, path), ""


def cli_rdp_target(target: str, user: str = "", domain: str = "") -> Tuple[Optional[ConnectionUrl], str]:
    """``--rdp host[:port] [--user U] [--domain D]`` or ``rdp://…``: ``(url, reason)`` (never a password)."""

    text = (target or "").strip()
    if not text.lower().startswith("rdp://"):
        if "://" in text:
            return None, "Use --rdp host[:port] or an rdp:// address."
        text = "rdp://" + text
    url, reason = parse_connection_url(text)
    if url is None:
        return None, reason
    if url.protocol != "rdp":
        return None, "Use --rdp host[:port] or an rdp:// address."
    user = (user or "").strip()
    domain = (domain or "").strip()
    if any(ord(ch) < 32 for ch in user + domain) or ":" in user:
        return None, "The user or domain is not valid (a password is never given on the command line)."
    if user:
        url = ConnectionUrl(url.protocol, url.host, user, url.port, url.path)
    if domain and "\\" not in url.user and "@" not in url.user:
        url = ConnectionUrl(url.protocol, url.host, f"{domain}\\{url.user}" if url.user else "", url.port,
                            url.path)
    return url, ""
