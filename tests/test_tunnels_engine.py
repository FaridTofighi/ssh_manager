"""The port-forwarding engine against an in-process SSH server (direct-tcpip, tcpip-forward) and an echo server."""

import logging
import socket
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path

import paramiko

from ssh_manager.transfer.settings import TransferSettings
from ssh_manager.tunnels import engine as E
from ssh_manager.tunnels.model import DYNAMIC, LOCAL, REMOTE, Forward

from tests.fake_sftp import FakeSftpServer

PASSWORD = "Tunnel-sentinel-pw"


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class EchoServer:
    def __init__(self) -> None:
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.stop = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        self.listener.settimeout(0.2)
        while not self.stop.is_set():
            try:
                conn, _peer = self.listener.accept()
            except (socket.timeout, OSError):
                continue
            threading.Thread(target=self._echo, args=(conn,), daemon=True).start()

    @staticmethod
    def _echo(conn) -> None:
        with conn:
            while True:
                data = conn.recv(65536)
                if not data:
                    return
                conn.sendall(data)

    def close(self) -> None:
        self.stop.set()
        self.listener.close()


def wait(predicate, seconds=10.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def roundtrip(port: int, payload: bytes = b"hello tunnel") -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=5) as conn:
        conn.sendall(payload)
        data = b""
        while len(data) < len(payload):
            chunk = conn.recv(65536)
            if not chunk:
                break
            data += chunk
        return data


def tunnel_threads() -> list:
    return [t for t in threading.enumerate() if t.name.startswith(E.THREAD_PREFIX)]


class EngineCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.server = FakeSftpServer(Path(self.tmp.name), users={"alice": PASSWORD})
        self.server.__enter__()
        self.addCleanup(self.server.__exit__)
        self.echo = EchoServer()
        self.addCleanup(self.echo.close)
        self.known = Path(self.tmp.name) / "known_hosts"
        key = self.server.host_key
        self.known.write_text(f"[127.0.0.1]:{self.server.port} {key.get_name()} {key.get_base64()}\n")
        self.password = PASSWORD
        self.connects = 0
        self.manager = E.TunnelManager(self.connect, backoff=(0.2, 1.0))
        self.addCleanup(self.manager.stop_all)

    def connect(self, host_id: str):
        from ssh_manager.transfer.sftp import SftpBackend

        self.connects += 1
        backend = SftpBackend("127.0.0.1", self.server.port, "alice", self.password, known_hosts=self.known,
                              allow_agent=False, settings=TransferSettings(timeout_read=5, timeout_connect=5))
        return backend.open_transport("Port forwarding")

    def local(self, **kw) -> Forward:
        base = dict(name="echo", kind=LOCAL, bind_port=free_port(), dest_host="127.0.0.1", dest_port=self.echo.port)
        base.update(kw)
        return Forward(**base)

    def state(self, forward, host="h1"):
        return self.manager.status(host, forward.id)

    def started(self, forward, host="h1"):
        self.manager.start(host, forward)
        self.assertTrue(wait(lambda: self.state(forward, host).state in (E.ACTIVE, E.FAILED)), self.state(forward, host))
        return self.state(forward, host)


class LocalAndSocksTests(EngineCase):
    def test_local_forward_both_ways_and_50_parallel_connections(self) -> None:
        forward = self.local()
        self.assertEqual(self.started(forward).state, E.ACTIVE)
        self.assertEqual(roundtrip(forward.bind_port, b"x" * 200_000), b"x" * 200_000)
        results = []

        def client(index):
            results.append(roundtrip(forward.bind_port, f"conn-{index}".encode()) == f"conn-{index}".encode())

        threads = [threading.Thread(target=client, args=(i,)) for i in range(50)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(20)
        self.assertEqual(results.count(True), 50)
        self.assertTrue(wait(lambda: self.state(forward).bytes_out >= 200_000))
        self.assertEqual(self.connects, 1)  # one transport for the host

    def socks(self, port, request: bytes):
        conn = socket.create_connection(("127.0.0.1", port), timeout=5)
        conn.sendall(b"\x05\x01\x00")
        self.assertEqual(conn.recv(2), b"\x05\x00")
        conn.sendall(request)
        reply = conn.recv(10)
        return conn, reply

    def test_socks5_ipv4_domain_and_unsupported_command(self) -> None:
        forward = Forward(name="socks", kind=DYNAMIC, bind_port=free_port())
        self.assertEqual(self.started(forward).state, E.ACTIVE)
        port = struct.pack(">H", self.echo.port)
        for request in (b"\x05\x01\x00\x01" + socket.inet_aton("127.0.0.1") + port,
                        b"\x05\x01\x00\x03" + bytes([len(b"localhost")]) + b"localhost" + port):
            conn, reply = self.socks(forward.bind_port, request)
            with conn:
                self.assertEqual(reply[:2], b"\x05\x00")
                conn.sendall(b"through socks")
                self.assertEqual(conn.recv(100), b"through socks")
        conn, reply = self.socks(forward.bind_port, b"\x05\x02\x00\x01" + socket.inet_aton("127.0.0.1") + port)  # BIND
        conn.close()
        self.assertEqual(reply[:2], b"\x05\x07")
        no_auth = socket.create_connection(("127.0.0.1", forward.bind_port), timeout=5)
        no_auth.sendall(b"\x05\x01\x02")  # username/password only: refused
        self.assertEqual(no_auth.recv(2), b"\x05\xff")
        no_auth.close()

    def test_port_in_use_fails_only_that_forward(self) -> None:
        busy = socket.create_server(("127.0.0.1", 0))
        self.addCleanup(busy.close)
        good = self.local()
        clash = self.local(name="clash", bind_port=busy.getsockname()[1])
        self.manager.start("h1", good)
        self.manager.start("h1", clash)
        self.assertTrue(wait(lambda: self.state(clash).state == E.FAILED and self.state(good).state == E.ACTIVE))
        self.assertEqual(self.state(clash).error, f"127.0.0.1:{busy.getsockname()[1]} is already used by another "
                                                  "program.")
        self.assertEqual(roundtrip(good.bind_port), b"hello tunnel")

    def test_non_loopback_needs_the_flag(self) -> None:
        forward = self.local(bind_host="0.0.0.0")
        self.manager.start("h1", forward)
        self.assertEqual(self.state(forward).state, E.FAILED)
        self.assertEqual(self.state(forward).error_kind, "unsafe")
        self.assertEqual(self.connects, 0)


class RemoteTests(EngineCase):
    def test_remote_forward_round_trip(self) -> None:
        forward = Forward(name="back", kind=REMOTE, bind_port=free_port(), dest_host="127.0.0.1",
                          dest_port=self.echo.port)
        self.assertEqual(self.started(forward).state, E.ACTIVE)
        self.assertEqual(roundtrip(forward.bind_port, b"from the server"), b"from the server")
        self.manager.stop("h1", forward.id)
        self.assertTrue(wait(lambda: self.server.remote_listeners[0].fileno() == -1))

    def test_server_refusing_is_explained(self) -> None:
        self.server.allow_remote_forward = False
        forward = Forward(name="back", kind=REMOTE, bind_port=free_port(), dest_host="127.0.0.1", dest_port=1)
        status = self.started(forward)
        self.assertEqual((status.state, status.error_kind), (E.FAILED, "refused"))
        self.assertIn("AllowTcpForwarding", status.error)


class LifecycleTests(EngineCase):
    def test_a_dropped_transport_reconnects_and_recovers(self) -> None:
        events = []
        self.manager.on_event.append(lambda host, event: events.append(event))
        forward = self.local()
        self.assertEqual(self.started(forward).state, E.ACTIVE)
        states = []
        self.manager.on_status.append(lambda host, fid, status: states.append(status.state))
        for transport in list(self.server._transports):
            transport.close()  # the server goes away under the tunnel
        self.assertTrue(wait(lambda: E.RECONNECTING in states, 15))
        self.assertTrue(wait(lambda: self.state(forward).state == E.ACTIVE and self.connects >= 2, 15))
        self.assertEqual(roundtrip(forward.bind_port), b"hello tunnel")
        self.assertEqual(events[:2], ["dropped", "recovered"])
        self.assertEqual(E.state_label(E.TunnelStatus(state=E.RECONNECTING, attempt=2)), "Reconnecting (attempt 2)")

    def test_stop_all_leaves_no_threads_or_sockets(self) -> None:
        forwards = [self.local(), Forward(name="s", kind=DYNAMIC, bind_port=free_port()),
                    Forward(name="r", kind=REMOTE, bind_port=free_port(), dest_host="127.0.0.1",
                            dest_port=self.echo.port)]
        for forward in forwards:
            self.manager.start("h1", forward)
        self.manager.start("h2", self.local())
        self.assertTrue(wait(lambda: self.manager.active_count() == 4))
        roundtrip(forwards[0].bind_port)
        self.manager.stop_all()
        self.assertTrue(wait(lambda: not tunnel_threads(), 5), tunnel_threads())
        self.assertEqual(self.manager.running_count(), 0)
        for forward in forwards[:2]:
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", forward.bind_port))  # free again

    def test_stop_one_keeps_the_others(self) -> None:
        first, second = self.local(), self.local()
        self.manager.start("h1", first)
        self.manager.start("h1", second)
        self.assertTrue(wait(lambda: self.manager.active_count() == 2))
        self.manager.stop("h1", first.id)
        self.assertEqual(self.state(first).state, E.STOPPED)
        self.assertEqual(roundtrip(second.bind_port), b"hello tunnel")
        with self.assertRaises(OSError):
            roundtrip(first.bind_port)


class CredentialsTests(EngineCase):
    def test_changed_host_key_fails_and_never_retries(self) -> None:
        self.known.write_text(f"[127.0.0.1]:{self.server.port} ecdsa-sha2-nistp256 "
                              f"{paramiko.ECDSAKey.generate().get_base64()}\n")
        forward = self.local()
        status = self.started(forward)
        self.assertEqual((status.state, status.error_kind), (E.FAILED, "host_key_changed"))
        time.sleep(0.5)
        self.assertEqual(self.connects, 1)

    def test_wrong_password_fails_without_a_retry_storm_and_no_secret_leaks(self) -> None:
        self.password = "wrong-" + PASSWORD
        forward = self.local()
        with self.assertLogs(level=logging.DEBUG) as logs:
            logging.getLogger("ssh_manager").debug("start")
            status = self.started(forward)
            time.sleep(0.5)
        self.assertEqual((status.state, status.error_kind), (E.FAILED, "auth"))
        self.assertIn("Needs password", status.error)
        self.assertEqual(self.connects, 1)
        self.assertNotIn(PASSWORD, "\n".join(logs.output) + status.error)


if __name__ == "__main__":
    unittest.main()
