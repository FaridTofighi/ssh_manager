"""Phase F1: file-transfer settings, listing parsers, FTP/FTPS and SFTP backends.

Integration tests run real sockets against the fake servers in
``tests/fake_ftp.py`` and ``tests/fake_sftp.py`` (synthetic data, 127.0.0.1).
"""

import calendar
import ftplib
import os
import socket
import ssl
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ssh_manager.transfer.backend import (
    AuthError, CertificateChanged, Disconnected, DiskFull, EntryKind, HostKeyChanged, NotFound, PermissionDenied,
    ProtocolError, Timeout, UnknownHostKey, UntrustedCertificate, Unsupported, check_remote_path, redact,
)
from ssh_manager.transfer.listing import parse_list_line, parse_mlsd_line
from ssh_manager.transfer.pins import PinStore, normalize_fingerprint
from ssh_manager.transfer.settings import TransferSettings, default_port_for

PASSWORD = "s3cret pass"
INTEGRATION = unittest.skipIf(os.environ.get("SSHM_SKIP_INTEGRATION") == "1",
                              "integration tests (real sockets on 127.0.0.1) disabled")
NOW = calendar.timegm((2024, 3, 10, 12, 0, 0))


# ------------------------------------------------------------------ settings
class SettingsTests(unittest.TestCase):
    def test_defaults_round_trip(self) -> None:
        settings, warnings = TransferSettings.from_json(None)
        self.assertEqual(warnings, [])
        data = settings.to_json()
        self.assertEqual(data["v"], 1)
        again, warnings = TransferSettings.from_json(data)
        self.assertEqual((again, warnings), (settings, []))
        self.assertTrue(settings.passive and settings.upload_atomic and not settings.trust_pasv_address)
        self.assertEqual((settings.tls_verify, settings.max_connections, settings.timeout_connect), ("system", 3, 15))

    def test_unknown_keys_survive_and_bad_values_fall_back(self) -> None:
        settings, warnings = TransferSettings.from_json({
            "v": 7, "future_flag": {"x": 1}, "passive": "yes", "max_connections": 99, "timeout_read": "45",
            "tls_verify": "maybe", "encoding": "no-such-codec", "active_port_range": "80-90",
            "ascii_extensions": "TXT, .md ,", "keepalive_s": True,
        })
        self.assertEqual(settings.extra, {"future_flag": {"x": 1}})
        self.assertEqual(settings.to_json()["future_flag"], {"x": 1})
        self.assertTrue(settings.passive)  # "yes" is not a bool: default
        self.assertEqual(settings.max_connections, 3)
        self.assertEqual(settings.timeout_read, 45)
        self.assertEqual((settings.tls_verify, settings.encoding, settings.active_port_range), ("system", "auto", ""))
        self.assertEqual(settings.ascii_extensions, ["txt", "md"])
        self.assertEqual(settings.keepalive_s, 30)
        self.assertGreaterEqual(len(warnings), 6)
        self.assertEqual(TransferSettings.from_json("garbage")[1][0][:30], "The file-transfer settings wer")

    def test_helpers(self) -> None:
        settings = TransferSettings(transfer_mode="auto", encoding="latin-1", active_port_range="50000-50010")
        self.assertTrue(settings.is_ascii_name("notes.TXT"))
        self.assertFalse(settings.is_ascii_name("photo.jpg"))
        self.assertEqual((settings.codec(), settings.port_range()), ("latin-1", (50000, 50010)))
        self.assertEqual(TransferSettings(tls_verify="custom_ca").problems(),
                         ["Choose the CA certificate file, or use the system certificates."])
        self.assertEqual(default_port_for("ftps", TransferSettings(tls_mode="implicit")), 990)
        self.assertEqual((default_port_for("ftps"), default_port_for("sftp")), (21, 22))


# ------------------------------------------------------------------ listings
class ListingTests(unittest.TestCase):
    def entry(self, line):
        return parse_list_line(line, "/pub", now=NOW)

    def test_unix(self) -> None:
        folder = self.entry("drwxr-xr-x    2 alice  staff     4096 Mar  1 10:15 my folder")
        self.assertEqual((folder.name, folder.kind, folder.size, folder.mode, folder.owner, folder.group),
                         ("my folder", EntryKind.DIR, 4096, 0o755, "alice", "staff"))
        self.assertEqual(folder.mtime, calendar.timegm((2024, 3, 1, 10, 15, 0)))
        link = self.entry("lrwxrwxrwx 1 root root 11 Dec 31 23:59 to target -> /etc/the target")
        self.assertEqual((link.kind, link.name, link.link_target), (EntryKind.SYMLINK, "to target", "/etc/the target"))
        self.assertEqual(link.mtime, calendar.timegm((2023, 12, 31, 23, 59, 0)))  # in the future -> last year
        no_group = self.entry("-rw-r--r-- 1 ftp 1234 Jan 05  2023 nogroup.txt")
        self.assertEqual((no_group.size, no_group.owner, no_group.mtime),
                         (1234, "ftp", calendar.timegm((2023, 1, 5, 0, 0, 0))))
        setuid = self.entry("-rwsr-xr-T 1 a b 1 Feb 2 09:00 suid")
        self.assertEqual(setuid.mode, 0o5754)
        device = self.entry("crw-rw---- 1 root tty 4, 1 Feb 2 09:00 tty1")
        self.assertEqual(device.kind, EntryKind.OTHER)

    def test_dos_netware_vms(self) -> None:
        folder = self.entry("01-02-24  03:04PM       <DIR>          Windows Dir")
        self.assertEqual((folder.kind, folder.name, folder.mtime),
                         (EntryKind.DIR, "Windows Dir", calendar.timegm((2024, 1, 2, 15, 4, 0))))
        old = self.entry("12-31-99  12:05AM              1234 old file.txt")
        self.assertEqual((old.size, old.mtime), (1234, calendar.timegm((1999, 12, 31, 0, 5, 0))))
        netware = self.entry("d [RWCEAFMS] admin 512 Jun 12 12:00 netware dir")
        self.assertEqual((netware.kind, netware.name), (EntryKind.DIR, "netware dir"))
        vms = self.entry("DATA.DIR;1        1/3  12-MAR-2024 10:11:12 [OWNER] (RWE,RWE,,)")
        self.assertEqual((vms.kind, vms.name), (EntryKind.DIR, "DATA"))

    def test_skipped_and_unparseable_lines(self) -> None:
        self.assertIsNone(self.entry("total 12"))
        self.assertIsNone(self.entry("drwxr-xr-x 2 a b 4096 Mar 1 10:15 ."))
        self.assertIsNone(self.entry(""))
        garbage = self.entry("this is not a listing line")
        self.assertEqual((garbage.kind, garbage.name), (EntryKind.OTHER, "this is not a listing line"))

    def test_server_time_offset(self) -> None:
        shifted = parse_list_line("-rw-r--r-- 1 a b 1 Jan 05  2023 f", "/", now=NOW, offset_min=60)
        self.assertEqual(shifted.mtime, calendar.timegm((2023, 1, 5, 0, 0, 0)) - 3600)

    def test_mlsd(self) -> None:
        entry = parse_mlsd_line("type=file;size=10;modify=20240102030405.123;perm=adfr;unix.mode=0644;"
                                "unix.owner=alice; name; with spaces", "/d")
        self.assertEqual((entry.name, entry.path, entry.size, entry.mode, entry.owner),
                         ("name; with spaces", "/d/name; with spaces", 10, 0o644, "alice"))
        self.assertEqual(entry.mtime, calendar.timegm((2024, 1, 2, 3, 4, 5)))
        self.assertIsNone(parse_mlsd_line("type=cdir;modify=20240102030405; .", "/"))
        self.assertIsNone(parse_mlsd_line("type=pdir; ..", "/"))
        self.assertEqual(parse_mlsd_line("type=OS.unix=slink:/Data/X; lnk", "/").link_target, "/Data/X")
        self.assertEqual(parse_mlsd_line("type=dir;modify=bogus; d", "/").mtime, None)


# ------------------------------------------------------------------ hygiene
class HygieneTests(unittest.TestCase):
    def test_paths_with_line_breaks_are_refused(self) -> None:
        for bad in ("a\r\nDELE b", "a\nb", "a\0b", "x" * 4097):
            with self.assertRaises(ProtocolError):
                check_remote_path(bad)
        self.assertEqual(check_remote_path("/ok name/ü.txt"), "/ok name/ü.txt")

    def test_redaction(self) -> None:
        self.assertEqual(redact("PASS hunter2"), "PASS ********")
        self.assertEqual(redact("  pass hunter2"), "  PASS ********")
        self.assertEqual(redact("530 bad hunter2", {"p": "hunter2"}), "530 bad ********")

    def test_pin_store(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            pins = PinStore(Path(d) / "sub" / "pins.json")
            self.assertEqual(pins.get("k"), "")
            pins.set("k", "ab" * 32, "CN=x")
            self.assertEqual(pins.get("k"), normalize_fingerprint("AB" * 32))
            self.assertEqual(stat.S_IMODE(os.stat(pins.path).st_mode), 0o600)
            self.assertTrue(pins.forget("k"))
            with self.assertRaises(ValueError):
                pins.set("k", "not a fingerprint")


# --------------------------------------------------------------- ftp: fixtures
class _FtpCase(unittest.TestCase):
    tls_mode = "none"
    server_kwargs: dict = {}

    def setUp(self) -> None:
        from tests.fake_ftp import FakeFtpServer, make_certificate

        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "root"
        (self.root / "pub").mkdir(parents=True)
        (self.root / "pub" / "a file.txt").write_text("hello")
        (self.root / "pub" / ".hidden").write_text("h")
        (self.root / "empty").mkdir()
        self.cert = make_certificate(Path(self.tmp.name)) if self.tls_mode != "none" else None
        self.server = FakeFtpServer(self.root, tls_mode=self.tls_mode, certificate=self.cert,
                                    anonymous=True, **self.server_kwargs)
        self.server.__enter__()
        self.pins = PinStore(Path(self.tmp.name) / "pins.json")
        self.logs = []

    def tearDown(self) -> None:
        self.server.__exit__(None, None, None)
        self.tmp.cleanup()

    def backend(self, user="alice", password=PASSWORD, **settings):
        from ssh_manager.transfer.ftp import FtpBackend

        settings.setdefault("timeout_read", 5)
        return FtpBackend("127.0.0.1", self.server.port, user, password, settings=TransferSettings(**settings),
                          tls_mode=self.tls_mode, pins=self.pins, host_id="h1", log=self.logs.append)

    def connected(self, **settings):
        backend = self.backend(**settings)
        try:
            backend.connect()
        except UntrustedCertificate as exc:
            backend.trust_certificate(exc.info)
            backend.connect()
        self.addCleanup(backend.close)
        return backend


@INTEGRATION
class FtpTests(_FtpCase):
    def test_browse_read_write_and_manage(self) -> None:
        backend = self.connected()
        info = backend.server_info()
        self.assertIn("Acme test FTP", info.banner)
        self.assertIn("mlsd", info.features)
        self.assertEqual(sorted(e.name for e in backend.list("/pub")), [".hidden", "a file.txt"])
        with backend.open_read("/pub/a file.txt") as handle:
            self.assertEqual(handle.read(), b"hello")
        with backend.open_write("/pub/new.bin") as handle:
            handle.write(b"xyz" * 1000)
        self.assertEqual(backend.stat("/pub/new.bin").size, 3000)
        backend.mkdir("/x/y/z", parents=True)
        backend.rename("/pub/new.bin", "/x/y/new.bin")
        backend.chmod("/x/y/new.bin", 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.root / "x/y/new.bin").st_mode), 0o600)
        backend.set_mtime("/x/y/new.bin", 1_700_000_000)
        self.assertEqual(int(os.stat(self.root / "x/y/new.bin").st_mtime), 1_700_000_000)
        backend.delete_dir("/x", recursive=True)
        self.assertFalse(backend.exists("/x"))
        with self.assertRaises(NotFound):
            backend.stat("/nope.txt")

    def test_list_fallback_hides_dotfiles_unless_asked(self) -> None:
        self.server.mlsd = False
        self.assertEqual([e.name for e in self.connected().list("/pub")], ["a file.txt"])
        self.assertEqual(sorted(e.name for e in self.connected(show_hidden=True).list("/pub")), [".hidden",
                                                                                                "a file.txt"])

    def test_unparseable_list_lines_are_kept(self) -> None:
        self.server.mlsd = False
        self.server.extra_list_lines = ["total 3", "%% weird server line %%"]
        names = [e.name for e in self.connected().list("/pub")]
        self.assertIn("%% weird server line %%", names)
        self.assertNotIn("total 3", names)

    def test_empty_directory_answered_with_550(self) -> None:
        self.server.mlsd = False
        self.server.empty_list_550 = True
        self.assertEqual(list(self.connected().list("/empty")), [])
        with self.assertRaises(NotFound):
            list(self.connected().list("/missing"))

    def test_anonymous_login(self) -> None:
        backend = self.connected(user="", password="", anonymous=True)
        self.assertEqual(backend.user, "anonymous")
        self.assertIn("a file.txt", [e.name for e in backend.list("/pub")])

    def test_wrong_password_is_an_auth_error_without_the_password(self) -> None:
        backend = self.backend(password="wrong-hunter2")
        with self.assertRaises(AuthError) as caught:
            backend.connect()
        self.assertNotIn("wrong-hunter2", caught.exception.technical + caught.exception.message)
        self.assertNotIn("wrong-hunter2", "\n".join(self.logs))
        self.assertIn("> PASS ********", self.logs)

    def test_command_injection_never_reaches_the_server(self) -> None:
        backend = self.connected()
        with self.assertRaises(ProtocolError):
            list(backend.list("/pub\r\nDELE /pub/a file.txt"))
        with self.assertRaises(ProtocolError):
            backend.delete_file("x\nDELE y")
        self.assertTrue((self.root / "pub/a file.txt").exists())
        self.assertFalse(any("DELE" in line for line in self.server.log))

    def test_resume_with_rest(self) -> None:
        (self.root / "pub/big.bin").write_bytes(bytes(range(256)) * 100)
        backend = self.connected()
        with backend.open_read("/pub/big.bin", offset=25000) as handle:
            self.assertEqual(handle.read(), (bytes(range(256)) * 100)[25000:])
        (self.root / "pub/part.bin").write_bytes(b"A" * 10 + b"garbage")
        with backend.open_write("/pub/part.bin", offset=10) as handle:
            handle.write(b"B" * 5)
        self.assertEqual((self.root / "pub/part.bin").read_bytes(), b"A" * 10 + b"B" * 5)

    def test_atomic_upload_by_part_and_rename(self) -> None:
        backend = self.connected()
        with backend.open_write("/pub/report.csv.part") as handle:
            handle.write(b"1,2,3\n")
        self.assertFalse((self.root / "pub/report.csv").exists())
        backend.rename("/pub/report.csv.part", "/pub/report.csv")
        self.assertEqual((self.root / "pub/report.csv").read_bytes(), b"1,2,3\n")

    def test_large_file_streams_in_chunks(self) -> None:
        payload = os.urandom(3 * 1024 * 1024)
        backend = self.connected()
        with backend.open_write("/pub/large.bin") as handle:
            for start in range(0, len(payload), 256 * 1024):
                handle.write(payload[start:start + 256 * 1024])
        received = bytearray()
        with backend.open_read("/pub/large.bin") as handle:
            while chunk := handle.read(256 * 1024):
                received += chunk
        self.assertEqual(bytes(received), payload)

    def test_server_drop_reconnects_and_retries(self) -> None:
        backend = self.connected()
        self.server.drop_after = 1  # the next command after one more gets 421 and a hang-up
        with mock.patch("ssh_manager.transfer.ftp.RECONNECT_BACKOFF_S", (0.01, 0.01, 0.01)):
            backend.keepalive()
            names = [e.name for e in backend.list("/pub")]
            backend.keepalive()
        self.assertIn("a file.txt", names)

    def test_missing_226_does_not_hang(self) -> None:
        self.server.no_226_after_retr = True
        backend = self.connected(timeout_read=1)
        with backend.open_read("/pub/a file.txt") as handle:
            self.assertEqual(handle.read(), b"hello")

    def test_disk_full(self) -> None:
        self.server.disk_limit = 10
        backend = self.connected()
        with self.assertRaises(DiskFull):
            with backend.open_write("/pub/full.bin") as handle:
                handle.write(b"x" * 100)

    def test_odd_names_round_trip(self) -> None:
        odd = os.fsencode("/") + b"caf\xe9 menu.txt"  # latin-1 on disk, not UTF-8
        Path(os.fsdecode(bytes(self.root) + b"/pub" + odd)).write_bytes(b"latin")
        backend = self.connected()
        entry = next(e for e in backend.list("/pub") if e.name.startswith("caf"))
        self.assertIn("\udce9", entry.name)  # surrogateescape keeps the byte
        with backend.open_read(entry.path) as handle:
            self.assertEqual(handle.read(), b"latin")

    def test_active_mode(self) -> None:
        backend = self.connected(passive=False)
        self.assertIn("a file.txt", [e.name for e in backend.list("/pub")])

    def test_pasv_address_from_the_server_is_ignored(self) -> None:
        self.server.mlsd = False
        self.server.pasv_ip = "10.255.255.1"  # a lie (bounce attempt): the control peer is used instead
        self.assertIn("a file.txt", [e.name for e in self.connected().list("/pub")])
        trusting = self.connected(trust_pasv_address=True, timeout_read=1, timeout_connect=1)
        with self.assertRaises((Disconnected, Timeout)):
            list(trusting.list("/pub"))

    def test_error_mapping_table(self) -> None:
        backend = self.backend(password="hunter2")
        table = [
            (ftplib.error_perm("530 Login incorrect."), AuthError),
            (ftplib.error_perm("550 No such file."), NotFound),
            (ftplib.error_perm("550 Permission denied."), PermissionDenied),
            (ftplib.error_perm("552 Exceeded storage allocation."), DiskFull),
            (ftplib.error_perm("553 Could not create file."), ProtocolError),
            (ftplib.error_perm("502 Not implemented."), Unsupported),
            (ftplib.error_temp("421 Too many connections."), Disconnected),
            (ftplib.error_temp("452 Insufficient storage."), DiskFull),
            (ftplib.error_temp("426 Connection closed; transfer aborted."), Disconnected),
            (ftplib.error_reply("200 unexpected"), ProtocolError),
            (socket.timeout("timed out"), Timeout),
            (ConnectionResetError("reset"), Disconnected),
            (EOFError(), Disconnected),
        ]
        for exc, expected in table:
            with self.subTest(exc=exc):
                mapped = backend._map(exc)
                self.assertIsInstance(mapped, expected)
                self.assertNotIn("hunter2", mapped.technical)

    def test_active_mode_refuses_a_foreign_data_connection(self) -> None:
        from ssh_manager.transfer.ftp import _Client

        client = _Client(tls_mode="none", context=None, encoding="utf-8", timeout=1, trust_pasv=False,
                         port_range=None, log=None, secrets={})
        client.sock = mock.Mock(getpeername=lambda: ("127.0.0.1", 21))
        stray = mock.Mock()
        listener = mock.MagicMock()
        listener.__enter__.return_value.accept.return_value = (stray, ("10.6.6.6", 4444))
        with mock.patch.object(_Client, "makeport", return_value=listener), \
                mock.patch.object(_Client, "sendcmd", return_value="150 ok"):
            with self.assertRaises(ProtocolError):
                client._active_transfer("RETR x", None)
        stray.close.assert_called_once()


@INTEGRATION
class FtpsExplicitTests(_FtpCase):
    tls_mode = "explicit"
    server_kwargs = {"require_session_reuse": True}

    def test_first_use_certificate_then_pinned(self) -> None:
        backend = self.backend()
        with self.assertRaises(UntrustedCertificate) as caught:
            backend.connect()
        info = caught.exception.info
        self.assertIn("CN=localhost", info.subject)
        self.assertEqual(len(info.sha256), 95)
        self.assertTrue(info.not_after.endswith("UTC"))
        backend.trust_certificate(info)
        backend.connect()
        self.addCleanup(backend.close)
        self.assertTrue(backend.server_info().tls_version.startswith("TLSv1."))

    def test_data_channel_reuses_the_tls_session(self) -> None:
        backend = self.connected()
        self.assertEqual(sorted(e.name for e in backend.list("/pub")), [".hidden", "a file.txt"])
        with backend.open_read("/pub/a file.txt") as handle:
            self.assertEqual(handle.read(), b"hello")
        with backend.open_write("/pub/up.txt") as handle:
            handle.write(b"up")
        self.assertEqual(self.server.reuse_refusals, 0)
        self.assertEqual((self.root / "pub/up.txt").read_bytes(), b"up")

    def test_stock_ftplib_is_refused_by_the_same_server(self) -> None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        client = ftplib.FTP_TLS(context=context, timeout=5)
        client.connect("127.0.0.1", self.server.port)
        client.login("alice", PASSWORD)
        client.prot_p()
        with self.assertRaises((ftplib.Error, ssl.SSLError, OSError)):
            client.nlst("/pub")
        client.close()
        self.assertEqual(self.server.reuse_refusals, 1)  # the enforcement this backend passes is real

    def test_changed_certificate_is_refused(self) -> None:
        backend = self.backend(tls_verify="pinned")
        self.pins.set(backend.pin_key, "AB" * 32)
        with self.assertRaises(CertificateChanged) as caught:
            backend.connect()
        self.assertEqual(caught.exception.expected, normalize_fingerprint("AB" * 32))

    def test_custom_ca_verifies(self) -> None:
        backend = self.backend(tls_verify="custom_ca", ca_file=str(self.cert[0]))
        backend.connect()
        self.addCleanup(backend.close)
        self.assertEqual(self.pins.get(backend.pin_key), "")  # nothing pinned

    def test_insecure_skips_verification(self) -> None:
        backend = self.backend(tls_verify="insecure")
        backend.connect()
        self.addCleanup(backend.close)
        self.assertIn("tls", backend.features())


@INTEGRATION
class FtpsImplicitTests(_FtpCase):
    tls_mode = "implicit"
    server_kwargs = {"require_session_reuse": True}

    def test_implicit_tls_from_the_first_byte(self) -> None:
        backend = self.connected()
        self.assertTrue(backend.server_info().tls_version)
        self.assertIn("a file.txt", [e.name for e in backend.list("/pub")])
        self.assertEqual(self.server.reuse_refusals, 0)


# ----------------------------------------------------------------------- sftp
@INTEGRATION
class SftpTests(unittest.TestCase):
    def setUp(self) -> None:
        import paramiko

        from tests.fake_sftp import FakeSftpServer

        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "root"
        (self.root / "pub").mkdir(parents=True)
        (self.root / "pub/a.txt").write_text("hello")
        os.symlink("a.txt", self.root / "pub/lnk")
        self.known_hosts = Path(self.tmp.name) / "known_hosts"
        self.server = FakeSftpServer(self.root)
        self.server.__enter__()
        self.client_key = paramiko.ECDSAKey.generate()
        self.key_file = Path(self.tmp.name) / "id_test"
        self.client_key.write_private_key_file(str(self.key_file), password="phrase")

    def tearDown(self) -> None:
        self.server.__exit__(None, None, None)
        self.tmp.cleanup()

    def backend(self, password=PASSWORD, **kwargs):
        from ssh_manager.transfer.sftp import SftpBackend

        return SftpBackend("127.0.0.1", self.server.port, "alice", password, known_hosts=self.known_hosts,
                           allow_agent=False, settings=TransferSettings(timeout_read=5), **kwargs)

    def connected(self, **kwargs):
        backend = self.backend(**kwargs)
        try:
            backend.connect()
        except UnknownHostKey as exc:
            backend.accept_host_key(exc)
            backend.connect()
        self.addCleanup(backend.close)
        return backend

    def test_unknown_host_key_is_confirmed_then_remembered(self) -> None:
        backend = self.backend()
        with self.assertRaises(UnknownHostKey) as caught:
            backend.connect()
        self.assertTrue(caught.exception.fingerprint.startswith("SHA256:"))
        backend.accept_host_key(caught.exception)
        line = self.known_hosts.read_text()
        self.assertTrue(line.startswith(f"[127.0.0.1]:{self.server.port} ecdsa-sha2-nistp256 "))
        self.assertEqual(stat.S_IMODE(os.stat(self.known_hosts).st_mode), 0o600)
        self.backend().connect()  # known now: no question

    def test_changed_host_key_is_refused(self) -> None:
        import paramiko

        self.known_hosts.write_text(f"[127.0.0.1]:{self.server.port} ecdsa-sha2-nistp256 "
                                    f"{paramiko.ECDSAKey.generate().get_base64()}\n")
        with self.assertRaises(HostKeyChanged):
            self.backend().connect()

    def test_auto_add_policy_accepts_unknown_keys(self) -> None:
        backend = self.backend(host_key_policy="auto-add")
        backend.connect()
        self.addCleanup(backend.close)
        self.assertFalse(self.known_hosts.exists())

    def test_operations(self) -> None:
        backend = self.connected()
        entries = {e.name: e for e in backend.list("/pub")}
        self.assertEqual((entries["lnk"].kind, entries["lnk"].link_target), (EntryKind.SYMLINK, "a.txt"))
        self.assertEqual(entries["a.txt"].size, 5)
        handle = backend.open_read("/pub/a.txt", offset=1)
        self.assertEqual(handle.read(), b"ello")
        handle.close()
        writer = backend.open_write("/pub/b.bin")
        writer.write(b"x" * 100_000)
        writer.close()
        backend.rename("/pub/b.bin", "/pub/a.txt")  # posix_rename replaces
        self.assertEqual(backend.stat("/pub/a.txt").size, 100_000)
        backend.chmod("/pub/a.txt", 0o640)
        self.assertEqual(backend.stat("/pub/a.txt").mode, 0o640)
        backend.set_mtime("/pub/a.txt", 1_700_000_000)
        self.assertEqual(backend.stat("/pub/a.txt").mtime, 1_700_000_000)
        backend.mkdir("/q/r/s", parents=True)
        backend.delete_dir("/q", recursive=True)
        self.assertFalse(backend.exists("/q"))
        with self.assertRaises(NotFound):
            backend.stat("/nope")

    def test_wrong_password(self) -> None:
        self.connected()  # remember the host key
        with self.assertRaises(AuthError) as caught:
            self.backend(password="hunter2").connect()
        self.assertNotIn("hunter2", caught.exception.technical)

    def test_key_authentication_with_passphrase(self) -> None:
        self.server.authorized_key = self.client_key
        backend = self.connected(password="", key_file=str(self.key_file), passphrase="phrase")
        self.assertIn("a.txt", [e.name for e in backend.list("/pub")])

    def test_one_time_codes_are_not_supported(self) -> None:
        self.connected()
        self.server.otp_only = True
        with self.assertRaises((Unsupported, AuthError)):
            self.backend().connect()


def _ed25519(folder: Path, name: str = "ed"):
    """An Ed25519 server key: cryptography → OpenSSH PEM → Paramiko (Paramiko cannot generate Ed25519)."""

    import paramiko
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519

    path = folder / name
    path.write_bytes(ed25519.Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH, serialization.NoEncryption()))
    return paramiko.Ed25519Key.from_private_key_file(str(path))


@INTEGRATION
class HostKeyMatchingTests(unittest.TestCase):
    """The Paramiko host-key check behaves like OpenSSH: steered key types, any matching line, new types."""

    def setUp(self) -> None:
        import paramiko

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        (self.dir / "root").mkdir()
        self.known_hosts = self.dir / "known_hosts"
        self.ed = _ed25519(self.dir)
        self.ecdsa = paramiko.ECDSAKey.generate()
        self.rsa = paramiko.RSAKey.generate(2048)

    def serve(self, *keys):
        from tests.fake_sftp import FakeSftpServer

        self.server = FakeSftpServer(self.dir / "root", host_key=keys[0], extra_host_keys=keys[1:])
        self.server.__enter__()
        self.addCleanup(self.server.__exit__, None, None, None)
        self.name = f"[127.0.0.1]:{self.server.port}"
        return self.server

    def line(self, key, hosts: str = "", marker: str = "") -> str:
        return " ".join(part for part in (marker, hosts or self.name, key.get_name(), key.get_base64()) if part)

    def write(self, *lines: str) -> None:
        self.known_hosts.write_text("".join(line + "\n" for line in lines))

    def open(self, policy: str = "reject"):
        """The presented key's type after a full login (the transport is closed again)."""

        from ssh_manager.transfer.sftp import SftpBackend

        backend = SftpBackend("127.0.0.1", self.server.port, "alice", PASSWORD, known_hosts=self.known_hosts,
                              allow_agent=False, host_key_policy=policy, settings=TransferSettings(timeout_read=5))
        transport = backend.open_transport("test")
        try:
            self.assertTrue(transport.is_authenticated())
            return transport.get_remote_server_key().get_name()
        finally:
            transport.close()

    def test_known_by_ecdsa_or_rsa_only_steers_to_that_type(self) -> None:
        self.serve(self.ed, self.ecdsa, self.rsa)
        self.write(self.line(self.ecdsa))
        self.assertEqual(self.open(), "ecdsa-sha2-nistp256")  # was HostKeyChanged: ed25519 came first
        self.write(self.line(self.rsa))
        self.assertEqual(self.open(), "ssh-rsa")

    def test_any_matching_line_of_the_type_is_enough(self) -> None:
        import paramiko

        self.serve(self.ed)
        stale = _ed25519(self.dir, "stale")
        self.write(self.line(stale), self.line(self.ed))  # stale first: only the first was compared
        self.assertEqual(self.open(), "ssh-ed25519")
        self.write(self.line(self.ed), self.line(stale))
        self.assertEqual(self.open(), "ssh-ed25519")
        self.write(self.line(self.ecdsa, "127.0.0.1"), self.line(paramiko.ECDSAKey.generate()))  # other port: ignored
        with self.assertRaises(UnknownHostKey):
            self.open()

    def test_wildcard_and_exact_lines(self) -> None:
        self.serve(self.ed)
        stale = _ed25519(self.dir, "stale")
        self.write(self.line(stale, f"[127.0.0.*]:{self.server.port}"), self.line(self.ed))
        self.assertEqual(self.open(), "ssh-ed25519")
        self.write(self.line(stale), self.line(self.ed, f"[127.0.0.?]:{self.server.port},other"))
        self.assertEqual(self.open(), "ssh-ed25519")

    def test_a_really_changed_key_is_refused_with_its_details(self) -> None:
        from ssh_manager.known_hosts import fingerprint_sha256
        from ssh_manager.transfer.sftp import key_fingerprint

        self.serve(self.ed)
        stale = _ed25519(self.dir, "stale")
        self.write("# a comment", self.line(self.ecdsa), self.line(stale))
        with self.assertRaises(HostKeyChanged) as caught:
            self.open(policy="auto-add")  # never accepted, whatever the policy
        error = caught.exception
        self.assertEqual((error.key_type, error.fingerprint), ("ssh-ed25519", key_fingerprint(self.ed)))
        self.assertEqual(error.old_fingerprints, (fingerprint_sha256(stale.get_base64()),))
        self.assertEqual(error.line_no, 3)
        self.assertEqual(self.server.password_attempts, 0)

    def test_only_another_type_known_is_a_new_key_not_a_changed_one(self) -> None:
        from ssh_manager.known_hosts import fingerprint_sha256

        self.serve(self.ed)
        self.write(self.line(self.ecdsa))
        with self.assertRaises(UnknownHostKey) as caught:
            self.open()
        self.assertEqual(caught.exception.other_key_types, ["ecdsa-sha2-nistp256"])
        self.assertEqual(caught.exception.other_fingerprints, [fingerprint_sha256(self.ecdsa.get_base64())])
        self.assertEqual(self.server.password_attempts, 0)  # reject: never logged in
        self.assertEqual(self.open(policy="auto-add"), "ssh-ed25519")
        self.assertEqual(self.known_hosts.read_text(), self.line(self.ecdsa) + "\n")  # auto-add saves nothing

    def test_hashed_entries_steer_and_match(self) -> None:
        import base64
        import hashlib
        import hmac

        self.serve(self.ed, self.ecdsa)
        salt = os.urandom(20)
        digest = hmac.new(salt, self.name.encode(), hashlib.sha1).digest()
        hashed = f"|1|{base64.b64encode(salt).decode()}|{base64.b64encode(digest).decode()}"
        self.write(self.line(self.ecdsa, hashed))
        self.assertEqual(self.open(), "ecdsa-sha2-nistp256")

    def test_revoked_keys_are_refused_and_cert_authority_lines_ignored(self) -> None:
        self.serve(self.ed, self.ecdsa)
        self.write(self.line(self.ed, marker="@cert-authority"), self.line(self.ecdsa))
        self.assertEqual(self.open(), "ecdsa-sha2-nistp256")  # the CA line does not steer to ed25519
        logins = self.server.password_attempts
        self.write(self.line(self.ecdsa, marker="@revoked"), self.line(self.ecdsa))
        with self.assertRaises(HostKeyChanged):
            self.open()
        self.write(self.line(self.ed, marker="@revoked"))
        with self.assertRaises(HostKeyChanged):
            self.open(policy="auto-add")
        self.assertEqual(self.server.password_attempts, logins)  # refused before logging in

    def test_key_type_order(self) -> None:
        import socket as socketmod

        import paramiko

        from ssh_manager.known_hosts import KnownHostsFile
        from ssh_manager.transfer.sftp import SftpBackend

        self.serve(self.ed)
        backend = SftpBackend("127.0.0.1", self.server.port, known_hosts=self.known_hosts)
        left, right = socketmod.socketpair()
        self.addCleanup(left.close)
        self.addCleanup(right.close)

        def order(*lines):
            self.write(*lines)
            transport = paramiko.Transport(left)
            default = list(transport.get_security_options().key_types)
            backend._steer_host_key_types(transport, KnownHostsFile(self.known_hosts).lookup("127.0.0.1",
                                                                                              self.server.port))
            result = list(transport.get_security_options().key_types)
            self.assertEqual(sorted(result), sorted(default))  # reordered, never fewer
            return default, result

        default, result = order()
        self.assertEqual(result, default)  # nothing known: unchanged
        _default, rsa = order(self.line(self.rsa))
        self.assertEqual(rsa[:3], ["rsa-sha2-512", "rsa-sha2-256", "ssh-rsa"])
        _default, ecdsa = order(self.line(self.ecdsa))
        self.assertEqual(ecdsa[0], "ecdsa-sha2-nistp256")


if __name__ == "__main__":
    unittest.main()
