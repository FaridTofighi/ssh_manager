"""Run Command on hosts: the engine (no GTK).

:class:`MultiRun` runs one command on many :class:`Target` hosts with a bounded
thread pool (``concurrency``). Per host: ``connect(target, timeout)`` returns an
authenticated ``paramiko.Transport`` — in the app the usual SSH connection
(``SftpBackend.open_transport``: jump hosts, keys / agent / saved passwords,
known_hosts and ``host_key_policy``) — then one ``exec`` channel:

* **script mode** (default): ``sh -s`` (or the chosen shell) with the command
  text written to its stdin, so multi-line scripts, quotes and heredocs need no
  escaping;
* **PTY on**: the text runs as one ``-c`` string (some programs want a
  terminal; stdin is then not the script).

``{host}``, ``{alias}``, ``{user}``, ``{port}`` are replaced per host before
sending (``{{`` / ``}}`` give literal braces; unknown names stay as they are).
Output is read incrementally and capped per host (``max_output`` bytes per
stream, the rest is discarded and flagged); ``command_timeout`` closes the
channel. Nothing is written to disk. Results go to a thread-safe ``events``
queue (the UI drains it) and are kept in memory.
"""

from __future__ import annotations

import logging
import queue
import re
import shlex
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ..gtkui.i18n import _

LOGGER = logging.getLogger(__name__)
THREAD_PREFIX = "sshm-multirun"

QUEUED, RUNNING, SUCCESS, FAILED, TIMEOUT = "queued", "running", "success", "failed", "timeout"
AUTH_FAILED, HOST_KEY_CHANGED, HOST_KEY_UNKNOWN = "auth_failed", "host_key_changed", "host_key_unknown"
UNREACHABLE, CANCELLED, SKIPPED = "unreachable", "cancelled", "skipped"
FAILURES = (FAILED, TIMEOUT, AUTH_FAILED, HOST_KEY_CHANGED, HOST_KEY_UNKNOWN, UNREACHABLE)
FINISHED = FAILURES + (SUCCESS, CANCELLED, SKIPPED)
RUNNABLE_PROTOCOLS = ("ssh",)
SUDO_HINT_RE = re.compile(r"a (terminal|password) is required", re.IGNORECASE)


@dataclass(frozen=True)
class Target:
    key: str
    alias: str
    hostname: str
    port: int = 22
    user: str = ""
    group: str = ""  # "Parent/Child" (shown lighter)
    protocol: str = "ssh"


@dataclass
class RunOptions:
    concurrency: int = 10
    connect_timeout: float = 10.0
    command_timeout: float = 60.0  # 0 = no limit
    stop_at_first_failure: bool = False
    pty: bool = False
    lang_c_utf8: bool = True
    max_output: int = 1024 * 1024  # bytes per stream and host
    shell: str = "sh"

    def clamp(self) -> "RunOptions":
        return replace(self, concurrency=max(1, min(50, int(self.concurrency))),
                       max_output=max(1024, int(self.max_output)))


@dataclass
class HostResult:
    key: str
    alias: str
    group: str = ""
    status: str = QUEUED
    exit_code: Optional[int] = None
    duration: float = 0.0
    stdout: str = ""
    stderr: str = ""
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    message: str = ""  # a short human reason (never a traceback, never a secret)
    hint: str = ""

    @property
    def first_line(self) -> str:
        for text in (self.stdout, self.stderr, self.message):
            for line in text.splitlines():
                if line.strip():
                    return line.strip()
        return ""

    @property
    def finished(self) -> bool:
        return self.status in FINISHED


def status_label(result: HostResult) -> str:
    """A word for the status (never colour alone)."""

    if result.status == FAILED and result.exit_code is not None:
        return _("Failed (exit {code})").format(code=result.exit_code)
    return {QUEUED: _("Queued"), RUNNING: _("Running"), SUCCESS: _("Success"), FAILED: _("Failed"),
            TIMEOUT: _("Timeout"), AUTH_FAILED: _("Auth failed"), HOST_KEY_CHANGED: _("Host key changed"),
            HOST_KEY_UNKNOWN: _("Host key unknown"), UNREACHABLE: _("Unreachable"), CANCELLED: _("Cancelled"),
            SKIPPED: _("Skipped")}.get(result.status, result.status)


_TOKEN = re.compile(r"\{\{|\}\}|\{(host|alias|user|port)\}")


def substitute(text: str, target: Target) -> str:
    """``{host}`` & co. replaced verbatim (inventory values); ``{{`` / ``}}`` are literal braces."""

    values = {"host": target.hostname, "alias": target.alias, "user": target.user, "port": str(target.port)}

    def replace_token(match: "re.Match[str]") -> str:
        token = match.group(0)
        if token == "{{":
            return "{"
        if token == "}}":
            return "}"
        return values[match.group(1)]

    return _TOKEN.sub(replace_token, text)


def _classify(exc: BaseException) -> Tuple[str, str]:
    from ..transfer.backend import AuthError, BackendError, HostKeyChanged, Timeout, UnknownHostKey

    if isinstance(exc, HostKeyChanged):
        return HOST_KEY_CHANGED, _("The server's host key has changed; nothing was run.")
    if isinstance(exc, UnknownHostKey):
        return HOST_KEY_UNKNOWN, _("The host key is not known yet; connect once in a session to check it.")
    if isinstance(exc, AuthError):
        return AUTH_FAILED, _("Needs interactive login (no saved password or key was accepted).")
    if isinstance(exc, Timeout):
        return UNREACHABLE, _("The connection timed out.")
    if isinstance(exc, BackendError):
        return UNREACHABLE, _(exc.message)
    if isinstance(exc, (OSError, EOFError)):
        return UNREACHABLE, _("The host could not be reached ({error}).").format(error=type(exc).__name__)
    return FAILED, _("The command could not be run ({error}).").format(error=type(exc).__name__)


class _Capture:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.data = bytearray()
        self.truncated = False

    def add(self, chunk: bytes) -> None:
        room = self.limit - len(self.data)
        if room > 0:
            self.data += chunk[:room]
        if len(chunk) > room:
            self.truncated = True

    def text(self) -> str:
        return bytes(self.data).decode("utf-8", "replace")


class MultiRun:
    """``connect(target, timeout)`` → an authenticated transport (raises the transfer backend's errors)."""

    def __init__(self, targets: Sequence[Target], command: str, options: Optional[RunOptions] = None, *,
                 connect: Callable[[Target, float], Any]) -> None:
        self.targets = list(targets)
        self.command = command
        self.options = (options or RunOptions()).clamp()
        self.connect = connect
        self.events: "queue.Queue[Tuple[str, HostResult]]" = queue.Queue()
        self._results: Dict[str, HostResult] = {t.key: HostResult(t.key, t.alias, t.group) for t in self.targets}
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._open: Dict[str, Any] = {}  # host key -> transport, closed by cancel()
        self._running = 0
        self.max_running = 0
        self.started_at = 0.0
        self.finished_at = 0.0
        self._done = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -------------------------------------------------------------- api
    def start(self) -> None:
        self.started_at = time.monotonic()
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"{THREAD_PREFIX}-main")
        self._thread.start()

    def cancel(self) -> None:
        self._cancel.set()
        with self._lock:
            transports = list(self._open.values())
        for transport in transports:
            try:
                transport.close()
            except Exception:
                pass

    def wait(self, timeout: Optional[float] = None) -> bool:
        return self._done.wait(timeout)

    @property
    def done(self) -> bool:
        return self._done.is_set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def results(self) -> List[HostResult]:
        with self._lock:
            return [replace(self._results[t.key]) for t in self.targets]

    def summary(self) -> Tuple[int, int, int]:
        """``(hosts, ok, failed)``."""

        results = self.results()
        return (len(results), sum(r.status == SUCCESS for r in results), sum(r.status in FAILURES for r in results))

    @property
    def skipped(self) -> int:
        return sum(r.status == SKIPPED for r in self.results())

    @property
    def duration(self) -> float:
        return (self.finished_at or time.monotonic()) - self.started_at if self.started_at else 0.0

    # ---------------------------------------------------------- internal
    def _update(self, key: str, **changes: Any) -> None:
        with self._lock:
            result = replace(self._results[key], **changes)
            self._results[key] = result
        self.events.put((key, replace(result)))

    def _run(self) -> None:
        runnable = []
        for target in self.targets:
            if target.protocol not in RUNNABLE_PROTOCOLS:
                self._update(target.key, status=SKIPPED, message=_("Not an SSH host."))
            else:
                runnable.append(target)
        try:
            with ThreadPoolExecutor(max_workers=self.options.concurrency,
                                    thread_name_prefix=f"{THREAD_PREFIX}-worker") as pool:
                futures = [pool.submit(self._one, target) for target in runnable]
                for future in futures:
                    future.result()
        finally:
            self.finished_at = time.monotonic()
            self._done.set()

    def _one(self, target: Target) -> None:
        if self._cancel.is_set():
            self._update(target.key, status=CANCELLED)
            return
        with self._lock:
            self._running += 1
            self.max_running = max(self.max_running, self._running)
        started = time.monotonic()
        self._update(target.key, status=RUNNING)
        try:
            changes = self._execute(target)
        except Exception as exc:
            status, message = _classify(exc)
            if self._cancel.is_set():
                status, message = CANCELLED, ""
            LOGGER.debug("multirun: %s failed (%s)", target.alias, type(exc).__name__)
            changes = dict(status=status, message=message)
        finally:
            with self._lock:
                self._running -= 1
                self._open.pop(target.key, None)
        changes["duration"] = time.monotonic() - started
        self._update(target.key, **changes)
        if changes["status"] in FAILURES and self.options.stop_at_first_failure:
            self._cancel.set()

    def _script(self, target: Target) -> str:
        text = substitute(self.command, target)
        if not text.endswith("\n"):
            text += "\n"
        if self.options.lang_c_utf8:
            text = "export LANG=C.UTF-8 LC_ALL=C.UTF-8\n" + text  # servers rarely accept env requests
        return text

    def _execute(self, target: Target) -> Dict[str, Any]:
        options = self.options
        transport = self.connect(target, options.connect_timeout)
        with self._lock:
            self._open[target.key] = transport
        try:
            if self._cancel.is_set():
                return dict(status=CANCELLED)
            channel = transport.open_session(timeout=options.connect_timeout)
            script = self._script(target)
            shell = options.shell.strip() or "sh"
            if options.pty:
                channel.get_pty()
                channel.exec_command(f"{shell} -c {shlex.quote(script)}")
                channel.shutdown_write()
            else:
                channel.exec_command(f"{shell} -s")
                channel.sendall(script.encode("utf-8"))
                channel.shutdown_write()
            return self._collect(target, channel)
        finally:
            try:
                transport.close()
            except Exception:
                pass

    def _collect(self, target: Target, channel: Any) -> Dict[str, Any]:
        options = self.options
        out, err = _Capture(options.max_output), _Capture(options.max_output)
        deadline = time.monotonic() + options.command_timeout if options.command_timeout else None
        channel.settimeout(0.2)
        while True:
            if self._cancel.is_set():
                channel.close()
                return dict(status=CANCELLED, stdout=out.text(), stderr=err.text(),
                            stdout_truncated=out.truncated, stderr_truncated=err.truncated)
            if deadline is not None and time.monotonic() > deadline:
                channel.close()
                return dict(status=TIMEOUT, message=_("No result after {n} s; the command was stopped.").format(
                    n=int(options.command_timeout)), stdout=out.text(), stderr=err.text(),
                    stdout_truncated=out.truncated, stderr_truncated=err.truncated)
            progressed = False
            while channel.recv_ready():
                out.add(channel.recv(65536))
                progressed = True
            while channel.recv_stderr_ready():
                err.add(channel.recv_stderr(65536))
                progressed = True
            if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                break
            if not progressed:
                time.sleep(0.02)
        code = channel.recv_exit_status()
        stderr = err.text()
        changes = dict(exit_code=code, status=SUCCESS if code == 0 else FAILED, stdout=out.text(), stderr=stderr,
                       stdout_truncated=out.truncated, stderr_truncated=err.truncated)
        if code != 0 and "sudo" in self.command and SUDO_HINT_RE.search(stderr):
            changes["hint"] = _("This host needs a sudo password; use NOPASSWD or run it in a normal session.")
        return changes


def audit_line(run: MultiRun) -> str:
    """For the app log: counts and time only (no output, a shortened command, no secret)."""

    hosts, ok, failed = run.summary()
    return f"multirun: {hosts} hosts, {ok} ok, {failed} failed, {run.duration:.1f} s"


__all__ = ["MultiRun", "RunOptions", "Target", "HostResult", "status_label", "substitute", "audit_line",
           "QUEUED", "RUNNING", "SUCCESS", "FAILED", "TIMEOUT", "AUTH_FAILED", "HOST_KEY_CHANGED",
           "HOST_KEY_UNKNOWN", "UNREACHABLE", "CANCELLED", "SKIPPED", "FAILURES", "THREAD_PREFIX"]
