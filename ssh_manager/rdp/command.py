"""Build a FreeRDP launch: argv, the secret payload and notes (pure Python, version-aware, allow-list based).

:func:`build_launch` turns ``(host, RdpSettings, identity, password, client)``
into a :class:`LaunchPlan`. Nothing is ever assembled as a shell string;
every value is checked (no line breaks, no NUL, length caps, commas only where
the option allows them) and options the client does not list in ``/help`` are
dropped with a note.

**Secrets.** The password (and a gateway password) never appear in ``argv``:

* FreeRDP 3.x (``/args-from``): ``argv`` is ``[client, "/args-from:stdin"]``
  and the *whole* option list, ``/p:`` included, is written to the child's
  stdin, one argument per line, ending with an empty line; the pipe is closed
  right after. ``/args-from`` "can not be combined with any other" argument.
* FreeRDP 2.x (``/from-stdin``): the options are in ``argv`` (no secrets),
  ``/from-stdin:force`` makes it read the password from stdin before
  connecting; the pipe is closed after that one line.
* A client with neither: no password is passed (``password_sent`` is False).

``display_args`` is the option list with every secret replaced by ``***``
(for the connection log and diagnostics).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .engines import ClientBinary
from .settings import RdpIdentity, RdpSettings, keyboard_layout_id, parse_extra_args, extra_args_problems

MAX_VALUE = 1024
SECRET_MASK = "***"
LOG_ARGS = ("/log-level:WARN", "/log-filters:com.freerdp.gdi:INFO,com.freerdp.core:INFO")
NETWORK_PRESETS = {"auto": "auto", "lan": "lan", "broadband": "broadband-high", "wan": "wan", "modem": "modem"}
_USB_SELECTOR = re.compile(r"^(auto|id:[0-9A-Fa-f]{4}:[0-9A-Fa-f]{4}|addr:\d{1,3}:\d{1,3})$")
_HOST_BAD = re.compile(r"[\s\x00-\x1f\x7f\"'`\\/,;@]")


class LaunchError(ValueError):
    """The settings cannot be turned into a safe command line (message for the user, English)."""


@dataclass
class LaunchPlan:
    argv: List[str]  # what /proc/<pid>/cmdline shows: never a secret
    stdin_payload: bytes  # written to the child's stdin, then closed (b"" = nothing, stdin is /dev/null)
    channel: str  # "args-from" | "from-stdin" | "none"
    display_args: List[str]  # the options with secrets masked (log / diagnostics)
    env: Dict[str, str] = field(default_factory=dict)  # additions to the environment (never secrets)
    notes: List[str] = field(default_factory=list)  # dropped or adjusted options (English)
    password_sent: bool = False
    secrets: Tuple[str, ...] = ()  # what the output reader must mask (never logged itself)

    def __repr__(self) -> str:  # never print the payload
        return f"LaunchPlan(argv={self.argv!r}, channel={self.channel!r}, notes={self.notes!r})"


def _clean(value: str, what: str, *, allow_comma: bool = True, limit: int = MAX_VALUE) -> str:
    text = str(value)
    if not text or len(text) > limit or any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        raise LaunchError(f"The {what} is empty, too long or contains control characters.")
    if not allow_comma and "," in text:
        raise LaunchError(f"The {what} must not contain a comma.")
    return text


def _secret_ok(secret: str) -> bool:
    return bool(secret) and "\n" not in secret and "\r" not in secret and "\x00" not in secret and len(secret) <= 512


def target_address(host: str, port: int) -> str:
    """``/v:`` value: ``host:port`` (IPv6 in brackets); refuses anything that could be read as an option."""

    host = (host or "").strip()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if not host or host.startswith("-") or len(host) > 253 or _HOST_BAD.search(host):
        raise LaunchError("The host name is not valid.")
    if not 1 <= int(port) <= 65535:
        raise LaunchError("The port must be between 1 and 65535.")
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


class _Options:
    """Collects options, dropping those the client does not know (each once, with a note)."""

    def __init__(self, client: ClientBinary) -> None:
        self.client = client
        self.items: List[str] = []
        self.secret_index: Dict[int, str] = {}  # index in items → masked display form
        self.notes: List[str] = []

    def known(self, option: str) -> bool:
        return self.client.has(option.lstrip("/+-").split(":", 1)[0])

    def add(self, token: str, *, note: str = "", secret_display: str = "") -> bool:
        if not self.known(token):
            name = token.split(":", 1)[0]
            self.notes.append(note or f"{self.client.name} {self.client.version_text} has no {name}; it was left out.")
            return False
        if secret_display:
            self.secret_index[len(self.items)] = secret_display
        self.items.append(token)
        return True

    def display(self) -> List[str]:
        return [self.secret_index.get(i, item) for i, item in enumerate(self.items)]


def build_launch(*, hostname: str, port: int, settings: RdpSettings, identity: RdpIdentity,
                 password: Optional[str], client: ClientBinary, title: str = "",
                 accepted_fingerprints: Sequence[str] = (), ignore_certificate: bool = False,
                 parent_window: Optional[int] = None, window_size: Optional[Tuple[int, int]] = None,
                 gateway_password: Optional[str] = None, server_name: str = "") -> LaunchPlan:
    """The launch plan for one connection attempt (raises :class:`LaunchError` for unusable values)."""

    if not client.usable:
        raise LaunchError(client.error or "The FreeRDP client cannot be used.")
    v3 = client.major >= 3
    o = _Options(client)
    o.add("/v:" + target_address(hostname, port))
    if identity.problem:
        raise LaunchError(identity.problem)
    if identity.user:
        o.add("/u:" + _clean(identity.user, "user name", limit=256))
    if identity.domain:
        o.add("/d:" + _clean(identity.domain, "domain", limit=256))
    if title:
        o.add("/t:" + "".join(ch for ch in title if ord(ch) >= 32 and ord(ch) != 127)[:128])
    if server_name:  # tunnelled: the real name for certificate / Kerberos checks (R3)
        o.add("/server-name:" + _clean(server_name, "server name", allow_comma=False, limit=253))
    o.items.extend(LOG_ARGS)

    _security(o, settings, v3, accepted_fingerprints, ignore_certificate)
    _display(o, settings, v3, window_size, parent_window)
    _performance(o, settings, v3)
    _input(o, settings, v3)
    _redirection(o, settings, v3)
    gateway_secret = _connection(o, settings, v3, gateway_password)
    _extra(o, settings)

    secrets: List[str] = []
    channel = client.secret_channel
    sent = False
    payload = b""
    argv: List[str]
    if password is not None and password != "" and not _secret_ok(password):
        raise LaunchError("The password contains a line break or is too long for FreeRDP.")
    if channel == "args-from":
        if password:
            o.add("/p:" + password, secret_display="/p:" + SECRET_MASK)
            secrets.append(password)
            sent = True
        if gateway_secret:
            secrets.append(gateway_secret)
        lines = [item for item in o.items]
        if any("\n" in line or "\r" in line or not line for line in lines):
            raise LaunchError("An option contains a line break.")
        payload = ("\n".join(lines) + "\n\n").encode("utf-8")
        argv = [client.path, "/args-from:stdin"]
    else:
        if gateway_secret:
            o.notes.append("This FreeRDP version cannot receive a separate gateway password safely; it was left out.")
            o.items = [item for item in o.items if not item.startswith("/gp:")]
        if channel == "from-stdin" and password:
            o.items.append("/from-stdin:force")
            payload = (password + "\n").encode("utf-8")
            secrets.append(password)
            sent = True
        elif password:
            o.notes.append(f"{client.name} cannot receive a password without showing it to other programs; "
                           "type it in the remote sign-in screen.")
        argv = [client.path] + list(o.items)
    if _leaks(argv[1:], secrets):  # last line of defence
        raise LaunchError("Refusing to start: a secret would appear on the command line.")
    return LaunchPlan(argv, payload, channel or "none", o.display(), {}, o.notes, sent, tuple(secrets))


def _leaks(arguments: Sequence[str], secrets: Sequence[str]) -> bool:
    """A secret in a password position of an option (``/p:``, ``/gp:``, ``,p:``, ``:p:``).

    Not a plain substring test: a short password such as ``32`` or ``bin`` would "match" ``/bpp:32`` or the
    client's path and block every connection.
    """

    for secret in secrets:
        if not secret:
            continue
        for argument in arguments:
            for marker in ("/p:", "/gp:", ",p:", ":p:", "/assistance:", "/pth:"):
                if marker + secret in argument:
                    return True
    return False


# ----------------------------------------------------------------- groups
def _security(o: _Options, s: RdpSettings, v3: bool, fingerprints: Sequence[str], ignore: bool) -> None:
    sec = {"nla": "nla", "nla_ext": "ext", "tls": "tls", "rdp": "rdp"}.get(s.security)
    if s.security == "auto" and not s.nla_fallback:
        sec = "nla"
    if sec:
        o.add("/sec:" + sec)
    if s.tls_security_level != "default":
        o.add(("/tls:seclevel:" if v3 else "/tls-seclevel:") + s.tls_security_level)
    if ignore:
        o.add("/cert:ignore" if o.known("cert") else "/cert-ignore")
    else:
        prints = []
        for fp in fingerprints:
            hexdigits = "".join(ch for ch in fp.lower() if ch in "0123456789abcdef")
            if len(hexdigits) == 64:
                prints.append("fingerprint:sha256:" + ":".join(hexdigits[i:i + 2] for i in range(0, 64, 2)))
        if prints:
            o.add("/cert:" + ",".join(["deny"] + prints))
        elif s.security != "rdp":
            o.add("/cert:deny" if o.known("cert") else "/cert-deny")  # never a hidden console prompt
    if s.admin_session:
        o.add("/admin")
    if s.restricted_admin:
        o.add("/restricted-admin")
    if s.remote_credential_guard:
        o.add("+remoteGuard", note="Remote Credential Guard needs FreeRDP 3; it was left out.")


def _display(o: _Options, s: RdpSettings, v3: bool, window_size: Optional[Tuple[int, int]],
             parent_window: Optional[int]) -> None:
    if parent_window is not None:
        o.add(f"/parent-window:{int(parent_window)}")
        o.add("-toggle-fullscreen")  # Ctrl+Alt+Enter is the tab's full screen, not FreeRDP's own window's
    if s.size_mode == "fullscreen":
        o.add("/f")
        if s.multimon:
            o.add("/multimon")
            if s.monitors:
                o.add("/monitors:" + ",".join(str(int(m)) for m in s.monitors))
    elif s.size_mode == "fixed":
        o.add(f"/size:{int(s.width)}x{int(s.height)}")
    else:
        width, height = window_size or (s.width, s.height)
        o.add(f"/size:{max(200, int(width))}x{max(200, int(height))}")
        o.add("/dynamic-resolution")
    if s.smart_sizing and s.size_mode != "window":
        o.add("/smart-sizing")
    o.add(f"/bpp:{int(s.color_depth)}")
    if s.scale_percent != 100:
        o.add(f"/scale:{int(s.scale_percent)}")
    if s.desktop_scale_factor != 100:
        o.add(f"/scale-desktop:{int(s.desktop_scale_factor)}")
    if s.device_scale_factor != 100:
        o.add(f"/scale-device:{int(s.device_scale_factor)}")


def _performance(o: _Options, s: RdpSettings, v3: bool) -> None:
    network = NETWORK_PRESETS.get(s.preset)
    if network:
        o.add("/network:" + network)
    else:  # custom: the toggles themselves
        for name, option, default in (("wallpaper", "wallpaper", True), ("themes", "themes", True),
                                      ("font_smoothing", "fonts", True), ("menu_animations", "menu-anims", False),
                                      ("window_drag", "window-drag", False), ("aero", "aero", False)):
            value = getattr(s, name)
            if value != default:
                o.add(("+" if value else "-") + option)
    if not s.compression:
        o.add("-compression")
    gfx = {"avc444": "/gfx:AVC444", "avc420": "/gfx:AVC420", "rfx": "/rfx"}.get(s.gfx)
    if gfx:
        o.add(gfx)
    elif s.gfx == "off":
        o.notes.append("Turning the graphics pipeline off is not supported from the command line; the client's "
                       "default is used.")


def _input(o: _Options, s: RdpSettings, v3: bool) -> None:
    layout = keyboard_layout_id(s.keyboard_layout)
    if v3:
        parts = ([f"layout:{layout}"] if layout else []) + (["unicode:on"] if s.unicode_input else [])
        if parts:
            o.add("/kbd:" + ",".join(parts))
    else:
        if layout:
            o.add("/kbd:" + layout)
        if s.unicode_input:
            o.notes.append("FreeRDP 2 has no Unicode keyboard input; it was left out.")
    if not s.grab_keyboard:
        o.add("-grab-keyboard")
    if not s.mouse_motion:
        o.add("-mouse-motion")
    if s.touch:
        o.add("+multitouch")
    if s.swap_alt_super:
        o.notes.append("Swapping Alt and the Windows key has no FreeRDP option; it was left out.")


def _redirection(o: _Options, s: RdpSettings, v3: bool) -> None:
    if s.clipboard == "both":
        o.add("+clipboard")
    elif s.clipboard == "off":
        o.add("-clipboard")
    else:
        direction = "remote" if s.clipboard == "to_remote" else "local"
        if v3:
            o.add(f"/clipboard:direction-to:{direction}")
        else:
            o.add("-clipboard")
            o.notes.append("FreeRDP 2 cannot limit the clipboard to one direction; the clipboard is off.")
    for folder in s.drives:
        path = os.path.realpath(os.path.expanduser(folder.path))
        if folder.read_only:
            o.notes.append(f"FreeRDP cannot share '{folder.name}' read-only; it was not shared.")
            continue
        if not os.path.isabs(path) or not os.path.isdir(path):
            o.notes.append(f"The folder for '{folder.name}' does not exist; it was not shared.")
            continue
        name = _clean(folder.name, "share name", allow_comma=False, limit=32)
        o.add(f"/drive:{name},{_clean(path, 'shared folder', allow_comma=False, limit=4096)}")
    if s.home_drive:
        o.add("+home-drive")
    if s.printers:
        o.add("/printer")
    if s.audio_out == "local":
        o.add("/sound")
    elif s.audio_out == "remote":
        o.add("/audio-mode:1")
    else:
        o.add("/audio-mode:2")
    if s.audio_in:
        o.add("/microphone")
    for selector in s.usb:
        if _USB_SELECTOR.match(selector):
            o.add("/usb:" + selector)
        else:
            o.notes.append("A USB selector was not understood; it was left out.")
    if s.smartcard:
        o.add("/smartcard")


def _connection(o: _Options, s: RdpSettings, v3: bool, gateway_password: Optional[str]) -> str:
    o.add(f"/timeout:{int(s.connect_timeout_s) * 1000}")
    if s.auto_reconnect:
        o.add("+auto-reconnect")
        o.add(f"/auto-reconnect-max-retries:{int(s.reconnect_max)}")
    if s.load_balance_info:
        o.add("/load-balance-info:" + _clean(s.load_balance_info, "load-balance information", limit=256))
    if s.initial_program:
        o.add("/shell:" + _clean(s.initial_program, "start program"))
    if s.working_dir:
        o.add("/shell-dir:" + _clean(s.working_dir, "working folder"))
    app = s.remote_app
    if app.program:
        program = _clean(app.program, "RemoteApp program", allow_comma=False)
        if v3:
            parts = [f"program:{program}"]
            if app.name:
                parts.append("name:" + _clean(app.name, "RemoteApp name", allow_comma=False, limit=128))
            if app.args:
                parts.append("cmd:" + _clean(app.args, "RemoteApp arguments", allow_comma=False))
            o.add("/app:" + ",".join(parts))
        else:
            o.add("/app:" + program)
            if app.name:
                o.add("/app-name:" + _clean(app.name, "RemoteApp name", limit=128))
            if app.args:
                o.add("/app-cmd:" + _clean(app.args, "RemoteApp arguments"))
    gw = s.gateway
    secret = ""
    if gw.enabled:
        host = target_address(gw.host, gw.port)
        if v3:
            parts = [f"g:{host}"]
            if not gw.use_same_credentials:
                if gw.user:
                    parts.append("u:" + _clean(gw.user, "gateway user", allow_comma=False, limit=256))
                if gw.domain:
                    parts.append("d:" + _clean(gw.domain, "gateway domain", allow_comma=False, limit=256))
                if gateway_password:
                    if _secret_ok(gateway_password) and not any(ch in gateway_password for ch in ",\"'"):
                        parts.append("p:" + gateway_password)
                        secret = gateway_password
                    else:
                        o.notes.append("The gateway password contains a comma or a quote, which FreeRDP cannot "
                                       "read here; FreeRDP will ask for it.")
            if gw.transport != "auto":
                parts.append(f"type:{gw.transport}")
            display = ",".join(part if not part.startswith("p:") else "p:" + SECRET_MASK for part in parts)
            o.add("/gateway:" + ",".join(parts), secret_display="/gateway:" + display if secret else "")
        else:
            o.add("/g:" + host)
            if not gw.use_same_credentials:
                if gw.user:
                    o.add("/gu:" + _clean(gw.user, "gateway user", limit=256))
                if gw.domain:
                    o.add("/gd:" + _clean(gw.domain, "gateway domain", limit=256))
                if gateway_password:
                    secret = gateway_password  # dropped by build_launch: 2.x has no safe channel for it
                    o.items.append("/gp:" + SECRET_MASK)
            if gw.transport != "auto":
                o.add(f"/gt:{gw.transport}")
    return secret


def _extra(o: _Options, s: RdpSettings) -> None:
    if not s.extra_args:
        return
    if extra_args_problems(s.extra_args, s.extra_args_unchecked):
        o.notes.append("The extra options are not valid; they were left out.")
        return
    tokens, _problem = parse_extra_args(s.extra_args)
    for token in tokens:
        o.add(token)


__all__ = ["build_launch", "LaunchPlan", "LaunchError", "target_address", "SECRET_MASK", "LOG_ARGS"]
