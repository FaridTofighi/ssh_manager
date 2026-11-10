"""Run Command results: identical outputs grouped, a diff against the majority, exports (no GTK).

Exports are written only when the user asks (Copy Results, Export…); nothing
here touches the disk. CSV cells that a spreadsheet would read as a formula
(starting with ``=``, ``+``, ``-``, ``@``) get a leading ``'``.
"""

from __future__ import annotations

import csv
import difflib
import io
import json
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Sequence

from ..i18n import _, ngettext
from .engine import FAILED, SUCCESS, HostResult, status_label

GROUPED = (SUCCESS, FAILED)  # finished with an exit status: their output can be compared


def normalise(text: str) -> str:
    """Trailing whitespace of every line, and trailing blank lines, do not count."""

    return "\n".join(line.rstrip() for line in text.splitlines()).rstrip("\n")


@dataclass
class OutputGroup:
    text: str
    keys: List[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        count = len(self.keys)
        if count == 1:
            return _("Output of 1 host")
        return ngettext("Same output on {n} host", "Same output on {n} hosts", count).format(n=count)


def group_by_output(results: Iterable[HostResult]) -> List[OutputGroup]:
    """Hosts with the same normalised stdout, the biggest group first (then by first host)."""

    groups: dict = {}
    for result in results:
        if result.status not in GROUPED:
            continue
        text = normalise(result.stdout)
        groups.setdefault(text, OutputGroup(text)).keys.append(result.key)
    return sorted(groups.values(), key=lambda group: -len(group.keys))


def majority(groups: Sequence[OutputGroup]) -> Optional[OutputGroup]:
    return groups[0] if groups else None


def unified_diff(expected: str, actual: str, expected_label: str, actual_label: str) -> str:
    lines = difflib.unified_diff(expected.splitlines(), actual.splitlines(), fromfile=expected_label,
                                 tofile=actual_label, lineterm="")
    return "\n".join(lines)


# ------------------------------------------------------------------ export
FORMULA_START = ("=", "+", "-", "@")


def cell(value: object) -> str:
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(FORMULA_START) else text


def _exit(result: HostResult) -> str:
    return "" if result.exit_code is None else str(result.exit_code)


def to_csv(results: Iterable[HostResult]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["host", "group", "status", "exit", "duration", "stdout", "stderr"])
    for result in results:
        writer.writerow([cell(result.alias), cell(result.group), cell(status_label(result)), _exit(result),
                         f"{result.duration:.2f}", cell(result.stdout), cell(result.stderr)])
    return buffer.getvalue()


def to_json(results: Iterable[HostResult]) -> str:
    return json.dumps([{"host": r.alias, "group": r.group, "status": r.status, "status_text": status_label(r),
                        "exit": r.exit_code, "duration": round(r.duration, 3), "stdout": r.stdout,
                        "stderr": r.stderr, "stdout_truncated": r.stdout_truncated,
                        "stderr_truncated": r.stderr_truncated, "message": r.message}
                       for r in results], ensure_ascii=False, indent=1) + "\n"


def to_text(results: Iterable[HostResult]) -> str:
    parts = []
    for r in results:
        head = f"=== {r.alias} ({r.group}) — {status_label(r)}" if r.group else f"=== {r.alias} — {status_label(r)}"
        body = [head]
        if r.message:
            body.append(r.message)
        if r.stdout:
            body.append(r.stdout.rstrip("\n"))
        if r.stderr:
            body.append("--- stderr ---\n" + r.stderr.rstrip("\n"))
        parts.append("\n".join(body))
    return "\n\n".join(parts) + "\n"


def to_tsv(results: Iterable[HostResult]) -> str:
    """Copy Results: one line per host (the first output line), tabs between columns."""

    def clean(text: str) -> str:
        return text.replace("\t", " ").replace("\n", " ")

    lines = ["host\tgroup\tstatus\texit\tduration\toutput"]
    for r in results:
        lines.append("\t".join((clean(r.alias), clean(r.group), clean(status_label(r)), _exit(r),
                                f"{r.duration:.2f}", clean(r.first_line))))
    return "\n".join(lines) + "\n"


__all__ = ["OutputGroup", "group_by_output", "majority", "unified_diff", "normalise", "to_csv", "to_json",
           "to_text", "to_tsv", "cell"]
