"""The remote file backend interface shared by FTP, FTPS and SFTP (pure Python, no GTK).

Every method is synchronous and meant for a worker thread; one backend object
is one connection and is not used from two threads at once. Remote data
(names, sizes, link targets, banners) is untrusted everywhere it is shown or
written locally.

Errors form a small hierarchy (:class:`BackendError` and subclasses) with a
user-facing ``message`` and a ``technical`` detail; neither ever contains a
password.
"""

from __future__ import annotations

import posixpath
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, BinaryIO, Dict, Iterator, Optional, Set

MAX_PATH = 4096  # characters of a remote path
MAX_NAME_BYTES = 255  # of one name (UTF-8)

# features()
F_MLSD, F_REST, F_CHMOD, F_SYMLINK, F_UTF8, F_MDTM, F_SIZE, F_OWNER, F_TLS, F_MFMT = (
    "mlsd", "rest", "chmod", "symlink", "utf8", "mdtm", "size", "owner_group", "tls", "mfmt")


# --------------------------------------------------------------------- errors
class BackendError(Exception):
    """A failure with a message for the user and a technical detail for the expander."""

    default_message = "The file server reported an error."

    def __init__(self, message: str = "", technical: str = "") -> None:
        self.message = message or self.default_message
        self.technical = technical
        super().__init__(self.message)


class AuthError(BackendError):
    default_message = "The server rejected the user name or password."


class HostKeyError(BackendError):
    default_message = "The server's host key could not be verified."


class UnknownHostKey(HostKeyError):
    """First connection: the user must confirm the fingerprint (``key_type``, ``fingerprint``)."""

    default_message = "This server's host key is not known yet."

    def __init__(self, message: str = "", technical: str = "", *, hostname: str = "", port: int = 22,
                 key_type: str = "", fingerprint: str = "", key: Any = None) -> None:
        super().__init__(message, technical)
        self.hostname, self.port, self.key_type, self.fingerprint, self.key = hostname, port, key_type, fingerprint, key


class HostKeyChanged(HostKeyError):
    default_message = "The server's host key has CHANGED. This can mean an attack; the connection was stopped."

    def __init__(self, message: str = "", technical: str = "", *, fingerprint: str = "", expected: str = "") -> None:
        super().__init__(message, technical)
        self.fingerprint, self.expected = fingerprint, expected


class TlsError(BackendError):
    default_message = "The secure (TLS) connection failed."


@dataclass(frozen=True)
class CertificateInfo:
    """What the "Trust this certificate?" dialog shows (all from the untrusted peer: escape before markup)."""

    subject: str = ""
    issuer: str = ""
    not_before: str = ""
    not_after: str = ""
    sha256: str = ""  # "AB:CD:…"
    reason: str = ""  # why it is not trusted (verification error)


class UntrustedCertificate(TlsError):
    default_message = "The server's certificate is not trusted."

    def __init__(self, message: str = "", technical: str = "", *, info: Optional[CertificateInfo] = None) -> None:
        super().__init__(message, technical)
        self.info = info or CertificateInfo()


class CertificateChanged(UntrustedCertificate):
    default_message = "The server's certificate has CHANGED since you trusted it. The connection was stopped."

    def __init__(self, message: str = "", technical: str = "", *, info: Optional[CertificateInfo] = None,
                 expected: str = "") -> None:
        super().__init__(message, technical, info=info)
        self.expected = expected


class NotFound(BackendError):
    default_message = "The file or folder does not exist."


class PermissionDenied(BackendError):
    default_message = "Permission denied."


class AlreadyExists(BackendError):
    default_message = "A file or folder with that name already exists."


class Timeout(BackendError):
    default_message = "The server did not answer in time."


class Disconnected(BackendError):
    default_message = "The connection to the server was lost."


class Unsupported(BackendError):
    default_message = "The server does not support this operation."


class ProtocolError(BackendError):
    default_message = "The server sent an unexpected answer."


class Cancelled(BackendError):
    default_message = "Cancelled."


class DiskFull(BackendError):
    default_message = "There is not enough space on the server."


# ------------------------------------------------------------------ helpers
class CancelToken:
    """Cooperative cancellation shared between the UI and a worker."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise Cancelled()


NEVER_CANCELLED = CancelToken()


def check_remote_path(path: str) -> str:
    """A remote path that is safe to put on a control channel: no CR, LF or NUL, not over-long."""

    if not isinstance(path, str):
        raise ProtocolError("Invalid path.", f"path of type {type(path).__name__}")
    if any(ch in path for ch in ("\r", "\n", "\0")):
        raise ProtocolError("A file name with line breaks cannot be used on this server.",
                            "CR, LF or NUL in a path (would inject a command)")
    if len(path) > MAX_PATH:
        raise ProtocolError("The path is too long.", f"{len(path)} characters (limit {MAX_PATH})")
    return path


def join(directory: str, name: str) -> str:
    return posixpath.join(directory or "/", name)


class EntryKind(str, Enum):
    FILE = "file"
    DIR = "dir"
    SYMLINK = "symlink"
    OTHER = "other"


@dataclass(frozen=True)
class RemoteEntry:
    """One listed item. Everything here comes from the server: untrusted."""

    name: str
    path: str
    kind: EntryKind = EntryKind.FILE
    size: Optional[int] = None
    mtime: Optional[float] = None  # seconds since the epoch, UTC
    mode: Optional[int] = None  # permission bits when known
    perms: str = ""  # "rwxr-xr-x" (LIST) or MLSD perm facts
    owner: str = ""
    group: str = ""
    link_target: str = ""

    @property
    def is_hidden(self) -> bool:
        return self.name.startswith(".")

    @property
    def is_dir(self) -> bool:
        return self.kind is EntryKind.DIR


@dataclass
class ServerInfo:
    """Diagnostics for "Test connection" and "Copy diagnostics" (no secrets)."""

    protocol: str = ""
    banner: str = ""
    system: str = ""
    features: Set[str] = field(default_factory=set)
    raw_features: str = ""
    tls_version: str = ""
    tls_cipher: str = ""
    certificate: Optional[CertificateInfo] = None
    host_key: str = ""  # SFTP: "ssh-ed25519 SHA256:…"
    home: str = ""
    seconds: float = 0.0

    def lines(self) -> list:
        out = [f"Protocol: {self.protocol}"]
        if self.banner:
            out.append(f"Banner: {self.banner}")
        if self.system:
            out.append(f"System: {self.system}")
        if self.tls_version:
            out.append(f"TLS: {self.tls_version} {self.tls_cipher}".rstrip())
        if self.certificate and self.certificate.sha256:
            out.append(f"Certificate SHA-256: {self.certificate.sha256}")
        if self.host_key:
            out.append(f"Host key: {self.host_key}")
        if self.features:
            out.append("Features: " + ", ".join(sorted(self.features)))
        if self.home:
            out.append(f"Home: {self.home}")
        if self.seconds:
            out.append(f"Connected in {self.seconds:.2f} s")
        return out


class RemoteFileBackend(ABC):
    """One connection to a file server. Synchronous; use it from one worker thread at a time."""

    protocol = ""

    @abstractmethod
    def connect(self) -> ServerInfo: ...

    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    def is_alive(self) -> bool: ...

    @abstractmethod
    def keepalive(self) -> None: ...

    @abstractmethod
    def home(self) -> str: ...

    @abstractmethod
    def pwd(self) -> str: ...

    @abstractmethod
    def list(self, path: str, cancel: CancelToken = NEVER_CANCELLED) -> Iterator[RemoteEntry]: ...

    @abstractmethod
    def stat(self, path: str) -> RemoteEntry: ...

    @abstractmethod
    def mkdir(self, path: str, parents: bool = False) -> None: ...

    @abstractmethod
    def rename(self, old: str, new: str) -> None: ...

    @abstractmethod
    def delete_file(self, path: str) -> None: ...

    @abstractmethod
    def delete_dir(self, path: str, recursive: bool = False, cancel: CancelToken = NEVER_CANCELLED) -> None: ...

    def chmod(self, path: str, mode: int) -> None:
        raise Unsupported("Changing permissions is not supported on this server.")

    def set_mtime(self, path: str, mtime: float) -> None:
        raise Unsupported("Setting the modification time is not supported on this server.")

    @abstractmethod
    def open_read(self, path: str, offset: int = 0, cancel: CancelToken = NEVER_CANCELLED) -> BinaryIO: ...

    @abstractmethod
    def open_write(self, path: str, offset: int = 0, append: bool = False,
                   cancel: CancelToken = NEVER_CANCELLED) -> BinaryIO: ...

    @abstractmethod
    def features(self) -> Set[str]: ...

    @abstractmethod
    def server_info(self) -> ServerInfo: ...

    def exists(self, path: str) -> bool:
        try:
            self.stat(path)
            return True
        except NotFound:
            return False

    def __enter__(self) -> "RemoteFileBackend":
        self.connect()
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()


def redact(text: str, secrets: Dict[str, str] | None = None) -> str:
    """Mask a ``PASS …`` command and any known secret in a log line."""

    stripped = text.lstrip()
    if stripped[:5].upper() == "PASS ":
        return text[: len(text) - len(stripped)] + "PASS ********"
    for secret in (secrets or {}).values():
        if secret:
            text = text.replace(secret, "********")
    return text
