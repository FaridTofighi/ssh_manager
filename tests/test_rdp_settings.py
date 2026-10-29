"""RDP settings (``metadata["rdp"]``), user names, extra options, shared folders (pure, synthetic data)."""

import os
import tempfile
import unittest

from ssh_manager import protocols
from ssh_manager.config import HostEntry
from ssh_manager.rdp.settings import (
    KEYBOARD_LAYOUTS, PRESET_TOGGLES, Gateway, RdpIdentity, RdpSettings, SharedFolder, extra_args_problems,
    keyboard_layout_id, resolve_identity, shared_folder_problems, split_rdp_user,
)
from ssh_manager.trust import TrustStore, pin_key


class RegistryTests(unittest.TestCase):
    def test_rdp_is_registered(self) -> None:
        kind = protocols.get("rdp")
        self.assertEqual((kind.label, kind.badge, kind.default_port, kind.icon, kind.family),
                         ("RDP", "RDP", 3389, "sshm-desktop", "desktop"))
        self.assertFalse(kind.terminal or kind.supports_keys or kind.supports_jump_host)
        self.assertEqual(kind.settings_defaults()["v"], 1)
        host = HostEntry("win", {"hostname": "10.0.0.50"}, protocol="rdp")
        self.assertEqual(protocols.badge_for(host), "RDP")
        self.assertEqual(kind.validate(host), [])
        self.assertTrue(kind.validate(HostEntry("win", {}, protocol="rdp")))  # a host name is required

    def test_filter_tooltip_and_export(self) -> None:
        from ssh_manager.gtkui.controllers.tree_controller import filter_host, host_tooltip, parse_filter
        from ssh_manager.config import GroupConfig
        from ssh_manager.inventory.openssh import non_ssh_hosts

        host = HostEntry("win", {"hostname": "10.0.0.50", "port": "3390"}, protocol="rdp")
        self.assertTrue(filter_host(host, parse_filter("proto:rdp")[0]))
        self.assertFalse(filter_host(HostEntry("web"), parse_filter("proto:rdp")[0]))
        tip = host_tooltip(host, "alice", "unknown")
        self.assertIn("10.0.0.50:3390", tip)
        self.assertIn("Protocol: RDP", tip)
        self.assertNotIn(":3389", host_tooltip(HostEntry("w", {"hostname": "h"}, protocol="rdp"), "", "unknown"))
        from pathlib import Path

        tree = {"Acme": {"": GroupConfig("Acme", "", Path(), [host, HostEntry("web")])}}
        self.assertEqual(non_ssh_hosts(tree), ["Acme · win"])  # OpenSSH export leaves it out


class SettingsTests(unittest.TestCase):
    def test_defaults_are_safe(self) -> None:
        s = RdpSettings()
        self.assertEqual((s.security, s.cert_policy, s.clipboard), ("auto", "ask", "both"))
        self.assertFalse(s.home_drive or s.printers or s.smartcard or s.audio_in or s.drives or s.usb)
        self.assertTrue(s.auto_reconnect)
        self.assertEqual((s.connect_timeout_s, s.reconnect_max), (15, 5))

    def test_round_trip_and_unknown_keys(self) -> None:
        s = RdpSettings(domain="CORP", drives=[SharedFolder("work", "/tmp", True)], monitors=[0, 1],
                        gateway=Gateway("gw.example.com", 4443, "bob", "CORP", False, "http"))
        data = s.to_json()
        data["from_the_future"] = {"x": 1}
        again, warnings = RdpSettings.from_json(data)
        self.assertEqual(warnings, [])
        self.assertEqual(again.to_json(), data)  # the unknown key survives
        self.assertEqual(again.gateway.port, 4443)
        self.assertEqual(again.drives[0], SharedFolder("work", "/tmp", True))

    def test_invalid_values_fall_back_with_warnings(self) -> None:
        s, warnings = RdpSettings.from_json({"v": 1, "security": "magic", "width": 50, "color_depth": 7,
                                             "clipboard": 3, "cert_policy": "ignore", "drives": "nope",
                                             "pinned_sha256": ["zz", "AB" * 32], "domain": "bad\nvalue"})
        self.assertEqual((s.security, s.width, s.color_depth, s.clipboard, s.drives), ("auto", 1920, 32, "both", []))
        self.assertEqual(s.cert_policy, "ignore")  # valid, kept
        self.assertEqual(s.pinned_sha256, [":".join(["AB"] * 32)])
        self.assertEqual(s.domain, "")
        self.assertGreaterEqual(len(warnings), 6)
        for junk in (None, "", [], "text", 5):
            settings, _w = RdpSettings.from_json(junk)
            self.assertEqual(settings.to_json(), RdpSettings().to_json())

    def test_for_host(self) -> None:
        host = HostEntry("w", {}, protocol="rdp", metadata={"rdp": {"v": 1, "domain": "ACME"}})
        self.assertEqual(RdpSettings.for_host(host)[0].domain, "ACME")

    def test_presets_set_toggles(self) -> None:
        s = RdpSettings()
        s.apply_preset("modem")
        self.assertFalse(any(getattr(s, name) for name in PRESET_TOGGLES["modem"]))
        s.apply_preset("lan")
        self.assertTrue(all(getattr(s, name) for name in PRESET_TOGGLES["lan"]))
        s.wallpaper = False
        s.apply_preset("custom")
        self.assertFalse(s.wallpaper)

    def test_problems(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ok = RdpSettings(drives=[SharedFolder("a", tmp)])
            self.assertEqual(ok.problems(), [])
            bad = RdpSettings(size_mode="fixed", width=100, height=100, domain="CO/RP",
                              drives=[SharedFolder("a", tmp), SharedFolder("A", tmp),
                                      SharedFolder("bad name!", "relative"), SharedFolder("c", os.path.join(tmp, "x"))],
                              gateway=Gateway(user="bob", use_same_credentials=False))
            text = " ".join(bad.problems())
            for expected in ("resolution", "domain", "same name", "letters", "absolute", "does not exist",
                             "gateway host"):
                self.assertIn(expected, text)

    def test_keyboard_layouts(self) -> None:
        self.assertEqual(keyboard_layout_id("fa-IR"), "0x00000429")
        self.assertEqual(keyboard_layout_id("auto"), "")
        self.assertIn("en-US", [layout[0] for layout in KEYBOARD_LAYOUTS])


class UserNameTests(unittest.TestCase):
    def test_split_rdp_user_table(self) -> None:
        cases = {
            "DOM\\user": RdpIdentity("user", "DOM"),
            "user@dom.local": RdpIdentity("user@dom.local", upn=True),
            "user": RdpIdentity("user"),
            "": RdpIdentity(""),
            "   ": RdpIdentity(""),
            "  DOM\\user  ": RdpIdentity("user", "DOM"),
            ".\\admin": RdpIdentity("admin", "."),
            "\\user": RdpIdentity("user", ""),
            "@nope": RdpIdentity("@nope"),
        }
        for text, expected in cases.items():
            self.assertEqual(split_rdp_user(text), expected, repr(text))
        for bad in ("a\\b\\c", "DOM\\", "us\ter", "x" * 300):
            self.assertTrue(split_rdp_user(bad).problem, repr(bad))

    def test_resolve_identity(self) -> None:
        self.assertEqual(resolve_identity("CORP\\alice", RdpSettings()), RdpIdentity("alice", "CORP"))
        self.assertEqual(resolve_identity("CORP\\alice", RdpSettings(domain="ACME")), RdpIdentity("alice", "ACME"))
        self.assertEqual(resolve_identity("CORP\\alice", RdpSettings(use_credential_domain=False)),
                         RdpIdentity("alice", ""))
        self.assertEqual(resolve_identity("alice", RdpSettings(domain="ACME")), RdpIdentity("alice", "ACME"))
        self.assertEqual(resolve_identity("alice@acme.example", RdpSettings(domain="ACME")),
                         RdpIdentity("alice@acme.example", upn=True))  # a UPN never gets a domain


class ExtraArgsTests(unittest.TestCase):
    def test_allow_list(self) -> None:
        self.assertEqual(extra_args_problems("+async-update /gdi:hw /tls:seclevel:0 /async-update -heartbeat"), [])
        for bad in ("/gdi:zz", "/frame-ack:abc", "+unknown-thing", "/jpeg-quality:1000"):
            self.assertTrue(extra_args_problems(bad), bad)

    def test_forbidden_even_when_unchecked(self) -> None:
        for bad in ("/p:x", "/u:me", "/v:evil:3389", "/args-from:stdin", "/cert:ignore", "/cert-ignore", "/gp:x",
                    "/a:somedll", "/vc:x", "/proxy:http://u:p@h", "/rdp2tcp:/bin/sh", "/action-script:/tmp/x",
                    "/gateway:g:h,p:x", "/P:upper"):
            self.assertTrue(extra_args_problems(bad, unchecked=True), bad)
        self.assertEqual(extra_args_problems("/whatever:1 +thing", unchecked=True), [])

    def test_messages_never_echo_values(self) -> None:
        for text in ("/p:hunter2", "hunter2", "/gdi:hunter2", "/x:hunter2"):
            self.assertNotIn("hunter2", " ".join(extra_args_problems(text)), text)

    def test_control_characters_and_quotes(self) -> None:
        self.assertTrue(extra_args_problems("+async-update\n/p:x"))
        self.assertTrue(extra_args_problems("'unterminated"))
        self.assertEqual(extra_args_problems('/timezone:"W. Europe Standard Time"'), [])


class SharedFolderTests(unittest.TestCase):
    def test_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(shared_folder_problems(SharedFolder("docs", tmp)), [])
            self.assertTrue(shared_folder_problems(SharedFolder("docs", tmp + ",evil")))
            self.assertTrue(shared_folder_problems(SharedFolder("", tmp)))
            file_path = os.path.join(tmp, "f")
            open(file_path, "w").close()
            self.assertTrue(shared_folder_problems(SharedFolder("f", file_path)))  # not a folder


class TrustStoreTests(unittest.TestCase):
    def test_scoped_keys_do_not_mix_and_ftps_keys_are_unchanged(self) -> None:
        self.assertEqual(pin_key("h1", "Files.Example", 21), "h1|files.example:21")
        self.assertEqual(pin_key("h1", "win", 3389, scope="rdp"), "rdp:h1|win:3389")
        with tempfile.TemporaryDirectory() as tmp:
            store = TrustStore(os.path.join(tmp, "pins.json"))
            store.set(pin_key("h1", "srv", 443), "AA" * 32)
            self.assertEqual(store.get(pin_key("h1", "srv", 443, scope="rdp")), "")
            from ssh_manager.transfer.pins import PinStore

            self.assertIs(PinStore, TrustStore)


if __name__ == "__main__":
    unittest.main()
