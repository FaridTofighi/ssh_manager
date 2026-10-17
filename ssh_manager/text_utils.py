"""Text-processing helpers used by terminal frontends."""

from __future__ import annotations

import re


def strip_ansi(text: str) -> str:
    """Remove common terminal escape sequences from captured text."""

    text = re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", text)
    text = re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)", "", text)
    return text.replace("\x07", "")
