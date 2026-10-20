"""Per-host file-transfer settings, stored in ``host.metadata["transfer"]`` (pure Python).

``TransferSettings.from_json()`` never raises: an invalid value falls back to
its default and is reported in ``warnings``. Keys this version does not know
are kept and written back unchanged, so a newer version's settings survive
an edit here.
"""

from __future__ import annotations

import codecs
from dataclasses import dataclass, field, fields
from typing import Any, Dict, List, Optional, Tuple

VERSION = 1
TLS_VERIFY = ("system", "custom_ca", "pinned", "insecure")
TLS_MODES = ("explicit", "implicit")
TRANSFER_MODES = ("binary", "auto")
LIST_COMMANDS = ("auto", "mlsd", "list", "list-a")
ENCODINGS = ("utf-8", "latin-1", "cp1252", "cp1256", "cp1251", "shift_jis")
DEFAULT_ASCII = ("txt", "csv", "log", "ini", "conf", "cfg", "xml", "html", "htm", "css", "js", "json", "md", "sh",
                 "php", "py", "sql", "yml", "yaml")
IMPLICIT_FTPS_PORT = 990


@dataclass
class TransferSettings:
    passive: bool = True
    encoding: str = "auto"  # auto = UTF-8 (OPTS UTF8 ON when the server offers it), else a codec name
    tls_mode: str = "explicit"  # FTPS: AUTH TLS on port 21, or implicit TLS on port 990
    tls_verify: str = "system"
    ca_file: str = ""
    pinned_sha256: str = ""
    trust_pasv_address: bool = False  # off: the PASV IP is ignored, the control peer is used (anti-bounce)
    initial_remote_dir: str = ""
    initial_local_dir: str = ""
    ascii_extensions: List[str] = field(default_factory=lambda: list(DEFAULT_ASCII))
    transfer_mode: str = "binary"
    timeout_connect: int = 15
    timeout_read: int = 60
    keepalive_s: int = 30
    max_connections: int = 3
    show_hidden: bool = False
    preserve_mtime: bool = True
    upload_atomic: bool = True  # upload to name.part, then rename
    server_time_offset_min: int = 0
    active_port_range: str = ""  # "50000-50100"; empty = any free port
    bandwidth_limit_kbps: int = 0
    follow_symlinks: bool = False
    list_command: str = "auto"
    anonymous: bool = False
    anonymous_password: str = "anonymous@"  # not a secret: the e-mail-like anonymous convention
    extra: Dict[str, Any] = field(default_factory=dict)  # unknown keys, kept as they are

    # ------------------------------------------------------------ json
    @classmethod
    def from_json(cls, data: Any) -> Tuple["TransferSettings", List[str]]:
        """``(settings, warnings)`` from ``metadata["transfer"]`` (anything; never raises)."""

        settings = cls()
        warnings: List[str] = []
        if not isinstance(data, dict):
            if data not in (None, ""):
                warnings.append("The file-transfer settings were unreadable; defaults are used.")
            return settings, warnings
        known = {f.name: f for f in fields(cls) if f.name != "extra"}
        for key, value in data.items():
            if key == "v":
                continue
            if key not in known:
                settings.extra[key] = value
                continue
            default = getattr(settings, key)
            coerced = _coerce(key, value, default)
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
            if item.name != "extra":
                value = getattr(self, item.name)
                data[item.name] = list(value) if isinstance(value, list) else value
        return data

    @classmethod
    def for_host(cls, host: Any) -> Tuple["TransferSettings", List[str]]:
        metadata = getattr(host, "metadata", {}) or {}
        return cls.from_json(metadata.get("transfer"))

    # ------------------------------------------------------- validation
    def _fix(self) -> List[str]:
        """Replace values out of range with defaults; return the warnings."""

        warnings: List[str] = []
        defaults = TransferSettings()
        for name, allowed in (("tls_verify", TLS_VERIFY), ("tls_mode", TLS_MODES), ("transfer_mode", TRANSFER_MODES),
                              ("list_command", LIST_COMMANDS)):
            if getattr(self, name) not in allowed:
                warnings.append(f"Invalid value for {name}; the default is used.")
                setattr(self, name, getattr(defaults, name))
        for name, low, high in (("timeout_connect", 1, 600), ("timeout_read", 1, 3600), ("keepalive_s", 0, 3600),
                                ("max_connections", 1, 8), ("bandwidth_limit_kbps", 0, 10_000_000),
                                ("server_time_offset_min", -24 * 60, 24 * 60)):
            if not low <= getattr(self, name) <= high:
                warnings.append(f"{name} must be between {low} and {high}; the default is used.")
                setattr(self, name, getattr(defaults, name))
        if self.encoding != "auto" and not encoding_known(self.encoding):
            warnings.append(f"Unknown text encoding '{self.encoding}'; UTF-8 is used.")
            self.encoding = "auto"
        if self.active_port_range and parse_port_range(self.active_port_range) is None:
            warnings.append("The active-mode port range must look like 50000-50100; any free port is used.")
            self.active_port_range = ""
        self.ascii_extensions = [e.strip().lstrip(".").lower() for e in self.ascii_extensions if e.strip()]
        return warnings

    def problems(self) -> List[str]:
        """What the host dialog shows inline (the values as entered, before falling back)."""

        problems: List[str] = []
        if self.tls_verify == "custom_ca" and not self.ca_file.strip():
            problems.append("Choose the CA certificate file, or use the system certificates.")
        if self.encoding != "auto" and not encoding_known(self.encoding):
            problems.append(f"Unknown text encoding '{self.encoding}'.")
        if self.active_port_range and parse_port_range(self.active_port_range) is None:
            problems.append("Use a port range like 50000-50100.")
        return problems

    # ---------------------------------------------------------- helpers
    def codec(self) -> str:
        return "utf-8" if self.encoding == "auto" else self.encoding

    def is_ascii_name(self, name: str) -> bool:
        if self.transfer_mode != "auto" or "." not in name:
            return False
        return name.rsplit(".", 1)[1].lower() in self.ascii_extensions

    def port_range(self) -> Optional[Tuple[int, int]]:
        return parse_port_range(self.active_port_range)


def encoding_known(name: str) -> bool:
    try:
        codecs.lookup(name)
        return True
    except (LookupError, TypeError):
        return False


def parse_port_range(text: str) -> Optional[Tuple[int, int]]:
    try:
        low_text, high_text = (part.strip() for part in text.split("-", 1))
        low, high = int(low_text), int(high_text)
    except (ValueError, AttributeError):
        return None
    if 1024 <= low <= high <= 65535:
        return low, high
    return None


def _coerce(key: str, value: Any, default: Any) -> Any:
    """``value`` as the type of ``default``, or ``None`` when it cannot be."""

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
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            return list(value)
        return None
    if isinstance(default, str):
        return value.strip() if isinstance(value, str) else None
    return None


def default_port_for(protocol: str, settings: Optional[TransferSettings] = None) -> int:
    if protocol == "ftps" and settings is not None and settings.tls_mode == "implicit":
        return IMPLICIT_FTPS_PORT
    return {"ftp": 21, "ftps": 21, "sftp": 22}.get(protocol, 21)
