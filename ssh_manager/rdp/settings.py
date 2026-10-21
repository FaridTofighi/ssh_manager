"""Per-host RDP settings, stored in ``host.metadata["rdp"]`` (pure Python, no GTK).

:meth:`RdpSettings.from_json` never raises: an invalid value falls back to its
default and is reported in ``warnings``; keys this version does not know are
kept in ``extra`` and written back unchanged, so a newer version's settings
survive an edit here. :meth:`RdpSettings.problems` is the inline validation
of the host dialog (the values as typed, before any fallback).

Nothing here is a secret: passwords live in the credential store and reach
FreeRDP only through the launcher's secret channel (``docs/rdp.md``).
"""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass, field, fields
from typing import Any, Dict, List, Tuple

VERSION = 1
DEFAULT_PORT = 3389
MIN_SIZE, MAX_SIZE = 200, 8192

SECURITY = ("auto", "nla", "nla_ext", "tls", "rdp")
TLS_LEVELS = ("default", "0", "1", "2")
CERT_POLICIES = ("ask", "tofu", "pinned", "deny", "ignore")
SIZE_MODES = ("window", "fixed", "fullscreen")
COLOR_DEPTHS = (8, 15, 16, 24, 32)
SCALES = (100, 140, 180)
PRESETS = ("auto", "lan", "broadband", "wan", "modem", "custom")
GFX = ("auto", "rfx", "avc420", "avc444", "off")
CLIPBOARD = ("both", "to_remote", "from_remote", "off")
AUDIO_OUT = ("local", "remote", "off")
GATEWAY_TRANSPORTS = ("auto", "http", "rpc")
KEYBOARD_TYPES = ("ibm_enhanced", "ibm_pc_xt", "olivetti", "ibm_pc_at", "nokia_1050", "nokia_9140", "japanese")

# (id stored in the settings, label, Windows keyboard layout id or "" = same as this computer)
KEYBOARD_LAYOUTS: Tuple[Tuple[str, str, str], ...] = (
    ("auto", "Same as local", ""),
    ("en-US", "English (United States)", "0x00000409"),
    ("en-GB", "English (United Kingdom)", "0x00000809"),
    ("fa-IR", "Persian (0x429)", "0x00000429"),
    ("ar", "Arabic (101)", "0x00000401"),
    ("de", "German", "0x00000407"),
    ("fr", "French", "0x0000040C"),
    ("es", "Spanish", "0x0000040A"),
    ("it", "Italian", "0x00000410"),
    ("tr", "Turkish (Q)", "0x0000041F"),
    ("ru", "Russian", "0x00000419"),
    ("he", "Hebrew", "0x0000040D"),
    ("ja", "Japanese", "0x00000411"),
)
KEYBOARD_IDS = tuple(layout[0] for layout in KEYBOARD_LAYOUTS)

# Performance presets → the toggles they set (still editable afterwards). FreeRDP's /network: values
# differ per version; the launcher maps the preset itself (docs/rdp.md, "Performance presets").
PRESET_TOGGLES: Dict[str, Dict[str, bool]] = {
    "lan": dict(wallpaper=True, themes=True, font_smoothing=True, menu_animations=True, window_drag=True, aero=True),
    "broadband": dict(wallpaper=True, themes=True, font_smoothing=True, menu_animations=False, window_drag=True,
                      aero=False),
    "wan": dict(wallpaper=False, themes=True, font_smoothing=True, menu_animations=False, window_drag=False,
                aero=False),
    "modem": dict(wallpaper=False, themes=False, font_smoothing=False, menu_animations=False, window_drag=False,
                  aero=False),
}
PRESET_TOGGLES["auto"] = dict(PRESET_TOGGLES["broadband"])

# Folders that are never shared without a red "I understand" confirmation (R3 enforces it at launch).
SENSITIVE_DIRS = ("/", "/etc", "~/.ssh", "~/.gnupg")

_GUID = re.compile(r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$")
_NAME_OK = re.compile(r"^[A-Za-z0-9_.\-]{1,32}$")  # a drive share name
_DOMAIN_BAD = re.compile(r'[\x00-\x1f\x7f"/\\\[\]:;|=,+*?<>@]')
_TEXT_MAX = 256

# extra_args: options that are safe to pass (a value after ':' is checked by the pattern).
EXTRA_ALLOWED: Dict[str, str] = {
    "+async-update": "", "+async-channels": "", "-decorations": "", "+workarea": "", "+relax-order-checks": "",
    "+old-license": "", "-mouse-motion": "", "+mouse-relative": "", "+nsc": "", "+jpeg": "", "-heartbeat": "",
    "-multitransport": "", "/ipv4": "", "/ipv6": "", "+disp": "", "-grab-mouse": "", "+span": "",
    "/gdi": r"sw|hw", "/frame-ack": r"\d{1,6}", "/max-fast-path-size": r"\d{1,9}", "/jpeg-quality": r"\d{1,3}",
    "/compression-level": r"[0-2]", "/rfx-mode": r"image|video", "/client-hostname": r"[A-Za-z0-9.\-]{1,63}",
    "/client-build-number": r"\d{1,6}", "/timezone": r"[A-Za-z0-9 ()+\-_.:]{1,64}",
    "/tls": r"(seclevel:[0-5]|enforce(:(ssl3|1\.0|1\.1|1\.2|1\.3))?)", "/orientation": r"0|90|180|270",
}
# Never accepted, not even with "I know what I'm doing": credentials, the target, secret channels, arbitrary
# files/code/proxies, certificate overrides and options the app manages itself.
EXTRA_FORBIDDEN = (
    "p", "u", "d", "v", "port", "args-from", "from-stdin", "g", "gu", "gd", "gp", "gt", "gateway", "gateway-usage-method",
    "cert", "cert-ignore", "cert-tofu", "cert-deny", "cert-name", "pth", "assistance", "proxy", "action-script",
    "rdp2tcp", "a", "vc", "dvc", "addin", "sspi-module", "winscard-module", "tune", "dump", "play-rfx", "kerberos",
    "smartcard-logon", "parent-window", "reconnect-cookie", "print-reconnect-cookie", "pcb", "azure",
    "secrets-file", "log-filters", "kbd-pipe", "app", "shell", "shell-dir", "drive", "drives", "home-drive",
    "usb", "printer", "serial", "parallel",
)


# --------------------------------------------------------------- identity
@dataclass(frozen=True)
class RdpIdentity:
    """A login as FreeRDP gets it: ``/u:`` ``user`` and (unless empty) ``/d:`` ``domain``."""

    user: str
    domain: str = ""
    upn: bool = False  # user@realm: sent as is, never with a domain
    problem: str = ""  # why the name cannot be used ("" = fine)


def split_rdp_user(text: str) -> RdpIdentity:
    """``DOMAIN\\user`` → domain + user; ``user@realm`` (UPN) stays intact; ``user`` alone has no domain.

    ``.\\user`` is a local account (domain ``.``). A name with a second
    backslash, control characters or that is too long is reported in
    ``problem`` (the user part is returned unchanged so the dialog can show it).
    """

    raw = (text or "").strip()
    if not raw:
        return RdpIdentity("")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in raw) or len(raw) > _TEXT_MAX:
        return RdpIdentity(raw, problem="The user name contains control characters or is too long.")
    if "\\" in raw:
        domain, _sep, user = raw.partition("\\")
        domain, user = domain.strip(), user.strip()
        if "\\" in user:
            return RdpIdentity(raw, problem="Use DOMAIN\\user with a single backslash.")
        if not user:
            return RdpIdentity("", domain, problem="The user name after the backslash is empty.")
        return RdpIdentity(user, domain)
    if "@" in raw and not raw.startswith("@") and not raw.endswith("@"):
        return RdpIdentity(raw, upn=True)
    return RdpIdentity(raw)


def resolve_identity(username: str, settings: "RdpSettings") -> RdpIdentity:
    """The login for a connect: the credential's (or host's) user name and the host's domain setting.

    The host's ``domain`` wins over a ``DOMAIN\\`` prefix; without
    ``use_credential_domain`` a prefix is ignored and only the host's domain
    is sent. A UPN never gets a domain.
    """

    parsed = split_rdp_user(username)
    if parsed.problem or parsed.upn:
        return parsed
    domain = parsed.domain if settings.use_credential_domain else ""
    if settings.domain.strip():
        domain = settings.domain.strip()
    return RdpIdentity(parsed.user, domain)


# ---------------------------------------------------------- nested values
@dataclass
class SharedFolder:
    name: str
    path: str
    read_only: bool = False
    allow_sensitive: bool = False  # the user confirmed the red warning for a private / system folder

    def to_json(self) -> Dict[str, Any]:
        return {"name": self.name, "path": self.path, "read_only": self.read_only,
                "allow_sensitive": self.allow_sensitive}


@dataclass
class Gateway:
    host: str = ""
    port: int = 443
    user: str = ""
    domain: str = ""
    use_same_credentials: bool = True
    transport: str = "auto"
    credential_id: str = ""  # a saved credential for the gateway (its password goes through the secret channel)

    @property
    def enabled(self) -> bool:
        return bool(self.host.strip())

    def to_json(self) -> Dict[str, Any]:
        return {"host": self.host, "port": self.port, "user": self.user, "domain": self.domain,
                "use_same_credentials": self.use_same_credentials, "transport": self.transport,
                "credential_id": self.credential_id}


@dataclass
class RemoteApp:
    program: str = ""
    name: str = ""
    args: str = ""

    def to_json(self) -> Dict[str, Any]:
        return {"program": self.program, "name": self.name, "args": self.args}


# ---------------------------------------------------------------- settings
@dataclass
class RdpSettings:
    # identity
    domain: str = ""
    use_credential_domain: bool = True
    # security
    security: str = "auto"
    nla_fallback: bool = True  # allow TLS / RDP security when NLA fails (auto mode)
    tls_security_level: str = "default"
    cert_policy: str = "ask"
    pinned_sha256: List[str] = field(default_factory=list)
    restricted_admin: bool = False
    remote_credential_guard: bool = False
    admin_session: bool = False
    prompt_for_credentials: bool = False
    # display
    size_mode: str = "window"
    width: int = 1920
    height: int = 1080
    color_depth: int = 32
    scale_percent: int = 100
    smart_sizing: bool = False
    multimon: bool = False
    monitors: List[int] = field(default_factory=list)
    desktop_scale_factor: int = 100  # 100–500
    device_scale_factor: int = 100  # 100 / 140 / 180
    refresh_hz: int = 0  # 0 = engine default
    # performance
    preset: str = "auto"
    gfx: str = "auto"
    compression: bool = True
    wallpaper: bool = True
    themes: bool = True
    font_smoothing: bool = True
    menu_animations: bool = False
    window_drag: bool = True
    aero: bool = False
    # input
    keyboard_layout: str = "auto"
    keyboard_type: str = "ibm_enhanced"
    unicode_input: bool = False
    grab_keyboard: bool = True
    swap_alt_super: bool = False
    mouse_motion: bool = True
    touch: bool = False
    # redirection
    clipboard: str = "both"
    drives: List[SharedFolder] = field(default_factory=list)
    home_drive: bool = False
    printers: bool = False
    audio_out: str = "local"
    audio_in: bool = False
    usb: List[str] = field(default_factory=list)
    smartcard: bool = False
    # connection
    connect_timeout_s: int = 15
    auto_reconnect: bool = True
    reconnect_max: int = 5
    network_autodetect: bool = True
    gateway: Gateway = field(default_factory=Gateway)
    load_balance_info: str = ""
    via_ssh: str = ""  # inventory id of an SSH host to tunnel through (R3)
    initial_program: str = ""
    working_dir: str = ""
    remote_app: RemoteApp = field(default_factory=RemoteApp)
    hyperv_vm_id: str = ""  # Hyper-V console (/vmconnect, port 2179): the VM's GUID
    extra_args: str = ""
    extra_args_unchecked: bool = False  # "I know what I'm doing": free-form flags (never the forbidden ones)
    engine: str = ""  # "" = the preference; or "frdp" / "xfreerdp-embed" / "external"
    extra: Dict[str, Any] = field(default_factory=dict)  # unknown keys, kept as they are

    # ------------------------------------------------------------ json
    @classmethod
    def from_json(cls, data: Any) -> Tuple["RdpSettings", List[str]]:
        """``(settings, warnings)`` from ``metadata["rdp"]`` (anything; never raises)."""

        settings = cls()
        warnings: List[str] = []
        if not isinstance(data, dict):
            if data not in (None, ""):
                warnings.append("The remote desktop settings were unreadable; defaults are used.")
            return settings, warnings
        known = {f.name for f in fields(cls) if f.name != "extra"}
        for key, value in data.items():
            if key == "v":
                continue
            if key not in known:
                settings.extra[key] = value
                continue
            coerced = _coerce(key, value, getattr(cls(), key))
            if coerced is None:
                warnings.append(f"Invalid value for {key}; the default is used.")
                continue
            setattr(settings, key, coerced)
        warnings += settings._fix()
        return settings, warnings

    def to_json(self) -> Dict[str, Any]:
        data: Dict[str, Any] = dict(self.extra)
        data["v"] = VERSION
        for item in fields(self):
            if item.name == "extra":
                continue
            value = getattr(self, item.name)
            if isinstance(value, (Gateway, RemoteApp)):
                value = value.to_json()
            elif item.name == "drives":
                value = [folder.to_json() for folder in value]
            elif isinstance(value, list):
                value = list(value)
            data[item.name] = value
        return data

    @classmethod
    def for_host(cls, host: Any) -> Tuple["RdpSettings", List[str]]:
        metadata = getattr(host, "metadata", {}) or {}
        return cls.from_json(metadata.get("rdp"))

    # -------------------------------------------------------- presets
    def apply_preset(self, preset: str) -> None:
        """Set the experience toggles of a preset (``custom`` keeps them)."""

        if preset not in PRESETS:
            return
        self.preset = preset
        for name, value in PRESET_TOGGLES.get(preset, {}).items():
            setattr(self, name, value)

    # ------------------------------------------------------ validation
    def _fix(self) -> List[str]:
        """Replace values out of range with defaults; return the warnings."""

        warnings: List[str] = []
        defaults = RdpSettings()
        for name, allowed in (("security", SECURITY), ("tls_security_level", TLS_LEVELS),
                              ("cert_policy", CERT_POLICIES), ("size_mode", SIZE_MODES), ("color_depth", COLOR_DEPTHS),
                              ("scale_percent", SCALES), ("device_scale_factor", SCALES), ("preset", PRESETS),
                              ("gfx", GFX), ("keyboard_layout", KEYBOARD_IDS), ("keyboard_type", KEYBOARD_TYPES),
                              ("clipboard", CLIPBOARD), ("audio_out", AUDIO_OUT),
                              ("engine", ("", "frdp", "xfreerdp-embed", "external"))):
            if getattr(self, name) not in allowed:
                warnings.append(f"Invalid value for {name}; the default is used.")
                setattr(self, name, getattr(defaults, name))
        for name, low, high in (("width", MIN_SIZE, MAX_SIZE), ("height", MIN_SIZE, MAX_SIZE),
                                ("desktop_scale_factor", 100, 500), ("refresh_hz", 0, 240),
                                ("connect_timeout_s", 1, 600), ("reconnect_max", 0, 1000)):
            if not low <= getattr(self, name) <= high:
                warnings.append(f"{name} must be between {low} and {high}; the default is used.")
                setattr(self, name, getattr(defaults, name))
        if self.gateway.transport not in GATEWAY_TRANSPORTS:
            warnings.append("Invalid gateway transport; automatic is used.")
            self.gateway.transport = "auto"
        if not 1 <= self.gateway.port <= 65535:
            warnings.append("Invalid gateway port; 443 is used.")
            self.gateway.port = 443
        for name in ("domain", "load_balance_info", "initial_program", "working_dir", "extra_args", "via_ssh"):
            value = getattr(self, name)
            if len(value) > 1024 or any(ord(ch) < 32 and ch != "\t" or ord(ch) == 127 for ch in value):
                warnings.append(f"{name} contained control characters or was too long; it was cleared.")
                setattr(self, name, "")
        from ..trust import normalize_fingerprint

        pins = [normalize_fingerprint(p) for p in self.pinned_sha256]
        if any(not p for p in pins):
            warnings.append("An invalid certificate fingerprint was dropped.")
        self.pinned_sha256 = [p for p in pins if p]
        self.monitors = [m for m in self.monitors if 0 <= m < 64]
        return warnings

    def problems(self) -> List[str]:
        """What the host dialog shows inline (English; the dialog translates)."""

        problems: List[str] = []
        if self.size_mode == "fixed" and not (MIN_SIZE <= self.width <= MAX_SIZE and MIN_SIZE <= self.height <= MAX_SIZE):
            problems.append(f"The resolution must be between {MIN_SIZE}×{MIN_SIZE} and {MAX_SIZE}×{MAX_SIZE}.")
        if self.domain and _DOMAIN_BAD.search(self.domain):
            problems.append("The domain contains characters a Windows domain cannot have.")
        problems += [p for folder in self.drives for p in shared_folder_problems(folder)]
        names = [folder.name.casefold() for folder in self.drives]
        if len(names) != len(set(names)):
            problems.append("Two shared folders have the same name.")
        if self.gateway.user and split_rdp_user(self.gateway.user).problem:
            problems.append("The gateway user name is not valid.")
        if (self.gateway.user or self.gateway.domain or not self.gateway.use_same_credentials) and \
                not self.gateway.host.strip():
            problems.append("Enter the gateway host, or clear the gateway settings.")
        if self.gateway.host and not _host_ok(self.gateway.host):
            problems.append("The gateway host name is not valid.")
        if len(self.load_balance_info) > 256:
            problems.append("The load-balance information is too long (256 characters at most).")
        if self.gateway.enabled and self.via_ssh:
            problems.append("Use either an RD Gateway or an SSH tunnel, not both.")
        if self.hyperv_vm_id and not _GUID.match(self.hyperv_vm_id):
            problems.append("The Hyper-V VM id must be a GUID like 1f3e7f8c-0d4b-4b63-9e57-2f0a5d6b8c11.")
        problems += extra_args_problems(self.extra_args, self.extra_args_unchecked)
        return problems


# ---------------------------------------------------------------- helpers
def shared_folder_problems(folder: SharedFolder) -> List[str]:
    """Inline checks of one shared folder: name, absolute existing folder, sensitive places (``drives``)."""

    from .drives import check_folder

    problems: List[str] = []
    if not _NAME_OK.match(folder.name or ""):
        problems.append(f"Shared folder name '{folder.name}' must be 1–32 letters, digits, '.', '_' or '-'.")
    check = check_folder(folder.path, allow_sensitive=folder.allow_sensitive)
    if check.problem:
        problems.append(f"'{folder.name}': {check.problem}")
    return problems


def _host_ok(host: str) -> bool:
    return bool(host) and not host.startswith("-") and len(host) <= 253 and not any(
        ch.isspace() or ord(ch) < 32 or ch in "\"'`\\/,;" for ch in host)


def parse_extra_args(text: str) -> Tuple[List[str], str]:
    """Split the advanced flags (shell-like quoting, never a shell); ``(tokens, problem)``."""

    if not text.strip():
        return [], ""
    if any(ord(ch) < 32 and ch not in "\t" or ord(ch) == 127 for ch in text):
        return [], "Extra options must not contain control characters or line breaks."
    try:
        return shlex.split(text), ""
    except ValueError as exc:
        return [], f"Extra options could not be read: {exc}."


def _option_name(token: str) -> str:
    return token[1:].split(":", 1)[0].lower()


def extra_args_problems(text: str, unchecked: bool = False) -> List[str]:
    """Allow-list check of ``extra_args`` (``unchecked`` allows any flag except the forbidden ones)."""

    tokens, problem = parse_extra_args(text)
    if problem:
        return [problem]
    problems: List[str] = []
    for token in tokens:
        shown = token.split(":", 1)[0][:40]  # only the option name: a value may be a secret typed by mistake
        if len(token) > 256 or token[:1] not in "/+-" or len(token) < 2:
            problems.append("Every extra option must start with /, + or - (a word that is not an option was found).")
            continue
        name = _option_name(token)
        if name in EXTRA_FORBIDDEN:
            problems.append(f"'{shown}' cannot be set here (the app manages it, or it could leak secrets).")
            continue
        if unchecked:
            continue
        key, _sep, value = token.partition(":")
        key = key.lower()
        if token[0] == "/" and key not in EXTRA_ALLOWED and ("+" + key[1:]) in EXTRA_ALLOWED:
            key = "+" + key[1:]  # '/' is a synonym of '+'
        if key not in EXTRA_ALLOWED:
            problems.append(f"'{shown}' is not on the list of allowed options.")
            continue
        pattern = EXTRA_ALLOWED[key]
        if (pattern and not re.fullmatch(pattern, value)) or (not pattern and value):
            problems.append(f"'{shown}' has a value that is not allowed.")
    return problems


def keyboard_layout_id(layout: str) -> str:
    """The Windows layout id (``0x00000429``) of a layout setting; "" for "Same as local"."""

    return next((klid for lid, _label, klid in KEYBOARD_LAYOUTS if lid == layout), "")


def _coerce(key: str, value: Any, default: Any) -> Any:
    """``value`` as the type of ``default``, or ``None`` when it cannot be."""

    if key == "drives":
        if not isinstance(value, list):
            return None
        folders: List[SharedFolder] = []
        for item in value:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str) or \
                    not isinstance(item.get("path"), str):
                return None
            folders.append(SharedFolder(item["name"][:64], item["path"][:4096], bool(item.get("read_only", False)),
                                        bool(item.get("allow_sensitive", False))))
        return folders
    if key == "gateway":
        if not isinstance(value, dict):
            return None
        gateway = Gateway()
        for name in ("host", "user", "domain", "transport", "credential_id"):
            if isinstance(value.get(name), str):
                setattr(gateway, name, value[name].strip()[:_TEXT_MAX])
        if isinstance(value.get("port"), int) and not isinstance(value.get("port"), bool):
            gateway.port = value["port"]
        if isinstance(value.get("use_same_credentials"), bool):
            gateway.use_same_credentials = value["use_same_credentials"]
        return gateway
    if key == "remote_app":
        if not isinstance(value, dict):
            return None
        return RemoteApp(*(str(value.get(name, "") or "")[:1024] if isinstance(value.get(name, ""), str) else ""
                           for name in ("program", "name", "args")))
    if key == "monitors":
        if isinstance(value, list) and all(isinstance(v, int) and not isinstance(v, bool) for v in value):
            return list(value)
        return None
    if isinstance(default, bool):
        return value if isinstance(value, bool) else None
    if isinstance(default, int):
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.strip().lstrip("-").isdigit():
            return int(value.strip())
        return None
    if isinstance(default, list):
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            return [item[:_TEXT_MAX] for item in value]
        return None
    if isinstance(default, str):
        return value.strip() if isinstance(value, str) else None
    return None


__all__ = ["RdpSettings", "RdpIdentity", "SharedFolder", "Gateway", "RemoteApp", "split_rdp_user",
           "resolve_identity", "keyboard_layout_id", "extra_args_problems", "shared_folder_problems",
           "KEYBOARD_LAYOUTS", "PRESET_TOGGLES", "DEFAULT_PORT"]
