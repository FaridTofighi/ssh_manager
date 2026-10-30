"""SFTP on ``paramiko`` (already a dependency of the SSH connector; no GTK).

* **Host keys** follow the SSH rules (``host_key_policy`` in the settings,
  the system ``known_hosts``): a known key must match; a **changed** key is
  refused (:class:`HostKeyChanged`); an **unknown** key raises
  :class:`UnknownHostKey` with its fingerprint so the UI can ask, then
  :meth:`SftpBackend.accept_host_key` records it like ``ssh`` does — unless
  the policy is ``auto-add``, which accepts it for this connection.
* **Authentication:** private key (``IdentityFile`` of the host, with an
  optional passphrase), SSH agent, password, and keyboard-interactive with a
  single password prompt. Other prompts (one-time codes) are reported as
  unsupported.
* **Jump hosts / ProxyCommand:** the same options the SSH connector
  understands (``ProxyJump``, ``ProxyCommand``) open the socket.
"""

from __future__ import annotations

import base64
import errno
import os
import socket
import stat as statmod
import time
from pathlib import Path
from typing import Any, BinaryIO, Callable, Iterator, List, Optional, Set

import paramiko
from paramiko import sftp as sftp_proto

from .backend import (
    F_CHMOD, F_MDTM, F_OWNER, F_REST, F_SIZE, F_SYMLINK, NEVER_CANCELLED, AlreadyExists, AuthError, BackendError,
    CancelToken, Cancelled, Disconnected, EntryKind, HostKeyChanged, NotFound, PermissionDenied, ProtocolError,
    RemoteEntry, RemoteFileBackend, ServerInfo, Timeout, UnknownHostKey, Unsupported, check_remote_path, join,
    redact,
)
from .settings import TransferSettings

LogFn = Callable[[str], None]
WINDOW_SIZE = 4 * 1024 * 1024
MAX_PACKET = 64 * 1024


def default_known_hosts() -> Path:
    return Path("~/.ssh/known_hosts").expanduser()


def key_fingerprint(key: paramiko.PKey) -> str:
    """``SHA256:…`` like ``ssh-keygen -l``."""

    import hashlib

    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def known_hosts_name(hostname: str, port: int) -> str:
    return hostname if port == 22 else f"[{hostname}]:{port}"


class _Stream:
    """A paramiko SFTPFile with cooperative cancellation."""

    def __init__(self, handle: paramiko.SFTPFile, cancel: CancelToken, backend: "SftpBackend") -> None:
        self._handle = handle
        self._cancel = cancel
        self._backend = backend
        self.bytes = 0

    def read(self, size: int = -1) -> bytes:
        self._cancel.raise_if_cancelled()
        try:
            data = self._handle.read(size)
        except (OSError, paramiko.SSHException) as exc:
            raise self._backend._map(exc)
        self.bytes += len(data)
        return data

    def write(self, data: bytes) -> int:
        self._cancel.raise_if_cancelled()
        try:
            self._handle.write(data)
        except (OSError, paramiko.SSHException) as exc:
            raise self._backend._map(exc)
        self.bytes += len(data)
        return len(data)

    def readable(self) -> bool:
        return "r" in self._handle._flags_text if hasattr(self._handle, "_flags_text") else True

    def close(self) -> None:
        try:
            self._handle.close()
        except (OSError, paramiko.SSHException) as exc:
            if not self._cancel.cancelled:
                raise self._backend._map(exc)

    def __enter__(self) -> "_Stream":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()


class SftpBackend(RemoteFileBackend):
    protocol = "sftp"

    def __init__(self, hostname: str, port: int = 22, user: str = "", password: str = "", *,
                 settings: Optional[TransferSettings] = None, key_file: str = "", passphrase: str = "",
                 options: Optional[dict] = None, host_key_policy: str = "reject",
                 known_hosts: Optional[Path] = None, allow_agent: bool = True, log: Optional[LogFn] = None) -> None:
        self.settings = settings or TransferSettings()
        self.hostname = hostname
        self.port = int(port)
        self.user = user
        self._password = password
        self.key_file = os.path.expanduser(key_file) if key_file else ""
        self._passphrase = passphrase
        self.options = dict(options or {})
        self.host_key_policy = host_key_policy
        self.known_hosts = Path(known_hosts) if known_hosts is not None else default_known_hosts()
        self.allow_agent = allow_agent
        self.log = log
        self._transport: Optional[paramiko.Transport] = None
        self._sftp: Optional[paramiko.SFTPClient] = None
        self._home = "/"
        self._cwd = "/"
        self._info = ServerInfo(protocol="sftp")
        self._accepted_key: Optional[paramiko.PKey] = None

    # ---------------------------------------------------------- connect
    def _socket(self) -> Any:
        from ..connector import _proxy_command_for

        proxy = _proxy_command_for(self.options, self.hostname, self.port, self.user)
        if proxy:
            return paramiko.ProxyCommand(proxy)
        return socket.create_connection((self.hostname, self.port), timeout=self.settings.timeout_connect)

    def _note(self, text: str) -> None:
        if self.log is not None:
            self.log(text)  # never a password: only names, fingerprints and outcomes

    def open_transport(self, purpose: str = "SFTP") -> Any:
        """Connect, check the host key and authenticate: an authenticated ``paramiko.Transport``.

        Shared by :meth:`connect` and the RDP SSH tunnel (``ssh_manager.rdp.tunnel``): the same jump host,
        known_hosts rules and key / agent / password order. The caller owns the transport.
        """

        self._note(f"Connecting to {self.hostname}:{self.port} ({purpose})")
        try:
            sock = self._socket()
            transport = paramiko.Transport(sock, default_window_size=WINDOW_SIZE, default_max_packet_size=MAX_PACKET)
            transport.banner_timeout = self.settings.timeout_connect
            transport.start_client(timeout=self.settings.timeout_connect)
        except BackendError as exc:
            self._note(f"Failed: {exc}")
            raise
        except Exception as exc:
            self._note(f"Failed: {type(exc).__name__}")
            raise self._map(exc)
        try:
            self._note(f"Server: {(transport.remote_version or '')[:200]}")
            key = transport.get_remote_server_key()
            self._note(f"Host key: {key.get_name()} {key_fingerprint(key)}")
            self._check_host_key(key)
            self._authenticate(transport)
            self._note(f"Authenticated as {self.user or '(no user)'}")
            if self.settings.keepalive_s:
                transport.set_keepalive(self.settings.keepalive_s)
        except BackendError as exc:
            self._note(f"Failed: {exc}")
            transport.close()
            raise
        except Exception as exc:
            self._note(f"Failed: {type(exc).__name__}")
            transport.close()
            raise self._map(exc)
        return transport

    def connect(self) -> ServerInfo:
        started = time.monotonic()
        self.close()
        transport = self.open_transport()
        key = transport.get_remote_server_key()
        try:
            sftp = paramiko.SFTPClient.from_transport(transport, window_size=WINDOW_SIZE, max_packet_size=MAX_PACKET)
            if sftp is None:
                raise ProtocolError("The server has no SFTP subsystem.")
            sftp.get_channel().settimeout(self.settings.timeout_read)
        except BackendError as exc:
            self._note(f"Failed: {exc}")
            transport.close()
            raise
        except Exception as exc:
            self._note(f"Failed: {type(exc).__name__}")
            transport.close()
            raise self._map(exc)
        self._transport, self._sftp = transport, sftp
        try:
            self._home = self._cwd = sftp.normalize(".") or "/"
        except (OSError, paramiko.SSHException):
            self._home = self._cwd = "/"
        self._note(f"SFTP session open; home folder {self._home}")
        self._info = ServerInfo(
            protocol="sftp", banner=(transport.remote_version or "")[:200],
            features={F_REST, F_CHMOD, F_SYMLINK, F_SIZE, F_MDTM, F_OWNER},
            host_key=f"{key.get_name()} {key_fingerprint(key)}", home=self._home,
            seconds=time.monotonic() - started,
        )
        return self._info

    def _check_host_key(self, key: paramiko.PKey) -> None:
        name = known_hosts_name(self.hostname, self.port)
        if self._accepted_key is not None and self._accepted_key == key:
            return
        known = paramiko.HostKeys()
        try:
            if self.known_hosts.exists():
                known.load(str(self.known_hosts))
        except (OSError, paramiko.SSHException):
            pass
        entries = known.lookup(name) or {}
        if key.get_name() in entries:
            if entries[key.get_name()] == key:
                return
            raise self._changed(key, entries[key.get_name()], entries)
        if entries:  # known with another key type: treat like OpenSSH (a changed key is suspicious)
            raise self._changed(key, next(iter(entries.values())), entries)
        if self.host_key_policy == "auto-add":
            return
        raise UnknownHostKey(hostname=self.hostname, port=self.port, key_type=key.get_name(),
                             fingerprint=key_fingerprint(key), key=key)

    def _changed(self, key: paramiko.PKey, expected: paramiko.PKey, entries) -> HostKeyChanged:
        """The error with what "Remove old key and retry" needs: the stored entry's file and line."""

        from ..known_hosts import KnownHostsFile

        line_no = 0
        try:
            stored = [e for e in KnownHostsFile(self.known_hosts).lookup(self.hostname, self.port) if not e.marker]
            same_type = [e for e in stored if e.key_type == expected.get_name()]
            line_no = (same_type or stored)[0].line_no if (same_type or stored) else 0
        except OSError:
            pass
        return HostKeyChanged(fingerprint=key_fingerprint(key), expected=key_fingerprint(expected),
                              hostname=self.hostname, port=self.port, key_type=key.get_name(),
                              old_fingerprints=[key_fingerprint(k) for k in entries.values()],
                              known_hosts_path=str(self.known_hosts), line_no=line_no)

    def accept_host_key(self, error: UnknownHostKey, *, remember: bool = True) -> None:
        """The user confirmed the fingerprint: accept it now and (``remember``) append it to known_hosts."""

        self._accepted_key = error.key
        if not remember or error.key is None:
            return
        line = f"{known_hosts_name(error.hostname, error.port)} {error.key.get_name()} {error.key.get_base64()}\n"
        self.known_hosts.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.known_hosts, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(line)

    def _authenticate(self, transport: paramiko.Transport) -> None:
        errors: List[str] = []
        if self.key_file:
            self._note("Trying the private key")
            try:
                key = paramiko.PKey.from_path(self.key_file, self._passphrase.encode() if self._passphrase else None) \
                    if hasattr(paramiko.PKey, "from_path") else _load_key(self.key_file, self._passphrase)
                transport.auth_publickey(self.user, key)
                if transport.is_authenticated():
                    return
            except paramiko.PasswordRequiredException:
                errors.append("the private key needs a passphrase")
            except (paramiko.AuthenticationException, paramiko.SSHException, OSError) as exc:
                errors.append(f"key: {type(exc).__name__}")
        if self.allow_agent:
            self._note("Trying the SSH agent")
            try:
                for agent_key in paramiko.Agent().get_keys():
                    try:
                        transport.auth_publickey(self.user, agent_key)
                        if transport.is_authenticated():
                            return
                    except paramiko.AuthenticationException:
                        continue
            except paramiko.SSHException:
                pass
        if self._password:
            self._note("Trying the password")
            try:
                transport.auth_password(self.user, self._password)
                if transport.is_authenticated():
                    return
            except paramiko.BadAuthenticationType as exc:
                if "keyboard-interactive" in (exc.allowed_types or []):
                    self._keyboard_interactive(transport)
                    return
                errors.append("password logins are not allowed")
            except paramiko.AuthenticationException as exc:
                errors.append(type(exc).__name__)
        raise AuthError(technical="; ".join(errors) or "no usable authentication method")

    def _keyboard_interactive(self, transport: paramiko.Transport) -> None:
        def handler(_title: str, _instructions: str, prompts: list) -> list:
            if len(prompts) == 1 and "password" in prompts[0][0].lower():
                return [self._password]
            raise Unsupported("The server asks for a one-time code; this is not supported for file transfer.")

        try:
            transport.auth_interactive(self.user, handler)
        except paramiko.AuthenticationException as exc:
            raise AuthError(technical=type(exc).__name__)

    # --------------------------------------------------------- lifecycle
    def close(self) -> None:
        for item in (self._sftp, self._transport):
            if item is not None:
                try:
                    item.close()
                except Exception:
                    pass
        self._sftp = None
        self._transport = None

    def is_alive(self) -> bool:
        return self._transport is not None and self._transport.is_active()

    def keepalive(self) -> None:
        if self._transport is not None and self._transport.is_active():
            self._transport.send_ignore()

    def _client(self) -> paramiko.SFTPClient:
        if self._sftp is None or not self.is_alive():
            raise Disconnected("Not connected.")
        return self._sftp

    def _do(self, operation: Callable[[paramiko.SFTPClient], Any]) -> Any:
        try:
            return operation(self._client())
        except BackendError:
            raise
        except Exception as exc:
            raise self._map(exc)

    # ------------------------------------------------------- directories
    def home(self) -> str:
        return self._home

    def pwd(self) -> str:
        return self._cwd

    def features(self) -> Set[str]:
        return set(self._info.features)

    def server_info(self) -> ServerInfo:
        return self._info

    def list(self, path: str, cancel: CancelToken = NEVER_CANCELLED) -> Iterator[RemoteEntry]:
        """Stream the entries one READDIR batch at a time (paramiko's ``listdir_iter`` read-ahead stalls on
        some servers; ``listdir_attr`` would hold a 100k-entry directory in memory)."""

        check_remote_path(path)
        client = self._client()
        try:
            kind, message = client._request(sftp_proto.CMD_OPENDIR, client._adjust_cwd(path))
            if kind != sftp_proto.CMD_HANDLE:
                raise ProtocolError("The server did not open the folder.")
            handle = message.get_binary()
        except BackendError:
            raise
        except Exception as exc:
            raise self._map(exc)
        try:
            while True:
                cancel.raise_if_cancelled()
                try:
                    kind, message = client._request(sftp_proto.CMD_READDIR, handle)
                except EOFError:
                    break
                if kind != sftp_proto.CMD_NAME:
                    raise ProtocolError("Unexpected answer while listing a folder.")
                for _index in range(message.get_int()):
                    filename = message.get_text()
                    longname = message.get_text()
                    attributes = paramiko.SFTPAttributes._from_msg(message, filename, longname)
                    if filename not in (".", ".."):
                        yield self._entry(path, attributes)
        except (Cancelled, BackendError):
            raise
        except Exception as exc:
            raise self._map(exc)
        finally:
            try:
                client._request(sftp_proto.CMD_CLOSE, handle)
            except Exception:
                pass

    def _entry(self, directory: str, attributes: paramiko.SFTPAttributes) -> RemoteEntry:
        name = attributes.filename
        mode = attributes.st_mode
        link_target = ""
        if mode is not None and statmod.S_ISLNK(mode):
            kind = EntryKind.SYMLINK
            try:
                link_target = self._client().readlink(join(directory, name)) or ""
            except (OSError, paramiko.SSHException, BackendError):
                link_target = ""
        elif mode is not None and statmod.S_ISDIR(mode):
            kind = EntryKind.DIR
        elif mode is None or statmod.S_ISREG(mode):
            kind = EntryKind.FILE
        else:
            kind = EntryKind.OTHER
        owner = group = ""
        longname = getattr(attributes, "longname", "") or ""
        parts = longname.split()
        if len(parts) >= 4:
            owner, group = parts[2], parts[3]
        elif attributes.st_uid is not None:
            owner, group = str(attributes.st_uid), str(attributes.st_gid or "")
        return RemoteEntry(
            name, join(directory, name), kind,
            size=attributes.st_size if kind is not EntryKind.DIR else None,
            mtime=float(attributes.st_mtime) if attributes.st_mtime is not None else None,
            mode=statmod.S_IMODE(mode) if mode is not None else None,
            perms=statmod.filemode(mode)[1:] if mode is not None else "",
            owner=owner, group=group, link_target=link_target,
        )

    def stat(self, path: str) -> RemoteEntry:
        check_remote_path(path)
        attributes = self._do(lambda c: c.lstat(path))
        directory, _, name = path.rstrip("/").rpartition("/")
        attributes.filename = name or "/"
        return self._entry(directory or "/", attributes)

    def mkdir(self, path: str, parents: bool = False) -> None:
        check_remote_path(path)
        if not parents:
            self._do(lambda c: c.mkdir(path))
            return
        current = "/" if path.startswith("/") else ""
        for part in [p for p in path.split("/") if p]:
            current = join(current, part) if current else part
            try:
                if self.stat(current).kind is EntryKind.DIR:
                    continue
            except NotFound:
                pass

            def make(client: paramiko.SFTPClient, target: str = current) -> None:
                client.mkdir(target)

            self._do(make)

    def rename(self, old: str, new: str) -> None:
        check_remote_path(old)
        check_remote_path(new)

        def go(client: paramiko.SFTPClient) -> None:
            try:
                client.posix_rename(old, new)  # atomic, replaces the target (OpenSSH extension)
            except (IOError, paramiko.SSHException) as exc:
                if getattr(exc, "errno", None) in (errno.ENOENT, errno.EACCES):
                    raise
                client.rename(old, new)

        self._do(go)

    def delete_file(self, path: str) -> None:
        check_remote_path(path)
        self._do(lambda c: c.remove(path))

    def delete_dir(self, path: str, recursive: bool = False, cancel: CancelToken = NEVER_CANCELLED) -> None:
        check_remote_path(path)
        if recursive:
            for entry in list(self.list(path, cancel)):
                cancel.raise_if_cancelled()
                if entry.kind is EntryKind.DIR:
                    self.delete_dir(entry.path, recursive=True, cancel=cancel)
                else:
                    self.delete_file(entry.path)  # symlinks are removed, never followed
        self._do(lambda c: c.rmdir(path))

    def chmod(self, path: str, mode: int) -> None:
        check_remote_path(path)
        self._do(lambda c: c.chmod(path, mode & 0o7777))

    def set_mtime(self, path: str, mtime: float) -> None:
        check_remote_path(path)
        self._do(lambda c: c.utime(path, (mtime, mtime)))

    def is_dir(self, path: str) -> bool:
        check_remote_path(path)
        try:
            attributes = self._do(lambda c: c.stat(path))  # stat follows links (lstat would not)
        except NotFound:
            return False
        return attributes.st_mode is not None and statmod.S_ISDIR(attributes.st_mode)

    def readlink(self, path: str) -> str:
        check_remote_path(path)
        return self._do(lambda c: c.readlink(path)) or ""

    # ------------------------------------------------------- transfers
    def open_read(self, path: str, offset: int = 0, cancel: CancelToken = NEVER_CANCELLED, **_kw) -> BinaryIO:
        check_remote_path(path)

        def go(client: paramiko.SFTPClient):
            handle = client.open(path, "rb")
            if offset:
                handle.seek(offset)
            handle.prefetch()
            return handle

        return _Stream(self._do(go), cancel, self)  # type: ignore[return-value]

    def open_write(self, path: str, offset: int = 0, append: bool = False,
                   cancel: CancelToken = NEVER_CANCELLED, **_kw) -> BinaryIO:
        check_remote_path(path)

        def go(client: paramiko.SFTPClient):
            if append:
                handle = client.open(path, "ab")
            elif offset:
                handle = client.open(path, "r+b")
                handle.seek(offset)
                handle.truncate(offset)
            else:
                handle = client.open(path, "wb")
            handle.set_pipelined(True)
            return handle

        return _Stream(self._do(go), cancel, self)  # type: ignore[return-value]

    # ---------------------------------------------------------- errors
    def _map(self, exc: BaseException) -> BackendError:
        technical = redact(f"{type(exc).__name__}: {exc}", {"password": self._password, "pass": self._passphrase})
        if isinstance(exc, BackendError):
            return exc
        if isinstance(exc, paramiko.AuthenticationException):
            return AuthError(technical=technical)
        if isinstance(exc, (socket.timeout, TimeoutError)):
            return Timeout(technical=technical)
        if isinstance(exc, socket.gaierror):
            return BackendError("The server's name could not be resolved.", technical)
        if isinstance(exc, ConnectionRefusedError):
            return Disconnected("The server refused the connection. Check the host name and port.", technical)
        code = getattr(exc, "errno", None)
        if isinstance(exc, FileNotFoundError) or code == errno.ENOENT:
            return NotFound(technical=technical)
        if isinstance(exc, PermissionError) or code == errno.EACCES:
            return PermissionDenied(technical=technical)
        if code == errno.EEXIST:
            return AlreadyExists(technical=technical)
        if isinstance(exc, IOError) and "failure" in str(exc).lower():
            return BackendError("The server could not do it (it may already exist or not be empty).", technical)
        if isinstance(exc, (EOFError, ConnectionError)) or (isinstance(exc, paramiko.SSHException)
                                                            and "closed" in str(exc).lower()):
            return Disconnected(technical=technical)
        if isinstance(exc, paramiko.SSHException):
            return ProtocolError(technical=technical)
        if isinstance(exc, OSError):
            return Disconnected(f"Network error: {exc.strerror or exc}", technical)
        return BackendError(technical=technical)


def _load_key(path: str, passphrase: str) -> paramiko.PKey:
    last: Optional[Exception] = None
    for cls in (paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey):
        try:
            return cls.from_private_key_file(path, password=passphrase or None)
        except paramiko.PasswordRequiredException:
            raise
        except (paramiko.SSHException, ValueError) as exc:
            last = exc
    raise paramiko.SSHException(f"unsupported key: {last}")


def backend_for(host: Any, user: str, password: str, *, settings: Optional[TransferSettings] = None,
                host_key_policy: str = "reject", passphrase: str = "", log: Optional[LogFn] = None) -> SftpBackend:
    """An :class:`SftpBackend` for an ``sftp`` :class:`HostEntry` (key and proxy options from the host)."""

    from ..host_utils import host_option

    settings = settings or TransferSettings.for_host(host)[0]
    hostname = host_option(host, "hostname", "host") or host.alias
    port_text = host_option(host, "port") or ""
    return SftpBackend(hostname, int(port_text) if port_text.isdigit() else 22, user, password, settings=settings,
                       key_file=host_option(host, "identityfile") or "", passphrase=passphrase,
                       options=dict(host.options), host_key_policy=host_key_policy, log=log)
