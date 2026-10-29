"""Small persistence helpers shared by settings and local state stores."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any, Optional


def write_text_atomic(path: Path, text: str, *, mode: Optional[int] = None) -> None:
    """Write text to a file atomically by replacing it from the same directory."""

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
        text=True,
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        target_mode = mode
        if target_mode is None and path.exists():
            target_mode = stat.S_IMODE(path.stat().st_mode)
        if target_mode is not None:
            os.chmod(tmp_path, target_mode)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


def write_json_atomic(
    path: Path,
    payload: Any,
    *,
    indent: Optional[int] = 2,
    mode: Optional[int] = None,
) -> None:
    text = json.dumps(payload, indent=indent)
    write_text_atomic(path, text, mode=mode)


def append_text(path: Path, text: str, *, mode: int = 0o600) -> None:
    """Append UTF-8 text to a file, creating it with a private mode by default."""

    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, mode)
    try:
        remaining = memoryview(text.encode("utf-8"))
        while remaining:
            written = os.write(fd, remaining)
            if written <= 0:
                raise OSError(f"Unable to append to {path}")
            remaining = remaining[written:]
    finally:
        os.close(fd)


def write_bytes_private(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    """``data`` to ``path`` atomically: a temporary file created with ``mode`` in the same folder, fsync, rename.

    The temporary file never exists with wider permissions; it is removed on any failure.
    """

    tmp = path.with_name(f".{path.name}.{os.getpid()}.{os.urandom(4).hex()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        tmp.unlink(missing_ok=True)
        raise
    os.close(fd)
    try:
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
