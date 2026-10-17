"""Detect interactive remote password-change flows."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


_CURRENT_PASSWORD_RE = re.compile(
    r"(?:\bcurrent\b|\bold\b|\bexisting\b|\(current\)).*\b(?:unix\s+)?password\s*[:：]?\s*$",
    re.IGNORECASE,
)
_NEW_PASSWORD_RE = re.compile(
    r"(?:\benter\s+)?\bnew\b(?:\s+\S+){0,3}\s+\bpassword\b|"
    r"\bnew\s+unix\s+password\b|"
    r"\bchoose\s+a\s+new\s+password\b",
    re.IGNORECASE,
)
_RETYPE_PASSWORD_RE = re.compile(
    r"(?:\bretype\b|\bre-enter\b|\breenter\b|\brepeat\b|\bconfirm\b|\bverify\b)"
    r".*\b(?:new\s+)?(?:unix\s+)?password\b",
    re.IGNORECASE,
)
_SUCCESS_RE = re.compile(
    r"password\s+(?:updated|changed)(?:\s+successfully)?|"
    r"password\s+change(?:d)?\s+(?:complete|successful)|"
    r"all\s+authentication\s+tokens\s+updated\s+successfully|"
    r"authentication\s+tokens\s+updated\s+successfully|"
    r"the\s+password\s+has\s+been\s+changed",
    re.IGNORECASE,
)
_FAILURE_RE = re.compile(
    r"password\s+unchanged|"
    r"passwords?\s+do\s+not\s+match|"
    r"mismatch|"
    r"bad\s+password|"
    r"authentication\s+token\s+manipulation\s+error|"
    r"try\s+again",
    re.IGNORECASE,
)
_EXPIRATION_RE = re.compile(
    r"password\s+(?:has\s+)?expired|"
    r"password\s+change\s+required|"
    r"must\s+change\s+(?:your\s+)?password|"
    r"required\s+to\s+change\s+(?:your\s+)?password|"
    r"you\s+are\s+required\s+to\s+change\s+your\s+password|"
    r"changing\s+password\s+for\b",
    re.IGNORECASE,
)


@dataclass
class PasswordChangeTracker:
    """Track a remote password-change prompt without storing normal input."""

    state: str = "idle"
    _buffer: str = ""
    _pending_new: Optional[str] = None
    _candidate: Optional[str] = None
    _inferred_entries: list[str] | None = None

    @property
    def pending_candidate(self) -> Optional[str]:
        return self._candidate

    @property
    def watching_output(self) -> bool:
        return self.state != "idle" or self._candidate is not None

    def reset(self) -> None:
        self.state = "idle"
        self._buffer = ""
        self._pending_new = None
        self._candidate = None
        self._inferred_entries = None

    def observe_output(self, output: str) -> Optional[str]:
        """Observe terminal output and return an accepted new password if known."""

        if not output:
            return None

        if self._candidate and _SUCCESS_RE.search(output):
            password = self._candidate
            self.reset()
            return password

        failed = bool(_FAILURE_RE.search(output))
        prompt = self._prompt_kind(output)

        if failed:
            self.reset()
        elif _EXPIRATION_RE.search(output) and self.state == "idle":
            self.state = "infer_expired"
            self._buffer = ""
            self._inferred_entries = []

        if prompt == "retype" and self._pending_new:
            self.state = "capture_retype"
            self._buffer = ""
        elif prompt == "new":
            self.state = "capture_new"
            self._buffer = ""
            self._pending_new = None
            self._candidate = None
        elif prompt == "current" and self.state in {"capture_new", "capture_retype"} and not self._buffer:
            self.state = "idle"

        return None

    def observe_input(self, output: str, data: str) -> Optional[str]:
        """Observe input committed at the terminal and return a confirmed candidate."""

        self.observe_output(output)
        if self.state not in {"capture_new", "capture_retype", "infer_expired"}:
            return None

        accepted: Optional[str] = None
        for char in data:
            if char in {"\r", "\n"}:
                accepted = self._finish_entry()
                if accepted:
                    break
                continue
            if char in {"\b", "\x7f"}:
                self._buffer = self._buffer[:-1]
                continue
            if char == "\x1b":
                continue
            if char == "\t" or (char >= " " and char != "\x7f"):
                self._buffer += char
        return accepted

    def _finish_entry(self) -> Optional[str]:
        value = self._buffer
        self._buffer = ""
        if self.state == "capture_new":
            if value:
                self._pending_new = value
                self.state = "await_retype"
            else:
                self.state = "idle"
            return None

        if self.state == "capture_retype":
            if value and self._pending_new and value == self._pending_new:
                self._candidate = value
                self._pending_new = None
                self.state = "await_success"
                return value
            self.reset()
        elif self.state == "infer_expired":
            if value:
                entries = self._inferred_entries if self._inferred_entries is not None else []
                entries.append(value)
                self._inferred_entries = entries[-4:]
                if len(self._inferred_entries) >= 2 and self._inferred_entries[-1] == self._inferred_entries[-2]:
                    self._candidate = value
                    self._pending_new = None
                    self.state = "await_success"
                    return value
        return None

    def _prompt_kind(self, output: str) -> Optional[str]:
        line = output.replace("\r", "\n").split("\n")[-1].strip()
        if not line:
            return None
        if _RETYPE_PASSWORD_RE.search(line):
            return "retype"
        if _CURRENT_PASSWORD_RE.search(line):
            return "current"
        if _NEW_PASSWORD_RE.search(line):
            return "new"
        return None
