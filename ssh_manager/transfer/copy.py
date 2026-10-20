"""Stream one file between two backends (local or remote) in constant memory (pure Python).

``copy_file`` reads ``chunk`` bytes at a time from the source and writes them
to the destination. With ``atomic`` the data goes to ``<name>.part`` first and
is renamed over the real name only after the size was checked, so a
half-written file never carries the real name. A cancel or an error removes
the partial file unless ``keep_partial`` asks to keep it for a later resume.
"""

from __future__ import annotations

import posixpath
import time
from dataclasses import dataclass
from typing import Callable, Optional

from .backend import NEVER_CANCELLED, BackendError, CancelToken, Cancelled, RemoteFileBackend

DEFAULT_CHUNK = 256 * 1024
PART_SUFFIX = ".part"

Progress = Callable[[int], None]  # bytes transferred so far (absolute)


@dataclass
class CopyResult:
    bytes: int
    seconds: float
    resumed_from: int = 0


def part_path(path: str) -> str:
    head, tail = posixpath.split(path) if "/" in path else ("", path)
    name = tail + PART_SUFFIX
    return posixpath.join(head, name) if head else name


def copy_file(source: RemoteFileBackend, source_path: str, target: RemoteFileBackend, target_path: str, *,
              size: Optional[int] = None, cancel: CancelToken = NEVER_CANCELLED,
              progress: Optional[Progress] = None, chunk: int = DEFAULT_CHUNK, atomic: bool = False,
              resume_from: int = 0, keep_partial: bool = False, ascii: bool = False,
              throttle: Optional[Callable[[int], None]] = None,
              local_join: Callable[[str], str] = part_path) -> CopyResult:
    """Copy one file; returns the bytes written. Raises :class:`BackendError` / :class:`Cancelled`."""

    started = time.monotonic()
    destination = local_join(target_path) if atomic else target_path
    offset = max(0, resume_from)
    written = offset
    kwargs = {"ascii": True} if ascii else {}
    reader = source.open_read(source_path, offset, cancel, **kwargs)
    try:
        writer = target.open_write(destination, offset, False, cancel, **kwargs)
    except BaseException:
        _close(reader)
        raise
    ok = False
    try:
        while True:
            cancel.raise_if_cancelled()
            data = reader.read(chunk)
            if not data:
                break
            if throttle is not None:
                throttle(len(data))
            writer.write(data)
            written += len(data)
            if progress is not None:
                progress(written)
        ok = True
    finally:
        _close(writer, raise_errors=ok)
        _close(reader, raise_errors=ok)
        if not ok and not keep_partial:
            _remove(target, destination)
    if size is not None and not ascii and written != size:
        if not keep_partial:
            _remove(target, destination)
        raise BackendError("The copy is incomplete (the size does not match).",
                           f"expected {size} bytes, wrote {written}")
    if atomic:
        target.rename(destination, target_path)
    return CopyResult(written, time.monotonic() - started, offset)


def _close(stream, *, raise_errors: bool = False) -> None:
    try:
        stream.close()
    except (BackendError, OSError):
        if raise_errors:
            raise
    except Cancelled:
        pass


def _remove(backend: RemoteFileBackend, path: str) -> None:
    try:
        backend.delete_file(path)
    except Exception:
        pass
