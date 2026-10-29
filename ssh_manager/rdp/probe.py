"""RDP "Test connection": reachability, negotiated security and the server certificate (pure Python).

The probe speaks only the first exchange of an RDP connection ([MS-RDPBCGR]
2.2.1.1 / 2.2.1.2): a TPKT + X.224 Connection Request carrying an
``RDP_NEG_REQ``, and the server's Connection Confirm with an ``RDP_NEG_RSP``
(the protocol it selected) or an ``RDP_NEG_FAILURE`` (why it refused). When
the server selected TLS or NLA, a TLS handshake **without verification**
reads its certificate, only to show it; the connection is then closed. NLA
(CredSSP) is never started and no credential is ever sent.

Everything the server sends is untrusted: every read has a byte limit and a
timeout, only the documented fields are interpreted, and a malformed reply is
reported as such instead of raising.
"""

from __future__ import annotations

import socket
import ssl
import struct
import time
import warnings
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

from ..trust import CertificateInfo, certificate_info, normalize_fingerprint

# requestedProtocols / selectedProtocol flags ([MS-RDPBCGR] 2.2.1.1.1)
PROTOCOL_RDP = 0x00000000
PROTOCOL_SSL = 0x00000001
PROTOCOL_HYBRID = 0x00000002
PROTOCOL_RDSTLS = 0x00000004
PROTOCOL_HYBRID_EX = 0x00000008
PROTOCOL_RDSAAD = 0x00000010

TYPE_RDP_NEG_REQ = 0x01
TYPE_RDP_NEG_RSP = 0x02
TYPE_RDP_NEG_FAILURE = 0x03

# failureCode ([MS-RDPBCGR] 2.2.1.2.2)
FAILURES = {
    0x01: ("SSL_REQUIRED_BY_SERVER", "The server requires TLS security."),
    0x02: ("SSL_NOT_ALLOWED_BY_SERVER", "The server only allows standard RDP security (no TLS)."),
    0x03: ("SSL_CERT_NOT_ON_SERVER", "The server has no certificate for TLS."),
    0x04: ("INCONSISTENT_FLAGS", "The server rejected the requested security flags."),
    0x05: ("HYBRID_REQUIRED_BY_SERVER", "The server requires Network Level Authentication (NLA)."),
    0x06: ("SSL_WITH_USER_AUTH_REQUIRED_BY_SERVER", "The server requires TLS with user authentication."),
}

MAX_TPKT = 4096  # a Connection Confirm is 11–19 bytes; anything this big is not one
DEFAULT_TIMEOUT = 8.0


@dataclass
class Negotiation:
    """The outcome of one Connection Request."""

    ok: bool = False
    selected: Optional[int] = None  # selectedProtocol, or None when refused / no negotiation data
    legacy: bool = False  # Connection Confirm without negotiation data: standard RDP security only
    failure_code: int = 0
    failure_name: str = ""
    error: str = ""  # malformed or no reply (English)


@dataclass
class ProbeResult:
    host: str
    port: int
    reachable: bool = False
    latency_ms: float = 0.0
    security: str = ""  # "nla_ext" | "nla" | "tls" | "rdp" | "rdstls" | "aad" | ""
    security_label: str = ""
    failure: str = ""  # RDP_NEG_FAILURE name, if any
    certificate: Optional[CertificateInfo] = None
    pin_status: str = ""  # "match" | "mismatch" | "none" | ""
    tls_version: str = ""
    error: str = ""
    notes: List[str] = field(default_factory=list)

    def lines(self) -> List[str]:
        """Plain-text report for the dialog (no markup; every field is from the server)."""

        out = [f"Server: {self.host}:{self.port}"]
        if not self.reachable:
            out.append(f"Not reachable: {self.error or 'no answer'}")
            return out
        out.append(f"Reachable · {self.latency_ms:.0f} ms")
        if self.security_label:
            out.append(f"Security: {self.security_label}")
        if self.failure:
            out.append(f"Refused: {self.failure}")
        if self.tls_version:
            out.append(f"TLS: {self.tls_version}")
        cert = self.certificate
        if cert is not None and cert.sha256:
            out += [f"Certificate subject: {cert.subject or '?'}", f"Issuer: {cert.issuer or '?'}",
                    f"Valid: {cert.not_before or '?'} – {cert.not_after or '?'}", f"SHA-256: {cert.sha256}"]
            if self.pin_status == "match":
                out.append("The certificate matches the trusted (pinned) one.")
            elif self.pin_status == "mismatch":
                out.append("WARNING: the certificate differs from the trusted (pinned) one.")
        if self.error:
            out.append(self.error)
        out += self.notes
        return out


def security_name(selected: int) -> Tuple[str, str]:
    """``(id, label)`` of a selectedProtocol value."""

    if selected & PROTOCOL_HYBRID_EX:
        return "nla_ext", "NLA with Early User Authorization (CredSSP + TLS)"
    if selected & PROTOCOL_HYBRID:
        return "nla", "Network Level Authentication (CredSSP + TLS)"
    if selected & PROTOCOL_RDSAAD:
        return "aad", "Azure AD authentication (RDSAAD)"
    if selected & PROTOCOL_RDSTLS:
        return "rdstls", "RDSTLS"
    if selected & PROTOCOL_SSL:
        return "tls", "TLS"
    return "rdp", "Standard RDP security (no TLS; weak)"


# ------------------------------------------------------------------ packets
def connection_request(requested: int, cookie: str = "") -> bytes:
    """TPKT + X.224 CR TPDU + optional ``Cookie: mstshash=…`` + RDP_NEG_REQ."""

    body = b""
    if cookie:
        safe = "".join(ch for ch in cookie if ch.isalnum() or ch in "._-")[:32]
        body += f"Cookie: mstshash={safe}\r\n".encode("ascii")
    body += struct.pack("<BBHI", TYPE_RDP_NEG_REQ, 0, 8, requested)
    x224 = struct.pack(">BBHHB", 6 + len(body), 0xE0, 0, 0, 0) + body  # LI, CR|CDT, DST-REF, SRC-REF, class 0
    return struct.pack(">BBH", 3, 0, 4 + len(x224)) + x224


def parse_connection_confirm(data: bytes) -> Negotiation:
    """Interpret a TPKT + X.224 Connection Confirm (never raises)."""

    if len(data) < 11:
        return Negotiation(error="The reply is too short to be an RDP Connection Confirm.")
    version, _reserved, length = struct.unpack(">BBH", data[:4])
    if version != 3:
        return Negotiation(error="The server did not answer with RDP (TPKT version is not 3).")
    if length != len(data) or length > MAX_TPKT:
        return Negotiation(error="The RDP reply has an inconsistent length.")
    li, code = data[4], data[5]
    if code & 0xF0 != 0xD0:
        return Negotiation(error="The server did not confirm the connection (no X.224 Connection Confirm).")
    if li + 5 != length:
        return Negotiation(error="The X.224 header length does not match the packet.")
    rest = data[11:]
    if not rest:
        return Negotiation(ok=True, selected=PROTOCOL_RDP, legacy=True)
    if len(rest) < 8:
        return Negotiation(error="The negotiation data is truncated.")
    neg_type, _flags, neg_len, value = struct.unpack("<BBHI", rest[:8])
    if neg_len != 8:
        return Negotiation(error="The negotiation data has an unexpected length.")
    if neg_type == TYPE_RDP_NEG_RSP:
        if value & ~0x1F:
            return Negotiation(error="The server selected an unknown security protocol.")
        return Negotiation(ok=True, selected=value)
    if neg_type == TYPE_RDP_NEG_FAILURE:
        name = FAILURES.get(value, (f"UNKNOWN_FAILURE_{value:#x}", ""))[0]
        return Negotiation(ok=False, failure_code=value, failure_name=name)
    return Negotiation(error="The negotiation reply has an unknown type.")


def _recv_tpkt(sock: socket.socket, deadline: float) -> bytes:
    """One TPKT (header first, then the announced length; both bounded)."""

    header = _recv_exact(sock, 4, deadline)
    if len(header) < 4:
        return header
    length = struct.unpack(">H", header[2:4])[0]
    if header[0] != 3 or not 4 <= length <= MAX_TPKT:
        return header  # parse_connection_confirm explains what is wrong
    return header + _recv_exact(sock, length - 4, deadline)


def _recv_exact(sock: socket.socket, size: int, deadline: float) -> bytes:
    data = b""
    while len(data) < size:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise socket.timeout("timed out")
        sock.settimeout(remaining)
        chunk = sock.recv(min(size - len(data), MAX_TPKT))
        if not chunk:
            break
        data += chunk
    return data


# ------------------------------------------------------------------- probe
Connect = Callable[[str, int, float], socket.socket]


def _connect(host: str, port: int, timeout: float) -> socket.socket:
    return socket.create_connection((host, port), timeout=timeout)


def negotiate(host: str, port: int, requested: int, *, timeout: float = DEFAULT_TIMEOUT,
              connect: Connect = _connect) -> Tuple[Negotiation, Optional[socket.socket], float]:
    """Connect, send one Connection Request; ``(negotiation, open socket or None, connect ms)``."""

    started = time.monotonic()
    sock = connect(host, port, timeout)
    latency = (time.monotonic() - started) * 1000.0
    deadline = time.monotonic() + timeout
    try:
        sock.settimeout(timeout)
        sock.sendall(connection_request(requested))
        reply = _recv_tpkt(sock, deadline)
    except (OSError, socket.timeout) as exc:
        sock.close()
        reason = "timed out" if isinstance(exc, socket.timeout) else (exc.strerror or type(exc).__name__)
        return Negotiation(error=f"No RDP reply ({reason})."), None, latency
    if not reply:
        sock.close()
        return Negotiation(error="The server closed the connection without answering."), None, latency
    result = parse_connection_confirm(reply)
    if not result.ok:
        sock.close()
        return result, None, latency
    return result, sock, latency


def _read_certificate(sock: socket.socket, host: str, timeout: float) -> Tuple[Optional[CertificateInfo], str, str]:
    """TLS handshake without verification, only to read the certificate; ``(info, tls version, error)``."""

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE  # display only: nothing is sent over this connection
    try:
        with warnings.catch_warnings():  # TLS 1.0 is deprecated; here it only lets old servers show their certificate
            warnings.simplefilter("ignore", DeprecationWarning)
            context.minimum_version = ssl.TLSVersion.TLSv1
        context.set_ciphers("DEFAULT:@SECLEVEL=0")
    except (ValueError, ssl.SSLError):
        pass
    try:
        sock.settimeout(timeout)
        with context.wrap_socket(sock, server_hostname=host if not _is_ip(host) else None) as tls:
            der = tls.getpeercert(binary_form=True) or b""
            return certificate_info(der), tls.version() or "", ""
    except (OSError, ssl.SSLError, ValueError) as exc:
        return None, "", f"TLS handshake failed: {type(exc).__name__}"


def _is_ip(host: str) -> bool:
    for family in (socket.AF_INET, socket.AF_INET6):
        try:
            socket.inet_pton(family, host)
            return True
        except OSError:
            continue
    return False


def probe(host: str, port: int = 3389, *, timeout: float = DEFAULT_TIMEOUT, pinned: Tuple[str, ...] = (),
          configured_security: str = "auto", connect: Connect = _connect) -> ProbeResult:
    """Reachability, security and certificate of an RDP server (never raises; never sends credentials)."""

    result = ProbeResult(host, port)
    requested = PROTOCOL_SSL | PROTOCOL_HYBRID | PROTOCOL_HYBRID_EX
    try:
        negotiation, sock, latency = negotiate(host, port, requested, timeout=timeout, connect=connect)
    except (OSError, socket.timeout) as exc:
        result.error = "timed out" if isinstance(exc, socket.timeout) else (exc.strerror or type(exc).__name__)
        return result
    result.reachable, result.latency_ms = True, latency
    if not negotiation.ok and not negotiation.failure_name and "closed" in negotiation.error:
        try:  # some old servers hang up on flags they do not know: ask again without HYBRID_EX
            negotiation, sock, _ms = negotiate(host, port, PROTOCOL_SSL | PROTOCOL_HYBRID, timeout=timeout,
                                               connect=connect)
        except (OSError, socket.timeout):
            pass
    if not negotiation.ok and negotiation.failure_name:
        result.failure = negotiation.failure_name
        if negotiation.failure_code == 0x02:  # TLS not allowed: does it accept standard RDP security?
            try:
                second, sock2, _ms = negotiate(host, port, PROTOCOL_RDP, timeout=timeout, connect=connect)
            except (OSError, socket.timeout):
                second, sock2 = Negotiation(error="The second probe (standard RDP security) failed."), None
            if sock2 is not None:
                sock2.close()
            if second.ok:
                result.security, result.security_label = security_name(PROTOCOL_RDP)
                result.notes.append("Standard RDP security is weak; use TLS or NLA when the server can.")
                return result
        result.error = FAILURES.get(negotiation.failure_code, ("", "The server refused the security request."))[1]
        return result
    if not negotiation.ok:
        result.error = negotiation.error
        return result
    assert sock is not None and negotiation.selected is not None
    result.security, result.security_label = security_name(negotiation.selected)
    if negotiation.legacy:
        result.notes.append("The server did not negotiate: it only offers standard RDP security (old server).")
    if negotiation.selected & (PROTOCOL_SSL | PROTOCOL_HYBRID | PROTOCOL_HYBRID_EX):
        cert, version, error = _read_certificate(sock, host, timeout)
        result.certificate, result.tls_version = cert, version
        if error:
            result.error = error
        if cert is not None and cert.sha256:
            pins = {normalize_fingerprint(p) for p in pinned if normalize_fingerprint(p)}
            result.pin_status = ("match" if cert.sha256 in pins else "mismatch") if pins else "none"
    else:
        sock.close()
    if result.security in ("nla", "nla_ext") and configured_security in ("rdp", "tls"):
        result.notes.append("The server asks for NLA, but this host forces "
                            f"{'standard RDP' if configured_security == 'rdp' else 'TLS'} security; "
                            "choose Automatic or NLA.")
    if result.security == "rdp" and configured_security in ("nla", "nla_ext", "tls"):
        result.notes.append("The server only offers standard RDP security; this host requires more and will "
                            "not connect.")
    return result


def probe_tls(host: str, port: int = 443, *, timeout: float = DEFAULT_TIMEOUT, pinned: Tuple[str, ...] = (),
              connect: Connect = _connect) -> ProbeResult:
    """An RD Gateway (HTTPS): reachability and its certificate (TLS without verification, display only)."""

    result = ProbeResult(host, port)
    started = time.monotonic()
    try:
        sock = connect(host, port, timeout)
    except (OSError, socket.timeout) as exc:
        result.error = "timed out" if isinstance(exc, socket.timeout) else (exc.strerror or type(exc).__name__)
        return result
    result.reachable, result.latency_ms = True, (time.monotonic() - started) * 1000.0
    result.security, result.security_label = "tls", "TLS (RD Gateway)"
    cert, version, error = _read_certificate(sock, host, timeout)
    result.certificate, result.tls_version, result.error = cert, version, error
    if cert is not None and cert.sha256:
        pins = {normalize_fingerprint(p) for p in pinned if normalize_fingerprint(p)}
        result.pin_status = ("match" if cert.sha256 in pins else "mismatch") if pins else "none"
    return result


__all__ = ["probe", "probe_tls", "ProbeResult", "Negotiation", "connection_request", "parse_connection_confirm",
           "security_name", "PROTOCOL_RDP", "PROTOCOL_SSL", "PROTOCOL_HYBRID", "PROTOCOL_HYBRID_EX"]
