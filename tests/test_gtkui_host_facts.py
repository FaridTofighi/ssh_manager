import json
import tempfile
import unittest
from pathlib import Path

from ssh_manager.config import HostEntry
from ssh_manager.gtkui.constants import RowKind
from ssh_manager.gtkui.host_facts import (
    AuthKind,
    LatencyLevel,
    address_text,
    auth_summary,
    breadcrumb_for,
    connection_text,
    format_latency,
    format_relative_time,
    key_type_for,
    latency_level,
    proxy_text,
    ssh_command_text,
    uses_proxy,
)
from ssh_manager.gtkui.host_history import EVENT_LIMIT, HostHistory
from ssh_manager.gtkui.latency import LatencyProbe


def host(**options):
    return HostEntry(alias="api-prod-01", options=options, parent_group="Production", child_group="Core services")


class RelativeTimeTests(unittest.TestCase):
    def test_buckets(self) -> None:
        now = 1_800_000_000.0
        cases = [
            (None, "Never"),
            (now - 10, "just now"),
            (now - 60, "1 minute ago"),
            (now - 5 * 60, "5 minutes ago"),
            (now - 2 * 3600, "2 hours ago"),
            (now - 26 * 3600, "yesterday"),
            (now - 3 * 86400, "3 days ago"),
            (now - 21 * 86400, "3 weeks ago"),
            (now - 90 * 86400, "3 months ago"),
            (now - 800 * 86400, "2 years ago"),
            (now + 50, "just now"),  # clock skew
        ]
        for ts, expected in cases:
            with self.subTest(ts=ts):
                self.assertEqual(format_relative_time(ts, now), expected)


class LatencyFormatTests(unittest.TestCase):
    def test_levels_and_text(self) -> None:
        self.assertIs(latency_level(24), LatencyLevel.GOOD)
        self.assertIs(latency_level(150), LatencyLevel.WARN)
        self.assertIs(latency_level(450), LatencyLevel.BAD)
        self.assertIs(latency_level(None), LatencyLevel.UNKNOWN)
        self.assertIs(latency_level(None, failed=True), LatencyLevel.BAD)
        self.assertEqual(format_latency(23.6), "24 ms")
        self.assertEqual(format_latency(0.2), "1 ms")
        self.assertEqual(format_latency(None), "—")
        self.assertEqual(format_latency(None, failed=True), "Timeout")


class AuthTests(unittest.TestCase):
    def test_key_type_from_pub_file_then_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / "work_key"
            Path(f"{key}.pub").write_text("ssh-ed25519 AAAAC3Nz user@box\n", encoding="utf-8")
            self.assertEqual(key_type_for(str(key)), "ed25519")
        self.assertEqual(key_type_for("~/.ssh/id_rsa", read_text=lambda p: (_ for _ in ()).throw(OSError())), "RSA")
        self.assertEqual(key_type_for("/nonexistent/id_ecdsa"), "ECDSA")
        self.assertEqual(key_type_for(""), "")

    def test_priority(self) -> None:
        keyed = host(identityfile="/nonexistent/id_ed25519")
        summary = auth_summary(keyed)
        self.assertEqual((summary.kind, summary.label, summary.caption), (AuthKind.KEY, "SSH key", "Using ed25519 authentication"))
        self.assertEqual(auth_summary(keyed, credential_name="ops").label, "Credential: ops")
        self.assertEqual(auth_summary(host(), has_password=True).caption, "Using password authentication")
        self.assertEqual(auth_summary(host()).label, "Agent")
        self.assertEqual(auth_summary(host(identityfile="/x/mykey"), key_type="").caption, "Using SSH key")


class ConnectionTextTests(unittest.TestCase):
    def test_strings(self) -> None:
        h = host(hostname="10.60.110.98", port="22")
        self.assertEqual(address_text(h, "deploy"), "deploy@10.60.110.98")
        self.assertEqual(connection_text(h, "deploy"), "deploy@10.60.110.98:22")
        self.assertEqual(ssh_command_text(h, "deploy"), "ssh deploy@10.60.110.98")
        self.assertEqual(ssh_command_text(host(hostname="h", port="2222"), ""), "ssh h -p 2222")
        self.assertEqual(proxy_text(h), "None")
        self.assertFalse(uses_proxy(h))
        jumped = host(hostname="x", proxyjump="bastion")
        self.assertEqual(proxy_text(jumped), "bastion")
        self.assertTrue(uses_proxy(jumped))
        self.assertEqual(proxy_text(host(proxycommand="nc %h %p")), "ProxyCommand")


class BreadcrumbTests(unittest.TestCase):
    def test_trails(self) -> None:
        self.assertEqual([c.label for c in breadcrumb_for(None)], ["Hosts"])
        trail = breadcrumb_for(RowKind.HOST, "Production", "Core services", "api-prod-01")
        self.assertEqual([c.label for c in trail], ["Hosts", "api-prod-01"])
        self.assertTrue(trail[-1].current)
        self.assertEqual(trail[0].target, ())
        trail = breadcrumb_for(RowKind.CHILD, "Production", "db/primary.conf")
        self.assertEqual([c.label for c in trail], ["Hosts", "Production", "db", "primary.conf"])
        self.assertEqual(trail[2].target, ("child", "Production", "db"))
        self.assertEqual([c.current for c in trail], [False, False, False, True])


class LatencyProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = [100.0]
        self.queue = []
        self.connects = []

        def connect(host, port, timeout):
            self.connects.append((host, port))
            self.now[0] += 0.024
            if host == "down":
                raise OSError("unreachable")

        self.probe = LatencyProbe(
            clock=lambda: self.now[0],
            connect=connect,
            run=lambda fn: fn(),
            dispatch=self.queue.append,
        )

    def _flush(self) -> None:
        while self.queue:
            self.queue.pop(0)()

    def test_measures_caches_and_refreshes(self) -> None:
        results = []
        self.assertTrue(self.probe.request(("api", 22), results.append))
        self.assertFalse(self.probe.request(("api", 22), results.append))  # pending
        self._flush()
        self.assertEqual(round(results[0].ms), 24)
        self.assertFalse(self.probe.request(("api", 22), results.append))  # fresh
        self.now[0] += 31
        self.assertTrue(self.probe.request(("api", 22), results.append))
        self._flush()
        self.assertEqual(len(self.connects), 2)

    def test_failures_disabled_and_shutdown(self) -> None:
        results = []
        self.probe.request(("down", 22), results.append)
        self._flush()
        self.assertTrue(results[0].failed)
        self.probe.enabled = False
        self.assertFalse(self.probe.request(("api", 22), results.append, force=True))
        self.probe.enabled = True
        self.probe.request(("api", 22), results.append)
        self.probe.shutdown()
        self._flush()
        self.assertEqual(len(results), 1)  # late result dropped
        self.assertIsNone(self.probe.cached(("api", 22)))


class HostHistoryTests(unittest.TestCase):
    def test_round_trip_bounds_and_corruption(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "host-history.json"
            clock = [1000.0]
            history = HostHistory(path, clock=lambda: clock[0])
            seen = []
            history.add_listener(seen.append)
            history.record_connected("k", text="Connected")
            for i in range(EVENT_LIMIT + 5):
                history.record_event("k", "info", f"event {i}")
            again = HostHistory(path)
            self.assertEqual(again.last_connected("k"), 1000.0)
            self.assertEqual(len(again.events("k")), EVENT_LIMIT)
            self.assertEqual(again.events("k")[0].text, f"event {EVENT_LIMIT + 4}")
            self.assertIn("k", seen)
            again.rename("k", "k2")
            self.assertIsNone(again.last_connected("k"))
            self.assertEqual(HostHistory(path).last_connected("k2"), 1000.0)

            path.write_text("{not json", encoding="utf-8")
            self.assertIsNone(HostHistory(path).last_connected("k2"))
            path.write_text(json.dumps({"hosts": {"x": {"last_connected": "soon", "events": [1, {"ts": 1}]}}}), encoding="utf-8")
            broken = HostHistory(path)
            self.assertIsNone(broken.last_connected("x"))
            self.assertEqual(broken.events("x"), [])


if __name__ == "__main__":
    unittest.main()
