"""SSH config validation and save/export helpers (GTK-free).

The dialog itself lives in :mod:`ophion.gtkui.dialogs.config_viewer`.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple
import shutil

from .config import split_config_directive
from .persistence import write_text_atomic


@dataclass
class ValidationResult:
    severity: str  # "warning" | "error" | "info"
    message: str
    line: int


def _safe_read(path: Path) -> Tuple[str, str]:
    if not path.exists():
        return "", f"File not found: {path}"
    try:
        return path.read_text(encoding="utf-8"), ""
    except OSError as exc:
        return "", f"Unable to read {path}: {exc}"


def validate_config(text: str) -> List[ValidationResult]:
    """Lightweight, non-blocking lint: duplicate Host, unknown tokens, malformed lines."""
    results: List[ValidationResult] = []
    seen_hosts = {}

    for idx, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, value = split_config_directive(line)
        if not key:
            continue
        key_lower = key.lower()
        if value is None and key_lower != "match":
            results.append(ValidationResult("warning", f"Directive '{key}' has no value.", idx))
            continue
        if key_lower == "host":
            try:
                aliases = shlex.split(value or "")
            except ValueError:
                aliases = (value or "").split()
            for alias in aliases:
                if alias in seen_hosts:
                    results.append(ValidationResult("warning", f"Duplicate Host alias '{alias}' (first at line {seen_hosts[alias]}).", idx))
                else:
                    seen_hosts[alias] = idx
        else:
            # Simple check: warn on obviously odd directives (non-alnum starting)
            if not key_lower.replace("-", "").isalpha():
                results.append(ValidationResult("warning", f"Unrecognized directive '{key}'.", idx))
        # Very basic malformed token detection
        try:
            shlex.split(raw_line)
        except ValueError:
            results.append(ValidationResult("error", "Malformed line (unbalanced quotes).", idx))
    return results


def save_config_text(path: Path, text: str) -> Path:
    """Persist edited config text atomically and return the backup path used."""

    if not text.strip():
        raise ValueError("Cannot save an empty config.")
    backup = path.with_suffix(path.suffix + ".bak")
    try:
        if path.exists():
            shutil.copy2(path, backup)
    except OSError:
        pass
    write_text_atomic(path, text)
    return backup


def export_path_for_directory(directory: Path, filename: str) -> Path:
    """Return a safe export path constrained to the selected directory."""

    if any(ord(ch) < 32 for ch in filename):
        raise ValueError("Export filename must not contain path separators or control characters.")
    name = filename.strip()
    if not name:
        raise ValueError("Export filename cannot be empty.")
    if any(ch in name for ch in ("/", "\\")):
        raise ValueError("Export filename must not contain path separators or control characters.")
    if name in {".", ".."} or Path(name).is_absolute():
        raise ValueError("Export filename must be a file name, not a path.")
    return directory / name


def __getattr__(name: str):
    """Lazily expose the GTK dialog, which now lives in ``gtkui/dialogs``."""

    if name == "ConfigViewerDialog":
        from ophion.gtkui.dialogs.config_viewer import ConfigViewerDialog

        return ConfigViewerDialog
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
