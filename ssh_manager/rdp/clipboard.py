"""Clipboard sharing policy of an RDP session (pure Python).

The host's ``clipboard`` setting (both / to the remote computer / from the
remote computer / off) decides which way text, images and — when the engine
supports file copying over ``cliprdr`` — files may travel. Large contents are
refused instead of being copied partly. Clipboard contents are never logged:
:meth:`ClipboardPolicy.describe` names only the kind and the size.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

TO_REMOTE = "to_remote"  # this computer → the remote computer
FROM_REMOTE = "from_remote"
MAX_TEXT = 4 * 1024 * 1024
MAX_IMAGE = 32 * 1024 * 1024
MAX_FILES = 256 * 1024 * 1024
KINDS = ("text", "image", "files")


@dataclass(frozen=True)
class ClipboardPolicy:
    mode: str = "both"  # both | to_remote | from_remote | off
    files_supported: bool = False  # the engine can copy files over the clipboard

    @property
    def enabled(self) -> bool:
        return self.mode != "off"

    def allows(self, direction: str, kind: str = "text", size: int = 0) -> Tuple[bool, str]:
        """``(allowed, reason)`` for one transfer (``reason`` is English, for the log / a toast)."""

        if kind not in KINDS:
            return False, "Unknown clipboard content."
        if self.mode == "off":
            return False, "Clipboard sharing is off for this host."
        if self.mode in (TO_REMOTE, FROM_REMOTE) and direction != self.mode:
            return False, ("The clipboard only goes to the remote computer." if self.mode == TO_REMOTE
                           else "The clipboard only comes from the remote computer.")
        if kind == "files" and not self.files_supported:
            return False, "This engine cannot copy files through the clipboard; use a shared folder."
        limit = {"text": MAX_TEXT, "image": MAX_IMAGE, "files": MAX_FILES}[kind]
        if size > limit:
            return False, f"The clipboard content is too large ({size // 1024} KiB; at most {limit // 1024} KiB)."
        return True, ""

    def indicator(self) -> str:
        """The toolbar's clipboard label."""

        return {"both": "Clipboard shared", TO_REMOTE: "Clipboard: to the remote computer only",
                FROM_REMOTE: "Clipboard: from the remote computer only", "off": "Clipboard not shared"}[self.mode]

    @staticmethod
    def describe(kind: str, size: int) -> str:
        """What the log may say about a clipboard transfer (never the content)."""

        return f"clipboard {kind}, {size} bytes"


__all__ = ["ClipboardPolicy", "TO_REMOTE", "FROM_REMOTE"]
