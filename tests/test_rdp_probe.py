"""The RDP negotiation probe: every outcome, malformed and hostile replies, timeouts (fake server, 127.0.0.1)."""

import os
import socket
import struct
import tempfile
import unittest
from pathlib import Path

from ssh_manager.rdp import probe as rdp_probe
from ssh_manager.rdp.probe import (
    PROTOCOL_HYBRID, PROTOCOL_HYBRID_EX, PROTOCOL_RDP, PROTOCOL_SSL, connection_request, parse_connection_confirm,
    probe, security_name,
)
from tests.fake_rdp import FakeRdpServer, confirm, neg_failure, neg_rsp, write_certificate

INTEGRATION = unittest.skipIf(os.environ.get("SSHM_SKIP_INTEGRATION") == "1", "integration tests skipped")


class PacketTests(unittest.TestCase):
    def test_connection_request_layout(self) -> None:
        data = connection_request(PROTOCOL_SSL | PROTOCOL_HYBRID | PROTOCOL_HYBRID_EX)
        self.assertEqual(data[:4], struct.pack(">BBH", 3, 0, 19))  # TPKT, length 19
        self.assertEqual(data[4:11], bytes([14, 0xE0, 0, 0, 0, 0, 0]))  # LI, CR, DST-REF, SRC-REF, class
        self.assertEqual(data[11:], struct.pack("<BBHI", 1, 0, 8, 0x0B))
        cookie = connection_request(PROTOCOL_SSL, cookie="probe\r\nX")
        self.assertIn(b"Cookie: mstshash=probeX\r\n", cookie)  # the cookie cannot inject lines
        self.assertEqual(struct.unpack(">H", cookie[2:4])[0], len(cookie))

    def test_parse_valid_replies(self) -> None:
        result = parse_connection_confirm(neg_rsp(PROTOCOL_HYBRID))
        self.assertTrue(result.ok)
        self.assertEqual(result.selected, PROTOCOL_HYBRID)
        legacy = parse_connection_confirm(confirm())
        self.assertTrue(legacy.ok and legacy.legacy)
        failed = parse_connection_confirm(neg_failure(5))
        self.assertEqual((failed.ok, failed.failure_name), (False, "HYBRID_REQUIRED_BY_SERVER"))
        unknown = parse_connection_confirm(neg_failure(0x99))
        self.assertEqual(unknown.failure_name, "UNKNOWN_FAILURE_0x99")

    def test_parse_malformed_replies(self) -> None:
        good = neg_rsp(PROTOCOL_SSL)
        hostile = [
            b"", b"\x03", good[:10], b"HTTP/1.1 400 Bad Request\r\n\r\n",
            b"\x02" + good[1:],  # TPKT version 2
            good[:3] + bytes([good[3] + 1]) + good[4:],  # length disagrees
            good[:5] + b"\xE0" + good[6:],  # not a Connection Confirm
            good[:4] + bytes([good[4] + 2]) + good[5:],  # wrong X.224 length indicator
            good[:-1],  # truncated negotiation data (lengths now disagree)
            confirm(struct.pack("<BBHI", 0x02, 0, 9, 1)),  # negotiation length must be 8
            confirm(struct.pack("<BBHI", 0x07, 0, 8, 1)),  # unknown type
            confirm(struct.pack("<BBHI", 0x02, 0, 8, 0x80000000)),  # unknown protocol bits
            confirm(b"\x02\x00"),  # partial negotiation data
            struct.pack(">BBH", 3, 0, 65535) + b"\x00" * 100,  # huge declared length
        ]
        for data in hostile:
            result = parse_connection_confirm(data)
            self.assertFalse(result.ok, data)
            self.assertTrue(result.error, data)

    def test_security_names(self) -> None:
        self.assertEqual(security_name(PROTOCOL_HYBRID_EX)[0], "nla_ext")
        self.assertEqual(security_name(PROTOCOL_HYBRID | PROTOCOL_SSL)[0], "nla")
        self.assertEqual(security_name(PROTOCOL_SSL)[0], "tls")
        self.assertEqual(security_name(PROTOCOL_RDP)[0], "rdp")


@INTEGRATION
class ProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cert = write_certificate(Path(self.tmp.name))

    def server(self, replies, cert=True):
        server = FakeRdpServer(replies, self.cert if cert else None)
        self.addCleanup(server.close)
        return server

    def test_nla_with_certificate_and_pins(self) -> None:
        server = self.server([neg_rsp(PROTOCOL_HYBRID), neg_rsp(PROTOCOL_HYBRID), neg_rsp(PROTOCOL_HYBRID)])
        result = probe("127.0.0.1", server.port, timeout=5)
        self.assertTrue(result.reachable)
        self.assertEqual(result.security, "nla")
        self.assertEqual(server.requested_protocols(), PROTOCOL_SSL | PROTOCOL_HYBRID | PROTOCOL_HYBRID_EX)
        self.assertIn("win-01.acme.test", result.certificate.subject)
        self.assertEqual(len(result.certificate.sha256), 95)
        self.assertEqual(result.pin_status, "none")
        self.assertTrue(result.tls_version.startswith("TLS"))
        pinned = probe("127.0.0.1", server.port, timeout=5, pinned=(result.certificate.sha256,))
        self.assertEqual(pinned.pin_status, "match")
        other = probe("127.0.0.1", server.port, timeout=5, pinned=("AB" * 32,))
        self.assertEqual(other.pin_status, "mismatch")
        self.assertIn("differs", "\n".join(other.lines()))

    def test_tls_and_forced_security_hint(self) -> None:
        server = self.server([neg_rsp(PROTOCOL_SSL), neg_rsp(PROTOCOL_HYBRID_EX)])
        self.assertEqual(probe("127.0.0.1", server.port, timeout=5).security, "tls")
        nla = probe("127.0.0.1", server.port, timeout=5, configured_security="rdp")
        self.assertEqual(nla.security, "nla_ext")
        self.assertTrue(any("forces" in note for note in nla.notes))

    def test_ssl_not_allowed_then_standard_rdp(self) -> None:
        server = self.server([neg_failure(2), neg_rsp(PROTOCOL_RDP)], cert=False)
        result = probe("127.0.0.1", server.port, timeout=5, configured_security="nla")
        self.assertEqual(result.security, "rdp")
        self.assertEqual(result.failure, "SSL_NOT_ALLOWED_BY_SERVER")
        self.assertEqual(server.requested_protocols(1), PROTOCOL_RDP)
        self.assertIsNone(result.certificate)
        self.assertTrue(any("weak" in note for note in result.notes))

    def test_every_failure_code(self) -> None:
        for code, name in ((1, "SSL_REQUIRED_BY_SERVER"), (3, "SSL_CERT_NOT_ON_SERVER"), (4, "INCONSISTENT_FLAGS"),
                           (5, "HYBRID_REQUIRED_BY_SERVER"), (6, "SSL_WITH_USER_AUTH_REQUIRED_BY_SERVER")):
            server = self.server([neg_failure(code)], cert=False)
            result = probe("127.0.0.1", server.port, timeout=5)
            self.assertEqual(result.failure, name)
            self.assertTrue(result.error)
            self.assertIsNone(result.certificate)

    def test_legacy_server_without_negotiation(self) -> None:
        server = self.server([confirm()], cert=False)
        result = probe("127.0.0.1", server.port, timeout=5)
        self.assertEqual(result.security, "rdp")
        self.assertTrue(any("old server" in note for note in result.notes))

    def test_server_hangs_up_then_answers_without_hybrid_ex(self) -> None:
        server = self.server([None, neg_rsp(PROTOCOL_SSL)])
        result = probe("127.0.0.1", server.port, timeout=5)
        self.assertEqual(result.security, "tls")
        self.assertEqual(server.requested_protocols(1), PROTOCOL_SSL | PROTOCOL_HYBRID)

    def test_garbage_and_oversized_replies(self) -> None:
        for reply in (b"SSH-2.0-OpenSSH_9.6\r\n", b"\x03\x00\xff\xff" + b"A" * 5000, b"\x03\x00\x00\x13" + b"\x00" * 15):
            server = self.server([reply, reply], cert=False)
            result = probe("127.0.0.1", server.port, timeout=3)
            self.assertTrue(result.reachable)
            self.assertFalse(result.security)
            self.assertTrue(result.error)

    def test_timeouts_and_unreachable(self) -> None:
        server = self.server(["hang"], cert=False)
        slow = probe("127.0.0.1", server.port, timeout=0.5)
        self.assertTrue(slow.reachable)
        self.assertIn("timed out", slow.error)
        free = socket.socket()
        free.bind(("127.0.0.1", 0))
        port = free.getsockname()[1]
        free.close()
        closed = probe("127.0.0.1", port, timeout=1)
        self.assertFalse(closed.reachable)
        self.assertTrue(closed.error)
        self.assertIn("Not reachable", "\n".join(closed.lines()))

    def test_never_sends_more_than_the_request(self) -> None:
        server = self.server([neg_rsp(PROTOCOL_HYBRID)])
        probe("127.0.0.1", server.port, timeout=5)
        self.assertEqual(server.requests[0], connection_request(PROTOCOL_SSL | PROTOCOL_HYBRID | PROTOCOL_HYBRID_EX))

    def test_injected_connect(self) -> None:
        def refuse(_host, _port, _timeout):
            raise ConnectionRefusedError(111, "Connection refused")

        result = rdp_probe.probe("win-01.acme.test", 3389, connect=refuse)
        self.assertFalse(result.reachable)
        self.assertEqual(result.error, "Connection refused")


if __name__ == "__main__":
    unittest.main()
