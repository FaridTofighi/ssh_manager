"""FTP and FTPS (explicit ``AUTH TLS`` and implicit TLS) on ``ftplib`` + ``ssl`` (no GTK).

Security-relevant behaviour:

* **PASV address:** the IP in a PASV reply is ignored and the control
  connection's peer is used (``trust_server_pasv_ipv4_address`` is off unless
  the host enables "Trust server-provided PASV address" for a NAT quirk).
  This stops FTP-bounce / redirect tricks.
* **Active mode:** the data connection must come from the control peer's
  address; any other is closed (``ProtocolError``). An optional local port
  range is used for the listening socket.
* **TLS data channel:** many servers (vsftpd, FileZilla Server, ProFTPD)
  require the data connection to *reuse the control connection's TLS
  session*. ``ftplib`` does not do that; :meth:`_Client.ntransfercmd` passes
  ``session=self.sock.session``.
* **Certificates:** verified against the system CAs (or a custom CA file) by
  default, TLS 1.2 minimum. A certificate the user trusted is pinned by its
  SHA-256 (:mod:`.pins`); a changed pinned certificate is refused
  (:class:`CertificateChanged`). "Insecure" turns verification off.
* **Control channel:** CR / LF / NUL in a path is refused before anything is
  sent (:func:`check_remote_path`); ``PASS`` is masked in the protocol log.
* **Names:** decoded with ``surrogateescape``, so names in an unexpected
  encoding round-trip unchanged.
"""

from __future__ import annotations

import calendar
import ftplib
import io
import socket
import ssl
import time
from typing import Any, BinaryIO, Callable, Dict, Iterator, List, Optional, Set, Tuple

from .backend import (
    F_CHMOD, F_MDTM, F_MFMT, F_MLSD, F_REST, F_SIZE, F_TLS, F_UTF8, NEVER_CANCELLED, AuthError, BackendError,
    CancelToken, Cancelled, CertificateChanged, CertificateInfo, Disconnected, DiskFull, EntryKind, NotFound,
    PermissionDenied, ProtocolError, RemoteEntry, RemoteFileBackend, ServerInfo, Timeout, TlsError,
    UntrustedCertificate, Unsupported, check_remote_path, join, redact,
)
from .listing import parse_list_line, parse_mlsd_line
from .pins import PinStore, fingerprint, normalize_fingerprint, pin_key
from .settings import TransferSettings

LogFn = Callable[[str], None]
RECONNECT_ATTEMPTS = 3
RECONNECT_BACKOFF_S = (0.5, 1.0, 2.0)


# ---------------------------------------------------------------- the client
class _Client(ftplib.FTP_TLS):
    """``ftplib`` with implicit TLS, TLS session reuse, safe active mode and a masked protocol log."""

    sock: Any  # set by connect(); ssl.SSLSocket after AUTH TLS / implicit TLS
    _prot_p: bool

    def __init__(self, *, tls_mode: str, context: Optional[ssl.SSLContext], encoding: str, timeout: float,
                 trust_pasv: bool, port_range: Optional[Tuple[int, int]], log: Optional[LogFn],
                 secrets: Dict[str, str]) -> None:
        ftplib.FTP_TLS.__init__(self, context=context or ssl.create_default_context(), timeout=timeout,
                                encoding=encoding)
        self.tls_mode = tls_mode  # "none" | "explicit" | "implicit"
        self.trust_server_pasv_ipv4_address = trust_pasv
        self.port_range = port_range
        self._log = log
        self._secrets = secrets

    # ---------------------------------------------------------- connect
    def connect(self, host: str = "", port: int = 0, timeout: float = -999, source_address=None) -> str:
        self.host, self.port = host, port
        if timeout != -999:
            self.timeout = timeout
        sock = socket.create_connection((self.host, self.port), self.timeout)
        if self.tls_mode == "implicit":  # TLS from the first byte (port 990)
            try:
                sock = self.context.wrap_socket(sock, server_hostname=self.host)
            except BaseException:
                sock.close()
                raise
        self.sock = sock
        self.af = sock.family
        self._make_file()
        self.welcome = self.getresp()
        return self.welcome

    def _make_file(self) -> None:
        self.file = self.sock.makefile("r", encoding=self.encoding, errors="surrogateescape")

    def auth(self):  # type: ignore[override]
        resp = ftplib.FTP_TLS.auth(self)
        self._make_file()  # ftplib recreates it without surrogateescape
        return resp

    # ---------------------------------------------------- control channel
    def putline(self, line: str) -> None:
        if "\r" in line or "\n" in line or "\0" in line:
            raise ProtocolError("A file name with line breaks cannot be used on this server.",
                                "CR, LF or NUL in a command")
        if self._log is not None:
            self._log("> " + redact(line, self._secrets))
        self.sock.sendall((line + ftplib.CRLF).encode(self.encoding, errors="surrogateescape"))

    def getline(self) -> str:
        line = ftplib.FTP_TLS.getline(self)
        if self._log is not None:
            self._log("< " + redact(line, self._secrets))
        return line

    # ------------------------------------------------------ data channel
    def makeport(self) -> socket.socket:  # type: ignore[override]
        ports = range(self.port_range[0], self.port_range[1] + 1) if self.port_range else [0]
        last: Optional[OSError] = None
        for port in ports:
            try:
                sock = socket.create_server(("", port), family=self.af, backlog=1)
                break
            except OSError as exc:
                last = exc
        else:
            raise BackendError("No free local port for active mode.", str(last))
        port = sock.getsockname()[1]
        host = self.sock.getsockname()[0]
        if self.af == socket.AF_INET:
            self.sendport(host, port)
        else:
            self.sendeprt(host, port)
        sock.settimeout(self.timeout)
        return sock

    def ntransfercmd(self, cmd: str, rest: Any = None):  # type: ignore[override]
        if self.passiveserver:
            conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        else:
            conn, size = self._active_transfer(cmd, rest)
        if self._prot_p:
            # Reuse the control channel's TLS session: required by vsftpd, FileZilla Server, ProFTPD …
            session = getattr(self.sock, "session", None)
            try:
                conn = self.context.wrap_socket(conn, server_hostname=self.host, session=session)
            except BaseException:
                conn.close()
                raise
        return conn, size

    def _active_transfer(self, cmd: str, rest: Any):
        expected_peer = self.sock.getpeername()[0]
        with self.makeport() as listener:
            if rest is not None:
                self.sendcmd(f"REST {rest}")
            resp = self.sendcmd(cmd)
            if resp[0] == "2":
                resp = self.getresp()
            if resp[0] != "1":
                raise ftplib.error_reply(resp)
            conn, address = listener.accept()
            if address[0] != expected_peer:  # never accept a data connection from another host
                conn.close()
                raise ProtocolError("The data connection came from an unexpected address and was refused.",
                                    f"expected {expected_peer}, got {address[0]}")
            conn.settimeout(self.timeout)
        size = ftplib.parse150(resp) if resp[:3] == "150" else None
        return conn, size


# --------------------------------------------------------------- streams
class _DataStream(io.RawIOBase):
    """A transfer's data connection as a file object; :meth:`close` reads the final reply."""

    def __init__(self, backend: "FtpBackend", conn: socket.socket, cancel: CancelToken, writing: bool) -> None:
        super().__init__()
        self._backend = backend
        self._conn = conn
        self._cancel = cancel
        self._writing = writing
        self._done = False
        self.bytes = 0

    def readable(self) -> bool:
        return not self._writing

    def writable(self) -> bool:
        return self._writing

    def readinto(self, buffer) -> int:
        self._cancel.raise_if_cancelled()
        try:
            count = self._conn.recv_into(buffer)
        except OSError as exc:
            raise self._backend._map(exc)
        self.bytes += count
        return count

    def write(self, data) -> int:
        self._cancel.raise_if_cancelled()
        try:
            self._conn.sendall(data)
        except OSError as exc:
            raise self._backend._map(exc)
        self.bytes += len(data)
        return len(data)

    def close(self) -> None:
        if self._done:
            return
        self._done = True
        aborted = self._cancel.cancelled
        try:
            if self._writing and isinstance(self._conn, ssl.SSLSocket) and not aborted:
                try:
                    self._conn.unwrap()  # close_notify: some servers need it to finish an upload
                except (OSError, ValueError):
                    pass
            self._conn.close()
        finally:
            super().close()
            self._backend._finish_transfer(aborted)


# ---------------------------------------------------------------- backend
class FtpBackend(RemoteFileBackend):
    """FTP (``tls_mode="none"``) and FTPS (``"explicit"`` / ``"implicit"``)."""

    def __init__(self, hostname: str, port: int, user: str = "", password: str = "", *,
                 settings: Optional[TransferSettings] = None, tls_mode: str = "none",
                 host_id: str = "", pins: Optional[PinStore] = None, log: Optional[LogFn] = None) -> None:
        self.settings = settings or TransferSettings()
        self.protocol = "ftp" if tls_mode == "none" else "ftps"
        self.hostname = hostname
        self.port = int(port)
        self.anonymous = self.settings.anonymous or not user or user.lower() == "anonymous"
        self.user = "anonymous" if self.anonymous else user
        self._password = self.settings.anonymous_password if self.anonymous else password
        self.tls_mode = tls_mode
        self.host_id = host_id
        self.pins = pins or PinStore()
        self.log = log
        self._client: Optional[_Client] = None
        self._features: Set[str] = set()
        self._raw_features = ""
        self._home = "/"
        self._cwd = "/"
        self._info = ServerInfo(protocol=self.protocol)
        self._in_transfer = False

    # -------------------------------------------------------------- tls
    @property
    def pin_key(self) -> str:
        return pin_key(self.host_id, self.hostname, self.port)

    def _context(self, verify: bool) -> ssl.SSLContext:
        if verify:
            custom = self.settings.tls_verify == "custom_ca" and self.settings.ca_file
            context = ssl.create_default_context(cafile=self.settings.ca_file if custom else None)
        else:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        return context

    def _expected_pin(self) -> str:
        return self.pins.get(self.pin_key) or normalize_fingerprint(self.settings.pinned_sha256)

    # ---------------------------------------------------------- connect
    def connect(self) -> ServerInfo:
        started = time.monotonic()
        verify = self.tls_mode != "none" and self.settings.tls_verify in ("system", "custom_ca")
        try:
            self._open(verify=verify)
        except UntrustedCertificate as exc:
            # Not verifiable by the CAs: a certificate the user trusted earlier (pinned) is accepted.
            pinned = self._expected_pin()
            if not verify or not pinned or exc.info.sha256 != pinned:
                raise
            self._open(verify=False)
        self._info.seconds = time.monotonic() - started
        return self._info

    def _open(self, *, verify: bool) -> None:
        self.close()
        settings = self.settings
        client = _Client(
            tls_mode=self.tls_mode,
            context=self._context(verify) if self.tls_mode != "none" else None,
            encoding=settings.codec(),
            timeout=float(settings.timeout_connect),
            trust_pasv=settings.trust_pasv_address,
            port_range=settings.port_range(),
            log=self.log,
            secrets={"password": "" if self.anonymous else self._password},
        )
        try:
            welcome = client.connect(self.hostname, self.port, timeout=float(settings.timeout_connect))
            if self.tls_mode == "explicit":
                client.auth()
            if self.tls_mode != "none":
                self._check_certificate(client, verified=verify)
            client.sock.settimeout(float(settings.timeout_read))
            client.timeout = float(settings.timeout_read)
            client.login(self.user, self._password, secure=False)
            if self.tls_mode != "none":
                client.prot_p()  # PBSZ 0 + PROT P: the data channel is encrypted too
            client.set_pasv(settings.passive)
        except ssl.SSLCertVerificationError as exc:
            client.close()
            raise UntrustedCertificate(technical=str(exc), info=self._fetch_certificate(str(exc.verify_message)))
        except BackendError:
            client.close()
            raise
        except Exception as exc:
            client.close()
            raise self._map(exc)
        self._client = client
        self._info = ServerInfo(protocol=self.protocol, banner=_printable(welcome))
        if isinstance(client.sock, ssl.SSLSocket):
            self._info.tls_version = client.sock.version() or ""
            cipher = client.sock.cipher()
            self._info.tls_cipher = cipher[0] if cipher else ""
            self._info.certificate = _certificate_info(client.sock.getpeercert(binary_form=True) or b"")
            self._features.add(F_TLS)
        self._read_features()
        try:
            self._info.system = _printable(client.sendcmd("SYST")[4:])
        except Exception:
            self._info.system = ""
        self._home = self._cwd = self._safe_pwd()
        self._info.home = self._home

    def _check_certificate(self, client: Any, *, verified: bool) -> None:
        der = client.sock.getpeercert(binary_form=True) or b""
        info = _certificate_info(der)
        if verified or self.settings.tls_verify == "insecure":
            return
        expected = self._expected_pin()
        if not expected:
            raise UntrustedCertificate(info=info)  # "Trust this certificate?" (first use)
        if info.sha256 != expected:
            raise CertificateChanged(info=info, expected=expected)

    def _fetch_certificate(self, reason: str) -> CertificateInfo:
        """The peer's certificate for the "Trust this certificate?" dialog (display only, then closed)."""

        try:
            probe = _Client(tls_mode=self.tls_mode, context=self._context(False), encoding="utf-8",
                            timeout=float(self.settings.timeout_connect), trust_pasv=False, port_range=None,
                            log=None, secrets={})
            probe.connect(self.hostname, self.port)
            if self.tls_mode == "explicit":
                probe.auth()
            der = probe.sock.getpeercert(binary_form=True) or b""
            probe.close()
        except Exception:
            der = b""
        info = _certificate_info(der)
        return CertificateInfo(info.subject, info.issuer, info.not_before, info.not_after, info.sha256, reason)

    def trust_certificate(self, info: CertificateInfo) -> None:
        """Pin ``info`` for this host (the user confirmed it); the next connect accepts it."""

        self.pins.set(self.pin_key, info.sha256, info.subject)

    def _read_features(self) -> None:
        client = self._require()
        features: Set[str] = set()
        try:
            raw = client.sendcmd("FEAT")
        except ftplib.all_errors:
            raw = ""
        self._raw_features = raw
        upper = raw.upper()
        for token, feature in (("MLST", F_MLSD), ("REST STREAM", F_REST), ("UTF8", F_UTF8), ("MDTM", F_MDTM),
                               ("SIZE", F_SIZE), ("MFMT", F_MFMT)):
            if token in upper:
                features.add(feature)
        if F_UTF8 in features and self.settings.encoding == "auto":
            try:
                client.sendcmd("OPTS UTF8 ON")
            except ftplib.all_errors:
                pass
        features.add(F_CHMOD)  # SITE CHMOD is tried; a refusal becomes Unsupported
        self._features |= features
        self._info.features = set(self._features)
        self._info.raw_features = _printable(raw)

    # --------------------------------------------------------- lifecycle
    def close(self) -> None:
        client, self._client = self._client, None
        if client is None:
            return
        try:
            client.quit()
        except Exception:
            try:
                client.close()
            except Exception:
                pass

    def is_alive(self) -> bool:
        return self._client is not None and self._client.sock is not None

    def keepalive(self) -> None:
        if self._in_transfer:
            return
        self._call(lambda c: c.voidcmd("NOOP"))

    def _require(self) -> _Client:
        if self._client is None:
            raise Disconnected("Not connected.")
        return self._client

    def _reconnect(self) -> None:
        last: Optional[BaseException] = None
        for delay in RECONNECT_BACKOFF_S[:RECONNECT_ATTEMPTS]:
            time.sleep(delay)
            try:
                self._open(verify=self.tls_mode != "none" and self.settings.tls_verify in ("system", "custom_ca"))
                if self._cwd and self._cwd != self._home:
                    self._require().cwd(self._cwd)
                return
            except (AuthError, UntrustedCertificate):
                raise
            except BackendError as exc:
                last = exc
        raise Disconnected("The connection was lost and could not be restored.", str(last or ""))

    def _call(self, operation: Callable[[_Client], Any], *, retry: bool = True) -> Any:
        """Run one command; reconnect (≤ 3 tries, backoff) and repeat it when the connection dropped."""

        try:
            return operation(self._require())
        except BackendError:
            raise
        except Exception as exc:
            error = self._map(exc)
            if not (retry and isinstance(error, Disconnected)):
                raise error
        self._reconnect()
        try:
            return operation(self._require())
        except BackendError:
            raise
        except Exception as exc:
            raise self._map(exc)

    # ------------------------------------------------------ directories
    def _safe_pwd(self) -> str:
        try:
            return self._require().pwd() or "/"
        except ftplib.all_errors:
            return "/"

    def home(self) -> str:
        return self._home

    def pwd(self) -> str:
        return self._cwd

    def cwd(self, path: str) -> None:
        check_remote_path(path)
        self._call(lambda c: c.cwd(path))
        self._cwd = path

    def features(self) -> Set[str]:
        return set(self._features)

    def server_info(self) -> ServerInfo:
        return self._info

    def list(self, path: str, cancel: CancelToken = NEVER_CANCELLED) -> Iterator[RemoteEntry]:
        check_remote_path(path)
        settings = self.settings
        mode = settings.list_command
        use_mlsd = mode == "mlsd" or (mode == "auto" and F_MLSD in self._features)
        if use_mlsd:
            command = f"MLSD {path}"
        elif mode == "list-a" or (mode == "auto" and settings.show_hidden):
            command = f"LIST -a {path}"
        else:
            command = f"LIST {path}"
        client = self._require()
        try:
            client.voidcmd("TYPE A")
            conn, _size = client.ntransfercmd(command)
        except ftplib.error_temp as exc:
            if str(exc)[:3] == "450" and self._is_dir(path):
                return  # some servers answer 450 for an empty directory
            raise self._map(exc)
        except ftplib.error_perm as exc:
            if str(exc)[:3] == "550" and self._is_dir(path):
                return  # … or 550
            raise self._map(exc)
        except BackendError:
            raise
        except Exception as exc:
            raise self._map(exc)
        now = time.time()
        stream = _DataStream(self, conn, cancel, writing=False)
        reader = io.TextIOWrapper(io.BufferedReader(stream), encoding=settings.codec(), errors="surrogateescape",
                                  newline=None)
        try:
            for line in reader:
                cancel.raise_if_cancelled()
                entry = (parse_mlsd_line(line, path) if use_mlsd else
                         parse_list_line(line, path, now=now, offset_min=settings.server_time_offset_min))
                if entry is not None:
                    yield entry
        finally:
            reader.close()

    def is_dir(self, path: str) -> bool:
        check_remote_path(path)
        return self._is_dir(path)  # CWD follows links

    def _is_dir(self, path: str) -> bool:
        client = self._require()
        try:
            previous = client.pwd()
            client.cwd(path)
            client.cwd(previous)
            return True
        except ftplib.all_errors:
            return False

    def stat(self, path: str) -> RemoteEntry:
        check_remote_path(path)
        name = path.rstrip("/").rsplit("/", 1)[-1] or "/"
        if F_MLSD in self._features:
            try:
                response = self._call(lambda c: c.sendcmd(f"MLST {path}"))
                lines = [line for line in response.splitlines()[1:-1] if line.strip()]
                if lines:
                    entry = parse_mlsd_line(lines[0].lstrip(" "), "/")
                    if entry is not None:
                        return RemoteEntry(name, path, entry.kind, entry.size, entry.mtime, entry.mode, entry.perms,
                                           entry.owner, entry.group, entry.link_target)
            except NotFound:
                raise
            except PermissionDenied:
                raise
            except BackendError:
                pass
        if self._is_dir(path):
            return RemoteEntry(name, path, EntryKind.DIR)
        size = None
        try:
            self._call(lambda c: c.voidcmd("TYPE I"))
            size = self._call(lambda c: c.size(path))
        except NotFound:
            raise
        except BackendError:
            size = None
        mtime = self._mdtm(path)
        if size is None and mtime is None:
            raise NotFound(f"“{name}” does not exist.")
        return RemoteEntry(name, path, EntryKind.FILE, size=size, mtime=mtime)

    def _mdtm(self, path: str) -> Optional[float]:
        if F_MDTM not in self._features:
            return None
        try:
            response = self._call(lambda c: c.sendcmd(f"MDTM {path}"))
            stamp = response[4:].strip()[:14]
            return float(calendar.timegm(time.strptime(stamp, "%Y%m%d%H%M%S")))  # MDTM is UTC
        except (BackendError, ValueError):
            return None

    def mkdir(self, path: str, parents: bool = False) -> None:
        check_remote_path(path)
        if not parents:
            self._call(lambda c: c.mkd(path))
            return
        current = "/" if path.startswith("/") else ""
        for part in [p for p in path.split("/") if p]:
            current = join(current, part) if current else part
            if self._is_dir(current):
                continue

            def make(client: _Client, target: str = current) -> None:
                client.mkd(target)

            self._call(make)

    def rename(self, old: str, new: str) -> None:
        check_remote_path(old)
        check_remote_path(new)
        self._call(lambda c: c.rename(old, new))

    def delete_file(self, path: str) -> None:
        check_remote_path(path)
        self._call(lambda c: c.delete(path))

    def delete_dir(self, path: str, recursive: bool = False, cancel: CancelToken = NEVER_CANCELLED) -> None:
        check_remote_path(path)
        if recursive:
            for entry in list(self.list(path, cancel)):
                cancel.raise_if_cancelled()
                if entry.kind is EntryKind.DIR:
                    self.delete_dir(entry.path, recursive=True, cancel=cancel)
                else:
                    self.delete_file(entry.path)
        self._call(lambda c: c.rmd(path))

    def chmod(self, path: str, mode: int) -> None:
        check_remote_path(path)
        try:
            self._call(lambda c: c.voidcmd(f"SITE CHMOD {mode & 0o7777:o} {path}"))
        except Unsupported:
            self._features.discard(F_CHMOD)
            raise

    def set_mtime(self, path: str, mtime: float) -> None:
        check_remote_path(path)
        if F_MFMT not in self._features:
            raise Unsupported("This server cannot set modification times.")
        stamp = time.strftime("%Y%m%d%H%M%S", time.gmtime(mtime))
        self._call(lambda c: c.voidcmd(f"MFMT {stamp} {path}"))

    # ------------------------------------------------------- transfers
    def open_read(self, path: str, offset: int = 0, cancel: CancelToken = NEVER_CANCELLED, *,
                  ascii: bool = False) -> BinaryIO:
        check_remote_path(path)
        if offset and F_REST not in self._features:
            raise Unsupported("This server cannot resume downloads.")
        return self._start(f"RETR {path}", offset or None, cancel, writing=False, ascii=ascii)

    def open_write(self, path: str, offset: int = 0, append: bool = False,
                   cancel: CancelToken = NEVER_CANCELLED, *, ascii: bool = False) -> BinaryIO:
        check_remote_path(path)
        if offset and F_REST not in self._features:
            raise Unsupported("This server cannot resume uploads.")
        command = f"APPE {path}" if append else f"STOR {path}"
        return self._start(command, offset or None, cancel, writing=True, ascii=ascii)

    def _start(self, command: str, rest: Optional[int], cancel: CancelToken, *, writing: bool,
               ascii: bool) -> BinaryIO:
        cancel.raise_if_cancelled()

        def begin(client: _Client):
            client.voidcmd("TYPE A" if ascii else "TYPE I")
            return client.ntransfercmd(command, rest)

        conn, _size = self._call(begin)
        self._in_transfer = True
        stream = _DataStream(self, conn, cancel, writing)
        return io.BufferedWriter(stream) if writing else io.BufferedReader(stream)  # type: ignore[return-value]

    def _finish_transfer(self, aborted: bool) -> None:
        """After a data connection closed: read the final 226 (or ABOR after a cancel)."""

        self._in_transfer = False
        client = self._client
        if client is None:
            return
        try:
            if aborted:
                try:
                    client.abort()
                except ftplib.all_errors:
                    pass
            else:
                client.voidresp()
        except (socket.timeout, TimeoutError):
            pass  # some servers close the data connection without a 226
        except ftplib.error_temp as exc:
            if not aborted:
                raise self._map(exc)
        except ftplib.error_perm as exc:
            if not aborted:
                raise self._map(exc)
        except Exception as exc:
            if not aborted:
                raise self._map(exc)

    # --------------------------------------------------------- errors
    def _map(self, exc: BaseException) -> BackendError:
        technical = redact(f"{type(exc).__name__}: {exc}", {"password": "" if self.anonymous else self._password})
        text = str(exc)
        code = text[:3]
        if isinstance(exc, BackendError):
            return exc
        if isinstance(exc, Cancelled):
            return exc
        if isinstance(exc, ftplib.error_perm):
            lowered = text.lower()
            if code == "530":
                return AuthError(technical=technical)
            if code == "550":
                if any(word in lowered for word in ("permission", "denied", "access", "not allowed")):
                    return PermissionDenied(technical=technical)
                if "exist" in lowered and "not" not in lowered:
                    return BackendError("A file or folder with that name already exists.", technical)
                return NotFound(technical=technical)
            if code == "553":
                return ProtocolError("The server does not accept this file name.", technical)
            if code == "552":
                return DiskFull(technical=technical)
            if code in ("500", "501", "502", "504"):
                return Unsupported(technical=technical)
            return BackendError(f"The server refused: {_printable(text[4:])}", technical)
        if isinstance(exc, ftplib.error_temp):
            if code == "421":
                return Disconnected("The server closed the connection (too many connections or an idle timeout).",
                                    technical)
            if code == "452":
                return DiskFull(technical=technical)
            if code in ("425", "426"):
                return Disconnected("The data connection failed.", technical)
            return BackendError(f"The server could not do it right now: {_printable(text[4:])}", technical)
        if isinstance(exc, (ftplib.error_reply, ftplib.error_proto)):
            return ProtocolError(technical=technical)
        if isinstance(exc, ssl.SSLCertVerificationError):
            return UntrustedCertificate(technical=technical)
        if isinstance(exc, ssl.SSLError):
            return TlsError(technical=technical)
        if isinstance(exc, (socket.timeout, TimeoutError)):
            return Timeout(technical=technical)
        if isinstance(exc, socket.gaierror):
            return BackendError("The server's name could not be resolved.", technical)
        if isinstance(exc, ConnectionRefusedError):
            return Disconnected("The server refused the connection. Check the host name and port.", technical)
        if isinstance(exc, (EOFError, ConnectionError, BrokenPipeError)):
            return Disconnected(technical=technical)
        if isinstance(exc, OSError):
            return Disconnected(f"Network error: {exc.strerror or exc}", technical)
        return BackendError(technical=technical)


# ---------------------------------------------------------------- helpers
def _printable(text: str) -> str:
    """Server text for display: control characters dropped, length bounded (it is untrusted)."""

    cleaned = "".join(ch if ch.isprintable() or ch == "\n" else " " for ch in (text or ""))
    return cleaned.strip()[:2000]


def _certificate_info(der: bytes) -> CertificateInfo:
    if not der:
        return CertificateInfo()
    sha = fingerprint(der)
    try:
        from cryptography import x509  # paramiko's dependency

        cert = x509.load_der_x509_certificate(der)
        before = getattr(cert, "not_valid_before_utc", None) or cert.not_valid_before
        after = getattr(cert, "not_valid_after_utc", None) or cert.not_valid_after
        return CertificateInfo(cert.subject.rfc4514_string(), cert.issuer.rfc4514_string(),
                                before.strftime("%Y-%m-%d %H:%M UTC"), after.strftime("%Y-%m-%d %H:%M UTC"), sha)
    except Exception:
        return CertificateInfo(sha256=sha)


def backend_for(host: Any, user: str, password: str, *, settings: Optional[TransferSettings] = None,
                pins: Optional[PinStore] = None, log: Optional[LogFn] = None) -> FtpBackend:
    """An :class:`FtpBackend` for an ``ftp`` / ``ftps`` :class:`HostEntry`."""

    from ..host_utils import host_option
    from .settings import default_port_for

    settings = settings or TransferSettings.for_host(host)[0]
    protocol = getattr(host, "protocol", "ftp")
    tls_mode = "none" if protocol == "ftp" else settings.tls_mode
    hostname = host_option(host, "hostname", "host") or host.alias
    port_text = host_option(host, "port") or ""
    port = int(port_text) if port_text.isdigit() else default_port_for(protocol, settings)
    return FtpBackend(hostname, port, user, password, settings=settings, tls_mode=tls_mode,
                      host_id=getattr(host, "id", ""), pins=pins, log=log)


__all__: List[str] = ["FtpBackend", "backend_for"]
