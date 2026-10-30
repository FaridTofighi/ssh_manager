"""Port forwards: model, validation, metadata round trip, parsing ssh's *Forward options (no GTK)."""

import tempfile
import unittest
from pathlib import Path

from ssh_manager.tunnels import model as M


def fwd(**kw) -> M.Forward:
    base = dict(name="db", kind=M.LOCAL, bind_port=15432, dest_host="db.internal", dest_port=5432)
    base.update(kw)
    return M.Forward(**base)


class ModelTests(unittest.TestCase):
    def test_defaults_and_round_trip_keep_unknown_keys(self) -> None:
        forward = fwd()
        self.assertEqual(forward.bind_host, "127.0.0.1")
        self.assertTrue(forward.id)
        data = forward.to_dict()
        data["future_field"] = {"x": 1}
        again = M.Forward.from_dict(data)
        self.assertEqual(again.to_dict(), data)
        self.assertEqual(again.stop_with_last_session, False)
        self.assertTrue(fwd(auto_start_with_session=True).stop_with_last_session)  # default on for with-session

    def test_garbage_metadata_is_tolerated(self) -> None:
        for metadata in ({}, {"forwards": "nope"}, {"forwards": [1, None, "x", {"kind": "teleport"},
                                                                 {"kind": "local", "bind_port": "abc"}]}):
            with self.subTest(metadata=metadata):
                forwards = M.forwards_from_metadata(metadata)
                self.assertTrue(all(isinstance(f, M.Forward) for f in forwards))
        kept = M.forwards_from_metadata({"forwards": [{"kind": "local", "bind_port": "8080", "dest_host": "a",
                                                       "dest_port": 80, "id": "x1"}]})
        self.assertEqual((kept[0].bind_port, kept[0].id), (8080, "x1"))

    def test_set_forwards_keeps_other_metadata(self) -> None:
        metadata = {"tags": ["a"], "forwards": [{"id": "old"}]}
        updated = M.set_forwards(metadata, [fwd(id="n1")])
        self.assertEqual(updated["tags"], ["a"])
        self.assertEqual([f["id"] for f in updated["forwards"]], ["n1"])
        self.assertNotIn("forwards", M.set_forwards(metadata, []))
        self.assertEqual(metadata["forwards"], [{"id": "old"}])  # the input is not changed

    def test_addresses(self) -> None:
        self.assertEqual(fwd().listen_address, "127.0.0.1:15432")
        self.assertEqual(fwd(kind=M.DYNAMIC, bind_port=1080).copy_address, "socks5://127.0.0.1:1080")
        self.assertEqual(fwd(bind_host="::1").listen_address, "[::1]:15432")
        self.assertEqual(fwd().destination, "db.internal:5432")
        self.assertEqual(fwd(kind=M.DYNAMIC).destination, "")


class ValidationTests(unittest.TestCase):
    def errors(self, forward, others=(), host_id="h1"):
        return M.validate(forward, host_id=host_id, others=others).errors

    def test_ports_and_hosts(self) -> None:
        self.assertEqual(self.errors(fwd()), [])
        for bad in (dict(bind_port=0), dict(bind_port=70000), dict(dest_port=0), dict(dest_host=""),
                    dict(dest_host="bad host"), dict(bind_host="no spaces please"), dict(kind="teleport")):
            with self.subTest(bad=bad):
                self.assertTrue(self.errors(fwd(**bad)))
        self.assertEqual(self.errors(fwd(kind=M.DYNAMIC, dest_host="", dest_port=0)), [])
        result = M.validate(fwd(bind_port=80), host_id="h1")
        self.assertEqual(result.errors, [])
        self.assertTrue(any("1024" in w for w in result.warnings))

    def test_non_loopback_needs_the_flag(self) -> None:
        self.assertTrue(self.errors(fwd(bind_host="0.0.0.0")))
        result = M.validate(fwd(bind_host="0.0.0.0", allow_remote_bind=True), host_id="h1")
        self.assertEqual(result.errors, [])
        self.assertTrue(result.warnings)
        for loopback in ("127.0.0.1", "127.0.0.2", "::1", "localhost"):
            self.assertEqual(self.errors(fwd(bind_host=loopback)), [], loopback)

    def test_duplicates(self) -> None:
        other = fwd(id="o1", name="other")
        self.assertTrue(self.errors(fwd(), others=[("h2", other)]))  # local: across the inventory
        self.assertEqual(self.errors(fwd(id="o1"), others=[("h2", other)]), [])  # itself (edit)
        self.assertTrue(self.errors(fwd(kind=M.DYNAMIC, dest_host="", dest_port=0), others=[("h2", other)]))
        wildcard = fwd(id="o2", bind_host="0.0.0.0", allow_remote_bind=True)
        self.assertTrue(self.errors(fwd(), others=[("h3", wildcard)]))  # 0.0.0.0 takes every address
        remote = fwd(kind=M.REMOTE, id="r1")
        self.assertEqual(self.errors(fwd(kind=M.REMOTE), others=[("h2", remote)]), [])  # another server
        self.assertTrue(self.errors(fwd(kind=M.REMOTE), others=[("h1", remote)]))  # the same server
        self.assertEqual(self.errors(fwd(kind=M.REMOTE), others=[("h2", other)]), [])


class SshOptionTests(unittest.TestCase):
    def test_parse(self) -> None:
        cases = {
            ("LocalForward", "8080 db:5432"): (M.LOCAL, "127.0.0.1", 8080, "db", 5432),
            ("LocalForward", "0.0.0.0:8080 db:5432"): (M.LOCAL, "0.0.0.0", 8080, "db", 5432),
            ("LocalForward", "[::1]:8080 [fd00::5]:22"): (M.LOCAL, "::1", 8080, "fd00::5", 22),
            ("RemoteForward", "9000 localhost:3000"): (M.REMOTE, "127.0.0.1", 9000, "localhost", 3000),
            ("DynamicForward", "1080"): (M.DYNAMIC, "127.0.0.1", 1080, "", 0),
            ("dynamicforward", "localhost:1081"): (M.DYNAMIC, "localhost", 1081, "", 0),
        }
        for (key, value), expected in cases.items():
            with self.subTest(key=key, value=value):
                forward = M.parse_ssh_forward(key, value)
                self.assertEqual((forward.kind, forward.bind_host, forward.bind_port, forward.dest_host,
                                  forward.dest_port), expected)
        for key, value in (("LocalForward", "nonsense"), ("LocalForward", "8080"), ("DynamicForward", "x"),
                           ("LocalForward", "/tmp/sock /tmp/other"), ("ProxyJump", "a")):
            self.assertIsNone(M.parse_ssh_forward(key, value))

    def test_raw_options_of_a_host(self) -> None:
        options = {"hostname": "x", "LocalForward": "8080 db:5432", "dynamicforward": "1080",
                   "RemoteForward": ["9000 localhost:3000", "bad"]}
        raw = M.raw_forward_options(options)
        self.assertEqual([(k, v) for k, v, _f in raw],
                         [("LocalForward", "8080 db:5432"), ("dynamicforward", "1080"),
                          ("RemoteForward", "9000 localhost:3000"), ("RemoteForward", "bad")])
        self.assertIsNone(raw[-1][2])


class StoreTests(unittest.TestCase):
    def test_forwards_survive_the_inventory_store(self) -> None:
        from ssh_manager.inventory.manager import StoreConfigManager
        from ssh_manager.inventory.store import InventoryStore, NewHost

        with tempfile.TemporaryDirectory() as tmp:
            store = InventoryStore(Path(tmp) / "inventory.db")
            self.addCleanup(store.close)
            group = store.create_group(None, "G")
            forwards = [fwd(id="f1").to_dict() | {"future": True}]
            store.add_host(NewHost(group.id, "web", "web.example", 22, "u", metadata={"forwards": forwards}))
            manager = StoreConfigManager(store)
            manager.refresh()
            host = next(h for c in manager.tree.values() for g in c.values() for h in g.hosts)
            self.assertEqual(host.metadata["forwards"], forwards)
            self.assertEqual(M.forwards_from_metadata(host.metadata)[0].extra, {"future": True})


if __name__ == "__main__":
    unittest.main()
