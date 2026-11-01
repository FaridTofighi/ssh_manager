"""RDP redirection, gateway, SSH tunnel, presets and advanced options (pure + sockets; synthetic data only)."""

import os
import socket
import socketserver
import ssl
import tempfile
import threading
import unittest
from pathlib import Path

from ssh_manager.rdp.command import LaunchError, build_launch, preset_for_latency
from ssh_manager.rdp.drives import check_folder, home_drive_check
from ssh_manager.rdp.engines import ClientBinary, parse_help_options
from ssh_manager.rdp.probe import probe_tls
from ssh_manager.rdp.settings import Gateway, RdpIdentity, RdpSettings, SharedFolder

FIXTURES = Path(__file__).parent / "fixtures" / "freerdp"
V3 = ClientBinary("xfreerdp3", "/usr/bin/xfreerdp3", (3, 15, 0), "3.15.0",
                  parse_help_options((FIXTURES / "help-3.15.txt").read_text()))
INTEGRATION = unittest.skipIf(os.environ.get("SSHM_SKIP_INTEGRATION") == "1", "integration tests skipped")


def args(plan):
    return plan.stdin_payload.decode().split("\n")[:-2]


def build(settings, **kwargs):
    return build_launch(hostname=kwargs.pop("hostname", "10.0.0.50"), port=kwargs.pop("port", 3389), settings=settings,
                        identity=RdpIdentity("alice", "CORP"), password="pw-x", client=V3,
                        accepted_fingerprints=kwargs.pop("fingerprints", ["AB" * 32]), **kwargs)


class SharedFolderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        for sub in (".ssh", ".gnupg", "work/projects", ".config/ssh-manager", ".config/ophion"):
            (self.home / sub).mkdir(parents=True)
        (self.home / "work" / "keys").symlink_to(self.home / ".ssh")
        (self.home / "work" / "up").symlink_to(self.home)

    def check(self, path, **kwargs):
        return check_folder(str(path), home=str(self.home), **kwargs)

    def test_ordinary_folders_are_fine(self) -> None:
        self.assertTrue(self.check(self.home / "work").ok)
        self.assertTrue(self.check(self.home / "work" / "projects").ok)
        self.assertTrue(self.check("/usr/share").ok)

    def test_forbidden_places_and_symlink_escapes(self) -> None:
        for path, place in ((self.home, "~"), (self.home / ".ssh", "~/.ssh"), (self.home / ".gnupg", "~/.gnupg"),
                            (self.home / ".config" / "ssh-manager", "~/.config/ssh-manager"),
                            (self.home / ".config" / "ophion", "~/.config/ophion"),
                            (self.home / "work" / "keys", "~/.ssh"), (self.home / "work" / "up", "~"),
                            ("/", "/"), ("/etc", "/etc")):
            result = self.check(path)
            self.assertFalse(result.ok, path)
            self.assertEqual(result.sensitive, place, path)

    def test_confirmed_sensitive_folder_is_allowed(self) -> None:
        result = self.check(self.home / ".ssh", allow_sensitive=True)
        self.assertTrue(result.ok)
        self.assertEqual(result.path, str((self.home / ".ssh").resolve()))

    def test_unusable_paths(self) -> None:
        for path in ("", "relative", str(self.home / "missing"), "a\nb"):
            self.assertFalse(self.check(path).ok, path)
        comma = self.home / "work" / "a,b"
        comma.mkdir()
        self.assertIn("comma", self.check(comma).problem)
        (self.home / "file").write_text("x")
        self.assertFalse(self.check(self.home / "file").ok)

    def test_builder_never_shares_unconfirmed_sensitive_folders(self) -> None:
        settings = RdpSettings(drives=[SharedFolder("keys", str(Path.home() / ".ssh"))])
        if not (Path.home() / ".ssh").is_dir():
            self.skipTest("no ~/.ssh here")
        plan = build(settings)
        self.assertFalse(any(a.startswith("/drive:") for a in args(plan)))
        self.assertIn("confirm sharing it", " ".join(plan.notes))
        self.assertFalse(home_drive_check(False).ok)
        self.assertTrue(home_drive_check(True).ok)


class PresetAndOptionTests(unittest.TestCase):
    def test_latency_table(self) -> None:
        self.assertEqual([preset_for_latency(ms) for ms in (None, 0, 1, 4.9, 5, 39, 40, 149, 150, 900)],
                         ["auto", "auto", "lan", "lan", "broadband", "broadband", "wan", "wan", "modem", "modem"])

    def test_auto_preset_uses_the_measured_latency(self) -> None:
        plan = build(RdpSettings(), latency_ms=80)
        self.assertIn("/network:wan", args(plan))
        self.assertIn("measured 80 ms", " ".join(plan.notes))
        self.assertIn("/network:auto", args(build(RdpSettings())))
        self.assertIn("/network:lan", args(build(RdpSettings(preset="lan"), latency_ms=900)))  # explicit wins

    def test_vmconnect_and_problems(self) -> None:
        vm = "1f3e7f8c-0d4b-4b63-9e57-2f0a5d6b8c11"
        self.assertIn("/vmconnect:" + vm, args(build(RdpSettings(hyperv_vm_id=vm), port=2179)))
        self.assertTrue(RdpSettings(hyperv_vm_id="not-a-guid").problems())
        both = RdpSettings(gateway=Gateway("gw.acme.test"), via_ssh="ssh-1")
        self.assertIn("either an RD Gateway or an SSH tunnel", " ".join(both.problems()))
        settings, _w = RdpSettings.from_json({"v": 1, "gateway": {"host": "gw", "credential_id": "c1"},
                                              "drives": [{"name": "k", "path": "/x", "allow_sensitive": True}]})
        self.assertEqual(settings.gateway.credential_id, "c1")
        self.assertTrue(settings.drives[0].allow_sensitive)
        self.assertEqual(RdpSettings.from_json(settings.to_json())[0].to_json(), settings.to_json())


class GatewayArgvTests(unittest.TestCase):
    def test_same_credentials(self) -> None:
        plan = build(RdpSettings(gateway=Gateway("gw.acme.test", 443)))
        self.assertIn("/gateway:g:gw.acme.test:443", args(plan))

    def test_own_credentials_and_password_channel(self) -> None:
        plan = build(RdpSettings(gateway=Gateway("gw.acme.test", 8443, "CORP\\bob", "", False, "http")),
                     gateway_password="gw-secret")
        self.assertIn("/gateway:g:gw.acme.test:8443,u:CORP\\bob,p:gw-secret,type:http", args(plan))
        self.assertNotIn("gw-secret", " ".join(plan.argv) + " ".join(plan.display_args))

    def test_tofu_behind_the_gateway(self) -> None:
        plan = build(RdpSettings(gateway=Gateway("gw.acme.test")), cert_tofu=True, fingerprints=["CD" * 32])
        self.assertIn("/cert:tofu,fingerprint:sha256:" + ":".join(["cd"] * 32), args(plan))

    def test_tunnel_server_name(self) -> None:
        plan = build(RdpSettings(), hostname="127.0.0.1", port=40123, server_name="win-01.acme.test")
        self.assertIn("/v:127.0.0.1:40123", args(plan))
        self.assertIn("/server-name:win-01.acme.test", args(plan))
        with self.assertRaises(LaunchError):
            build(RdpSettings(), server_name="a,b")


class _TlsHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        try:
            with self.server.context.wrap_socket(self.request, server_side=True) as tls:
                tls.recv(1)
        except (OSError, ssl.SSLError):
            pass


@INTEGRATION
class GatewayProbeTests(unittest.TestCase):
    def test_reads_the_gateway_certificate(self) -> None:
        from tests.fake_rdp import write_certificate

        with tempfile.TemporaryDirectory() as tmp:
            cert = write_certificate(Path(tmp))
            server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _TlsHandler)
            server.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            server.context.load_cert_chain(*(str(p) for p in cert))
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.shutdown)
            self.addCleanup(server.server_close)
            result = probe_tls("127.0.0.1", server.server_address[1], timeout=5)
        self.assertTrue(result.reachable)
        self.assertIn("win-01.acme.test", result.certificate.subject)
        self.assertEqual(result.pin_status, "none")
        free = socket.socket()
        free.bind(("127.0.0.1", 0))
        port = free.getsockname()[1]
        free.close()
        self.assertFalse(probe_tls("127.0.0.1", port, timeout=1).reachable)


class _Echo(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        while True:
            data = self.request.recv(4096)
            if not data:
                return
            self.request.sendall(data)


@INTEGRATION
class TunnelTests(unittest.TestCase):
    def setUp(self) -> None:
        from tests.fake_sftp import FakeSftpServer

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ssh = FakeSftpServer(Path(self.tmp.name))
        self.ssh.__enter__()
        self.addCleanup(self.ssh.__exit__, None, None, None)
        self.echo = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Echo)
        self.echo.daemon_threads = True
        threading.Thread(target=self.echo.serve_forever, daemon=True).start()
        self.addCleanup(self.echo.server_close)
        self.addCleanup(self.echo.shutdown)

    def backend(self, password="s3cret pass"):
        from ssh_manager.transfer.sftp import SftpBackend

        return SftpBackend("127.0.0.1", self.ssh.port, "alice", password, host_key_policy="auto-add",
                           known_hosts=Path(self.tmp.name) / "known_hosts", allow_agent=False)

    def test_forwards_over_loopback_only(self) -> None:
        from ssh_manager.rdp.tunnel import open_tunnel

        log = []
        tunnel = open_tunnel(self.backend(), "127.0.0.1", self.echo.server_address[1], log=log.append)
        self.addCleanup(tunnel.close)
        self.assertEqual(tunnel.listener.getsockname()[0], "127.0.0.1")
        self.assertNotEqual(tunnel.port, self.echo.server_address[1])
        with socket.create_connection(("127.0.0.1", tunnel.port), timeout=5) as client:
            client.sendall(b"hello RDP")
            self.assertEqual(client.recv(64), b"hello RDP")
            with socket.create_connection(("127.0.0.1", tunnel.port), timeout=5) as second:
                second.settimeout(3)
                self.assertEqual(second.recv(64), b"")  # one client at a time: refused
        self.assertIn(("127.0.0.1", self.echo.server_address[1]), list(self.ssh.forwards.values()))
        self.assertTrue(any("SSH tunnel" in line for line in log))
        tunnel.close()
        self.assertFalse(tunnel.alive)
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", tunnel.port), timeout=1).close()

    def test_failures_are_reported(self) -> None:
        from ssh_manager.rdp.tunnel import open_tunnel
        from ssh_manager.transfer.backend import AuthError

        with self.assertRaises(AuthError):
            open_tunnel(self.backend("wrong"), "127.0.0.1", 1)
        self.ssh.allow_forward = False
        tunnel = open_tunnel(self.backend(), "127.0.0.1", self.echo.server_address[1])
        self.addCleanup(tunnel.close)
        with socket.create_connection(("127.0.0.1", tunnel.port), timeout=5) as client:
            client.settimeout(5)
            self.assertEqual(client.recv(64), b"")
        self.assertIn("could not reach", tunnel.failed)


if __name__ == "__main__":
    unittest.main()


def _display() -> bool:
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return False
    try:
        import gi

        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk  # type: ignore

        return Gtk.init_check(None)[0]
    except Exception:
        return False


@INTEGRATION
@unittest.skipUnless(_display(), "needs a display")
class RouteTabTests(unittest.TestCase):
    """The session tab through an SSH tunnel (real probe through the tunnel) and through an RD Gateway."""

    def setUp(self) -> None:
        from unittest import mock

        from ssh_manager.gtkui.rdp.runtime import RdpRuntime
        from ssh_manager.rdp import engines
        from ssh_manager.rdp.engines import EngineReport, Platform, build_engines
        from ssh_manager.rdp.process import ChildRegistry
        from ssh_manager.trust import TrustStore
        from tests import fake_freerdp
        from tests.fake_rdp import write_certificate

        self.mock = mock
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.record = self.dir / "record.jsonl"
        path = fake_freerdp.install(self.dir, "xfreerdp3")
        client = ClientBinary("xfreerdp3", str(path), (3, 15, 0), "3.15.0", V3.options)
        platform = Platform("wayland", False, "", "", False, "debian")
        built = build_engines([client], platform)
        self.report = EngineReport(platform, [client], built, engines.choose_engine(built)[0])
        self.trust = TrustStore(self.dir / "pins.json")
        self.runtime = RdpRuntime(registry=ChildRegistry(self.dir / "c.json"), trust_store=self.trust, reap=False,
                                  state_path=self.dir / "e.json")
        self.cert = write_certificate(self.dir)
        env = mock.patch.dict(os.environ, fake_freerdp.env_for(self.record, lines=[
            "[INFO][com.freerdp.gdi] - Local framebuffer format  PIXEL_FORMAT_BGRX32"], sleep=30))
        env.start()
        self.addCleanup(env.stop)
        self.records = lambda: fake_freerdp.records(self.record)

    def tab(self, host, **kwargs):
        from ssh_manager.auth_flow import PasswordResolution
        from ssh_manager.gtkui.rdp.tab import RdpSessionTab

        tab = RdpSessionTab(host, lambda *a: None, runtime=self.runtime,
                            password_provider=lambda: PasswordResolution("pw-x", "credential"),
                            report_provider=lambda: self.report, **kwargs)
        tab.show_all()
        self.addCleanup(lambda: tab.get_parent() is None and tab.destroy())
        return tab

    def pump(self, predicate, timeout=15):
        from tests.test_rdp_session import pump_until

        return pump_until(predicate, timeout)

    def test_through_an_ssh_tunnel(self) -> None:
        from ssh_manager.config import HostEntry
        from ssh_manager.rdp.probe import PROTOCOL_HYBRID
        from ssh_manager.transfer.sftp import SftpBackend
        from tests.fake_rdp import FakeRdpServer, neg_rsp
        from tests.fake_sftp import FakeSftpServer

        rdp = FakeRdpServer([neg_rsp(PROTOCOL_HYBRID)], self.cert)
        self.addCleanup(rdp.close)
        ssh = FakeSftpServer(self.dir)
        ssh.__enter__()
        self.addCleanup(ssh.__exit__, None, None, None)
        made = []

        def factory(host_id):
            made.append(host_id)
            return SftpBackend("127.0.0.1", ssh.port, "alice", "s3cret pass", host_key_policy="auto-add",
                               known_hosts=self.dir / "known_hosts", allow_agent=False)

        host = HostEntry("win-01", {"hostname": "127.0.0.1", "port": str(rdp.port), "user": "alice"},
                         protocol="rdp", id="h-win", metadata={"rdp": {"v": 1, "via_ssh": "ssh-1",
                                                                         "cert_policy": "tofu"}})
        tab = self.tab(host, tunnel_factory=factory)
        self.assertTrue(self.pump(lambda: tab.connected_method == "log"), tab.connection_log.text())
        self.assertEqual(made, ["ssh-1"])
        lines = self.records()[0]["stdin"].split("\n")
        self.assertIn(f"/v:127.0.0.1:{tab.tunnel.port}", lines)
        self.assertNotEqual(tab.tunnel.port, rdp.port)
        self.assertIn("/server-name:127.0.0.1", lines)
        self.assertEqual(len(rdp.requests), 1)  # the probe went through the tunnel
        self.assertIn(("127.0.0.1", rdp.port), list(ssh.forwards.values()))
        self.assertTrue(any(line.startswith("/cert:deny,fingerprint:sha256:") for line in lines))
        self.assertIn("SSH tunnel", dict(tab.info_lines())["Route"])
        tunnel = tab.tunnel
        tab.disconnect()
        self.assertTrue(self.pump(lambda: tab.process is None))
        self.assertIsNone(tab.tunnel)
        self.assertFalse(tunnel.alive)

    def test_tunnel_failure_is_explained(self) -> None:
        from ssh_manager.config import HostEntry

        host = HostEntry("win-01", {"hostname": "10.0.0.50", "user": "alice"}, protocol="rdp", id="h-win",
                         metadata={"rdp": {"v": 1, "via_ssh": "gone"}})

        def factory(_host_id):
            raise LookupError("no such host")

        tab = self.tab(host, tunnel_factory=factory)
        self.assertTrue(self.pump(lambda: tab.status.value == "failed"))
        self.assertIn("no longer exists", tab.status_label.get_text())
        self.assertEqual(self.records(), [])

    def test_through_an_rd_gateway(self) -> None:
        from ssh_manager.config import HostEntry
        from ssh_manager.rdp.probe import ProbeResult
        from ssh_manager.trust import CertificateInfo, pin_key

        gw_fp = ":".join(["CD"] * 32)
        host = HostEntry("win-01", {"hostname": "10.0.0.50", "user": "alice"}, protocol="rdp", id="h-win",
                         metadata={"rdp": {"v": 1, "cert_policy": "tofu",
                                           "gateway": {"host": "gw.acme.test", "port": 443,
                                                       "use_same_credentials": False, "user": "bob",
                                                       "credential_id": "c-gw"}}})
        probes = []
        tab = self.tab(host, probe_fn=lambda *a, **k: probes.append(a),
                       tls_probe_fn=lambda *a, **k: ProbeResult("gw.acme.test", 443, reachable=True, security="tls",
                                                               certificate=CertificateInfo("CN=gw", "CN=gw", "", "",
                                                                                           gw_fp)),
                       gateway_password_provider=lambda cred_id: "gw-secret" if cred_id == "c-gw" else None)
        self.assertTrue(self.pump(lambda: tab.connected_method == "log"), tab.connection_log.text())
        self.assertEqual(probes, [])  # the server behind the gateway is not probed directly
        lines = self.records()[0]["stdin"].split("\n")
        self.assertIn("/gateway:g:gw.acme.test:443,u:bob,p:gw-secret", lines)
        self.assertIn("/cert:tofu,fingerprint:sha256:" + gw_fp.lower(), lines)
        self.assertEqual(self.trust.get(pin_key("h-win", "gw.acme.test", 443, scope="rdp-gw")), gw_fp)
        self.assertNotIn("gw-secret", tab.connection_log.text())
        self.assertIn("RD Gateway", dict(tab.info_lines())["Route"])
        tab.disconnect()
