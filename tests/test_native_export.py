"""SSH Manager export (``*.sshm``): the document, determinism, integrity, encryption, the secret audit."""

import base64
import json
import logging
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ophion import native_format as F
from ophion.exporters.native import ExportOptions, build_export, normalize_color, render_export_report
from ophion.persistence import write_bytes_private
from tests.native_fixture import SENTINEL, build_rich, make_store


class ExportCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = make_store(Path(self.tmp.name))
        self.addCleanup(self.store.close)
        self.ids, self.group_colors, self.host_colors, self.creds, _unused = build_rich(self.store)

    def export(self, **kw):
        kw.setdefault("group_colors", self.group_colors)
        kw.setdefault("host_colors", self.host_colors)
        kw.setdefault("credential_for", self.creds.get)
        kw.setdefault("now", 1_800_000_000)
        return build_export(self.store, **kw)

    @staticmethod
    def find(groups, *path):
        node = {"children": groups}
        for name in path:
            node = next(g for g in node["children"] if g["name"] == name)
        return node

    @staticmethod
    def host(group, alias):
        return next(h for h in group["hosts"] if h["alias"] == alias)


class StructureTests(ExportCase):
    def test_tree_hosts_subgroups_empty_groups_and_order(self) -> None:
        doc, report = self.export()
        self.assertEqual(list(doc), ["format", "version", "app_version", "exported_at", "scope", "counts", "options",
                                     "groups", "credentials", "smart_groups", "integrity"])
        self.assertEqual((doc["format"], doc["version"], doc["scope"]), ("ssh-manager-export", 1, "all"))
        self.assertEqual([g["name"] for g in doc["groups"]], ["Files", "Prod", "Windows", "سرورها"])
        prod = self.find(doc["groups"], "Prod")
        self.assertEqual(prod["note"], "production servers")
        self.assertEqual([h["alias"] for h in prod["hosts"]], ["bastion", "web-01"])
        self.assertEqual([g["name"] for g in prod["children"]], ["Databases", "Empty"])
        self.assertEqual(self.find(doc["groups"], "Prod", "Empty"), {"name": "Empty", "note": "", "color": "",
                                                                     "position": 0, "hosts": [], "children": []})
        replicas = self.find(doc["groups"], "Prod", "Databases", "Replicas")
        self.assertEqual(replicas["hosts"][0]["alias"], "db-02")
        self.assertNotIn("port", replicas["hosts"][0])  # no port stored: none exported
        refs = [h["ref"] for g in [self.find(doc["groups"], "Files"), prod, self.find(doc["groups"], "Prod", "Databases"),
                                   replicas, self.find(doc["groups"], "Windows")] for h in g["hosts"]]
        self.assertEqual(refs, [f"h{i}" for i in range(1, 10)])  # sequential in tree order
        self.assertEqual(doc["counts"], {"groups": 7, "hosts": 9, "by_protocol": {
            "ssh": 4, "rdp": 1, "ftp": 1, "ftps": 1, "sftp": 1, "vnc": 1}})
        self.assertEqual((report.groups, report.hosts), (7, 9))
        for gid in self.ids.values():
            self.assertNotIn(gid, json.dumps(doc))  # database ids never leak

    def test_protocols_metadata_and_per_protocol_settings(self) -> None:
        doc, _report = self.export()
        files = self.find(doc["groups"], "Files")
        self.assertEqual({h["alias"]: h["protocol"] for h in files["hosts"]},
                         {"ftp-old": "ftp", "ftps-new": "ftps", "sftp-1": "sftp", "vnc-1": "vnc"})
        self.assertEqual(self.host(files, "ftps-new")["metadata"]["transfer"], {"v": 1, "tls_mode": "implicit"})
        self.assertEqual(self.host(files, "ftp-old")["metadata"]["transfer"],
                         {"v": 1, "anonymous": True, "anonymous_password": "me@x"})
        self.assertEqual(self.host(files, "vnc-1")["metadata"], {"future": {"x": 1}})  # unknown keys kept
        web = self.host(self.find(doc["groups"], "Prod"), "web-01")
        self.assertEqual((web["hostname"], web["port"], web["user"]), ("web1.example.org", 2222, "deploy"))
        self.assertEqual(web["options"], {"proxyjump": "jump@bastion.example.org", "identityfile": "~/.ssh/id_ed25519",
                                          "proxycommand": "sshpass -p <removed> ssh -W %h:%p jump"})
        self.assertEqual(web["metadata"], {"tags": ["web", "frontend"], "notes": "main site",
                                           "environment": "production", "owner": "ops", "service": "nginx",
                                           "permissions": {"read_only": True},
                                           "default_session_profile_id": "profile-1"})
        no_notes, _ = self.export(options=ExportOptions(notes=False))
        web = self.host(self.find(no_notes["groups"], "Prod"), "web-01")
        self.assertNotIn("tags", web["metadata"])
        self.assertNotIn("notes", web["metadata"])
        self.assertEqual(self.find(no_notes["groups"], "Prod")["note"], "")

    def test_colours(self) -> None:
        doc, report = self.export()
        self.assertEqual(self.find(doc["groups"], "Prod")["color"], "#AA0000")
        self.assertEqual(self.find(doc["groups"], "Prod", "Databases")["color"], "#00AA00")
        self.assertEqual(self.find(doc["groups"], "Prod", "Databases", "Replicas")["color"], "#0000AA")
        self.assertEqual(self.find(doc["groups"], "Windows")["color"], "")  # unreadable: left out, counted
        self.assertEqual(self.host(self.find(doc["groups"], "Prod", "Databases", "Replicas"), "db-02")["color"],
                         "#123456")
        self.assertEqual(self.host(self.find(doc["groups"], "Files"), "sftp-1")["color"], "#ABCDEF")
        self.assertEqual((report.colors, report.colors_invalid), (5, 1))
        none, _ = self.export(options=ExportOptions(colors=False))
        self.assertEqual(self.find(none["groups"], "Prod")["color"], "")
        for raw, expected in (("#abc", "#AABBCC"), ("rgb(255,0,16)", "#FF0010"), ("rgba(0,0,0,0.5)", "#00000080"),
                              ("#11223344", "#11223344"), ("red", None), ("rgb(300,0,0)", None), ("", None)):
            self.assertEqual(normalize_color(raw), expected, raw)

    def test_credentials_and_tunnels(self) -> None:
        doc, report = self.export()
        self.assertEqual(doc["credentials"], [{"ref": "c1", "name": "Ops account", "username": "deploy"},
                                              {"ref": "c2", "name": "Postgres", "username": "postgres"}])
        prod = self.find(doc["groups"], "Prod")
        self.assertEqual(self.host(prod, "web-01")["credential"], {"ref": "c1", "mode": "shared"})
        self.assertEqual(self.host(self.find(doc["groups"], "Files"), "sftp-1")["credential"],
                         {"ref": "c1", "mode": "shared"})  # deduplicated
        self.assertEqual(self.host(prod, "bastion")["credential"], {"mode": "host-specific"})
        rdp = self.host(self.find(doc["groups"], "Windows"), "dc-01")["metadata"]["rdp"]
        self.assertEqual(rdp["via_ssh_ref"], self.host(prod, "bastion")["ref"])
        self.assertNotIn("via_ssh", rdp)
        self.assertEqual(rdp["gateway"], {"host": "", "port": 443})
        self.assertEqual((report.credentials, report.host_specific), (2, 1))
        # A tunnel host outside the export: the tunnel is dropped, with a warning.
        only_windows, report = self.export(scope="groups", group_id=self.ids["windows"])
        self.assertEqual([g["name"] for g in only_windows["groups"]], ["Windows"])
        self.assertNotIn("via_ssh_ref", self.host(only_windows["groups"][0], "dc-01")["metadata"]["rdp"])
        self.assertTrue(any("tunnel" in w for w in report.warnings))
        no_refs, _ = self.export(options=ExportOptions(credential_refs=False))
        self.assertEqual(no_refs["credentials"], [])
        self.assertNotIn("credential", self.host(self.find(no_refs["groups"], "Prod"), "web-01"))

    def test_scopes(self) -> None:
        sub, _ = self.export(scope="groups", group_id=self.ids["databases"])
        self.assertEqual([g["name"] for g in sub["groups"]], ["Databases"])  # the subtree becomes top level
        self.assertEqual(sub["groups"][0]["children"][0]["name"], "Replicas")
        self.assertEqual(sub["groups"][0]["color"], "#00AA00")
        hosts, _ = self.export(scope="hosts", host_ids=[self.ids["db2"], self.ids["sftp"]])
        self.assertEqual([g["name"] for g in hosts["groups"]], ["Files", "Prod"])
        self.assertEqual([h["alias"] for h in self.find(hosts["groups"], "Files")["hosts"]], ["sftp-1"])
        self.assertEqual(self.find(hosts["groups"], "Prod")["hosts"], [])
        self.assertEqual([g["name"] for g in self.find(hosts["groups"], "Prod")["children"]], ["Databases"])
        self.assertEqual(hosts["counts"]["hosts"], 2)
        with self.assertRaises(LookupError):
            self.export(scope="groups", group_id="missing")

    def test_deterministic_and_integrity(self) -> None:
        first, _ = self.export()
        second, _ = self.export(now=1_900_000_000)
        self.assertNotEqual(first["exported_at"], second["exported_at"])
        second["exported_at"] = first["exported_at"]
        self.assertEqual(F.dump_document(first), F.dump_document(second))
        raw = F.dump_document(first)
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
        self.assertNotIn(b"\r\n", raw)
        self.assertEqual(F.check_integrity(first), "ok")
        edited = json.loads(raw)
        edited["groups"][0]["name"] = "Changed"
        self.assertEqual(F.check_integrity(edited), "mismatch")
        edited.pop("integrity")
        self.assertEqual(F.check_integrity(edited), "missing")
        relabelled = json.loads(raw)
        relabelled["exported_at"] = "2030-01-01T00:00:00Z"  # not hashed
        self.assertEqual(F.check_integrity(relabelled), "ok")


class EncryptionTests(unittest.TestCase):
    PLAIN = b'{"format": "ssh-manager-export"}\n'

    def test_round_trip_and_failures(self) -> None:
        encrypted = F.encrypt_document(self.PLAIN, "correct horse")
        header = json.loads(encrypted)
        self.assertEqual(set(header), {"format", "version", "kdf", "cipher", "nonce", "data"})
        self.assertEqual((header["format"], header["cipher"], header["kdf"]["name"]),
                         ("ssh-manager-export-encrypted", "aes-256-gcm", "pbkdf2-sha256"))
        self.assertGreaterEqual(header["kdf"]["iterations"], 600_000)
        self.assertEqual(len(base64.b64decode(header["kdf"]["salt"])), 16)
        self.assertNotIn(b"ssh-manager-export\"}", encrypted.replace(b"ssh-manager-export-encrypted", b""))
        self.assertEqual(F.decrypt_document(header, "correct horse"), self.PLAIN)
        with self.assertRaises(F.NativeFormatError) as caught:
            F.decrypt_document(header, "wrong")
        self.assertEqual(caught.exception.code, "wrong_passphrase")
        tampered = dict(header, nonce=base64.b64encode(os.urandom(12)).decode())
        with self.assertRaises(F.NativeFormatError):
            F.decrypt_document(tampered, "correct horse")
        data = bytearray(base64.b64decode(header["data"]))
        data[0] ^= 1
        with self.assertRaises(F.NativeFormatError):
            F.decrypt_document(dict(header, data=base64.b64encode(bytes(data)).decode()), "correct horse")
        weak = dict(header, kdf=dict(header["kdf"], iterations=1000))
        with self.assertRaises(F.NativeFormatError) as caught:
            F.decrypt_document(weak, "correct horse")
        self.assertEqual(caught.exception.code, "damaged")
        with self.assertRaises(F.NativeFormatError) as caught:
            F.decrypt_document(dict(header, version=2), "correct horse")
        self.assertEqual(caught.exception.code, "newer")

    def test_the_iteration_count_is_read_from_the_file(self) -> None:
        encrypted = json.loads(F.encrypt_document(self.PLAIN, "pw", iterations=650_000))
        self.assertEqual(encrypted["kdf"]["iterations"], 650_000)
        with mock.patch.object(F, "_key", wraps=F._key) as key:
            self.assertEqual(F.decrypt_document(encrypted, "pw"), self.PLAIN)
        self.assertEqual(key.call_args.args[2], 650_000)
        changed = dict(encrypted, kdf=dict(encrypted["kdf"], iterations=700_000))  # header is authenticated too
        with self.assertRaises(F.NativeFormatError):
            F.decrypt_document(changed, "pw")


class WriteTests(unittest.TestCase):
    def test_private_atomic_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "out.sshm"
            write_bytes_private(target, b"data")
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            self.assertEqual(os.listdir(tmp), ["out.sshm"])
            with mock.patch("os.replace", side_effect=OSError(28, "No space left on device")):
                with self.assertRaises(OSError):
                    write_bytes_private(Path(tmp) / "second.sshm", b"x")
            self.assertEqual(os.listdir(tmp), ["out.sshm"])  # no temporary file left
        self.assertRegex(F.default_file_name(0), r"^ophion-\d{8}-\d{4}\.ophion$")


class SecretAuditTests(ExportCase):
    def test_no_secret_reaches_the_file_the_report_or_the_log(self) -> None:
        with self.assertLogs(level=logging.DEBUG) as logs:
            logging.getLogger("ophion").debug("export")
            doc, report = self.export()
            raw = F.dump_document(doc)
            text = render_export_report(report)
            encrypted = F.encrypt_document(raw, "pw")
        for blob in (raw, text.encode(), "\n".join(logs.output).encode(), encrypted):
            self.assertNotIn(SENTINEL.encode(), blob)
            self.assertNotIn(base64.b64encode(SENTINEL.encode()), blob)
        self.assertNotIn(b"gw-", raw)
        self.assertIn("web-01: proxycommand", report.flagged)
        self.assertIn("web-01: ~/.ssh/id_ed25519", report.identity_files)
        for what in ("option password", "option pwd", "secret inside option proxycommand", "FTPS certificate pin",
                     "RDP certificate pin", "transfer.ftp_password", "rdp.gateway_password"):
            self.assertIn(what, report.removed, what)
        self.assertIn("Removed (could hold a secret)", text)


if __name__ == "__main__":
    unittest.main()
