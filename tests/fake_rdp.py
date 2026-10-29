"""A fake RDP server for the negotiation probe: scripted X.224 replies and an optional TLS handshake.

Each connection reads one Connection Request, records the requested
protocols, then answers with the next scripted reply (``bytes``, ``None`` =
close without answering, ``"hang"`` = never answer). When the reply selects
TLS / NLA and a certificate is configured, the server completes a TLS
handshake so the probe can read the certificate. Nothing real: 127.0.0.1 only.
"""

from __future__ import annotations

import socket
import ssl
import struct
import threading
import time
from pathlib import Path
from typing import List, Optional, Union

Reply = Union[bytes, None, str]


def confirm(neg: Optional[bytes] = None) -> bytes:
    """TPKT + X.224 Connection Confirm, optionally with negotiation data."""

    body = neg or b""
    x224 = struct.pack(">BBHHB", 6 + len(body), 0xD0, 0, 0x1234, 0) + body
    return struct.pack(">BBH", 3, 0, 4 + len(x224)) + x224


def neg_rsp(selected: int) -> bytes:
    return confirm(struct.pack("<BBHI", 0x02, 0x1F, 8, selected))


def neg_failure(code: int) -> bytes:
    return confirm(struct.pack("<BBHI", 0x03, 0, 8, code))


class FakeRdpServer:
    def __init__(self, replies: List[Reply], certificate: Optional[tuple] = None) -> None:
        self.replies = list(replies)
        self.requests: List[bytes] = []
        self.context: Optional[ssl.SSLContext] = None
        if certificate is not None:
            self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            self.context.load_cert_chain(*(str(p) for p in certificate))
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        self.listener.settimeout(0.2)
        while not self._stop.is_set():
            try:
                conn, _addr = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        with conn:
            conn.settimeout(5)
            try:
                header = conn.recv(4)
                length = struct.unpack(">H", header[2:4])[0] if len(header) == 4 else 4
                data = header
                while len(data) < length:
                    chunk = conn.recv(length - len(data))
                    if not chunk:
                        break
                    data += chunk
            except OSError:
                return
            self.requests.append(data)
            reply = self.replies.pop(0) if self.replies else None
            if reply is None:
                return
            if reply == "hang":
                time.sleep(3)
                return
            assert isinstance(reply, bytes)
            try:
                conn.sendall(reply)
                selected = struct.unpack("<I", reply[-4:])[0] if len(reply) == 19 and reply[11] == 0x02 else 0
                if selected and self.context is not None:
                    with self.context.wrap_socket(conn, server_side=True) as tls:
                        tls.recv(1)  # the probe closes after reading the certificate
            except (OSError, ssl.SSLError):
                pass

    def requested_protocols(self, index: int = 0) -> int:
        return struct.unpack("<I", self.requests[index][-4:])[0]

    def close(self) -> None:
        self._stop.set()
        self.listener.close()


def write_certificate(directory: Path) -> tuple:
    from tests.fake_ftp import make_certificate

    return make_certificate(directory, "win-01.acme.test")
