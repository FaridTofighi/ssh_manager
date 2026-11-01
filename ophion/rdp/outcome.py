"""What a FreeRDP run means: connected (verified), and why it ended (pure Python).

The table is derived from FreeRDP's own sources (2.11.7 and 3.15.0):

* ``client/X11/xfreerdp.h`` ``enum XF_EXIT_CODE`` (identical in both, except
  that 3.x dropped 146 ``SECURITY_NEGO_CONNECT_FAILED``): 0–15 protocol
  independent (after a connection they are the server's ``ERRINFO`` code),
  16–26 licensing, 128–159 connection errors, 255 unknown;
* ``libfreerdp/core/errconnect.c`` names (``ERRCONNECT_*``), logged at ERROR by
  ``freerdp_set_last_error_ex`` as ``ERRCONNECT_LOGON_FAILURE [0x00020014]``;
* ``libfreerdp/core/errinfo.c`` names (``ERRINFO_*``), logged at INFO on
  ``com.freerdp.core`` by ``rdp_print_errinfo`` (the launcher enables it);
* ``libfreerdp/crypto/tls.c``: "certificate not trusted, aborting." and
  "REMOTE HOST IDENTIFICATION HAS CHANGED".

**Verified success:** ``PostConnect`` runs only after the connection is
active (``rdp_client_connect`` waits for activation in both versions), and
every client calls ``gdi_init`` there, which logs "Local framebuffer format"
at INFO on ``com.freerdp.gdi`` (enabled by the launcher). With NLA that line
proves the credentials were accepted; with TLS or standard RDP security the
sign-in happens on the remote screen, so it only proves the connection.

Only :attr:`Kind.AUTH_FAILED` may open the password dialog; account,
network, certificate and security problems never do.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Optional, Tuple

SUCCESS_RE = re.compile(r"Local framebuffer format")
_ERRCONNECT_RE = re.compile(r"\b(ERRCONNECT_[A-Z_]+)\b")
_ERRINFO_RE = re.compile(r"\b(ERRINFO_[A-Z0-9_]+)\b")
_CERT_REJECTED_RE = re.compile(r"certificate not trusted, aborting|certificate denied by configuration|"
                               r"REMOTE HOST IDENTIFICATION HAS CHANGED|The host key for .* has changed",
                               re.IGNORECASE)
_PARSE_QUIRK_RE = re.compile(r"Command line parsing failed at '.*' \[0\]$")  # 3.15 logs this on success too


class Kind(str, Enum):
    OK_DISCONNECT = "ok_disconnect"  # the session ended normally (user closed it, logoff, server ended it)
    AUTH_FAILED = "auth_failed"
    ACCOUNT_PROBLEM = "account_problem"
    CERT_REJECTED = "cert_rejected"
    NETWORK = "network"
    DNS = "dns"
    TLS = "tls"
    SECURITY_MISMATCH = "security_mismatch"
    NEEDS_ENGINE_FEATURE = "needs_engine_feature"
    CANCELLED = "cancelled"
    OTHER = "other"


@dataclass(frozen=True)
class RdpOutcome:
    kind: Kind
    reason: str  # a short id: "logoff", "idle_timeout", "replaced", "locked_out", "wrong_password" …
    message: str  # for the user (English; the UI translates)
    technical: str = ""  # token / exit code (never secrets)
    connected: bool = False  # the session had been verified as connected before it ended

    @property
    def retry_password(self) -> bool:
        return self.kind is Kind.AUTH_FAILED

    @property
    def auto_reconnect(self) -> bool:
        """Reconnecting without asking makes sense (a dropped network, not a logoff or a takeover)."""

        return self.connected and self.kind is Kind.NETWORK

    @property
    def is_error(self) -> bool:
        return self.kind not in (Kind.OK_DISCONNECT, Kind.CANCELLED)


_E = Tuple[Kind, str, str]
# ERRCONNECT_* → (kind, reason, message)
ERRCONNECT: dict = {
    "ERRCONNECT_LOGON_FAILURE": (Kind.AUTH_FAILED, "logon_failure", "The user name or password is not correct."),
    "ERRCONNECT_WRONG_PASSWORD": (Kind.AUTH_FAILED, "wrong_password", "The password is not correct."),
    "ERRCONNECT_AUTHENTICATION_FAILED": (Kind.AUTH_FAILED, "authentication_failed",
                                         "The server did not accept the credentials."),
    "ERRCONNECT_NO_OR_MISSING_CREDENTIALS": (Kind.AUTH_FAILED, "missing_credentials",
                                             "The server needs a user name and password."),
    "ERRCONNECT_ACCOUNT_LOCKED_OUT": (Kind.ACCOUNT_PROBLEM, "locked_out",
                                      "The account is locked out. Ask an administrator to unlock it; trying "
                                      "again now keeps it locked."),
    "ERRCONNECT_ACCOUNT_DISABLED": (Kind.ACCOUNT_PROBLEM, "disabled", "The account is disabled."),
    "ERRCONNECT_ACCOUNT_EXPIRED": (Kind.ACCOUNT_PROBLEM, "account_expired", "The account has expired."),
    "ERRCONNECT_PASSWORD_EXPIRED": (Kind.ACCOUNT_PROBLEM, "password_expired",
                                    "The password has expired. Change it (for example in a browser-based "
                                    "portal or on a Windows computer), then connect again."),
    "ERRCONNECT_PASSWORD_CERTAINLY_EXPIRED": (Kind.ACCOUNT_PROBLEM, "password_expired",
                                              "The password has expired. Change it, then connect again."),
    "ERRCONNECT_PASSWORD_MUST_CHANGE": (Kind.ACCOUNT_PROBLEM, "password_must_change",
                                        "The password must be changed before the first sign-in."),
    "ERRCONNECT_LOGON_TYPE_NOT_GRANTED": (Kind.ACCOUNT_PROBLEM, "no_logon_right",
                                          "This user may not sign in remotely (not in the Remote Desktop "
                                          "Users group, or remote sign-in is denied by policy)."),
    "ERRCONNECT_INSUFFICIENT_PRIVILEGES": (Kind.ACCOUNT_PROBLEM, "insufficient_privileges",
                                           "The account does not have the rights to connect."),
    "ERRCONNECT_ACCESS_DENIED": (Kind.ACCOUNT_PROBLEM, "access_denied", "The server denied access."),
    "ERRCONNECT_ACCOUNT_RESTRICTION": (Kind.ACCOUNT_PROBLEM, "account_restriction",
                                       "An account restriction prevents the sign-in (for example sign-in "
                                       "hours, or a blank password)."),
    "ERRCONNECT_CLIENT_REVOKED": (Kind.ACCOUNT_PROBLEM, "client_revoked",
                                  "The client certificate or account has been revoked."),
    "ERRCONNECT_DNS_NAME_NOT_FOUND": (Kind.DNS, "dns", "The host name could not be found."),
    "ERRCONNECT_DNS_ERROR": (Kind.DNS, "dns", "The host name could not be looked up."),
    "ERRCONNECT_CONNECT_TRANSPORT_FAILED": (Kind.NETWORK, "transport", "The connection to the server failed."),
    "ERRCONNECT_CONNECT_FAILED": (Kind.NETWORK, "connect_failed", "The server could not be reached."),
    "ERRCONNECT_MCS_CONNECT_INITIAL_ERROR": (Kind.NETWORK, "mcs", "The server ended the connection early."),
    "ERRCONNECT_KDC_UNREACHABLE": (Kind.NETWORK, "kdc", "The Kerberos server (KDC) could not be reached."),
    "ERRCONNECT_ACTIVATION_TIMEOUT": (Kind.NETWORK, "activation_timeout",
                                      "The server did not finish the connection in time."),
    "ERRCONNECT_TLS_CONNECT_FAILED": (Kind.TLS, "tls", "The secure (TLS) connection failed."),
    "ERRCONNECT_SECURITY_NEGO_CONNECT_FAILED": (Kind.SECURITY_MISMATCH, "nego",
                                                "The server and this host do not agree on a security mode. "
                                                "Try another setting under Security (Automatic usually works)."),
    "ERRCONNECT_CONNECT_CANCELLED": (Kind.CANCELLED, "cancelled", "The connection was cancelled."),
    "ERRCONNECT_PRE_CONNECT_FAILED": (Kind.NEEDS_ENGINE_FEATURE, "pre_connect",
                                      "FreeRDP could not prepare the connection (an option or channel it "
                                      "cannot load)."),
    "ERRCONNECT_POST_CONNECT_FAILED": (Kind.NEEDS_ENGINE_FEATURE, "post_connect",
                                       "FreeRDP connected but could not open the remote display."),
    "ERRCONNECT_CONNECT_UNDEFINED": (Kind.OTHER, "undefined", "The connection failed."),
}

# ERRINFO_* (server-side reason, logged at INFO) → (kind, reason, message)
ERRINFO: dict = {
    "ERRINFO_RPC_INITIATED_DISCONNECT": (Kind.OK_DISCONNECT, "admin_disconnect",
                                         "An administrator disconnected the session."),
    "ERRINFO_RPC_INITIATED_LOGOFF": (Kind.OK_DISCONNECT, "admin_logoff", "An administrator signed the session out."),
    "ERRINFO_IDLE_TIMEOUT": (Kind.OK_DISCONNECT, "idle_timeout", "The session was idle too long and was ended."),
    "ERRINFO_LOGON_TIMEOUT": (Kind.OK_DISCONNECT, "logon_timeout", "The session's time limit was reached."),
    "ERRINFO_DISCONNECTED_BY_OTHER_CONNECTION": (Kind.OK_DISCONNECT, "replaced",
                                                 "Someone else connected to this session."),
    "ERRINFO_SERVER_DENIED_CONNECTION": (Kind.ACCOUNT_PROBLEM, "denied", "The server refused the connection."),
    "ERRINFO_SERVER_INSUFFICIENT_PRIVILEGES": (Kind.ACCOUNT_PROBLEM, "insufficient_privileges",
                                               "The account does not have the rights to connect."),
    "ERRINFO_SERVER_FRESH_CREDENTIALS_REQUIRED": (Kind.AUTH_FAILED, "fresh_credentials",
                                                  "The server wants the password typed again."),
    "ERRINFO_RPC_INITIATED_DISCONNECT_BY_USER": (Kind.OK_DISCONNECT, "user_disconnect",
                                                 "The session was disconnected from the remote computer."),
    "ERRINFO_LOGOFF_BY_USER": (Kind.OK_DISCONNECT, "logoff", "You signed out of the remote computer."),
}

# exit code → (kind, reason, message) — XF_EXIT_CODE (2.11 and 3.15)
EXIT_CODES: dict = {
    0: (Kind.OK_DISCONNECT, "closed", "The session ended."),
    1: (Kind.OK_DISCONNECT, "admin_disconnect", "The server ended the session."),
    2: (Kind.OK_DISCONNECT, "admin_logoff", "The session was signed out."),
    3: ERRINFO["ERRINFO_IDLE_TIMEOUT"],
    4: ERRINFO["ERRINFO_LOGON_TIMEOUT"],
    5: ERRINFO["ERRINFO_DISCONNECTED_BY_OTHER_CONNECTION"],
    6: (Kind.OTHER, "out_of_memory", "The server ran out of memory."),
    7: ERRINFO["ERRINFO_SERVER_DENIED_CONNECTION"],
    8: (Kind.SECURITY_MISMATCH, "fips", "The server requires FIPS-compliant encryption."),
    9: ERRINFO["ERRINFO_SERVER_INSUFFICIENT_PRIVILEGES"],
    10: ERRINFO["ERRINFO_SERVER_FRESH_CREDENTIALS_REQUIRED"],
    11: ERRINFO["ERRINFO_RPC_INITIATED_DISCONNECT_BY_USER"],
    12: ERRINFO["ERRINFO_LOGOFF_BY_USER"],
    128: (Kind.NEEDS_ENGINE_FEATURE, "arguments", "The FreeRDP client did not accept the connection options."),
    129: (Kind.OTHER, "memory", "FreeRDP ran out of memory."),
    130: (Kind.OTHER, "protocol", "An RDP protocol error ended the connection."),
    131: (Kind.NETWORK, "connect_failed", "The connection failed."),
    132: ERRCONNECT["ERRCONNECT_AUTHENTICATION_FAILED"],
    133: ERRCONNECT["ERRCONNECT_SECURITY_NEGO_CONNECT_FAILED"],
    134: ERRCONNECT["ERRCONNECT_LOGON_FAILURE"],
    135: ERRCONNECT["ERRCONNECT_ACCOUNT_LOCKED_OUT"],
    136: ERRCONNECT["ERRCONNECT_PRE_CONNECT_FAILED"],
    137: ERRCONNECT["ERRCONNECT_CONNECT_UNDEFINED"],
    138: ERRCONNECT["ERRCONNECT_POST_CONNECT_FAILED"],
    139: ERRCONNECT["ERRCONNECT_DNS_ERROR"],
    140: ERRCONNECT["ERRCONNECT_DNS_NAME_NOT_FOUND"],
    141: ERRCONNECT["ERRCONNECT_CONNECT_FAILED"],
    142: ERRCONNECT["ERRCONNECT_MCS_CONNECT_INITIAL_ERROR"],
    143: ERRCONNECT["ERRCONNECT_TLS_CONNECT_FAILED"],
    144: ERRCONNECT["ERRCONNECT_INSUFFICIENT_PRIVILEGES"],
    145: ERRCONNECT["ERRCONNECT_CONNECT_CANCELLED"],
    146: ERRCONNECT["ERRCONNECT_SECURITY_NEGO_CONNECT_FAILED"],  # 2.x only
    147: ERRCONNECT["ERRCONNECT_CONNECT_TRANSPORT_FAILED"],
    148: ERRCONNECT["ERRCONNECT_PASSWORD_EXPIRED"],
    149: ERRCONNECT["ERRCONNECT_PASSWORD_MUST_CHANGE"],
    150: ERRCONNECT["ERRCONNECT_KDC_UNREACHABLE"],
    151: ERRCONNECT["ERRCONNECT_ACCOUNT_DISABLED"],
    152: ERRCONNECT["ERRCONNECT_PASSWORD_CERTAINLY_EXPIRED"],
    153: ERRCONNECT["ERRCONNECT_CLIENT_REVOKED"],
    154: ERRCONNECT["ERRCONNECT_WRONG_PASSWORD"],
    155: ERRCONNECT["ERRCONNECT_ACCESS_DENIED"],
    156: ERRCONNECT["ERRCONNECT_ACCOUNT_RESTRICTION"],
    157: ERRCONNECT["ERRCONNECT_ACCOUNT_EXPIRED"],
    158: ERRCONNECT["ERRCONNECT_LOGON_TYPE_NOT_GRANTED"],
    159: ERRCONNECT["ERRCONNECT_NO_OR_MISSING_CREDENTIALS"],
    255: (Kind.OTHER, "unknown", "FreeRDP ended with an unknown error."),
}
LICENSE_MESSAGE = "The server could not issue a Remote Desktop license (licensing error {code})."
CERT_MESSAGE = ("The server's certificate was not the trusted one, so the connection was stopped. If it changed "
                "on purpose, test the connection in the host dialog and trust the new certificate.")


def connected_line(line: str) -> bool:
    """True for the log line that proves the session became active (see the module docstring)."""

    return bool(SUCCESS_RE.search(line or ""))


def classify_rdp_exit(code: Optional[int], lines: Iterable[str], version: int = 3, *,
                      connected: bool = False, stopped_by_user: bool = False) -> RdpOutcome:
    """The outcome of a finished FreeRDP process (``code`` < 0: killed by that signal)."""

    tokens_connect: list = []
    tokens_info: list = []
    cert = False
    for line in lines:
        if _PARSE_QUIRK_RE.search(line):
            continue
        tokens_connect += _ERRCONNECT_RE.findall(line)
        tokens_info += _ERRINFO_RE.findall(line)
        if _CERT_REJECTED_RE.search(line):
            cert = True
    if stopped_by_user:
        return RdpOutcome(Kind.OK_DISCONNECT, "user_disconnect", "You disconnected the session.",
                          f"exit {code}", connected)
    if cert:
        return RdpOutcome(Kind.CERT_REJECTED, "certificate", CERT_MESSAGE, "certificate not trusted", connected)
    for token in tokens_connect:  # the first specific connect error is the cause; later ones are follow-ups
        if token in ERRCONNECT and token not in ("ERRCONNECT_CONNECT_UNDEFINED",):
            kind, reason, message = ERRCONNECT[token]
            return RdpOutcome(kind, reason, message, token, connected)
    for token in reversed(tokens_info):  # the last server reason before the end
        if token in ERRINFO:
            kind, reason, message = ERRINFO[token]
            return RdpOutcome(kind, reason, message, token, connected)
    if code is None or code < 0:
        if connected:
            return RdpOutcome(Kind.NETWORK, "killed", "The FreeRDP process ended unexpectedly.",
                              f"signal {-(code or 0)}", connected)
        return RdpOutcome(Kind.OTHER, "killed", "The FreeRDP process ended unexpectedly.",
                          f"signal {-(code or 0)}", connected)
    if 16 <= code <= 26:
        return RdpOutcome(Kind.OTHER, "license", LICENSE_MESSAGE.format(code=code), f"exit {code}", connected)
    if code in EXIT_CODES:
        kind, reason, message = EXIT_CODES[code]
        if kind is Kind.OK_DISCONNECT and code == 1 and not connected:
            return RdpOutcome(Kind.NETWORK, "disconnected", "The connection ended before the session started.",
                              f"exit {code}", connected)
        return RdpOutcome(kind, reason, message, f"exit {code}", connected)
    if 32 <= code <= 127:
        return RdpOutcome(Kind.OTHER, "protocol", "An RDP protocol error ended the session.", f"exit {code}",
                          connected)
    return RdpOutcome(Kind.OTHER, "unknown", "FreeRDP ended with an error.", f"exit {code}", connected)


__all__ = ["classify_rdp_exit", "connected_line", "RdpOutcome", "Kind", "ERRCONNECT", "ERRINFO", "EXIT_CODES"]
