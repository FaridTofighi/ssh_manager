"""Run a FreeRDP client: secret hand-off, output reading, stop, and no orphans (pure Python, no GTK).

* **Spawn:** ``subprocess.Popen`` with an argv list (never a shell),
  ``close_fds``, its own session (process group), stdin a pipe that receives
  the launch payload and is closed at once (or ``/dev/null``).
* **Output:** stdout and stderr are read line by line on threads. Lines are
  capped (4 KiB), ANSI sequences removed, every secret of the launch masked;
  the last :data:`MAX_LINES` stay in memory. ``on_line`` gets each cleaned
  line on the reader thread.
* **Stop:** ``SIGTERM`` to the whole process group, ``SIGKILL`` after a grace
  period; the exit is reaped by a waiter thread (no zombies).
* **Registry:** every child's pid, process-group id, start time and client
  path are recorded in ``$XDG_RUNTIME_DIR/ssh-manager/rdp-children.json``
  (0600). After a crash of the app, :func:`reap_stale` kills a recorded
  group only if that pid still runs the same client with the same start time
  (a reused pid is never touched).
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable, Deque, Dict, List, Optional

from .. import gc_guard
from ..text_utils import strip_ansi
from .command import LaunchPlan

MAX_LINES = 400
MAX_LINE_CHARS = 4096
GRACE_S = 2.0


def runtime_dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR")
    if base and os.path.isdir(base):
        return Path(base) / "ssh-manager"
    return Path("/tmp") / f"ssh-manager-{os.getuid()}"


def _start_time(pid: int) -> Optional[int]:
    """Field 22 of /proc/<pid>/stat (clock ticks since boot): identifies a process across pid reuse."""

    try:
        data = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    try:
        return int(data[data.rindex(")") + 2:].split()[19])
    except (ValueError, IndexError):
        return None


def _cmdline0(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0", 1)[0].decode("utf-8", "replace")
    except OSError:
        return ""


class ChildRegistry:
    """The FreeRDP children of this app (thread-safe; a small JSON file)."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path is not None else runtime_dir() / "rdp-children.json"
        self._lock = threading.Lock()

    def _load(self) -> Dict[str, dict]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return {k: v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}

    def _save(self, data: Dict[str, dict]) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        os.replace(tmp, self.path)

    def add(self, pid: int, pgid: int, binary: str) -> None:
        with self._lock:
            data = self._load()
            data[str(pid)] = {"pgid": pgid, "start": _start_time(pid), "binary": binary, "owner": os.getpid()}
            try:
                self._save(data)
            except OSError:
                pass

    def remove(self, pid: int) -> None:
        with self._lock:
            data = self._load()
            if data.pop(str(pid), None) is not None:
                try:
                    self._save(data)
                except OSError:
                    pass

    def entries(self) -> Dict[str, dict]:
        with self._lock:
            return self._load()


def reap_stale(registry: ChildRegistry, *, kill: Callable[[int, int], None] = os.killpg) -> List[int]:
    """Kill children recorded by an app instance that no longer runs; returns the pids handled."""

    handled: List[int] = []
    for pid_text, entry in registry.entries().items():
        try:
            pid, pgid = int(pid_text), int(entry.get("pgid", 0))
        except (TypeError, ValueError):
            if pid_text.isdigit():
                registry.remove(int(pid_text))
            continue
        owner = int(entry.get("owner", 0) or 0)
        if owner and owner != os.getpid() and _start_time(owner) is not None:
            continue  # another running instance owns it
        same = (_start_time(pid) is not None and _start_time(pid) == entry.get("start")
                and os.path.realpath(_cmdline0(pid)) == os.path.realpath(str(entry.get("binary", ""))))
        if same and pgid > 1:
            try:
                kill(pgid, signal.SIGTERM)
                handled.append(pid)
            except (ProcessLookupError, PermissionError):
                pass
        registry.remove(pid)
    return handled


class RdpProcess:
    """One running FreeRDP client.

    ``on_line(line)`` is called for every cleaned output line and
    ``on_exit(code)`` once when it ends — both on worker threads: the GTK
    layer hands them over with ``GLib.idle_add``.
    """

    def __init__(self, plan: LaunchPlan, *, on_line: Callable[[str], None] = lambda _l: None,
                 on_exit: Callable[[int], None] = lambda _c: None, registry: Optional[ChildRegistry] = None,
                 env: Optional[Dict[str, str]] = None, popen: Callable[..., subprocess.Popen] = subprocess.Popen) -> None:
        self._secrets = tuple(s for s in plan.secrets if s)
        self.lines: Deque[str] = deque(maxlen=MAX_LINES)
        self._lock = threading.Lock()
        self._on_line = on_line
        self._on_exit = on_exit
        self._registry = registry
        self.stopped_by_user = False
        self.exit_code: Optional[int] = None
        self.started = time.monotonic()
        environment = dict(os.environ if env is None else env)
        environment.update(plan.env)
        payload = plan.stdin_payload
        self.proc = popen(plan.argv, stdin=subprocess.PIPE if payload else subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True, start_new_session=True,
                          env=environment)
        self.pid = self.proc.pid
        try:
            self.pgid = os.getpgid(self.pid)
        except OSError:
            self.pgid = self.pid
        if registry is not None:
            registry.add(self.pid, self.pgid, plan.argv[0])
        stdin = self.proc.stdin
        if payload and stdin is not None:
            try:
                stdin.write(payload)
                stdin.flush()
            except (BrokenPipeError, OSError):
                pass
            finally:
                try:
                    stdin.close()  # the secret leaves this process right here
                except OSError:
                    pass
        self._readers = [threading.Thread(target=self._read, args=(stream,), daemon=True, name="sshm-rdp-out")
                         for stream in (self.proc.stdout, self.proc.stderr) if stream is not None]
        for reader in self._readers:
            reader.start()
        self._waiter = threading.Thread(target=self._wait, daemon=True, name="sshm-rdp-wait")
        self._waiter.start()

    # ------------------------------------------------------------- output
    def mask(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "***")
        return text

    def _read(self, stream) -> None:
        try:
            for raw in iter(lambda: stream.readline(MAX_LINE_CHARS), b""):
                line = self.mask(strip_ansi(raw.decode("utf-8", "replace")).rstrip("\r\n"))
                line = "".join(ch if ch.isprintable() or ch == "\t" else "?" for ch in line)
                with self._lock:
                    self.lines.append(line)
                try:
                    self._on_line(line)
                except Exception:
                    pass
        except (OSError, ValueError):
            pass
        finally:
            try:
                stream.close()
            except OSError:
                pass

    def output(self) -> List[str]:
        with self._lock:
            return list(self.lines)

    # --------------------------------------------------------------- life
    def _wait(self) -> None:
        code = self.proc.wait()
        for reader in self._readers:
            reader.join(timeout=2)
        self.exit_code = code
        if self._registry is not None:
            self._registry.remove(self.pid)
        try:
            self._on_exit(code)
        except Exception:
            pass

    @property
    def running(self) -> bool:
        return self.proc.poll() is None

    def alive_s(self) -> float:
        return time.monotonic() - self.started

    def stop(self, *, user: bool = True, grace: float = GRACE_S) -> None:
        """SIGTERM the process group, SIGKILL after ``grace`` seconds (in a thread; never blocks the caller)."""

        if user:
            self.stopped_by_user = True
        if not self.running:
            return

        def work() -> None:
            self._signal(signal.SIGTERM)
            deadline = time.monotonic() + grace
            while time.monotonic() < deadline and self.running:
                time.sleep(0.05)
            if self.running:
                self._signal(signal.SIGKILL)

        threading.Thread(target=gc_guard.worker(work), daemon=True, name="sshm-rdp-stop").start()

    def stop_now(self, grace: float = GRACE_S) -> None:
        """Synchronous stop (application exit)."""

        self.stopped_by_user = True
        self._signal(signal.SIGTERM)
        try:
            self.proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            self._signal(signal.SIGKILL)
            try:
                self.proc.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                pass

    def _signal(self, sig: int) -> None:
        try:
            os.killpg(self.pgid, sig)
        except (ProcessLookupError, PermissionError):
            try:
                self.proc.send_signal(sig)
            except (ProcessLookupError, OSError):
                pass


class SessionLimiter:
    """At most ``limit`` RDP sessions at once (Preferences → Remote Desktop → Sessions at once)."""

    def __init__(self, limit: int = 8) -> None:
        self.limit = limit
        self._active: set = set()
        self._lock = threading.Lock()

    def acquire(self, key: object) -> bool:
        with self._lock:
            if key in self._active:
                return True
            if len(self._active) >= self.limit:
                return False
            self._active.add(key)
            return True

    def release(self, key: object) -> None:
        with self._lock:
            self._active.discard(key)

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._active)


__all__ = ["RdpProcess", "ChildRegistry", "reap_stale", "SessionLimiter", "runtime_dir"]
