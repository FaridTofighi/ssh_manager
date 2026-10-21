"""The RDP session tab with a fake FreeRDP client: trust, passwords, outcomes, cleanup (skipped without a display)."""

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from ssh_manager.auth_flow import PasswordResolution
from ssh_manager.config import HostEntry
from ssh_manager.rdp import engines
from ssh_manager.rdp.engines import ClientBinary, EngineReport, Platform, build_engines, parse_help_options
from ssh_manager.rdp.probe import ProbeResult
from ssh_manager.trust import CertificateInfo, TrustStore, pin_key
from tests import fake_freerdp

FIXTURES = Path(__file__).parent / "fixtures" / "freerdp"
A = ":".join(["AA"] * 32)
B = ":".join(["BB"] * 32)
CONNECTED = "[INFO][com.freerdp.gdi] - [gdi_init_ex]: Local framebuffer format  PIXEL_FORMAT_BGRX32"


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


def pump_until(predicate, timeout=10.0) -> bool:
    from gi.repository import GLib  # type: ignore

    deadline = time.monotonic() + timeout
    context = GLib.MainContext.default()
    while time.monotonic() < deadline:
        while context.pending():
            context.iteration(False)
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def probe_result(security="nla", sha=A, reachable=True, error=""):
    cert = CertificateInfo("CN=win-01.acme.test", "CN=win-01.acme.test", "", "", sha) if sha else None
    labels = {"nla": "Network Level Authentication (CredSSP + TLS)", "tls": "TLS", "rdp": "Standard RDP security"}
    return ProbeResult("10.0.0.50", 3389, reachable=reachable, latency_ms=3.0, security=security if reachable else "",
                       security_label=labels.get(security, ""), certificate=cert, error=error)


class FakePasswordDialog:
    """Stands in for PasswordRequestDialog: answers from a queue on the next main-loop turn."""

    answers: list = []
    requests: list = []

    def __init__(self, _parent, request, on_done) -> None:
        from gi.repository import GLib  # type: ignore

        FakePasswordDialog.requests.append(request)
        answer = FakePasswordDialog.answers.pop(0) if FakePasswordDialog.answers else None
        GLib.idle_add(lambda: on_done(answer) and False)

    def dismiss(self) -> None:
        pass


class FakeSaveDialog:
    offers: list = []

    def __init__(self, _parent, offer, on_decision) -> None:
        FakeSaveDialog.offers.append(offer)

    def dismiss(self) -> None:
        pass


@unittest.skipUnless(_display(), "needs a display")
class SessionTabTests(unittest.TestCase):
    def setUp(self) -> None:
        from ssh_manager.gtkui.rdp.runtime import RdpRuntime
        from ssh_manager.rdp.process import ChildRegistry

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.record = self.dir / "record.jsonl"
        self.client_path = fake_freerdp.install(self.dir, "xfreerdp3")
        client = ClientBinary("xfreerdp3", str(self.client_path), (3, 15, 0), "3.15.0",
                              parse_help_options((FIXTURES / "help-3.15.txt").read_text()))
        platform = Platform("wayland", False, "", "", False, "debian")  # external window engine
        built = build_engines([client], platform)
        self.report = EngineReport(platform, [client], built, engines.choose_engine(built)[0])
        self.trust = TrustStore(self.dir / "pins.json")
        self.runtime = RdpRuntime(registry=ChildRegistry(self.dir / "children.json"), trust_store=self.trust,
                                  reap=False)
        self.logs = []
        self.probes = []
        self.probe_answer = probe_result()
        FakePasswordDialog.answers, FakePasswordDialog.requests, FakeSaveDialog.offers = [], [], []
        for target, value in (("PasswordRequestDialog", FakePasswordDialog), ("SavePasswordDialog", FakeSaveDialog),
                              ("RETRY_DELAY_S", 0)):
            patcher = mock.patch(f"ssh_manager.gtkui.rdp.tab.{target}", value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def host(self, **rdp) -> HostEntry:
        return HostEntry("win-01", {"hostname": "10.0.0.50", "user": "CORP\\alice"}, protocol="rdp", id="h-win",
                         metadata={"rdp": {"v": 1, **rdp}})

    def pin(self, sha=A) -> None:
        self.trust.set(pin_key("h-win", "10.0.0.50", 3389, scope="rdp"), sha, "CN=win-01")

    def tab(self, host=None, password=None, report="default", **script):
        from ssh_manager.gtkui.rdp.tab import RdpSessionTab

        resolution = PasswordResolution(password, "credential" if password else "none")
        env = mock.patch.dict(os.environ, fake_freerdp.env_for(self.record, **script))
        env.start()
        self.addCleanup(env.stop)
        tab = RdpSessionTab(host or self.host(), lambda message, level: self.logs.append(message),
                            runtime=self.runtime, password_provider=lambda: resolution, password_service=None,
                            report_provider=lambda: self.report if report == "default" else report,
                            probe_fn=lambda *a, **k: (self.probes.append(k), self.probe_answer)[1], autostart=True)
        tab.show_all()
        self.addCleanup(lambda: tab.get_parent() is None and tab.destroy())
        return tab

    def records(self):
        return fake_freerdp.records(self.record)

    def ended(self, tab):
        return tab.process is None and tab.last_outcome is not None

    # ------------------------------------------------------------ tests
    def test_verified_nla_login_with_a_typed_password_offers_save(self) -> None:
        self.pin()
        service = mock.Mock()
        service.password_save_context.return_value = __import__("ssh_manager.auth_flow").auth_flow.SaveContext()
        FakePasswordDialog.answers = ["typed secret"]
        tab = self.tab(lines=[CONNECTED], sleep=30)
        tab._password_service = service
        self.assertTrue(pump_until(lambda: tab.connected_method == "log"), tab.connection_log.text())
        self.assertEqual(tab.status.value, "connected")
        record = self.records()[0]
        self.assertNotIn("typed secret", json.dumps(record["argv"]) + json.dumps(record["env"]) + record["cmdline"])
        self.assertIn("/p:typed secret", record["stdin"].split("\n"))
        self.assertIn("/u:alice", record["stdin"].split("\n"))
        self.assertIn("/d:CORP", record["stdin"].split("\n"))
        self.assertIn("fingerprint:sha256:" + A.lower(), record["stdin"])
        self.assertEqual(len(FakeSaveDialog.offers), 1)
        self.assertNotIn("typed secret", tab.connection_log.text())
        tab.disconnect()
        self.assertTrue(pump_until(lambda: self.ended(tab)))
        self.assertEqual(tab.last_outcome.reason, "user_disconnect")
        self.assertFalse(tab._banner.get_revealed())

    def test_tls_or_heuristic_never_offers_save(self) -> None:
        self.pin()
        self.probe_answer = probe_result("tls")
        FakePasswordDialog.answers = ["typed"]
        with mock.patch("ssh_manager.gtkui.rdp.tab.HEURISTIC_S", 1):
            tab = self.tab(sleep=30)
            self.assertTrue(pump_until(lambda: tab.connected_method == "heuristic", 15))
        self.assertEqual(FakeSaveDialog.offers, [])
        self.assertIn("Probably connected", tab.connection_log.text())

    def test_wrong_password_asks_again_then_stops(self) -> None:
        self.pin()
        FakePasswordDialog.answers = ["second", None]
        tab = self.tab(password="stored", lines=["ERRCONNECT_LOGON_FAILURE [0x00020014]"], exit=134)
        self.assertTrue(pump_until(lambda: len(FakePasswordDialog.requests) == 2 and tab.status.value == "failed"))
        stdins = [r["stdin"] for r in self.records()]
        self.assertIn("/p:stored", stdins[0].split("\n"))
        self.assertIn("/p:second", stdins[1].split("\n"))
        self.assertTrue(FakePasswordDialog.requests[0].rejected)
        self.assertEqual(FakePasswordDialog.requests[1].attempt, 2)

    def test_account_problems_never_ask_for_the_password(self) -> None:
        self.pin()
        for token, code, words in (("ERRCONNECT_ACCOUNT_LOCKED_OUT", 135, "locked"),
                                   ("ERRCONNECT_PASSWORD_EXPIRED", 148, "expired"),
                                   ("ERRCONNECT_LOGON_TYPE_NOT_GRANTED", 158, "Remote Desktop Users")):
            self.record.unlink(missing_ok=True)
            tab = self.tab(password="stored", lines=[f"[ERROR][com.freerdp.core] - {token} [0x0]"], exit=code)
            self.assertTrue(pump_until(lambda: self.ended(tab)), token)
            self.assertEqual(FakePasswordDialog.requests, [], token)
            self.assertIn(words, tab.status_label.get_text())
            self.assertTrue(tab._banner.get_revealed())
            tab.destroy()

    def test_first_certificate_ask_and_remember(self) -> None:
        with mock.patch("ssh_manager.gtkui.rdp.tab.rdp_trust_choice", return_value="remember") as ask:
            tab = self.tab(password="stored", lines=[CONNECTED], sleep=30)
            self.assertTrue(pump_until(lambda: tab.connected_method == "log"))
        ask.assert_called_once()
        self.assertEqual(self.trust.get(pin_key("h-win", "10.0.0.50", 3389, scope="rdp")), A)
        tab.disconnect()

    def test_trust_once_does_not_store(self) -> None:
        with mock.patch("ssh_manager.gtkui.rdp.tab.rdp_trust_choice", return_value="once"):
            tab = self.tab(password="stored", lines=[CONNECTED], sleep=30)
            self.assertTrue(pump_until(lambda: tab.connected_method == "log"))
        self.assertEqual(self.trust.get(pin_key("h-win", "10.0.0.50", 3389, scope="rdp")), "")
        tab.disconnect()

    def test_changed_certificate_is_not_accepted_without_consent(self) -> None:
        self.pin(B)
        with mock.patch("ssh_manager.gtkui.rdp.tab.trust_certificate", return_value=False) as warn:
            tab = self.tab(password="stored")
            self.assertTrue(pump_until(lambda: tab.status.value == "failed"))
        self.assertTrue(warn.call_args.kwargs["changed"])
        self.assertEqual(self.records(), [])  # FreeRDP never started
        self.assertIn("changed", tab.status_label.get_text())

    def test_tofu_remembers_and_deny_refuses(self) -> None:
        tab = self.tab(self.host(cert_policy="tofu"), password="stored", lines=[CONNECTED], sleep=30)
        self.assertTrue(pump_until(lambda: tab.connected_method == "log"))
        self.assertEqual(self.trust.get(pin_key("h-win", "10.0.0.50", 3389, scope="rdp")), A)
        tab.destroy()
        self.trust.forget(pin_key("h-win", "10.0.0.50", 3389, scope="rdp"))
        deny = self.tab(self.host(cert_policy="deny"), password="stored")
        self.assertTrue(pump_until(lambda: deny.status.value == "failed"))
        self.assertIn("refuses unknown certificates", deny.status_label.get_text())

    def test_ignore_asks_every_time_and_is_flagged(self) -> None:
        with mock.patch("ssh_manager.gtkui.rdp.tab.confirm_unchecked_certificate", return_value=True) as ask:
            tab = self.tab(self.host(cert_policy="ignore"), password="stored", lines=[CONNECTED], sleep=30)
            self.assertTrue(pump_until(lambda: tab.connected_method == "log"))
        ask.assert_called_once()
        self.assertIn("/cert:ignore", self.records()[0]["stdin"].split("\n"))
        self.assertIn("certificate not verified", tab.status_label.get_text())
        self.assertEqual(tab.facts["certificate"].get_text(), "Not verified")
        tab.disconnect()

    def test_certificate_swapped_after_the_probe_is_reported(self) -> None:
        self.pin()
        tab = self.tab(password="stored", stderr=["[ERROR][com.freerdp.crypto] - certificate not trusted, aborting.",
                                                  "ERRCONNECT_TLS_CONNECT_FAILED [0x00020008]"], exit=143)
        self.assertTrue(pump_until(lambda: self.ended(tab)))
        self.assertEqual(tab.last_outcome.kind.value, "cert_rejected")
        self.assertEqual(FakePasswordDialog.requests, [])

    def test_unreachable_and_no_engine(self) -> None:
        self.probe_answer = probe_result(reachable=False, error="Connection refused")
        tab = self.tab(password="stored")
        self.assertTrue(pump_until(lambda: tab.status.value == "failed"))
        self.assertIn("Connection refused", tab.status_label.get_text())
        self.assertEqual(self.records(), [])
        none = EngineReport(Platform(distro="fedora"), [], build_engines([], Platform()), None)
        missing = self.tab(password="stored", report=none)
        self.assertTrue(pump_until(lambda: missing.status.value == "failed"))
        self.assertIn("dnf install freerdp", missing.status_label.get_text())

    def test_session_limit(self) -> None:
        self.pin()
        self.runtime.set_limit(1)
        first = self.tab(password="stored", lines=[CONNECTED], sleep=30)
        self.assertTrue(pump_until(lambda: first.connected_method == "log"))
        second = self.tab(password="stored")
        self.assertTrue(pump_until(lambda: second.status.value == "failed"))
        self.assertIn("At most 1", second.status_label.get_text())
        first.disconnect()

    def test_close_asks_and_destroy_stops_the_process(self) -> None:
        self.pin()
        tab = self.tab(password="stored", lines=[CONNECTED], child=True, sleep=60)
        self.assertTrue(pump_until(lambda: tab.connected_method == "log"))
        with mock.patch("ssh_manager.gtkui.rdp.tab.confirm", return_value=False) as ask:
            self.assertFalse(tab.confirm_close())
        ask.assert_called_once()
        record = self.records()[0]
        process = tab.process
        tab.destroy()
        self.assertTrue(pump_until(lambda: not process.running, 10))
        self.assertTrue(pump_until(lambda: not fake_freerdp.alive(record["child"]), 10))

    def test_reconnect_restarts(self) -> None:
        self.pin()
        tab = self.tab(password="stored", lines=[CONNECTED], sleep=30)
        self.assertTrue(pump_until(lambda: tab.connected_method == "log"))
        tab.reconnect()
        self.assertTrue(pump_until(lambda: len(self.records()) == 2 and tab.connected_method == "log"))
        tab.disconnect()


@unittest.skipUnless(_display(), "needs a display")
class WindowTests(unittest.TestCase):
    def test_open_from_the_inventory_and_quit_stops_everything(self) -> None:
        from tests.gtk_window_fixture import WindowSandbox

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        record = Path(tmp.name) / "record.jsonl"
        client_path = fake_freerdp.install(Path(tmp.name), "xfreerdp3")
        client = ClientBinary("xfreerdp3", str(client_path), (3, 15, 0), "3.15.0",
                              parse_help_options((FIXTURES / "help-3.15.txt").read_text()))
        platform = Platform("wayland", False, "", "", False, "debian")
        built = build_engines([client], platform)
        sandbox = WindowSandbox(store=True)
        win = sandbox.window
        win.sessions.rdp_discovery.join(10)  # the window's own discovery must not overwrite the fake report
        engines.store_report(EngineReport(platform, [client], built, engines.choose_engine(built)[0]))
        self.addCleanup(engines.store_report, None)
        host = HostEntry("win-01", {"hostname": "10.0.0.50", "user": "alice"}, protocol="rdp",
                         metadata={"rdp": {"v": 1, "cert_policy": "tofu"}})
        win.manager.add_host("staging", "hosts.conf", host)
        win.tree.rebuild(refresh=True)
        stored = win.tree.find_host_by_alias("win-01")
        env = mock.patch.dict(os.environ, fake_freerdp.env_for(record, lines=[CONNECTED], child=True, sleep=60))
        env.start()
        self.addCleanup(env.stop)
        with mock.patch("ssh_manager.gtkui.rdp.tab.probe", return_value=probe_result()), \
                mock.patch.object(win.creds, "resolve_password", return_value=PasswordResolution("pw", "credential")):
            win.sessions.open_hosts([stored])
            tab = win.terminal_tabs.current_tab()
            self.assertEqual(type(tab).__name__, "RdpSessionTab")
            self.assertTrue(pump_until(lambda: tab.connected_method == "log"))
        self.assertEqual(win.tree.host_status.get(win.creds.host_key(stored)), "connected")
        child = fake_freerdp.records(record)[0]["child"]
        with mock.patch.object(win, "_confirm", return_value=True) as ask:
            self.assertFalse(win._on_delete_event())
        self.assertIn("remote desktop", ask.call_args.kwargs["detail"])
        process = tab.process
        sandbox.close()
        self.assertFalse(process.running)
        self.assertTrue(pump_until(lambda: not fake_freerdp.alive(child), 10))


if __name__ == "__main__":
    unittest.main()
