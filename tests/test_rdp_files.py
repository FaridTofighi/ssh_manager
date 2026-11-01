""".rdp files (import, export, round trip, hostile files) and Wake-on-LAN packets (pure; synthetic data only)."""

import codecs
import os
import tempfile
import unittest
from pathlib import Path

from ophion.rdp.rdpfile import RdpFileError, decode, encode_rdp, export_rdp, import_rdp, parse_lines
from ophion.rdp.settings import Gateway, RdpSettings, RemoteApp, SharedFolder
from ophion.rdp.wol import broadcast_ok, magic_packet, parse_mac, wake

SAMPLE = "\r\n".join([
    "screen mode id:i:2",
    "use multimon:i:1",
    "selectedmonitors:s:0,1",
    "desktopwidth:i:1920",
    "desktopheight:i:1080",
    "session bpp:i:16",
    "compression:i:1",
    "keyboardhook:i:2",
    "audiocapturemode:i:1",
    "connection type:i:5",
    "networkautodetect:i:0",
    "full address:s:win-01.acme.test:3390",
    "audiomode:i:1",
    "redirectprinters:i:1",
    "redirectcomports:i:1",
    "redirectsmartcards:i:1",
    "redirectclipboard:i:0",
    "drivestoredirect:s:C:\\;*",
    "autoreconnection enabled:i:1",
    "authentication level:i:2",
    "prompt for credentials:i:1",
    "enablecredsspsupport:i:1",
    "alternate shell:s:C:\\Tools\\app.exe",
    "shell working directory:s:C:\\Work",
    "gatewayhostname:s:gw.acme.test:8443",
    "gatewayusagemethod:i:1",
    "promptcredentialonce:i:0",
    "administrative session:i:1",
    "username:s:CORP\\alice",
    "password 51:b:01000000D08C9DDF0115D1118C7A00C04FC297EB0100000052A9E191EA75A948B359790578C9371A",
    "loadbalanceinfo:s:tsv://MS Terminal Services Plugin.1.Pool",
    "use redirection server name:i:1",
    "winposstr:s:0,1,0,0,800,600",
    "",
])


class ImportTests(unittest.TestCase):
    def test_maps_the_known_keys(self) -> None:
        result = import_rdp(encode_rdp(SAMPLE), "/downloads/Win Server 01.rdp")
        s = result.settings
        self.assertEqual(result.alias, "Win Server 01")
        self.assertEqual(result.options, {"hostname": "win-01.acme.test", "port": "3390", "user": "CORP\\alice"})
        self.assertEqual((s.size_mode, s.multimon, s.monitors, s.color_depth), ("fullscreen", True, [0, 1], 16))
        self.assertEqual((s.preset, s.network_autodetect, s.grab_keyboard), ("wan", False, True))
        self.assertEqual((s.audio_out, s.audio_in, s.printers, s.smartcard, s.clipboard),
                         ("remote", True, True, True, "off"))
        self.assertEqual((s.cert_policy, s.prompt_for_credentials, s.admin_session), ("ask", True, True))
        self.assertEqual((s.initial_program, s.working_dir), ("C:\\Tools\\app.exe", "C:\\Work"))
        self.assertEqual((s.gateway.host, s.gateway.port, s.gateway.use_same_credentials),
                         ("gw.acme.test", 8443, False))
        self.assertEqual(s.load_balance_info, "tsv://MS Terminal Services Plugin.1.Pool")
        self.assertEqual(s.drives, [])

    def test_report_and_secrets(self) -> None:
        result = import_rdp(encode_rdp(SAMPLE), "x.rdp")
        notes = " ".join(result.notes)
        self.assertIn("'password 51' holds a credential", notes)
        self.assertIn("Serial ports", notes)
        self.assertIn("Windows drives", notes)
        self.assertEqual(sorted(result.unknown), ["use redirection server name", "winposstr"])
        self.assertNotIn("01000000D08C", repr(result.settings.to_json()))
        self.assertEqual(result.settings.extra["rdpfile"]["winposstr"], ["s", "0,1,0,0,800,600"])

    def test_authentication_level_zero_never_disables_checking(self) -> None:
        result = import_rdp(b"full address:s:h\nauthentication level:i:0\n")
        self.assertEqual(result.settings.cert_policy, "ask")
        self.assertIn("asks the first time", " ".join(result.notes))
        self.assertEqual(import_rdp(b"full address:s:h\nauthentication level:i:1\n").settings.cert_policy, "deny")

    def test_shared_folders_are_checked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ok = Path(tmp) / "share"
            ok.mkdir()
            data = f"full address:s:h\ndrivestoredirect:s:{ok};/etc;{tmp}/missing\n".encode()
            result = import_rdp(data)
        self.assertEqual([f.path for f in result.settings.drives], [str(ok.resolve())])
        self.assertFalse(result.settings.drives[0].allow_sensitive)
        notes = " ".join(result.notes)
        self.assertIn("/etc", notes)
        self.assertIn("does not exist", notes)

    def test_encodings(self) -> None:
        text = "full address:s:سرور.acme.test\nusername:s:alice\n"
        for data in (encode_rdp(text), codecs.BOM_UTF8 + text.encode(), text.encode(), text.encode("utf-16-le")):
            self.assertEqual(import_rdp(data).hostname, "سرور.acme.test")
        self.assertEqual(decode(b"full address:s:caf\xe9\n"), "full address:s:café\n")  # cp1252 fallback

    def test_hostile_files(self) -> None:
        for data in (b"", b"no colons here\n", b"username:s:alice\n", b"full address:s:-oProxyCommand=x\n",
                     b"full address:s:a b\n", b"full address:s:host/evil\n", b"x" * (300 * 1024)):
            with self.assertRaises(RdpFileError, msg=data[:40]):
                import_rdp(data)
        values, problems = parse_lines("full address:s:h\nbad\x07line:s:x\n" + "k:s:" + "x" * 5000 + "\n")
        self.assertEqual(list(values), ["full address"])
        self.assertEqual(len(problems), 2)
        tricky = import_rdp(b"full address:s:h\nalternate shell:s:" + b"x" * 3000 + b"\nsession bpp:i:abc\n")
        self.assertEqual(len(tricky.settings.initial_program), 1024)
        self.assertEqual(tricky.settings.color_depth, 32)


class ExportTests(unittest.TestCase):
    def test_round_trip_without_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            s = RdpSettings(domain="CORP", size_mode="fixed", width=1600, height=900, color_depth=24,
                            prompt_for_credentials=True, clipboard="off", printers=True, audio_out="off",
                            drives=[SharedFolder("share", tmp)], gateway=Gateway("gw.acme.test", 8443),
                            remote_app=RemoteApp("||notepad", "Notepad", "a.txt"), preset="lan")
            s.extra["rdpfile"] = {"winposstr": ["s", "0,1,0,0,800,600"], "password 51": ["b", "AA"]}
            text = export_rdp("win-01.acme.test", 3390, "alice", s)
            back = import_rdp(encode_rdp(text), "win.rdp")
        self.assertIn("prompt for credentials:i:1", text)
        self.assertNotIn("password", text.replace("prompt for credentials", ""))
        self.assertIn("winposstr:s:0,1,0,0,800,600", text)
        b = back.settings
        self.assertEqual(back.options, {"hostname": "win-01.acme.test", "port": "3390", "user": "alice"})
        self.assertEqual((b.domain, b.size_mode, b.width, b.height, b.color_depth),
                         ("CORP", "fixed", 1600, 900, 24))
        self.assertEqual((b.clipboard, b.printers, b.audio_out, b.preset), ("off", True, "off", "lan"))
        self.assertEqual((b.gateway.host, b.gateway.port), ("gw.acme.test", 8443))
        self.assertEqual(b.remote_app, RemoteApp("||notepad", "Notepad", "a.txt"))
        self.assertEqual(len(b.drives), 1)

    def test_ipv6_and_default_port(self) -> None:
        text = export_rdp("fe80::1", 3389, "", RdpSettings())
        self.assertIn("full address:s:[fe80::1]\r\n", text)
        self.assertNotIn("server port", text)
        self.assertEqual(import_rdp(encode_rdp(text)).hostname, "fe80::1")


class WakeOnLanTests(unittest.TestCase):
    def test_mac_parsing(self) -> None:
        for text in ("AA:BB:CC:DD:EE:01", "aa-bb-cc-dd-ee-01", "aabbccddee01"):
            self.assertEqual(parse_mac(text), bytes.fromhex("aabbccddee01"))
        for bad in ("", "AA:BB:CC:DD:EE", "AA:BB-CC:DD:EE:01", "00:00:00:00:00:00", "FF:FF:FF:FF:FF:FF", "zz"):
            self.assertIsNone(parse_mac(bad), bad)

    def test_packet_and_targets(self) -> None:
        packet = magic_packet(bytes.fromhex("aabbccddee01"))
        self.assertEqual(len(packet), 102)
        self.assertEqual(packet[:6], b"\xff" * 6)
        self.assertEqual(packet[6:12] * 16, packet[6:])
        self.assertTrue(broadcast_ok("255.255.255.255") and broadcast_ok("10.0.0.255"))
        self.assertFalse(broadcast_ok("8.8.8.8") or broadcast_ok("not-an-ip") or broadcast_ok("::1"))
        sent = []
        wake("aa:bb:cc:dd:ee:01", "10.0.0.255", sender=lambda data, address: sent.append((len(data), address)))
        self.assertEqual(sent, [(102, "10.0.0.255")])
        with self.assertRaises(ValueError):
            wake("aa:bb:cc:dd:ee:01", "93.184.216.34", sender=lambda *a: None)


if __name__ == "__main__":
    unittest.main()
