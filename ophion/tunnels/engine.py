"""The port-forwarding engine: Paramiko transports, listeners, SOCKS5, remote forwards (no GTK).

:class:`TunnelManager` owns one :class:`HostLink` per host. A link opens **one**
authenticated transport (``connect(host_id)``, the app's usual SSH
connection: the same host-key policy, jump hosts and credentials as the file
browser) and carries every forward of that host:

* **local** — a listening socket; each accepted connection becomes a
  ``direct-tcpip`` channel to the destination (resolved by the server);
* **dynamic** — a SOCKS5 server (CONNECT only, no authentication, IPv4 /
  IPv6 / domain names; BIND and UDP ASSOCIATE get "command not supported");
* **remote** — ``tcpip-forward`` on the server; each incoming channel is
  connected to the destination from this computer.

Limits: at most :data:`MAX_CONNECTIONS` simultaneous connections per forward.
A failed bind ("already used by another program") fails only that forward.
When the transport dies unexpectedly the link reconnects with exponential
backoff (2 s … 60 s, reset after 60 s without trouble) and requests every
forward again; authentication and host-key errors never retry. Every status
change is handed to ``dispatch`` (the GTK main loop in the app); the engine
never touches a widget and never logs a secret.
"""

from __future__ import annotations

import errno
import logging
import select
import socket
import struct
import threading
import time
from dataclasses import dataclass, replace
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..gtkui.i18n import _
from .model import DYNAMIC, REMOTE, Forward, is_loopback

LOGGER = logging.getLogger(__name__)

STOPPED, STARTING, ACTIVE, RECONNECTING, FAILED = "stopped", "starting", "active", "reconnecting", "failed"
MAX_CONNECTIONS = 64
KEEPALIVE_S = 30
BUFFER = 64 * 1024
THREAD_PREFIX = "sshm-tunnel"

Key = Tuple[str, str]  # (host id, forward id)


@dataclass(frozen=True)
class TunnelStatus:
    state: str = STOPPED
    error: str = ""
    error_kind: str = ""  # auth, host_key_changed, unknown_host_key, bind, refused, network, unsafe
    started_at: Optional[float] = None
    connections: int = 0
    bytes_in: int = 0
    bytes_out: int = 0
    attempt: int = 0

    @property
    def running(self) -> bool:
        return self.state in (STARTING, ACTIVE, RECONNECTING)


def state_label(status: TunnelStatus) -> str:
    """Worded state (never colour alone)."""

    if status.state == RECONNECTING:
        return _("Reconnecting (attempt {n})").format(n=status.attempt)
    return {STOPPED: _("Stopped"), STARTING: _("Starting"), ACTIVE: _("Active"), FAILED: _("Failed")}.get(
        status.state, status.state)


def _classify(exc: BaseException) -> Tuple[str, str]:
    """``(kind, message)`` for a connect error; the message never holds a secret."""

    try:
        from ..transfer.backend import AuthError, BackendError, HostKeyChanged, UnknownHostKey
    except Exception:  # pragma: no cover - the transfer package is always there
        AuthError = BackendError = HostKeyChanged = UnknownHostKey = ()  # type: ignore[assignment]
    if HostKeyChanged and isinstance(exc, HostKeyChanged):
        return "host_key_changed", _("The server's host key has changed; the tunnel was not opened.")
    if UnknownHostKey and isinstance(exc, UnknownHostKey):
        from ..host_key_preflight import untrusted_text

        return "unknown_host_key", untrusted_text()  # never accepted here
    if AuthError and isinstance(exc, AuthError):
        return "auth", _("Needs password: the server did not accept the login.")
    if BackendError and isinstance(exc, BackendError):
        return "network", _(exc.message)
    if isinstance(exc, LookupError):
        return "network", _("The host no longer exists.")
    return "network", _("The SSH connection failed ({error}).").format(error=type(exc).__name__)


NO_RETRY = {"auth", "host_key_changed", "unknown_host_key"}


# ------------------------------------------------------------------ pumping
class _Counters:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.connections = 0
        self.bytes_in = 0
        self.bytes_out = 0


def _pump(a: Any, b: Any, closed: threading.Event, counters: _Counters) -> None:
    """Copy both ways until EOF or an error; ``a`` is the local side (bytes out = a → b)."""

    try:
        while not closed.is_set():
            readable, _w, _x = select.select([a, b], [], [], 0.5)
            if a in readable:
                data = a.recv(BUFFER)
                if not data:
                    break
                b.sendall(data)
                with counters.lock:
                    counters.bytes_out += len(data)
            if b in readable:
                data = b.recv(BUFFER)
                if not data:
                    break
                a.sendall(data)
                with counters.lock:
                    counters.bytes_in += len(data)
    except (OSError, EOFError, ValueError):
        pass
    finally:
        for item in (a, b):
            try:
                item.close()
            except Exception:
                pass


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    data = b""
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise EOFError("closed")
        data += chunk
    return data


class ForwardError(Exception):
    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


# --------------------------------------------------------------- forwarders
class _Forwarder:
    def __init__(self, forward: Forward, transport: Any, counters: _Counters, max_connections: int,
                 handler: Optional[Callable[..., None]] = None) -> None:
        self.handler = handler  # remote forwards: the link's dispatcher (Paramiko keeps one per transport)
        self.forward = forward
        self.transport = transport
        self.counters = counters
        self.max_connections = max_connections
        self.closed = threading.Event()
        self._threads: List[threading.Thread] = []

    def _spawn(self, target, *args, name: str) -> None:
        thread = threading.Thread(target=target, args=args, daemon=True, name=f"{THREAD_PREFIX}-{name}")
        self._threads = [t for t in self._threads if t.is_alive()]
        self._threads.append(thread)
        thread.start()

    def _begin_connection(self) -> bool:
        with self.counters.lock:
            if self.counters.connections >= self.max_connections:
                return False
            self.counters.connections += 1
            return True

    def _end_connection(self) -> None:
        with self.counters.lock:
            self.counters.connections -= 1

    def close(self) -> None:
        self.closed.set()

    def join(self, timeout: float = 2.0) -> None:
        deadline = time.monotonic() + timeout
        for thread in list(self._threads):
            if thread is not threading.current_thread():
                thread.join(max(0.0, deadline - time.monotonic()))


class _ListeningForwarder(_Forwarder):
    """Local and dynamic forwards: a listening socket on this computer."""

    def start(self) -> None:
        forward = self.forward
        if not is_loopback(forward.bind_host) and not forward.allow_remote_bind:
            raise ForwardError("unsafe", _("Listening on {address} needs \"Bind to all interfaces\".").format(
                address=forward.bind_host))
        family = socket.AF_INET6 if ":" in forward.bind_host.strip("[]") else socket.AF_INET
        listener = socket.socket(family, socket.SOCK_STREAM)
        try:
            listener.bind((forward.bind_host.strip("[]"), forward.bind_port))  # no SO_REUSEADDR: a used port fails
            listener.listen(16)
        except OSError as exc:
            listener.close()
            if exc.errno == errno.EADDRINUSE:
                raise ForwardError("bind", _("{address} is already used by another program.").format(
                    address=forward.listen_address)) from None
            if exc.errno == errno.EACCES:
                raise ForwardError("bind", _("{address} needs administrator rights (ports below 1024).").format(
                    address=forward.listen_address)) from None
            raise ForwardError("bind", _("{address} cannot be used ({error}).").format(
                address=forward.listen_address, error=exc.strerror or exc)) from None
        listener.settimeout(0.5)
        self.listener = listener
        self._spawn(self._serve, name="listen")

    def _serve(self) -> None:
        while not self.closed.is_set():
            try:
                conn, peer = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            if not self._begin_connection():
                conn.close()  # the per-forward limit
                continue
            self._spawn(self._handle, conn, peer, name="conn")
        try:
            self.listener.close()
        except OSError:
            pass

    def _handle(self, conn: socket.socket, peer: Any) -> None:
        try:
            if self.forward.kind == DYNAMIC:
                self._socks(conn, peer)
            else:
                channel = self._channel(self.forward.dest_host, self.forward.dest_port, peer)
                if channel is None:
                    conn.close()
                    return
                _pump(conn, channel, self.closed, self.counters)
        except Exception as exc:  # never kills the listener
            LOGGER.debug("tunnel connection ended (%s)", type(exc).__name__)
            try:
                conn.close()
            except OSError:
                pass
        finally:
            self._end_connection()

    def _channel(self, host: str, port: int, peer: Any) -> Any:
        try:
            return self.transport.open_channel("direct-tcpip", (host, int(port)), tuple(peer[:2]), timeout=15)
        except Exception as exc:
            LOGGER.info("tunnel: the server could not reach %s:%s (%s)", host, port, type(exc).__name__)
            return None

    # ------------------------------------------------------------- SOCKS5
    def _socks(self, conn: socket.socket, peer: Any) -> None:
        conn.settimeout(15)
        version, count = _recv_exact(conn, 2)
        if version != 5:
            conn.close()
            return
        methods = _recv_exact(conn, count)
        if 0 not in methods:
            conn.sendall(b"\x05\xff")  # only "no authentication" is offered: it listens on loopback
            conn.close()
            return
        conn.sendall(b"\x05\x00")
        version, command, _reserved, address_type = _recv_exact(conn, 4)
        if address_type == 1:
            host = socket.inet_ntop(socket.AF_INET, _recv_exact(conn, 4))
        elif address_type == 4:
            host = socket.inet_ntop(socket.AF_INET6, _recv_exact(conn, 16))
        elif address_type == 3:
            host = _recv_exact(conn, _recv_exact(conn, 1)[0]).decode("utf-8", "replace")
        else:
            conn.sendall(b"\x05\x08\x00\x01" + b"\x00" * 6)  # address type not supported
            conn.close()
            return
        (port,) = struct.unpack(">H", _recv_exact(conn, 2))
        if command != 1:
            conn.sendall(b"\x05\x07\x00\x01" + b"\x00" * 6)  # command not supported (BIND, UDP ASSOCIATE)
            conn.close()
            return
        channel = self._channel(host, port, peer)
        if channel is None:
            conn.sendall(b"\x05\x05\x00\x01" + b"\x00" * 6)  # connection refused
            conn.close()
            return
        conn.sendall(b"\x05\x00\x00\x01" + b"\x00" * 6)
        conn.settimeout(None)
        _pump(conn, channel, self.closed, self.counters)

    def close(self) -> None:
        super().close()
        listener = getattr(self, "listener", None)
        if listener is not None:
            try:
                listener.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                listener.close()
            except OSError:
                pass


class _RemoteForwarder(_Forwarder):
    """``tcpip-forward`` on the server; channels come in through the link's dispatcher."""

    def start(self) -> None:
        forward = self.forward
        try:
            self.server_port = self.transport.request_port_forward(forward.bind_host or "127.0.0.1",
                                                                    forward.bind_port, handler=self.handler)
        except Exception as exc:
            raise ForwardError("refused", _("The server refused to listen on {address} (AllowTcpForwarding or "
                                            "GatewayPorts in its sshd_config) ({error}).").format(
                address=forward.listen_address, error=type(exc).__name__)) from None

    def incoming(self, channel: Any) -> None:
        if self.closed.is_set() or not self._begin_connection():
            channel.close()
            return
        self._spawn(self._connect, channel, name="remote")

    def _connect(self, channel: Any) -> None:
        try:
            local = socket.create_connection((self.forward.dest_host, self.forward.dest_port), timeout=15)
            local.settimeout(None)
        except OSError as exc:
            LOGGER.info("tunnel: %s:%s cannot be reached from here (%s)", self.forward.dest_host,
                        self.forward.dest_port, type(exc).__name__)
            channel.close()
            self._end_connection()
            return
        try:
            _pump(local, channel, self.closed, self.counters)
        finally:
            self._end_connection()

    def close(self) -> None:
        super().close()
        try:
            if self.transport.is_active():
                self.transport.cancel_port_forward(self.forward.bind_host or "127.0.0.1", self.forward.bind_port)
        except Exception:
            pass


# --------------------------------------------------------------- host link
class HostLink:
    """One transport for one host, all its forwards, reconnection."""

    def __init__(self, manager: "TunnelManager", host_id: str) -> None:
        self.manager = manager
        self.host_id = host_id
        self.forwards: Dict[str, Forward] = {}
        self.forwarders: Dict[str, _Forwarder] = {}
        self.counters: Dict[str, _Counters] = {}
        self.transport: Any = None
        self.wake = threading.Event()
        self.stopping = False
        self.lock = threading.RLock()
        self.thread = threading.Thread(target=gc_safe(self._run), daemon=True,
                                       name=f"{THREAD_PREFIX}-host-{host_id[:8]}")

    # ---------------------------------------------------------- requests
    def add(self, forward: Forward) -> None:
        with self.lock:
            self.forwards[forward.id] = forward
            previous = self.forwarders.pop(forward.id, None)
        if previous is not None:  # started again (edited): the new settings replace the old listener
            previous.close()
            previous.join()
        with self.lock:
            self.counters.setdefault(forward.id, _Counters())
        self.manager._set(self.host_id, forward.id, state=STARTING, error="", error_kind="", attempt=0)
        if not self.thread.is_alive() and not self.thread.ident:
            self.thread.start()
        self.wake.set()

    def remove(self, forward_id: str) -> None:
        with self.lock:
            self.forwards.pop(forward_id, None)
            forwarder = self.forwarders.pop(forward_id, None)
        if forwarder is not None:
            forwarder.close()
            forwarder.join()
        self.manager._set(self.host_id, forward_id, state=STOPPED, connections=0)
        self.wake.set()

    def stop(self) -> None:
        self.stopping = True
        self.wake.set()

    # ---------------------------------------------------------- the loop
    def _run(self) -> None:
        manager = self.manager
        attempt = 0
        stable_since = 0.0
        try:
            while not self.stopping:
                with self.lock:
                    wanted = dict(self.forwards)
                if not wanted:
                    break  # nothing left to carry
                if self.transport is None or not self.transport.is_active():
                    if self.transport is not None:  # it died: not asked for
                        self._drop_forwarders()
                        if not manager.keep_trying:
                            self._fail_all("network", _("The SSH connection was lost."))
                            break
                        attempt += 1
                        for forward_id in wanted:
                            manager._set(self.host_id, forward_id, state=RECONNECTING, attempt=attempt)
                        manager._event(self.host_id, "dropped")
                        if self._sleep(min(manager.backoff_max, manager.backoff_min * (2 ** (attempt - 1)))):
                            break
                    try:
                        transport = manager.connect(self.host_id)
                    except Exception as exc:
                        kind, message = _classify(exc)
                        if kind in NO_RETRY or attempt == 0 or not manager.keep_trying:
                            self._fail_all(kind, message, exc)
                            break
                        self.transport = _DeadTransport()  # keep counting attempts
                        LOGGER.info("tunnels: reconnecting %s failed (%s)", self.host_id, kind)
                        continue
                    self.transport = transport
                    try:
                        transport.set_keepalive(KEEPALIVE_S)
                    except Exception:
                        pass
                    if attempt:
                        manager._event(self.host_id, "recovered")
                    stable_since = time.monotonic()
                self._sync_forwarders(wanted)
                if self._only_failures(wanted):
                    break  # every forward of this host failed on its own (bind, refused): no transport needed
                if attempt and time.monotonic() - stable_since >= manager.stable_after:
                    attempt = 0  # the backoff starts again from 2 s next time
                self._refresh_counters()
                self.wake.wait(0.5)
                self.wake.clear()
        finally:
            self._drop_forwarders()
            transport, self.transport = self.transport, None
            if transport is not None:
                try:
                    transport.close()
                except Exception:
                    pass
            manager._link_ended(self)

    def _sleep(self, seconds: float) -> bool:
        """Wait (a stop wakes it); ``True`` when the link should stop."""

        end = time.monotonic() + seconds
        while time.monotonic() < end and not self.stopping:
            with self.lock:
                if not self.forwards:
                    return True
            self.wake.wait(min(0.5, max(0.0, end - time.monotonic())))
            self.wake.clear()
        return self.stopping

    def _dispatch_remote(self, channel: Any, _origin: Any, server: Any) -> None:
        port = int(server[1]) if server else 0
        with self.lock:
            target = next((f for f in self.forwarders.values()
                           if isinstance(f, _RemoteForwarder) and getattr(f, "server_port", None) == port), None)
        if target is None:
            channel.close()
            return
        target.incoming(channel)

    def _sync_forwarders(self, wanted: Dict[str, Forward]) -> None:
        manager = self.manager
        for forward_id, forward in wanted.items():
            if forward_id in self.forwarders:
                continue
            status = manager.status(self.host_id, forward_id)
            if status.state == FAILED and status.error_kind in ("bind", "refused", "unsafe"):
                continue  # this forward failed alone; the others go on
            counters = self.counters.setdefault(forward_id, _Counters())
            cls = _RemoteForwarder if forward.kind == REMOTE else _ListeningForwarder
            forwarder = cls(forward, self.transport, counters, manager.max_connections,
                            handler=self._dispatch_remote if forward.kind == REMOTE else None)
            try:
                forwarder.start()
            except ForwardError as exc:
                manager._set(self.host_id, forward_id, state=FAILED, error=exc.message, error_kind=exc.kind)
                continue
            with self.lock:
                if forward_id not in self.forwards:  # stopped meanwhile
                    forwarder.close()
                    continue
                self.forwarders[forward_id] = forwarder
            manager._set(self.host_id, forward_id, state=ACTIVE, error="", error_kind="", attempt=0,
                         started_at=time.time())

    def _only_failures(self, wanted: Dict[str, Forward]) -> bool:
        with self.lock:
            if self.forwarders:
                return False
            failed = [fid for fid in wanted if self.manager.status(self.host_id, fid).state == FAILED]
            if len(failed) != len(wanted):
                return False
            for fid in failed:
                self.forwards.pop(fid, None)
        return True

    def _refresh_counters(self) -> None:
        for forward_id, counters in list(self.counters.items()):
            if forward_id not in self.forwarders:
                continue
            with counters.lock:
                values = (counters.connections, counters.bytes_in, counters.bytes_out)
            status = self.manager.status(self.host_id, forward_id)
            if (status.connections, status.bytes_in, status.bytes_out) != values:
                self.manager._set(self.host_id, forward_id, connections=values[0], bytes_in=values[1],
                                  bytes_out=values[2])

    def _drop_forwarders(self) -> None:
        with self.lock:
            forwarders, self.forwarders = dict(self.forwarders), {}
        for forwarder in forwarders.values():
            forwarder.close()
        for forwarder in forwarders.values():
            forwarder.join()

    def _fail_all(self, kind: str, message: str, exc: Optional[BaseException] = None) -> None:
        with self.lock:
            ids = list(self.forwards)
            self.forwards.clear()
        for forward_id in ids:
            self.manager._set(self.host_id, forward_id, state=FAILED, error=message, error_kind=kind,
                              connections=0)
        self.manager._failed(self.host_id, kind, exc)


class _DeadTransport:
    def is_active(self) -> bool:
        return False

    def close(self) -> None:
        pass


def gc_safe(target: Callable[[], None]) -> Callable[[], None]:
    try:
        from .. import gc_guard

        return gc_guard.worker(target)
    except Exception:  # pragma: no cover
        return target


# ------------------------------------------------------------------ manager
class TunnelManager:
    """``connect(host_id)`` returns an authenticated ``paramiko.Transport`` (it runs in a worker thread).

    ``dispatch(callable)`` runs a callable where the listeners live (``GLib.idle_add`` in the app).
    Listeners: ``on_status(host_id, forward_id, status)``, ``on_event(host_id, "dropped" | "recovered")``,
    ``on_failed(host_id, kind, exception)``.
    """

    def __init__(self, connect: Callable[[str], Any], *, dispatch: Optional[Callable[[Callable[[], None]], Any]] = None,
                 keep_trying: bool = True, max_connections: int = MAX_CONNECTIONS, backoff: Tuple[float, float] = (2.0, 60.0),
                 stable_after: float = 60.0) -> None:
        self.connect = connect
        self.dispatch = dispatch or (lambda call: call())
        self.keep_trying = keep_trying
        self.max_connections = max_connections
        self.backoff_min, self.backoff_max = backoff
        self.stable_after = stable_after
        self.on_status: List[Callable[[str, str, TunnelStatus], None]] = []
        self.on_event: List[Callable[[str, str], None]] = []
        self.on_failed: List[Callable[[str, str, Optional[BaseException]], None]] = []
        self._links: Dict[str, HostLink] = {}
        self._status: Dict[Key, TunnelStatus] = {}
        self._lock = threading.RLock()

    # -------------------------------------------------------------- api
    def start(self, host_id: str, forward: Forward) -> None:
        if not is_loopback(forward.bind_host) and not forward.allow_remote_bind and forward.kind != REMOTE:
            self._set(host_id, forward.id, state=FAILED, error_kind="unsafe",
                      error=_("Listening on {address} needs \"Bind to all interfaces\".").format(
                          address=forward.bind_host))
            return
        with self._lock:
            link = self._links.get(host_id)
            if link is None or link.stopping or (link.thread.ident and not link.thread.is_alive()):
                link = HostLink(self, host_id)
                self._links[host_id] = link
        link.add(replace(forward))

    def stop(self, host_id: str, forward_id: str) -> None:
        with self._lock:
            link = self._links.get(host_id)
        if link is None:
            self._set(host_id, forward_id, state=STOPPED, connections=0)
            return
        link.remove(forward_id)

    def stop_host(self, host_id: str, *, wait: float = 3.0) -> None:
        with self._lock:
            link = self._links.pop(host_id, None)
        if link is None:
            return
        with link.lock:
            ids = list(link.forwards)
            link.forwards.clear()
        link.stop()
        if link.thread.ident and link.thread is not threading.current_thread():
            link.thread.join(wait)
        for forward_id in ids:
            self._set(host_id, forward_id, state=STOPPED, connections=0)

    def stop_all(self, *, wait: float = 3.0) -> None:
        with self._lock:
            hosts = list(self._links)
        for host_id in hosts:
            self.stop_host(host_id, wait=wait)

    def status(self, host_id: str, forward_id: str) -> TunnelStatus:
        with self._lock:
            return self._status.get((host_id, forward_id), TunnelStatus())

    def statuses(self) -> Dict[Key, TunnelStatus]:
        with self._lock:
            return dict(self._status)

    def active_count(self) -> int:
        with self._lock:
            return sum(1 for status in self._status.values() if status.state == ACTIVE)

    def running_count(self) -> int:
        with self._lock:
            return sum(1 for status in self._status.values() if status.running)

    def running_hosts(self) -> List[str]:
        with self._lock:
            return sorted({host for (host, _f), status in self._status.items() if status.running})

    # ---------------------------------------------------------- internal
    def _set(self, host_id: str, forward_id: str, **changes: Any) -> None:
        with self._lock:
            old = self._status.get((host_id, forward_id), TunnelStatus())
            if changes.get("state") in (STOPPED, FAILED):
                changes.setdefault("started_at", None)
            new = replace(old, **changes)
            if new == old:
                return
            self._status[(host_id, forward_id)] = new
        listeners = list(self.on_status)
        if listeners:
            self.dispatch(lambda: [listener(host_id, forward_id, new) for listener in listeners])

    def _event(self, host_id: str, event: str) -> None:
        listeners = list(self.on_event)
        if listeners:
            self.dispatch(lambda: [listener(host_id, event) for listener in listeners])

    def _failed(self, host_id: str, kind: str, exc: Optional[BaseException]) -> None:
        listeners = list(self.on_failed)
        if listeners:
            self.dispatch(lambda: [listener(host_id, kind, exc) for listener in listeners])

    def _link_ended(self, link: HostLink) -> None:
        with self._lock:
            if self._links.get(link.host_id) is link:
                del self._links[link.host_id]


__all__ = ["TunnelManager", "TunnelStatus", "state_label", "STOPPED", "STARTING", "ACTIVE", "RECONNECTING", "FAILED",
           "MAX_CONNECTIONS", "THREAD_PREFIX"]
