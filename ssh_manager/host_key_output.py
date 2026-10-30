"""Recognise ssh's host-key messages in terminal output (no GTK).

Two cases, each strict enough that text shown *inside* a session (``cat`` of a
saved warning, a log file) never matches:

* **Changed key**: the exact ``@@@ WARNING: REMOTE HOST IDENTIFICATION HAS
  CHANGED! @@@`` block, then ``Host key verification failed.`` as the last
  output, and ssh has exited (the caller says so). Parsed: the host, the
  offending ``known_hosts`` path and line, the key type and the new fingerprint.
* **Unknown key** (first connection): ``The authenticity of host … can't be
  established.``, the fingerprint, and the ``Are you sure you want to continue
  connecting (yes/no/[fingerprint])?`` question as the very last output (ssh is
  waiting for the answer).

The detector keeps a bounded buffer of ANSI-stripped text; bytes may arrive split
anywhere (an incremental UTF-8 decoder).
"""

from __future__ import annotations

import codecs
import re
from dataclasses import dataclass
from typing import Optional, Tuple, Union

from .text_utils import strip_ansi

_CHANGED_BLOCK = re.compile(
    r"^@{20,}[ \t]*\n@[ \t]+WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED![ \t]+@[ \t]*\n@{20,}[ \t]*$",
    re.MULTILINE)
_FAILED_AT_END = re.compile(r"^Host key verification failed\.[ \t]*\s*\Z", re.MULTILINE)
_NEW_FINGERPRINT = re.compile(
    r"^The fingerprint for the (\S+) key sent by the remote host is[ \t]*\n[ \t]*(SHA256:[A-Za-z0-9+/]+=*)\.?",
    re.MULTILINE)
_OFFENDING = re.compile(r"^Offending (\S+) key in (.+?):(\d+)[ \t]*$", re.MULTILINE)
_HOST = re.compile(r"^Host key for (\S+) has changed and you have requested strict checking\.", re.MULTILINE)
_REMOVE_WITH = re.compile(r"ssh-keygen -f '.*?' -R '([^']+)'")

_AUTHENTICITY = re.compile(r"^The authenticity of host '(.+?)(?: \((.*?)\))?' can't be established\.[ \t]*$",
                           re.MULTILINE)
_KEY_FINGERPRINT = re.compile(r"^(\S+) key fingerprint is (SHA256:[A-Za-z0-9+/]+=*)\.?[ \t]*$", re.MULTILINE)
_QUESTION_AT_END = re.compile(
    r"^Are you sure you want to continue connecting \(yes/no(?:/\[fingerprint\])?\)\?[ \t]*\Z", re.MULTILINE)


def _clean(text: str) -> str:
    return strip_ansi(text).replace("\r", "")


def split_host(host: str) -> Tuple[str, int]:
    """``[name]:port`` → ``(name, port)``; ``name`` → ``(name, 22)``."""

    match = re.fullmatch(r"\[(.+)\]:(\d+)", host)
    return (match.group(1), int(match.group(2))) if match else (host, 22)


@dataclass(frozen=True)
class HostKeyChange:
    host: str  # as ssh names it: ``web.example`` or ``[web.example]:2222`` ("" if not printed)
    key_type: str  # ECDSA, ED25519, RSA … (ssh's label)
    new_fingerprint: str
    known_hosts_path: str
    line_no: int

    def host_and_port(self) -> Tuple[str, int]:
        return split_host(self.host) if self.host else ("", 22)


@dataclass(frozen=True)
class UnknownHostKeyPrompt:
    host: str
    key_type: str
    fingerprint: str


def detect_changed(text: str, *, exited: bool) -> Optional[HostKeyChange]:
    if not exited:
        return None
    clean = _clean(text)
    blocks = list(_CHANGED_BLOCK.finditer(clean))
    if not blocks:
        return None
    tail = clean[blocks[-1].start():]  # the last warning only
    if not _FAILED_AT_END.search(tail):
        return None
    fingerprint = _NEW_FINGERPRINT.search(tail)
    offending = _OFFENDING.search(tail)
    if fingerprint is None or offending is None:
        return None
    host = _HOST.search(tail) or _REMOVE_WITH.search(tail)
    return HostKeyChange(host=host.group(1) if host else "", key_type=fingerprint.group(1),
                         new_fingerprint=fingerprint.group(2).rstrip("."),
                         known_hosts_path=offending.group(2), line_no=int(offending.group(3)))


def detect_unknown(text: str) -> Optional[UnknownHostKeyPrompt]:
    clean = _clean(text)
    question = _QUESTION_AT_END.search(clean)
    if question is None:
        return None
    head = clean[:question.start()]
    authenticity = list(_AUTHENTICITY.finditer(head))
    fingerprints = list(_KEY_FINGERPRINT.finditer(head))
    if not authenticity or not fingerprints or fingerprints[-1].start() < authenticity[-1].start():
        return None
    return UnknownHostKeyPrompt(host=authenticity[-1].group(1), key_type=fingerprints[-1].group(1),
                                fingerprint=fingerprints[-1].group(2).rstrip("."))


class HostKeyOutputDetector:
    """Feed terminal output (bytes or text, any chunking); ask :meth:`changed` / :meth:`unknown`."""

    LIMIT = 16384

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.text = ""

    def feed(self, data: Union[bytes, str]) -> None:
        chunk = self._decoder.decode(data) if isinstance(data, (bytes, bytearray)) else data
        self.text = (self.text + chunk)[-self.LIMIT:]

    def changed(self, *, exited: bool) -> Optional[HostKeyChange]:
        return detect_changed(self.text, exited=exited)

    def unknown(self) -> Optional[UnknownHostKeyPrompt]:
        return detect_unknown(self.text)


__all__ = ["HostKeyOutputDetector", "HostKeyChange", "UnknownHostKeyPrompt", "detect_changed", "detect_unknown",
           "split_host"]
