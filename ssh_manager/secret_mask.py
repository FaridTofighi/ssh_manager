"""Masking known secrets in program output before it is shown or logged (no GTK).

A secret is replaced wherever it appears — but only when it is long enough
(:data:`MIN_MASKED_LENGTH`). A very short secret ("1", "abc") also matches
ordinary text (times, process ids, "x11"): replacing it everywhere would make
the log unreadable *and* show the secret, since every mask marks where it was.
The programs whose output is masked (FreeRDP, ssh-add, the FTP log) never print
a password on purpose; masking is a safety net, so short secrets are left out
of it rather than revealed by it.
"""

from __future__ import annotations

from typing import Iterable

MIN_MASKED_LENGTH = 6


def mask_secrets(text: str, secrets: Iterable[str], mask: str = "***") -> str:
    for secret in secrets:
        if secret and len(secret) >= MIN_MASKED_LENGTH:
            text = text.replace(secret, mask)
    return text


__all__ = ["mask_secrets", "MIN_MASKED_LENGTH"]
