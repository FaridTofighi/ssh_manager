"""Trusted TLS certificates, shared by FTPS and RDP (pure Python).

One JSON file, ``$XDG_DATA_HOME/ophion/tls-pins.json`` (mode 0600 in a
0700 folder): ``{"<key>": {"sha256": "AB:CD:…", "subject": "…", "added":
<epoch>}}``. A key is :func:`pin_key` — the host id *and* the address, so
moving a host to another server never reuses an old pin. RDP keys carry the
scope ``rdp:`` so an FTPS pin and an RDP pin on the same address never mix
(the file and the FTPS keys are unchanged from before the move).

:class:`CertificateInfo` and :func:`certificate_info` describe a peer
certificate for the "Trust this certificate?" dialogs. Every field comes from
an untrusted peer: escape it before it is used as markup.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

from .persistence import write_json_atomic


@dataclass(frozen=True)
class CertificateInfo:
    """What the "Trust this certificate?" dialog shows (all from the untrusted peer: escape before markup)."""

    subject: str = ""
    issuer: str = ""
    not_before: str = ""
    not_after: str = ""
    sha256: str = ""  # "AB:CD:…"
    reason: str = ""  # why it is not trusted (verification error)


def certificate_info(der: bytes) -> CertificateInfo:
    """Subject, issuer, validity and SHA-256 of a DER certificate (only the fingerprint if it does not parse)."""

    if not der:
        return CertificateInfo()
    sha = fingerprint(der)
    try:
        from cryptography import x509  # paramiko's dependency

        cert = x509.load_der_x509_certificate(der)
        before = getattr(cert, "not_valid_before_utc", None) or cert.not_valid_before
        after = getattr(cert, "not_valid_after_utc", None) or cert.not_valid_after
        return CertificateInfo(cert.subject.rfc4514_string()[:512], cert.issuer.rfc4514_string()[:512],
                               before.strftime("%Y-%m-%d %H:%M UTC"), after.strftime("%Y-%m-%d %H:%M UTC"), sha)
    except Exception:
        return CertificateInfo(sha256=sha)


def default_pins_path() -> Path:
    from .legacy_paths import app_dir

    return app_dir("data") / "tls-pins.json"


def fingerprint(der: bytes) -> str:
    """``AB:CD:…`` SHA-256 of a DER certificate."""

    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))


def normalize_fingerprint(text: str) -> str:
    hexdigits = "".join(ch for ch in (text or "").upper() if ch in "0123456789ABCDEF")
    if len(hexdigits) != 64:
        return ""
    return ":".join(hexdigits[i:i + 2] for i in range(0, 64, 2))


def pin_key(host_id: str, hostname: str, port: int, scope: str = "") -> str:
    """``<host id>|<host>:<port>`` (FTPS), or ``<scope>:<host id>|<host>:<port>`` (``scope="rdp"``)."""

    key = f"{host_id or '-'}|{hostname.lower()}:{int(port)}"
    return f"{scope}:{key}" if scope else key


class TrustStore:
    """Load, query and save trusted fingerprints (thread-safe, small file)."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path is not None else default_pins_path()
        self._lock = threading.Lock()

    def _load(self) -> Dict[str, dict]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return {k: v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}

    def get(self, key: str) -> str:
        with self._lock:
            return normalize_fingerprint(str(self._load().get(key, {}).get("sha256", "")))

    def set(self, key: str, sha256: str, subject: str = "") -> None:
        value = normalize_fingerprint(sha256)
        if not value:
            raise ValueError("not a SHA-256 fingerprint")
        with self._lock:
            data = self._load()
            data[key] = {"sha256": value, "subject": subject[:256], "added": int(time.time())}
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            write_json_atomic(self.path, data, mode=0o600)

    def forget(self, key: str) -> bool:
        with self._lock:
            data = self._load()
            if data.pop(key, None) is None:
                return False
            write_json_atomic(self.path, data, mode=0o600)
            return True


PinStore = TrustStore  # the FTPS name
