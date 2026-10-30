"""A small, real SFTP server (paramiko ``ServerInterface`` + ``SFTPServerInterface``) over a temp directory.

Password and public-key authentication; an optional keyboard-interactive mode
that asks for a one-time code. Synthetic data only.
"""

from __future__ import annotations

import os
import socket
import threading
from pathlib import Path
from typing import Optional

import paramiko
from paramiko import SFTPAttributes, SFTPHandle, SFTPServer, SFTPServerInterface
from paramiko.sftp import SFTP_FAILURE, SFTP_OK


class _Handle(SFTPHandle):
    def stat(self):
        try:
            return SFTPAttributes.from_stat(os.fstat(self.readfile.fileno()))
        except OSError as exc:
            return SFTPServer.convert_errno(exc.errno)

    def chattr(self, attr):
        return SFTP_OK


class _SftpServer(SFTPServerInterface):
    def __init__(self, server, *args, **kwargs) -> None:
        super().__init__(server, *args, **kwargs)
        self.root = server.root

    def _real(self, path: str) -> str:
        return os.path.join(self.root, os.path.normpath("/" + path).lstrip("/"))

    def canonicalize(self, path: str) -> str:
        return os.path.normpath("/" + path) if path not in (".", "") else "/"

    def list_folder(self, path):
        real = self._real(path)
        try:
            out = []
            for name in os.listdir(real):
                attributes = SFTPAttributes.from_stat(os.lstat(os.path.join(real, name)))
                attributes.filename = name
                out.append(attributes)
            return out
        except OSError as exc:
            return SFTPServer.convert_errno(exc.errno)

    def stat(self, path):
        try:
            return SFTPAttributes.from_stat(os.stat(self._real(path)))
        except OSError as exc:
            return SFTPServer.convert_errno(exc.errno)

    def lstat(self, path):
        try:
            return SFTPAttributes.from_stat(os.lstat(self._real(path)))
        except OSError as exc:
            return SFTPServer.convert_errno(exc.errno)

    def open(self, path, flags, attr):
        real = self._real(path)
        try:
            fd = os.open(real, flags | getattr(os, "O_BINARY", 0), 0o644)
        except OSError as exc:
            return SFTPServer.convert_errno(exc.errno)
        if flags & os.O_WRONLY:
            mode = "ab" if flags & os.O_APPEND else "wb"
        elif flags & os.O_RDWR:
            mode = "a+b" if flags & os.O_APPEND else "r+b"
        else:
            mode = "rb"
        handle = _Handle(flags)
        handle.filename = real
        handle.readfile = handle.writefile = os.fdopen(fd, mode)
        return handle

    def remove(self, path):
        try:
            os.remove(self._real(path))
        except OSError as exc:
            return SFTPServer.convert_errno(exc.errno)
        return SFTP_OK

    def rename(self, old, new):
        if os.path.exists(self._real(new)):
            return SFTP_FAILURE
        try:
            os.rename(self._real(old), self._real(new))
        except OSError as exc:
            return SFTPServer.convert_errno(exc.errno)
        return SFTP_OK

    def posix_rename(self, old, new):
        try:
            os.replace(self._real(old), self._real(new))
        except OSError as exc:
            return SFTPServer.convert_errno(exc.errno)
        return SFTP_OK

    def mkdir(self, path, attr):
        try:
            os.mkdir(self._real(path))
        except OSError as exc:
            return SFTPServer.convert_errno(exc.errno)
        return SFTP_OK

    def rmdir(self, path):
        try:
            os.rmdir(self._real(path))
        except OSError as exc:
            return SFTPServer.convert_errno(exc.errno)
        return SFTP_OK

    def chattr(self, path, attr):
        try:
            if attr.st_mode is not None:
                os.chmod(self._real(path), attr.st_mode)
            if attr.st_atime is not None:
                os.utime(self._real(path), (attr.st_atime, attr.st_mtime))
        except OSError as exc:
            return SFTPServer.convert_errno(exc.errno)
        return SFTP_OK

    def symlink(self, target, path):
        try:
            os.symlink(target, self._real(path))
        except OSError as exc:
            return SFTPServer.convert_errno(exc.errno)
        return SFTP_OK

    def readlink(self, path):
        try:
            return os.readlink(self._real(path))
        except OSError as exc:
            return SFTPServer.convert_errno(exc.errno)


class _Server(paramiko.ServerInterface):
    def __init__(self, owner: "FakeSftpServer", transport=None) -> None:
        self.owner = owner
        self.root = owner.root
        self.transport = transport
        self.listeners: dict = {}  # tcpip-forward: port -> listening socket

    def check_auth_password(self, username, password):
        if self.owner.otp_only:
            return paramiko.AUTH_FAILED
        return paramiko.AUTH_SUCCESSFUL if self.owner.users.get(username) == password else paramiko.AUTH_FAILED

    def check_auth_publickey(self, username, key):
        if username in self.owner.users and self.owner.authorized_key is not None and key == self.owner.authorized_key:
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def check_auth_interactive(self, username, submethods):
        if not self.owner.otp_only:
            return paramiko.AUTH_FAILED
        query = paramiko.InteractiveQuery("", "")
        query.add_prompt("Verification code: ", False)
        return query

    def check_auth_interactive_response(self, responses):
        return paramiko.AUTH_FAILED

    def get_allowed_auths(self, username):
        return "keyboard-interactive" if self.owner.otp_only else "password,publickey"

    def check_channel_request(self, kind, chanid):
        if kind == "direct-tcpip":
            return paramiko.OPEN_SUCCEEDED if self.owner.allow_forward else \
                paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED
        return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_port_forward_request(self, address, port):
        """``tcpip-forward`` (remote forwards): listen on 127.0.0.1 and hand each connection back as a channel."""

        if not self.owner.allow_remote_forward:
            return False
        listener = socket.create_server(("127.0.0.1", port))
        real = listener.getsockname()[1]
        self.listeners[real] = listener
        self.owner.remote_listeners.append(listener)

        def accept() -> None:
            listener.settimeout(0.2)
            while not self.owner._stop.is_set():
                try:
                    conn, peer = listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    return
                try:
                    channel = self.transport.open_forwarded_tcpip_channel(peer, (address, real))
                except Exception:
                    conn.close()
                    continue
                threading.Thread(target=_bridge, args=(channel, conn, self.owner._stop), daemon=True).start()

        threading.Thread(target=accept, daemon=True).start()
        return real

    def cancel_port_forward_request(self, address, port):
        listener = self.listeners.pop(port, None)
        if listener is not None:
            listener.close()

    def check_channel_direct_tcpip_request(self, chanid, origin, destination):
        if not self.owner.allow_forward:
            return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED
        self.owner.forwards[chanid] = destination
        return paramiko.OPEN_SUCCEEDED


def _bridge(channel, target, stop) -> None:
    import select

    try:
        while not stop.is_set():
            ready, _w, _x = select.select([channel, target], [], [], 0.5)
            if channel in ready:
                data = channel.recv(65536)
                if not data:
                    break
                target.sendall(data)
            if target in ready:
                data = target.recv(65536)
                if not data:
                    break
                channel.sendall(data)
    except OSError:
        pass
    finally:
        channel.close()
        target.close()


class FakeSftpServer:
    def __init__(self, root: Path, *, users: Optional[dict] = None, host_key: Optional[paramiko.PKey] = None) -> None:
        self.root = str(root)
        self.users = users or {"alice": "s3cret pass"}
        self.host_key = host_key or paramiko.ECDSAKey.generate()
        self.authorized_key: Optional[paramiko.PKey] = None
        self.otp_only = False
        self.allow_forward = True  # direct-tcpip (RDP SSH tunnel tests)
        self.allow_remote_forward = True  # tcpip-forward (port forwarding tests)
        self.remote_listeners: list = []
        self.forwards: dict = {}  # channel id -> (host, port) asked for
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._transports: list = []

    def _serve(self) -> None:
        self.listener.settimeout(0.2)
        while not self._stop.is_set():
            try:
                conn, _addr = self.listener.accept()
            except (socket.timeout, OSError):
                continue
            transport = paramiko.Transport(conn)
            transport.add_server_key(self.host_key)
            transport.set_subsystem_handler("sftp", paramiko.SFTPServer, _SftpServer)
            try:
                transport.start_server(server=_Server(self, transport))
            except (paramiko.SSHException, EOFError, OSError):
                continue
            self._transports.append(transport)
            threading.Thread(target=self._accept_forwards, args=(transport,), daemon=True).start()

    def _accept_forwards(self, transport) -> None:
        """Bridge every accepted direct-tcpip channel to the destination it asked for."""

        import select

        while transport.is_active() and not self._stop.is_set():
            channel = transport.accept(0.2)
            if channel is None or channel.get_id() not in self.forwards:
                continue
            host, port = self.forwards[channel.get_id()]
            try:
                target = socket.create_connection((host, port), timeout=5)
            except OSError:
                channel.close()
                continue

            def bridge(channel=channel, target=target) -> None:
                try:
                    while True:
                        ready, _w, _x = select.select([channel, target], [], [], 0.5)
                        if channel in ready:
                            data = channel.recv(65536)
                            if not data:
                                break
                            target.sendall(data)
                        if target in ready:
                            data = target.recv(65536)
                            if not data:
                                break
                            channel.sendall(data)
                except (OSError, EOFError):
                    pass
                finally:
                    channel.close()
                    target.close()

            threading.Thread(target=bridge, daemon=True).start()

    def __enter__(self) -> "FakeSftpServer":
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self._stop.set()
        for listener in self.remote_listeners:
            listener.close()
        for transport in self._transports:
            transport.close()
        self.listener.close()
        self._thread.join(timeout=2)
