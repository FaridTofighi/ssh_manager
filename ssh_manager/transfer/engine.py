"""The transfer engine: planning, a queue with concurrency limits, conflicts, retries, speed limits (no GTK).

A :class:`TransferJob` (upload, download, server-to-server copy, local copy)
is expanded by :func:`plan` into file-level :class:`TransferItem` objects:
- folders recursively, with empty folders kept;
- symlinks skipped unless ``follow_symlinks``;
- loops caught by device + inode (local) or visited real paths (remote);
- depth capped at 64;
- every downloaded name made safe and contained in the destination
  (:mod:`.names`).

:class:`TransferQueue` runs items on a thread pool:
- **Limits:** a global cap, and per-endpoint connection pools (FTP needs one
  connection per running transfer; ``max_connections`` per host).
- **States:** ``queued → running → paused | done | failed | cancelled |
  skipped``.
- **Retries:** transient errors (disconnects, timeouts) are retried twice
  with backoff.
- **Conflicts:** an existing target follows the job's policy; ``ask`` calls
  back to the UI and waits.
- **Speed limits:** token buckets (per endpoint and global).
- **Progress:** listeners get snapshots at most 10 times a second per job;
  they run in worker threads, so the GTK glue posts them to the main loop.

Nothing here touches GTK, and nothing ever runs a shell.
"""

from __future__ import annotations

import concurrent.futures
import itertools
import os
import posixpath
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

from .backend import (
    F_REST, BackendError, CancelToken, Cancelled, Disconnected, EntryKind, NotFound, RemoteEntry,
    RemoteFileBackend, Timeout, Unsupported,
)
from .copy import DEFAULT_CHUNK, copy_file, part_path
from .names import UnsafePath, contained_path, safe_local_name, unique_name

MAX_DEPTH = 64
MAX_PLANNED_ITEMS = 500_000
DEFAULT_RETRIES = 2
RETRY_BACKOFF_S = (1.0, 3.0)
PROGRESS_INTERVAL_S = 0.1  # ≤ 10 Hz per job
RATE_SMOOTHING = 0.3


class Direction(str, Enum):
    UPLOAD = "upload"
    DOWNLOAD = "download"
    REMOTE_COPY = "remote_copy"  # server to server through a local streaming relay
    LOCAL_COPY = "local_copy"
    REMOTE_MOVE = "remote_move"  # same server: rename


class State(str, Enum):
    QUEUED = "queued"
    PLANNING = "planning"
    RUNNING = "running"
    PAUSED = "paused"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


FINISHED = {State.DONE, State.FAILED, State.CANCELLED, State.SKIPPED}


class Conflict(str, Enum):
    ASK = "ask"
    OVERWRITE = "overwrite"
    OVERWRITE_NEWER = "overwrite_newer"
    SKIP = "skip"
    RENAME = "rename"
    RESUME = "resume"


# ------------------------------------------------------------------ endpoints
class Endpoint:
    """One side of a transfer: a way to get connected backends (``max_connections`` at a time)."""

    def __init__(self, name: str, factory: Callable[[], RemoteFileBackend], *, max_connections: int = 1,
                 local: bool = False, limit_kbps: int = 0) -> None:
        self.name = name
        self._factory = factory
        self.local = local
        self.max_connections = max(1, max_connections)
        self._idle: List[RemoteFileBackend] = []
        self._all: List[RemoteFileBackend] = []
        self._lock = threading.Condition()
        self.limiter = RateLimiter(limit_kbps * 1024) if limit_kbps else None

    def acquire(self, cancel: CancelToken) -> RemoteFileBackend:
        with self._lock:
            while True:
                cancel.raise_if_cancelled()
                if self._idle:
                    return self._idle.pop()
                if len(self._all) < self.max_connections:
                    break
                self._lock.wait(0.2)
            placeholder = object()
            self._all.append(placeholder)  # type: ignore[arg-type]
        try:
            backend = self._factory()
            if not self.local:
                backend.connect()
        except BaseException:
            with self._lock:
                self._all.remove(placeholder)  # type: ignore[arg-type]
                self._lock.notify_all()
            raise
        with self._lock:
            self._all[self._all.index(placeholder)] = backend  # type: ignore[arg-type]
        return backend

    def release(self, backend: RemoteFileBackend, *, broken: bool = False) -> None:
        with self._lock:
            if broken:
                if backend in self._all:
                    self._all.remove(backend)
                try:
                    backend.close()
                except Exception:
                    pass
            else:
                self._idle.append(backend)
            self._lock.notify_all()

    def close(self) -> None:
        with self._lock:
            backends, self._all, self._idle = list(self._all), [], []
        for backend in backends:
            try:
                if hasattr(backend, "close"):
                    backend.close()
            except Exception:
                pass

    def join(self, directory: str, name: str) -> str:
        return os.path.join(directory, name) if self.local else posixpath.join(directory or "/", name)


class RateLimiter:
    """A thread-safe token bucket (bytes per second); ``consume`` sleeps when it runs dry."""

    def __init__(self, rate: float, burst: Optional[float] = None) -> None:
        self.rate = float(rate)
        self.capacity = float(burst if burst is not None else max(rate, DEFAULT_CHUNK))
        self._tokens = self.capacity
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def set_rate(self, rate: float) -> None:
        with self._lock:
            self.rate = float(rate)
            self.capacity = max(self.rate, DEFAULT_CHUNK)

    def delay_for(self, amount: int) -> float:
        """Take ``amount`` tokens; return how long the caller must wait (0 when there were enough)."""

        with self._lock:
            if self.rate <= 0:
                return 0.0
            now = time.monotonic()
            self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
            self._last = now
            self._tokens -= amount
            return 0.0 if self._tokens >= 0 else -self._tokens / self.rate

    def consume(self, amount: int, cancel: Optional[CancelToken] = None) -> None:
        wait = self.delay_for(amount)
        end = time.monotonic() + wait
        while wait > 0:
            if cancel is not None:
                cancel.raise_if_cancelled()
            time.sleep(min(wait, 0.2))
            wait = end - time.monotonic()


# ---------------------------------------------------------------------- items
@dataclass
class TransferItem:
    source: str
    target: str
    size: Optional[int] = None
    mtime: Optional[float] = None
    is_dir: bool = False  # a folder to create (keeps empty folders)
    state: State = State.QUEUED
    done: int = 0
    attempts: int = 0
    error: str = ""
    technical: str = ""
    warnings: List[str] = field(default_factory=list)
    resume_from: int = 0
    partial_in_part: bool = False  # paused mid-transfer: the data so far is in ``<target>.part``

    @property
    def name(self) -> str:
        return os.path.basename(self.target.rstrip("/")) or self.target


@dataclass
class ConflictInfo:
    """What the conflict dialog shows (both sides)."""

    item: TransferItem
    target_size: Optional[int]
    target_mtime: Optional[float]
    can_resume: bool


ConflictAsk = Callable[["TransferJob", ConflictInfo], Tuple[Conflict, bool]]  # (decision, apply to all)

_ids = itertools.count(1)


@dataclass
class TransferJob:
    direction: Direction
    sources: List[RemoteEntry]
    destination: str
    source: Endpoint
    target: Endpoint
    conflict: Conflict = Conflict.ASK
    atomic: bool = True
    preserve_mtime: bool = True
    follow_symlinks: bool = False
    keep_partial: bool = False
    retries: int = DEFAULT_RETRIES
    ascii_for: Optional[Callable[[str], bool]] = None
    id: int = field(default_factory=lambda: next(_ids))
    items: List[TransferItem] = field(default_factory=list)
    state: State = State.QUEUED
    planned: int = 0
    warnings: List[str] = field(default_factory=list)
    error: str = ""
    started: float = 0.0
    finished: float = 0.0
    rate: float = 0.0  # bytes per second, smoothed
    cancel: CancelToken = field(default_factory=CancelToken)
    _pause: threading.Event = field(default_factory=threading.Event)
    _last_bytes: int = 0
    _last_time: float = 0.0

    @property
    def title(self) -> str:
        if len(self.sources) == 1:
            return self.sources[0].name
        return f"{len(self.sources)} items"

    @property
    def total(self) -> int:
        return sum(item.size or 0 for item in self.items if not item.is_dir)

    @property
    def done_bytes(self) -> int:
        return sum(item.done for item in self.items)

    @property
    def eta(self) -> Optional[float]:
        if self.rate <= 0 or self.state is not State.RUNNING:
            return None
        return max(0.0, (self.total - self.done_bytes) / self.rate)

    @property
    def paused(self) -> bool:
        return self._pause.is_set()

    def counts(self) -> Dict[State, int]:
        counts: Dict[State, int] = {}
        for item in self.items:
            counts[item.state] = counts.get(item.state, 0) + 1
        return counts

    def failures(self) -> List[TransferItem]:
        return [item for item in self.items if item.state is State.FAILED]


# --------------------------------------------------------------------- planner
def plan(job: TransferJob, source: RemoteFileBackend, cancel: CancelToken,
         on_progress: Callable[[int], None] = lambda _n: None) -> List[TransferItem]:
    """Expand the job's sources into file items (folders recursively); safe local names for downloads."""

    items: List[TransferItem] = []
    to_local = job.target.local
    from_local = job.source.local

    def target_for(directory: str, name: str) -> Tuple[str, List[str]]:
        if to_local:
            safe, warnings = safe_local_name(name)
            path = str(contained_path(Path(directory), safe)) if job.direction is not Direction.LOCAL_COPY \
                else os.path.join(directory, safe)
            return path, warnings
        if not name or "/" in name or name in (".", ".."):
            raise UnsafePath(f"unusable name {name!r}")
        return job.target.join(directory, name), []

    visited: Set[object] = set()

    def identity(entry: RemoteEntry) -> object:
        if from_local:
            try:
                info = os.stat(entry.path)
                return (info.st_dev, info.st_ino)
            except OSError:
                return entry.path
        return posixpath.normpath(entry.path)

    def walk(entry: RemoteEntry, directory: str, depth: int) -> None:
        cancel.raise_if_cancelled()
        if len(items) >= MAX_PLANNED_ITEMS:
            raise BackendError("The selection is too large to transfer in one job.")
        kind = entry.kind
        if kind is EntryKind.SYMLINK:
            if not job.follow_symlinks:
                job.warnings.append(f"{entry.path}: symbolic link skipped")
                return
            try:
                kind = EntryKind.DIR if source.is_dir(entry.path) else EntryKind.FILE
                if kind is EntryKind.FILE:
                    entry = RemoteEntry(entry.name, entry.path, EntryKind.FILE,
                                        size=source.stat(entry.path).size if from_local else entry.size)
            except BackendError:
                job.warnings.append(f"{entry.path}: broken link skipped")
                return
        if kind is EntryKind.OTHER:
            job.warnings.append(f"{entry.path}: not a regular file, skipped")
            return
        try:
            target, warnings = target_for(directory, entry.name)
        except UnsafePath as exc:
            job.warnings.append(f"{entry.name!r}: {exc}")
            return
        if warnings:
            job.warnings.append(f"{entry.name!r} → {os.path.basename(target)!r}: " + ", ".join(warnings))
        if kind is EntryKind.DIR:
            if depth >= MAX_DEPTH:
                job.warnings.append(f"{entry.path}: deeper than {MAX_DEPTH} folders, skipped")
                return
            key = identity(entry)
            if key in visited:
                job.warnings.append(f"{entry.path}: folder loop skipped")
                return
            visited.add(key)
            items.append(TransferItem(entry.path, target, is_dir=True))
            for child in source.list(entry.path, cancel):
                walk(child, target, depth + 1)
            return
        items.append(TransferItem(entry.path, target, size=entry.size, mtime=entry.mtime))
        if len(items) % 200 == 0:
            on_progress(len(items))

    for entry in job.sources:
        walk(entry, job.destination, 0)
    on_progress(len(items))
    return items


def is_inside(child: str, parent: str) -> bool:
    """``child`` is ``parent`` or below it (POSIX paths): a folder cannot be copied into itself."""

    child_n, parent_n = posixpath.normpath(child), posixpath.normpath(parent)
    return child_n == parent_n or child_n.startswith(parent_n.rstrip("/") + "/")


# ----------------------------------------------------------------------- queue
Listener = Callable[[TransferJob], None]


class TransferQueue:
    """Runs jobs: planning, items on a bounded pool, retries, conflicts, pause / resume / cancel."""

    def __init__(self, *, concurrency: int = 3, limit_kbps: int = 0, ask: Optional[ConflictAsk] = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.concurrency = max(1, concurrency)
        # Coordinators (planning, waiting for a job's items) and the file transfers have separate pools,
        # so many queued jobs can never starve their own transfers.
        self._jobs_pool = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="sshm-job")
        self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=self.concurrency,
                                                           thread_name_prefix="sshm-transfer")
        self.jobs: List[TransferJob] = []
        self.listeners: List[Listener] = []
        self.limiter = RateLimiter(limit_kbps * 1024) if limit_kbps else None
        self._ask = ask
        self._sleep = sleep
        self._lock = threading.Lock()
        self._last_emit: Dict[int, float] = {}
        self._closed = False

    # ---------------------------------------------------------------- api
    def add(self, job: TransferJob) -> TransferJob:
        with self._lock:
            self.jobs.append(job)
        self._emit(job, force=True)
        self._jobs_pool.submit(self._run_job, job)
        return job

    def pause(self, job: TransferJob) -> None:
        if job.state not in FINISHED:
            job._pause.set()
            job.state = State.PAUSED
            self._emit(job, force=True)

    def resume(self, job: TransferJob) -> None:
        if job.state is State.PAUSED:
            job._pause.clear()
            job.state = State.RUNNING
            self._emit(job, force=True)

    def cancel(self, job: TransferJob) -> None:
        job.cancel.cancel()
        job._pause.clear()

    def retry(self, job: TransferJob) -> Optional[TransferJob]:
        """A new job with the failed items of ``job`` (same endpoints and options)."""

        failed = job.failures()
        if not failed:
            return None
        retry = TransferJob(job.direction, [], job.destination, job.source, job.target, conflict=Conflict.OVERWRITE,
                            atomic=job.atomic, preserve_mtime=job.preserve_mtime,
                            follow_symlinks=job.follow_symlinks, keep_partial=job.keep_partial, retries=job.retries)
        retry.items = [TransferItem(i.source, i.target, i.size, i.mtime) for i in failed]
        return self.add(retry)

    def clear_finished(self) -> None:
        with self._lock:
            self.jobs = [job for job in self.jobs if job.state not in FINISHED]

    def running(self) -> List[TransferJob]:
        return [job for job in self.jobs if job.state not in FINISHED]

    def shutdown(self) -> None:
        self._closed = True
        for job in list(self.jobs):
            self.cancel(job)
        self._pool.shutdown(wait=False, cancel_futures=True)
        self._jobs_pool.shutdown(wait=False, cancel_futures=True)

    def set_limit(self, kbps: int) -> None:
        self.limiter = RateLimiter(kbps * 1024) if kbps else None

    # ------------------------------------------------------------ running
    def _emit(self, job: TransferJob, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_emit.get(job.id, 0.0) < PROGRESS_INTERVAL_S:
            return
        self._last_emit[job.id] = now
        if job.started and job.state is State.RUNNING:
            elapsed = now - (job._last_time or job.started)
            if elapsed > 0:
                instant = (job.done_bytes - job._last_bytes) / elapsed
                job.rate = instant if not job.rate else (RATE_SMOOTHING * instant + (1 - RATE_SMOOTHING) * job.rate)
                job._last_bytes, job._last_time = job.done_bytes, now
        for listener in list(self.listeners):
            try:
                listener(job)
            except Exception:
                pass

    def _run_job(self, job: TransferJob) -> None:
        if job.cancel.cancelled:
            job.state = State.CANCELLED
            self._emit(job, force=True)
            return
        job.started = time.monotonic()
        try:
            if not job.items:
                job.state = State.PLANNING
                self._emit(job, force=True)
                backend = job.source.acquire(job.cancel)
                try:
                    job.items = plan(job, backend, job.cancel, lambda n: self._planned(job, n))
                finally:
                    job.source.release(backend)
            if job.state is not State.PAUSED:
                job.state = State.RUNNING
            self._emit(job, force=True)
            for item in [i for i in job.items if i.is_dir]:
                self._make_dir(job, item)
            files = [i for i in job.items if not i.is_dir]
            futures = [self._pool.submit(self._run_item, job, item) for item in files]
            concurrent.futures.wait(futures)
        except Cancelled:
            job.state = State.CANCELLED
        except BackendError as exc:
            job.state = State.FAILED
            job.error = exc.message
        except Exception as exc:  # the queue never dies with a job
            job.state = State.FAILED
            job.error = f"{type(exc).__name__}"
        else:
            if job.cancel.cancelled:
                job.state = State.CANCELLED
            elif any(i.state is State.FAILED for i in job.items):
                job.state = State.FAILED
                job.error = f"{len(job.failures())} of {len(job.items)} failed"
            else:
                job.state = State.DONE
        job.finished = time.monotonic()
        for item in job.items:
            if item.state in (State.QUEUED, State.RUNNING, State.PAUSED) and job.state is State.CANCELLED:
                item.state = State.CANCELLED
        self._emit(job, force=True)

    def _planned(self, job: TransferJob, count: int) -> None:
        job.planned = count
        self._emit(job)

    def _make_dir(self, job: TransferJob, item: TransferItem) -> None:
        job.cancel.raise_if_cancelled()
        backend = job.target.acquire(job.cancel)
        try:
            backend.mkdir(item.target, parents=True)
            item.state = State.DONE
        except BackendError as exc:
            if backend.is_dir(item.target):
                item.state = State.DONE
            else:
                item.state, item.error, item.technical = State.FAILED, exc.message, exc.technical
        finally:
            job.target.release(backend)

    def _wait_if_paused(self, job: TransferJob) -> None:
        while job._pause.is_set():
            job.cancel.raise_if_cancelled()
            self._sleep(0.1)

    def _run_item(self, job: TransferJob, item: TransferItem) -> None:
        if True:  # the item pool's size is the concurrency limit
            while True:
                try:
                    self._wait_if_paused(job)
                    job.cancel.raise_if_cancelled()
                    item.state = State.RUNNING
                    self._copy(job, item)
                    return
                except Cancelled:
                    if job._pause.is_set() and not job.cancel.cancelled:
                        item.state = State.PAUSED
                        continue  # resumes (from the partial file when the server supports it)
                    item.state = State.CANCELLED
                    self._discard_part(job, item)
                    return
                except (Disconnected, Timeout) as exc:
                    item.attempts += 1
                    if item.attempts > job.retries:
                        item.state, item.error, item.technical = State.FAILED, exc.message, exc.technical
                        return
                    self._sleep(RETRY_BACKOFF_S[min(item.attempts - 1, len(RETRY_BACKOFF_S) - 1)])
                except BackendError as exc:
                    item.state, item.error, item.technical = State.FAILED, exc.message, exc.technical
                    return
                except OSError as exc:  # local disk: full, permission …
                    item.state, item.error = State.FAILED, exc.strerror or str(exc)
                    return
                finally:
                    self._emit(job)

    def _discard_part(self, job: TransferJob, item: TransferItem) -> None:
        """A paused item that is then cancelled: its kept ``.part`` file goes too."""

        if not item.partial_in_part or job.keep_partial:
            return
        item.partial_in_part, item.resume_from = False, 0
        try:
            backend = job.target.acquire(CancelToken())
        except BackendError:
            return
        try:
            backend.delete_file(part_path(item.target))
        except BackendError:
            pass
        finally:
            job.target.release(backend)

    def _copy(self, job: TransferJob, item: TransferItem) -> None:
        source = job.source.acquire(job.cancel)
        broken_source = broken_target = False
        try:
            target = job.target.acquire(job.cancel)
            try:
                if job.direction is Direction.REMOTE_MOVE:
                    target.rename(item.source, item.target)
                    item.done = item.size or 0
                    item.state = State.DONE
                    return
                if item.partial_in_part and item.resume_from:
                    resume_from, atomic = item.resume_from, True  # continue the paused .part file
                else:
                    decision = self._resolve_conflict(job, item, target)
                    if decision is Conflict.SKIP:
                        item.state = State.SKIPPED
                        return
                    resume_from = item.resume_from if decision is Conflict.RESUME else 0
                    atomic = job.atomic and not resume_from  # resuming an existing target writes it in place
                limiter = self._limiter(job)

                def progress(done: int) -> None:
                    item.done = done
                    self._emit(job)
                    if job._pause.is_set():
                        raise Cancelled()  # pause: stop this transfer, keep the partial file for a resume

                ascii_mode = bool(job.ascii_for and job.ascii_for(item.source))
                try:
                    copy_file(source, item.source, target, item.target, size=item.size, cancel=job.cancel,
                              progress=progress, atomic=atomic, resume_from=resume_from,
                              keep_partial=lambda: job.keep_partial or job._pause.is_set(), ascii=ascii_mode,
                              throttle=limiter)
                except Cancelled:
                    if job._pause.is_set() and F_REST in target.features() and F_REST in source.features():
                        item.resume_from, item.partial_in_part = item.done, atomic
                    else:
                        item.resume_from, item.partial_in_part = 0, False
                    raise
                except (Disconnected, Timeout):
                    broken_source = broken_target = True
                    item.resume_from, item.partial_in_part = 0, False  # a retry starts the file again
                    raise
                if job.preserve_mtime and item.mtime is not None:
                    try:
                        target.set_mtime(item.target, item.mtime)
                    except (Unsupported, BackendError):
                        pass
                item.done = item.size if item.size is not None else item.done
                item.state = State.DONE
            finally:
                job.target.release(target, broken=broken_target)
        finally:
            job.source.release(source, broken=broken_source)

    def _limiter(self, job: TransferJob) -> Optional[Callable[[int], None]]:
        limiters = [limiter for limiter in (job.source.limiter, job.target.limiter, self.limiter) if limiter]
        if not limiters:
            return None

        def throttle(amount: int) -> None:
            for limiter in limiters:
                limiter.consume(amount, job.cancel)

        return throttle

    def _resolve_conflict(self, job: TransferJob, item: TransferItem, target: RemoteFileBackend) -> Conflict:
        try:
            existing = target.stat(item.target)
        except NotFound:
            return Conflict.OVERWRITE
        except BackendError:
            return Conflict.OVERWRITE
        policy = job.conflict
        can_resume = (F_REST in target.features() and existing.size is not None and item.size is not None
                      and 0 < existing.size < item.size)
        if policy is Conflict.ASK and self._ask is not None:
            decision, apply_all = self._ask(job, ConflictInfo(item, existing.size, existing.mtime, can_resume))
            if apply_all:
                job.conflict = decision
            policy = decision
        elif policy is Conflict.ASK:
            policy = Conflict.SKIP
        if policy is Conflict.OVERWRITE_NEWER:
            newer = item.mtime is not None and (existing.mtime is None or item.mtime > existing.mtime)
            return Conflict.OVERWRITE if newer else Conflict.SKIP
        if policy is Conflict.RENAME:
            directory = os.path.dirname(item.target)
            name = os.path.basename(item.target)
            taken: Set[str] = set()
            candidate = unique_name(Path(directory), name, taken) if job.target.local else _remote_unique(
                target, directory, name)
            item.target = job.target.join(directory, candidate)
            return Conflict.OVERWRITE
        if policy is Conflict.RESUME:
            if can_resume:
                item.resume_from = existing.size or 0
                return Conflict.RESUME
            return Conflict.OVERWRITE
        return policy


def _remote_unique(backend: RemoteFileBackend, directory: str, name: str) -> str:
    stem, dot, extension = name.rpartition(".")
    if not dot or not stem:
        stem, dot, extension = name, "", ""
    for counter in range(1, 10_000):
        candidate = f"{stem} ({counter}){dot}{extension}"
        if not backend.exists(posixpath.join(directory or "/", candidate)):
            return candidate
    raise BackendError("No free name for the copy.")


def summarize(jobs: Iterable[TransferJob]) -> Tuple[int, float]:
    """``(running jobs, total bytes per second)`` for the status bar."""

    running = [job for job in jobs if job.state in (State.RUNNING, State.PLANNING)]
    return len(running), sum(job.rate for job in running)


__all__ = ["Direction", "State", "Conflict", "Endpoint", "RateLimiter", "TransferItem", "TransferJob",
           "TransferQueue", "plan", "is_inside", "summarize", "part_path"]
