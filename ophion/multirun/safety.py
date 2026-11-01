"""Run Command: commands that can disrupt servers (advisory; it never claims a command is safe).

The text is lower-cased and its whitespace collapsed, then each pattern of
:data:`DANGEROUS_PATTERNS` is searched. The patterns are simple (no nested
repetition), so even a 100 KB script is checked in linear time. A match only
asks for an explicit "I understand"; anything else can still be harmful.
"""

from __future__ import annotations

import re
from typing import List, Tuple

CONFIRM_ABOVE = 5  # more targets than this always ask first

# (shown as, regular expression on lower-cased text with single spaces)
DANGEROUS_PATTERNS: Tuple[Tuple[str, str], ...] = (
    # flags in one or two words (-rf, -fr, -r -f …): lookaheads in the flag run, possessive runs (linear)
    ("rm -rf", r"\brm (?=(?:-[a-z]*+ ?){1,4})(?=(?:-[a-z]*+ ){0,3}-[a-z]*?r)(?=(?:-[a-z]*+ ){0,3}-[a-z]*?f)"),
    ("mkfs", r"\bmkfs\b"),
    ("dd if=", r"\bdd if="),
    ("shutdown", r"\bshutdown\b(?![-.])"),
    ("reboot", r"\breboot\b(?![-.])"),
    ("poweroff", r"\bpoweroff\b"),
    ("halt", r"(?<![\w.-])halt\b(?![-.])"),
    ("init 0", r"\binit 0\b"),
    (":(){", r":\(\) ?\{"),
    ("> /dev/sd", r"> ?/dev/(?:sd|nvme|hd|vd|xvd)"),
    ("chmod -R", r"\bchmod -(?=[a-z]*?r)[a-z]*+\b"),
    ("chown -R", r"\bchown -(?=[a-z]*?r)[a-z]*+\b"),
    ("systemctl stop", r"\bsystemctl (?:--[a-z-]+ )?stop\b"),
    ("systemctl disable", r"\bsystemctl (?:--[a-z-]+ )?disable\b"),
    ("systemctl mask", r"\bsystemctl (?:--[a-z-]+ )?mask\b"),
    ("iptables -F", r"\biptables -(?=[a-z]*?f)[a-z]*+\b"),
    ("kill -9 1", r"\bkill -9 1\b(?!\d)"),
)
_COMPILED = tuple((label, re.compile(pattern)) for label, pattern in DANGEROUS_PATTERNS)
_SPACES = re.compile(r"\s+")


def normalise(text: str) -> str:
    return _SPACES.sub(" ", text.lower())


def dangerous_matches(text: str) -> List[str]:
    """The labels of the patterns found, in the order of :data:`DANGEROUS_PATTERNS`."""

    clean = normalise(text)
    return [label for label, pattern in _COMPILED if pattern.search(clean)]


def needs_confirmation(host_count: int, text: str) -> bool:
    return host_count > CONFIRM_ABOVE or bool(dangerous_matches(text))


__all__ = ["DANGEROUS_PATTERNS", "dangerous_matches", "needs_confirmation", "CONFIRM_ABOVE", "normalise"]
