"""A small, real FTP / FTPS server for integration tests (stdlib only; no pyftpdlib needed).

It serves a temporary directory over a real socket and supports what the
backend uses: USER/PASS (+ anonymous), AUTH TLS, PBSZ/PROT, implicit TLS,
FEAT/OPTS/SYST, PWD/CWD/CDUP, TYPE, PASV/EPSV, PORT/EPRT, LIST/MLSD/MLST,
RETR/STOR/APPE with REST, SIZE/MDTM/MFMT, DELE/MKD/RMD, RNFR/RNTO,
SITE CHMOD, NOOP, ABOR, QUIT.

Test knobs: ``require_session_reuse`` (refuses a TLS data connection that does
not resume the control session, like vsftpd ``require_ssl_reuse=YES``),
``pasv_ip`` (lie in PASV replies), ``mlsd`` (advertise MLST or not),
``drop_after`` (close the control connection after N commands, like a 421).
Generated data only; synthetic names.
"""

from __future__ import annotations

import datetime
import ipaddress
import os
import socket
import socketserver
import ssl
import stat
import tempfile
import threading
import time
from pathlib import Path
from typing import Dict, Optional


def make_certificate(directory: Path, common_name: str = "localhost") -> tuple:
    """A self-signed certificate for 127.0.0.1 / localhost: ``(cert_path, key_path)``."""

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name),
                      x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Acme Test")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=30))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost"),
                                                        x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
                           critical=False)
            .sign(key, hashes.SHA256()))
    cert_path, key_path = directory / f"{common_name}.crt", directory / f"{common_name}.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    return cert_path, key_path


class _Handler(socketserver.StreamRequestHandler):
    server: "FakeFtpServer"

    def setup(self) -> None:
        server = self.server
        if server.tls_mode == "implicit":
            self.request = server.context.wrap_socket(self.request, server_side=True)
        super().setup()
        self.user = ""
        self.logged_in = False
        self.cwd = "/"
        self.prot_p = False
        self.binary = True
        self.rest = 0
        self.rename_from: Optional[str] = None
        self.data_listener: Optional[socket.socket] = None
        self.data_address: Optional[tuple] = None
        self.commands = 0

    # ----------------------------------------------------------- plumbing
    def send(self, line: str) -> None:
        self.wfile.write((line + "\r\n").encode("utf-8", "surrogateescape"))
        self.wfile.flush()

    def real(self, path: str) -> Path:
        virtual = os.path.normpath(os.path.join(self.cwd, path or "."))
        if not virtual.startswith("/"):
            virtual = "/" + virtual
        target = (self.server.root / virtual.lstrip("/")).resolve()
        if not str(target).startswith(str(self.server.root.resolve())):
            raise PermissionError("outside the root")
        return target

    def open_data(self) -> Optional[socket.socket]:
        if self.data_listener is not None:
            self.data_listener.settimeout(5)
            conn, _addr = self.data_listener.accept()
            self.data_listener.close()
            self.data_listener = None
        elif self.data_address is not None:
            conn = socket.create_connection(self.data_address, timeout=5)
            self.data_address = None
        else:
            self.send("425 Use PASV or PORT first.")
            return None
        if self.prot_p:
            conn = self.server.context.wrap_socket(conn, server_side=True)
            if self.server.require_session_reuse and not conn.session_reused:
                conn.close()
                self.server.reuse_refusals += 1
                self.send("522 SSL connection failed: session reuse required")
                return None
        return conn

    def handle(self) -> None:
        self.send(f"220 {self.server.banner}")
        while True:
            try:
                raw = self.rfile.readline()
            except (OSError, ssl.SSLError):
                return
            if not raw:
                return
            line = raw.decode("utf-8", "surrogateescape").rstrip("\r\n")
            self.server.log.append(line if not line.upper().startswith("PASS ") else "PASS ****")
            self.commands += 1
            if self.server.drop_after and self.commands > self.server.drop_after:
                self.server.drop_after = 0  # once
                self.send("421 Too many connections, closing.")
                return
            verb, _, arg = line.partition(" ")
            verb = verb.upper()
            try:
                if not self.dispatch(verb, arg):
                    return
            except PermissionError:
                self.send("550 Permission denied.")
            except FileNotFoundError:
                self.send("550 No such file or directory.")
            except FileExistsError:
                self.send("550 File exists.")
            except OSError as exc:
                self.send(f"451 Local error: {exc.strerror}")

    # ----------------------------------------------------------- commands
    def dispatch(self, verb: str, arg: str) -> bool:  # noqa: C901 - a protocol table
        server = self.server
        open_verbs = {"USER", "PASS", "AUTH", "FEAT", "QUIT", "PBSZ", "PROT", "SYST", "NOOP", "OPTS"}
        if not self.logged_in and verb not in open_verbs:
            self.send("530 Please log in.")
            return True
        if verb == "AUTH":
            if arg.upper() not in ("TLS", "SSL") or server.context is None:
                self.send("504 AUTH not supported.")
                return True
            self.send("234 Proceed with negotiation.")
            self.request = server.context.wrap_socket(self.request, server_side=True)
            self.rfile = self.request.makefile("rb")
            self.wfile = self.request.makefile("wb")
        elif verb == "PBSZ":
            self.send("200 PBSZ=0")
        elif verb == "PROT":
            self.prot_p = arg.upper() == "P"
            self.send("200 Protection level set.")
        elif verb == "USER":
            self.user = arg
            self.send("331 Password required.")
        elif verb == "PASS":
            ok = server.users.get(self.user) == arg or (self.user == "anonymous" and server.anonymous)
            self.logged_in = ok
            self.send("230 Login successful." if ok else "530 Login incorrect.")
        elif verb == "FEAT":
            self.send("211-Features:")
            for feature in (["MLST type*;size*;modify*;perm*;unix.mode*;"] if server.mlsd else []) + \
                    ["REST STREAM", "UTF8", "SIZE", "MDTM", "MFMT", "PASV", "EPSV"] + \
                    (["AUTH TLS", "PBSZ", "PROT"] if server.context else []):
                self.send(f" {feature}")
            self.send("211 End")
        elif verb == "OPTS":
            self.send("200 OK")
        elif verb == "SYST":
            self.send("215 UNIX Type: L8")
        elif verb == "NOOP":
            self.send("200 NOOP ok.")
        elif verb == "QUIT":
            self.send("221 Goodbye.")
            return False
        elif verb == "PWD":
            self.send(f'257 "{self.cwd}" is the current directory')
        elif verb in ("CWD", "CDUP"):
            target = self.real(".." if verb == "CDUP" else arg)
            if not target.is_dir():
                self.send("550 Failed to change directory.")
                return True
            relative = target.relative_to(self.server.root.resolve()).as_posix()
            self.cwd = "/" if relative == "." else "/" + relative
            self.send("250 Directory changed.")
        elif verb == "TYPE":
            self.binary = arg.upper().startswith("I")
            self.send("200 Type set.")
        elif verb == "PASV":
            listener = socket.create_server(("127.0.0.1", 0), backlog=1)
            self.data_listener = listener
            port = listener.getsockname()[1]
            ip = (server.pasv_ip or "127.0.0.1").replace(".", ",")
            self.send(f"227 Entering Passive Mode ({ip},{port >> 8},{port & 255}).")
        elif verb == "EPSV":
            listener = socket.create_server(("127.0.0.1", 0), backlog=1)
            self.data_listener = listener
            self.send(f"229 Entering Extended Passive Mode (|||{listener.getsockname()[1]}|)")
        elif verb == "PORT":
            parts = arg.split(",")
            self.data_address = (".".join(parts[:4]), int(parts[4]) * 256 + int(parts[5]))
            self.send("200 PORT command successful.")
        elif verb == "EPRT":
            _sep, _proto, host, port, _end = arg.split(arg[0])
            self.data_address = (host, int(port))
            self.send("200 EPRT command successful.")
        elif verb == "REST":
            self.rest = int(arg)
            self.send(f"350 Restarting at {self.rest}.")
        elif verb in ("LIST", "MLSD", "NLST"):
            show_all = arg.startswith("-a")
            path = arg[2:].strip() if show_all else arg
            target = self.real(path)
            if not target.is_dir():
                self.send("550 No such directory.")
                return True
            if server.empty_list_550 and not any(target.iterdir()):
                self.send("550 No files found.")
                return True
            self.send("150 Here comes the directory listing.")
            conn = self.open_data()
            if conn is None:
                return True
            with conn:
                lines = list(server.extra_list_lines) if verb == "LIST" else []
                for child in sorted(target.iterdir()):
                    if child.name.startswith(".") and verb == "LIST" and not show_all:
                        continue
                    lines.append(self.mlsd_line(child) if verb == "MLSD" else self.list_line(child))
                conn.sendall("".join(line + "\r\n" for line in lines).encode("utf-8", "surrogateescape"))
                _close_notify(conn)
            self.send("226 Directory send OK.")
        elif verb == "MLST":
            target = self.real(arg)
            if not target.exists():
                self.send("550 No such file.")
                return True
            self.send("250-Listing")
            self.send(" " + self.mlsd_line(target))
            self.send("250 End")
        elif verb == "RETR":
            target = self.real(arg)
            if not target.is_file():
                self.send("550 No such file.")
                return True
            self.send("150 Opening data connection.")
            conn = self.open_data()
            if conn is None:
                return True
            with conn, open(target, "rb") as handle:
                handle.seek(self.rest)
                self.rest = 0
                while True:
                    chunk = handle.read(65536)
                    if not chunk:
                        break
                    conn.sendall(chunk)
                _close_notify(conn)
            if not server.no_226_after_retr:
                self.send("226 Transfer complete.")
        elif verb in ("STOR", "APPE"):
            target = self.real(arg)
            if not target.parent.is_dir():
                self.send("553 Could not create file.")
                return True
            self.send("150 Ok to send data.")
            conn = self.open_data()
            if conn is None:
                return True
            mode = "ab" if verb == "APPE" else ("r+b" if self.rest and target.exists() else "wb")
            with conn, open(target, mode) as handle:
                if mode == "r+b":
                    handle.seek(self.rest)
                    handle.truncate()
                self.rest = 0
                while True:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    handle.write(chunk)
                    if server.disk_limit and handle.tell() > server.disk_limit:
                        break
            if server.disk_limit and target.stat().st_size > server.disk_limit:
                self.send("452 Insufficient storage space.")
            else:
                self.send("226 Transfer complete.")
        elif verb == "SIZE":
            target = self.real(arg)
            if not target.is_file():
                self.send("550 Could not get file size.")
            else:
                self.send(f"213 {target.stat().st_size}")
        elif verb == "MDTM":
            target = self.real(arg)
            if not target.exists():
                self.send("550 Could not get modification time.")
            else:
                self.send("213 " + time.strftime("%Y%m%d%H%M%S", time.gmtime(target.stat().st_mtime)))
        elif verb == "MFMT":
            stamp, _, path = arg.partition(" ")
            target = self.real(path)
            moment = time.mktime(time.strptime(stamp, "%Y%m%d%H%M%S")) - time.timezone
            os.utime(target, (moment, moment))
            self.send(f"213 Modify={stamp}; {path}")
        elif verb == "DELE":
            self.real(arg).unlink()
            self.send("250 Delete operation successful.")
        elif verb == "MKD":
            target = self.real(arg)
            target.mkdir()
            self.send(f'257 "{arg}" created')
        elif verb == "RMD":
            self.real(arg).rmdir()
            self.send("250 Remove directory operation successful.")
        elif verb == "RNFR":
            if not self.real(arg).exists():
                self.send("550 RNFR command failed.")
                return True
            self.rename_from = arg
            self.send("350 Ready for RNTO.")
        elif verb == "RNTO":
            if self.rename_from is None:
                self.send("503 RNFR required first.")
                return True
            self.real(self.rename_from).rename(self.real(arg))
            self.rename_from = None
            self.send("250 Rename successful.")
        elif verb == "SITE":
            sub, _, rest = arg.partition(" ")
            if sub.upper() != "CHMOD" or not server.chmod:
                self.send("500 Unknown SITE command.")
                return True
            mode, _, path = rest.partition(" ")
            os.chmod(self.real(path), int(mode, 8))
            self.send("200 SITE CHMOD command ok.")
        elif verb == "ABOR":
            self.send("226 Abort successful.")
        else:
            self.send("502 Command not implemented.")
        return True

    @staticmethod
    def list_line(path: Path) -> str:
        info = path.lstat()
        kind = "l" if stat.S_ISLNK(info.st_mode) else ("d" if stat.S_ISDIR(info.st_mode) else "-")
        perms = stat.filemode(info.st_mode)[1:]
        when = time.strftime("%b %d %H:%M", time.gmtime(info.st_mtime))
        name = path.name + (f" -> {os.readlink(path)}" if kind == "l" else "")
        return f"{kind}{perms} 1 alice staff {info.st_size:>8} {when} {name}"

    @staticmethod
    def mlsd_line(path: Path) -> str:
        info = path.lstat()
        kind = "dir" if stat.S_ISDIR(info.st_mode) else "file"
        modify = time.strftime("%Y%m%d%H%M%S", time.gmtime(info.st_mtime))
        return (f"type={kind};size={info.st_size};modify={modify};perm=rw;"
                f"unix.mode={stat.S_IMODE(info.st_mode):04o}; {path.name}")


def _close_notify(conn: socket.socket) -> None:
    """Send TLS close_notify; a client that just closes is fine (real servers tolerate it too)."""

    if isinstance(conn, ssl.SSLSocket):
        try:
            conn.unwrap()
        except (OSError, ssl.SSLError):
            pass


class FakeFtpServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, root: Path, *, users: Optional[Dict[str, str]] = None, anonymous: bool = False,
                 tls_mode: str = "none", certificate: Optional[tuple] = None, require_session_reuse: bool = False,
                 pasv_ip: str = "", mlsd: bool = True, banner: str = "Acme test FTP ready") -> None:
        self.root = Path(root)
        self.users = users or {"alice": "s3cret pass"}
        self.anonymous = anonymous
        self.tls_mode = tls_mode
        self.context: Optional[ssl.SSLContext] = None
        if certificate is not None:
            self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            self.context.load_cert_chain(*certificate)
        self.require_session_reuse = require_session_reuse
        self.reuse_refusals = 0
        self.pasv_ip = pasv_ip
        self.mlsd = mlsd
        self.chmod = True
        self.banner = banner
        self.drop_after = 0
        self.disk_limit = 0
        self.no_226_after_retr = False
        self.empty_list_550 = False
        self.extra_list_lines: list = []
        self.log: list = []
        super().__init__(("127.0.0.1", 0), _Handler)
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)

    def handle_error(self, request, client_address) -> None:
        pass  # a client that rejects the certificate or hangs up is part of the tests

    @property
    def port(self) -> int:
        return self.server_address[1]

    def __enter__(self) -> "FakeFtpServer":
        self.thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self.shutdown()
        self.server_close()


def temp_root() -> tempfile.TemporaryDirectory:
    return tempfile.TemporaryDirectory(prefix="fake-ftp-")
