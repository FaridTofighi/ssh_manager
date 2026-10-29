"""Reach an RDP server through an SSH host of the inventory (a local port forward; pure Python + paramiko).

:class:`SshTunnel` takes an authenticated ``paramiko.Transport`` (from
``SftpBackend.open_transport`` — the same jump host, known_hosts rules and
key / agent / password order as every SSH connection of the app) and listens
on ``127.0.0.1`` on a free port chosen by the kernel. Every accepted
connection is forwarded over a ``direct-tcpip`` channel to the RDP server.

* **Loopback only:** the listener is bound to 127.0.0.1 (never 0.0.0.0) and
  without ``SO_REUSEADDR``; connections from another address are refused.
* **One client at a time:** FreeRDP opens one connection (and one more when
  it reconnects); further simultaneous connections are refused.
* **Lifetime:** the tunnel lives as long as the session and is closed with
  the tab; a closed SSH transport ends it, and :attr:`failed` says why.

FreeRDP connects to ``127.0.0.1:<port>`` but checks the certificate and
Kerberos names against the real server (``/server-name:``), and the app
pins the certificate it probed *through* the tunnel, so verification is not
weakened.
"""

from __future__ import annotations

import select
import socket
import threading
from typing import Any, Callable, List, Optional

BUFFER = 64 * 1024
LogFn = Callable[[str], None]


class SshTunnel:
    def __init__(self, transport: Any, target_host: str, target_port: int, *, log: LogFn = lambda _m: None,
                 max_clients: int = 1) -> None:
        self.transport = transport
        self.target_host = target_host
        self.target_port = int(target_port)
        self.log = log
        self.max_clients = max_clients
        self.failed = ""
        self._closed = threading.Event()
        self._clients: List[threading.Thread] = []
        self._lock = threading.Lock()
        self._active = 0
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(("127.0.0.1", 0))  # loopback only, a free port chosen by the kernel
        self.listener.listen(2)
        self.port = self.listener.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True, name="sshm-rdp-tunnel")
        self._thread.start()
        self.log(f"SSH tunnel: 127.0.0.1:{self.port} → {target_host}:{target_port}")

    @property
    def alive(self) -> bool:
        return not self._closed.is_set() and bool(self.transport.is_active())

    def _serve(self) -> None:
        self.listener.settimeout(0.5)
        while not self._closed.is_set():
            try:
                conn, address = self.listener.accept()
            except socket.timeout:
                if not self.transport.is_active():
                    self.failed = "The SSH connection of the tunnel closed."
                    self.log(self.failed)
                    self.close()
                continue
            except OSError:
                return
            if address[0] != "127.0.0.1":
                conn.close()
                continue
            with self._lock:
                if self._active >= self.max_clients:
                    self.log("SSH tunnel: refused a second simultaneous connection")
                    conn.close()
                    continue
                self._active += 1
            thread = threading.Thread(target=self._forward, args=(conn,), daemon=True, name="sshm-rdp-tunnel-io")
            self._clients.append(thread)
            thread.start()

    def _forward(self, conn: socket.socket) -> None:
        channel = None
        try:
            channel = self.transport.open_channel("direct-tcpip", (self.target_host, self.target_port),
                                                  conn.getpeername(), timeout=15)
        except Exception as exc:
            self.failed = f"The SSH host could not reach {self.target_host}:{self.target_port} ({type(exc).__name__})."
            self.log(self.failed)
            conn.close()
            with self._lock:
                self._active -= 1
            return
        try:
            while not self._closed.is_set():
                readable, _w, _x = select.select([conn, channel], [], [], 0.5)
                if conn in readable:
                    data = conn.recv(BUFFER)
                    if not data:
                        break
                    channel.sendall(data)
                if channel in readable:
                    data = channel.recv(BUFFER)
                    if not data:
                        break
                    conn.sendall(data)
        except (OSError, EOFError) as exc:
            self.log(f"SSH tunnel connection ended ({type(exc).__name__})")
        finally:
            for item in (channel, conn):
                try:
                    if item is not None:
                        item.close()
                except Exception:
                    pass
            with self._lock:
                self._active -= 1

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        try:
            self.listener.shutdown(socket.SHUT_RDWR)  # wakes the accepting thread; the port stops listening now
        except OSError:
            pass
        try:
            self.listener.close()
        except OSError:
            pass
        if threading.current_thread() is not self._thread:
            self._thread.join(timeout=2)
        try:
            self.transport.close()
        except Exception:
            pass


def open_tunnel(backend: Any, target_host: str, target_port: int, *, log: LogFn = lambda _m: None) -> SshTunnel:
    """Connect ``backend`` (an ``SftpBackend`` for the SSH host) and start the forward (blocking: use a thread).

    Raises the backend's errors (``UnknownHostKey``, ``AuthError`` …) so the UI can ask or explain.
    """

    transport = backend.open_transport("SSH tunnel")
    try:
        return SshTunnel(transport, target_host, target_port, log=log)
    except OSError:
        transport.close()
        raise


__all__ = ["SshTunnel", "open_tunnel"]
