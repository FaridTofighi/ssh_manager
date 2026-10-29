"""Certificate trust for RDP, decided by the app *before* FreeRDP starts (pure Python).

FreeRDP would ask about an unknown certificate on its own console, which the
app cannot show; so the app probes the server first (:mod:`.probe`), decides
here, and launches FreeRDP with ``/cert:deny,fingerprint:sha256:…`` for the
certificate it accepted (or ``/cert:ignore`` only when the user chose that).
If the server then presents another certificate, FreeRDP refuses it and the
session ends with :attr:`Kind.CERT_REJECTED`.

Policies (host setting ``cert_policy``):

=========  ===================  ====================  ========================
policy     no pin yet           pin matches           pin differs
=========  ===================  ====================  ========================
ask        ask (once/remember)  connect               CHANGED warning (Cancel)
tofu       remember, connect    connect               CHANGED warning (Cancel)
pinned     ask to pin           connect               CHANGED warning (Cancel)
deny       refuse               connect               refuse
ignore     confirm every time, ``/cert:ignore``, "Certificate not verified"
=========  ===================  ====================  ========================

A changed certificate is never accepted automatically; the warning's default
button is Cancel.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Optional, Set

from ..trust import CertificateInfo, normalize_fingerprint


class Action(str, Enum):
    CONNECT = "connect"  # launch with the fingerprint
    ASK = "ask"  # first time: Trust once / Trust and remember / Cancel
    REMEMBER = "remember"  # TOFU: store the pin, then connect
    CHANGED = "changed"  # strong warning; Cancel is the default
    REFUSE = "refuse"  # policy "deny" without a matching pin
    CONFIRM_IGNORE = "confirm_ignore"  # policy "ignore": confirm every time
    NO_TLS = "no_tls"  # standard RDP security: there is no TLS certificate to check
    UNREADABLE = "unreadable"  # the probe could not read a certificate


@dataclass(frozen=True)
class TrustDecision:
    action: Action
    fingerprint: str = ""  # the presented certificate ("AB:CD:…")
    expected: str = ""  # a pinned fingerprint it differs from (CHANGED)


def decide(policy: str, certificate: Optional[CertificateInfo], pins: Iterable[str], *,
           security: str = "") -> TrustDecision:
    """What to do with the certificate the probe saw (``pins``: TrustStore + host settings)."""

    if policy == "ignore":
        return TrustDecision(Action.CONFIRM_IGNORE, certificate.sha256 if certificate else "")
    if security == "rdp":
        return TrustDecision(Action.NO_TLS)
    if certificate is None or not certificate.sha256:
        return TrustDecision(Action.UNREADABLE)
    presented = normalize_fingerprint(certificate.sha256)
    known: Set[str] = {normalize_fingerprint(p) for p in pins if normalize_fingerprint(p)}
    if presented in known:
        return TrustDecision(Action.CONNECT, presented)
    if known:
        if policy == "deny":
            return TrustDecision(Action.REFUSE, presented, sorted(known)[0])
        return TrustDecision(Action.CHANGED, presented, sorted(known)[0])
    if policy == "tofu":
        return TrustDecision(Action.REMEMBER, presented)
    if policy == "deny":
        return TrustDecision(Action.REFUSE, presented)
    return TrustDecision(Action.ASK, presented)  # ask, pinned (nothing pinned yet)


__all__ = ["decide", "TrustDecision", "Action"]
