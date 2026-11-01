"""Microsoft ``.rdp`` files: import as RDP hosts, export a host (pure Python; nothing is ever executed).

The format is one ``key:type:value`` per line (``s`` string, ``i`` integer,
``b`` binary as hex), in UTF-16 LE with a BOM (what mstsc writes) or UTF-8.

**Import** maps the keys SSH Manager understands to the host and its
:class:`RdpSettings` and reports everything else:

* ``password 51:b:`` blobs (DPAPI-encrypted, only usable by the Windows
  account that wrote them) are **never** imported — the report says so;
* keys with no counterpart are listed in the report; harmless ones (strings
  and integers that are not credentials) are kept in
  ``settings.extra["rdpfile"]`` and written back on export;
* ``drivestoredirect`` values are checked like shared folders
  (:mod:`ophion.rdp.drives`); Windows drive letters and ``*`` are not
  imported;
* ``authentication level:i:0`` ("connect without warning") never turns
  certificate checking off: it becomes "Ask the first time" with a note.

Strict parsing: at most :data:`MAX_BYTES` per file and :data:`MAX_LINE` per
line, control characters refused, unknown types ignored.

**Export** writes the host's address, user, display, devices, gateway and
programs — never a password — with ``prompt for credentials`` following the
host's "Ask for the password every time".
"""

from __future__ import annotations

import codecs
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .settings import (
    DEFAULT_PORT, Gateway, RdpSettings, RemoteApp, SharedFolder, split_rdp_user,
)

MAX_BYTES = 256 * 1024
MAX_LINE = 4096
_LINE = re.compile(r"^([^:]{1,64}):([sib]):(.*)$")
SECRET_KEYS = {"password 51", "password", "gatewaypassword", "gatewayaccesstoken", "clearpassword",
               "rdgiskdcproxy", "kdcproxyname", "signature", "signscope"}
CONNECTION_TYPES = {1: "modem", 2: "wan", 3: "wan", 4: "broadband", 5: "wan", 6: "lan", 7: "auto"}
PRESET_TO_TYPE = {"modem": 1, "broadband": 4, "wan": 5, "lan": 6, "auto": 7}
HANDLED = {
    "full address", "server port", "username", "domain", "screen mode id", "desktopwidth", "desktopheight",
    "session bpp", "smart sizing", "dynamic resolution", "redirectclipboard", "redirectprinters",
    "drivestoredirect", "audiomode", "audiocapturemode", "authentication level", "enablecredsspsupport",
    "prompt for credentials", "gatewayhostname", "gatewayusagemethod", "promptcredentialonce",
    "gatewaycredentialssource", "alternate shell", "shell working directory", "administrative session",
    "connect to console", "keyboardhook", "use multimon", "selectedmonitors", "networkautodetect",
    "connection type", "compression", "redirectsmartcards", "loadbalanceinfo", "remoteapplicationmode",
    "remoteapplicationprogram", "remoteapplicationname", "remoteapplicationcmdline", "disable wallpaper",
    "disable themes", "allow font smoothing", "disable menu anims", "disable full window drag",
    "allow desktop composition", "autoreconnection enabled", "autoreconnect max retries",
}
NOT_SUPPORTED = {"redirectcomports": "Serial ports are not redirected.",
                 "redirectposdevices": "Point-of-service devices are not redirected.",
                 "usbdevicestoredirect": "USB devices from a .rdp file are not imported; choose them in the host "
                                         "dialog.",
                 "camerastoredirect": "Cameras are not redirected.",
                 "devicestoredirect": "Plug-and-play devices are not redirected."}


@dataclass
class RdpFileImport:
    alias: str
    options: Dict[str, str]  # hostname / port / user, as host options
    settings: RdpSettings
    notes: List[str] = field(default_factory=list)  # what was not imported, and why (English)
    unknown: List[str] = field(default_factory=list)  # keys with no counterpart (names only)

    @property
    def hostname(self) -> str:
        return self.options.get("hostname", "")


class RdpFileError(ValueError):
    """The file cannot be read as a .rdp file (message for the user, English)."""


def decode(data: bytes) -> str:
    if len(data) > MAX_BYTES:
        raise RdpFileError("The file is too large to be a .rdp file.")
    if data.startswith(codecs.BOM_UTF16_LE) or data.startswith(codecs.BOM_UTF16_BE):
        return data.decode("utf-16")
    if data.startswith(codecs.BOM_UTF8):
        return data[3:].decode("utf-8", "strict")
    if len(data) > 1 and data[1:2] == b"\x00":  # UTF-16 LE without a BOM
        return data.decode("utf-16-le")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1252", "replace")


def parse_lines(text: str) -> Tuple[Dict[str, Tuple[str, str]], List[str]]:
    """``({key: (type, value)}, problems)``; keys lower-cased; the last occurrence wins."""

    values: Dict[str, Tuple[str, str]] = {}
    problems: List[str] = []
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip("﻿ \t")
        if not line:
            continue
        if len(line) > MAX_LINE:
            problems.append(f"Line {number} is too long and was skipped.")
            continue
        if any(ord(ch) < 32 and ch != "\t" or ord(ch) == 127 for ch in line):
            problems.append(f"Line {number} contains control characters and was skipped.")
            continue
        match = _LINE.match(line)
        if not match:
            problems.append(f"Line {number} is not key:type:value and was skipped.")
            continue
        key, kind, value = match.group(1).strip().lower(), match.group(2), match.group(3)
        values[key] = (kind, value)
    return values, problems


def _int(values: Dict[str, Tuple[str, str]], key: str) -> Optional[int]:
    item = values.get(key)
    if item is None or item[0] != "i":
        return None
    try:
        return int(item[1].strip())
    except ValueError:
        return None


def _str(values: Dict[str, Tuple[str, str]], key: str) -> str:
    item = values.get(key)
    return item[1].strip() if item is not None and item[0] == "s" else ""


def _split_address(text: str) -> Tuple[str, Optional[int]]:
    text = text.strip()
    if text.startswith("["):
        host, _sep, rest = text[1:].partition("]")
        return host, int(rest[1:]) if rest.startswith(":") and rest[1:].isdigit() else None
    if text.count(":") == 1:
        host, port = text.split(":")
        return host, int(port) if port.isdigit() else None
    return text, None


def import_rdp(data: bytes, name: str = "") -> RdpFileImport:
    """One host from a .rdp file (raises :class:`RdpFileError` when it is not one)."""

    text = decode(data)
    values, problems = parse_lines(text)
    address = _str(values, "full address")
    if not address:
        raise RdpFileError("The file has no 'full address'; it does not describe a connection.")
    host, port = _split_address(address)
    if not host or host.startswith("-") or any(ch.isspace() or ch in "\"'`\\/,;@" for ch in host):
        raise RdpFileError("The server address in the file is not valid.")
    port = _int(values, "server port") or port
    s = RdpSettings()
    notes: List[str] = list(problems)
    options: Dict[str, str] = {"hostname": host}
    if port and port != DEFAULT_PORT and 1 <= port <= 65535:
        options["port"] = str(port)
    user = _str(values, "username")
    if user and not split_rdp_user(user).problem:
        options["user"] = user
    elif user:
        notes.append("The user name was not usable and was left out.")
    domain = _str(values, "domain")
    if domain:
        s.domain = domain

    # display
    mode = _int(values, "screen mode id")
    width, height = _int(values, "desktopwidth"), _int(values, "desktopheight")
    if mode == 2:
        s.size_mode = "fullscreen"
    elif _int(values, "dynamic resolution") == 1:
        s.size_mode = "window"
    elif width and height:
        s.size_mode = "fixed"
    if width and height and 200 <= width <= 8192 and 200 <= height <= 8192:
        s.width, s.height = width, height
    bpp = _int(values, "session bpp")
    if bpp in (8, 15, 16, 24, 32):
        s.color_depth = bpp
    s.smart_sizing = _int(values, "smart sizing") == 1
    if _int(values, "use multimon") == 1:
        s.multimon = True
        monitors = _str(values, "selectedmonitors")
        s.monitors = [int(m) for m in monitors.split(",") if m.strip().isdigit() and int(m) < 64]

    # experience
    connection_type = _int(values, "connection type")
    if connection_type in CONNECTION_TYPES:
        s.apply_preset(CONNECTION_TYPES[connection_type])
    for key, attr, invert in (("disable wallpaper", "wallpaper", True), ("disable themes", "themes", True),
                              ("allow font smoothing", "font_smoothing", False),
                              ("disable menu anims", "menu_animations", True),
                              ("disable full window drag", "window_drag", True),
                              ("allow desktop composition", "aero", False)):
        value = _int(values, key)
        if value in (0, 1):
            setattr(s, attr, (value == 0) if invert else (value == 1))
            s.preset = "custom" if connection_type not in CONNECTION_TYPES else s.preset
    if _int(values, "compression") == 0:
        s.compression = False
    if _int(values, "networkautodetect") == 0:
        s.network_autodetect = False
    keyboard = _int(values, "keyboardhook")
    if keyboard is not None:
        s.grab_keyboard = keyboard != 0

    # devices
    clipboard = _int(values, "redirectclipboard")
    if clipboard is not None:
        s.clipboard = "both" if clipboard == 1 else "off"
    s.printers = _int(values, "redirectprinters") == 1
    s.smartcard = _int(values, "redirectsmartcards") == 1
    audio = _int(values, "audiomode")
    if audio in (0, 1, 2):
        s.audio_out = ("local", "remote", "off")[audio]
    s.audio_in = _int(values, "audiocapturemode") == 1
    drives = _str(values, "drivestoredirect")
    if drives:
        s.drives, drive_notes = _drives(drives)
        notes += drive_notes

    # security
    level = _int(values, "authentication level")
    if level == 0:
        notes.append("The file says to connect without checking the server; Ophion asks the first time "
                     "instead.")
        s.cert_policy = "ask"
    elif level == 1:
        s.cert_policy = "deny"
    if _int(values, "enablecredsspsupport") == 0:
        s.security = "tls"
    s.prompt_for_credentials = _int(values, "prompt for credentials") == 1
    if _int(values, "administrative session") == 1 or _int(values, "connect to console") == 1:
        s.admin_session = True
    if _int(values, "autoreconnection enabled") == 0:
        s.auto_reconnect = False
    retries = _int(values, "autoreconnect max retries")
    if retries is not None and 0 <= retries <= 1000:
        s.reconnect_max = retries

    # gateway
    gateway_host = _str(values, "gatewayhostname")
    usage = _int(values, "gatewayusagemethod")
    if gateway_host and usage in (1, 2, None):
        gw_host, gw_port = _split_address(gateway_host)
        s.gateway = Gateway(gw_host, gw_port or 443,
                            use_same_credentials=_int(values, "promptcredentialonce") != 0)

    # programs
    shell = _str(values, "alternate shell")
    if shell:
        s.initial_program = shell[:1024]
    folder = _str(values, "shell working directory")
    if folder:
        s.working_dir = folder[:1024]
    if _int(values, "remoteapplicationmode") == 1:
        s.remote_app = RemoteApp(_str(values, "remoteapplicationprogram")[:1024],
                                 _str(values, "remoteapplicationname")[:128],
                                 _str(values, "remoteapplicationcmdline")[:1024])
    balance = _str(values, "loadbalanceinfo")
    if balance and len(balance) <= 256:
        s.load_balance_info = balance

    unknown: List[str] = []
    preserved: Dict[str, Any] = {}
    for key, (kind, raw) in values.items():
        if key in HANDLED:
            continue
        if key in SECRET_KEYS or key.startswith("password"):
            notes.append(f"'{key}' holds a credential (encrypted for one Windows account); it was not imported.")
            continue
        if key in NOT_SUPPORTED:
            notes.append(NOT_SUPPORTED[key])
            continue
        unknown.append(key)
        if kind in ("s", "i") and len(raw) <= 512:
            preserved[key] = [kind, raw]
    if preserved:
        s.extra["rdpfile"] = preserved
    if unknown:
        notes.append(f"{len(unknown)} setting(s) have no counterpart and are kept for export: "
                     + ", ".join(sorted(unknown)[:12]) + ("…" if len(unknown) > 12 else ""))
    _fix_warnings = s._fix()
    notes += _fix_warnings
    alias = _alias(name, host)
    return RdpFileImport(alias, options, s, notes, unknown)


def _alias(name: str, host: str) -> str:
    base = (name or "").rsplit("/", 1)[-1]
    if base.lower().endswith(".rdp"):
        base = base[:-4]
    base = "".join(ch for ch in base if ch.isprintable() and ch not in "/").strip()
    return (base or host)[:128]


def _drives(value: str) -> Tuple[List[SharedFolder], List[str]]:
    """``drivestoredirect`` entries that are folders of this computer (checked like shared folders)."""

    from .drives import check_folder

    folders: List[SharedFolder] = []
    notes: List[str] = []
    for entry in value.split(";"):
        entry = entry.strip()
        if not entry:
            continue
        if entry in ("*", "DynamicDrives") or re.match(r"^[A-Za-z]:\\?$", entry):
            notes.append(f"Shared drive '{entry}' refers to Windows drives; choose folders in the host dialog.")
            continue
        check = check_folder(entry)
        if not check.ok:
            notes.append(f"Shared folder '{entry[:80]}' was not imported: {check.problem}")
            continue
        name = re.sub(r"[^A-Za-z0-9_.\-]", "_", check.path.rstrip("/").rsplit("/", 1)[-1])[:32] or "share"
        if any(f.name.casefold() == name.casefold() for f in folders):
            name = f"{name[:28]}{len(folders) + 1}"
        folders.append(SharedFolder(name, check.path))
    return folders, notes


# ------------------------------------------------------------------ export
def export_rdp(hostname: str, port: int, user: str, settings: RdpSettings) -> str:
    """A .rdp file for other clients (never a password)."""

    s = settings
    lines: List[str] = []

    def add(key: str, kind: str, value: Any) -> None:
        text = str(value)
        if any(ord(ch) < 32 for ch in text):
            return
        lines.append(f"{key}:{kind}:{text}")

    host = f"[{hostname}]" if ":" in hostname else hostname
    add("full address", "s", host if port == DEFAULT_PORT else f"{host}:{port}")
    if port != DEFAULT_PORT:
        add("server port", "i", port)
    if user:
        add("username", "s", user)
    if s.domain:
        add("domain", "s", s.domain)
    add("prompt for credentials", "i", 1 if s.prompt_for_credentials else 0)
    add("screen mode id", "i", 2 if s.size_mode == "fullscreen" else 1)
    add("desktopwidth", "i", s.width)
    add("desktopheight", "i", s.height)
    add("dynamic resolution", "i", 1 if s.size_mode == "window" else 0)
    add("smart sizing", "i", 1 if s.smart_sizing else 0)
    add("session bpp", "i", s.color_depth)
    if s.multimon:
        add("use multimon", "i", 1)
        if s.monitors:
            add("selectedmonitors", "s", ",".join(str(m) for m in s.monitors))
    if s.preset in PRESET_TO_TYPE:
        add("connection type", "i", PRESET_TO_TYPE[s.preset])
    add("networkautodetect", "i", 1 if s.network_autodetect else 0)
    add("compression", "i", 1 if s.compression else 0)
    add("disable wallpaper", "i", 0 if s.wallpaper else 1)
    add("disable themes", "i", 0 if s.themes else 1)
    add("allow font smoothing", "i", 1 if s.font_smoothing else 0)
    add("disable menu anims", "i", 0 if s.menu_animations else 1)
    add("disable full window drag", "i", 0 if s.window_drag else 1)
    add("allow desktop composition", "i", 1 if s.aero else 0)
    add("keyboardhook", "i", 1 if s.grab_keyboard else 0)
    add("redirectclipboard", "i", 0 if s.clipboard == "off" else 1)
    add("redirectprinters", "i", 1 if s.printers else 0)
    add("redirectsmartcards", "i", 1 if s.smartcard else 0)
    add("audiomode", "i", {"local": 0, "remote": 1, "off": 2}[s.audio_out])
    add("audiocapturemode", "i", 1 if s.audio_in else 0)
    if s.drives:
        add("drivestoredirect", "s", ";".join(folder.path for folder in s.drives))
    add("authentication level", "i", 1 if s.cert_policy in ("deny", "pinned") else 2)
    add("enablecredsspsupport", "i", 0 if s.security in ("tls", "rdp") else 1)
    if s.admin_session:
        add("administrative session", "i", 1)
    add("autoreconnection enabled", "i", 1 if s.auto_reconnect else 0)
    if s.gateway.enabled:
        gateway = s.gateway.host if s.gateway.port == 443 else f"{s.gateway.host}:{s.gateway.port}"
        add("gatewayhostname", "s", gateway)
        add("gatewayusagemethod", "i", 1)
        add("promptcredentialonce", "i", 1 if s.gateway.use_same_credentials else 0)
    if s.initial_program:
        add("alternate shell", "s", s.initial_program)
    if s.working_dir:
        add("shell working directory", "s", s.working_dir)
    if s.remote_app.program:
        add("remoteapplicationmode", "i", 1)
        add("remoteapplicationprogram", "s", s.remote_app.program)
        if s.remote_app.name:
            add("remoteapplicationname", "s", s.remote_app.name)
        if s.remote_app.args:
            add("remoteapplicationcmdline", "s", s.remote_app.args)
    if s.load_balance_info:
        add("loadbalanceinfo", "s", s.load_balance_info)
    written = {line.split(":", 1)[0] for line in lines}
    preserved = s.extra.get("rdpfile")
    if isinstance(preserved, dict):
        for key, item in sorted(preserved.items()):
            if key in written or key in SECRET_KEYS or key.startswith("password"):
                continue
            if isinstance(item, list) and len(item) == 2 and item[0] in ("s", "i") and isinstance(item[1], str):
                add(str(key)[:64], item[0], item[1][:512])
    return "\r\n".join(lines) + "\r\n"


def encode_rdp(text: str) -> bytes:
    """UTF-16 LE with a BOM, as mstsc writes it."""

    return codecs.BOM_UTF16_LE + text.encode("utf-16-le")


__all__ = ["import_rdp", "export_rdp", "encode_rdp", "decode", "parse_lines", "RdpFileImport", "RdpFileError"]
