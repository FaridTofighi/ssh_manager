"""The FreeRDP argv builder: goldens for 2.x and 3.x, secrets never in argv, validation and injection attempts."""

import tempfile
import unittest
from pathlib import Path

from ssh_manager.rdp.command import LOG_ARGS, LaunchError, build_launch, target_address
from ssh_manager.rdp.engines import ClientBinary, parse_help_options
from ssh_manager.rdp.settings import Gateway, RdpIdentity, RdpSettings, RemoteApp, SharedFolder

FIXTURES = Path(__file__).parent / "fixtures" / "freerdp"
V3 = ClientBinary("xfreerdp3", "/usr/bin/xfreerdp3", (3, 15, 0), "3.15.0",
                  parse_help_options((FIXTURES / "help-3.15.txt").read_text()))
V2 = ClientBinary("xfreerdp", "/usr/bin/xfreerdp", (2, 11, 7), "2.11.7",
                  parse_help_options((FIXTURES / "help-2.11.txt").read_text()))
NO_CHANNEL = ClientBinary("wlfreerdp", "/usr/bin/wlfreerdp", (2, 0, 0), "2.0.0", V2.options - {"from-stdin"})
FP = "AB:" * 31 + "AB"
PASSWORD = 'p@ss "w,rd" with spaces; $(rm -rf ~) ünïcødé'


def build(client=V3, settings=None, password=PASSWORD, **kwargs):
    kwargs.setdefault("accepted_fingerprints", [FP])
    return build_launch(hostname=kwargs.pop("hostname", "10.0.0.50"), port=kwargs.pop("port", 3389),
                        settings=settings or RdpSettings(), identity=kwargs.pop("identity", RdpIdentity("alice", "CORP")),
                        password=password, client=client, title=kwargs.pop("title", "win-01"), **kwargs)


def args_of(plan):
    """The option list FreeRDP receives (the stdin lines for 3.x, argv for 2.x)."""

    if plan.channel == "args-from":
        return plan.stdin_payload.decode().split("\n")[:-2]
    return plan.argv[1:]


class GoldenTests(unittest.TestCase):
    def test_v3_defaults(self) -> None:
        plan = build()
        self.assertEqual(plan.argv, ["/usr/bin/xfreerdp3", "/args-from:stdin"])
        self.assertTrue(plan.stdin_payload.endswith(b"\n\n"))  # an empty line ends /args-from
        expected = ["/v:10.0.0.50:3389", "/u:alice", "/d:CORP", "/t:win-01", *LOG_ARGS,
                    "/cert:deny,fingerprint:sha256:" + ":".join(["ab"] * 32), "/size:1920x1080",
                    "/dynamic-resolution", "/bpp:32", "/network:auto", "+clipboard", "/sound", "/timeout:15000",
                    "+auto-reconnect", "/auto-reconnect-max-retries:5", "/p:" + PASSWORD]
        self.assertEqual(args_of(plan), expected)
        self.assertEqual(plan.display_args[-1], "/p:***")
        self.assertTrue(plan.password_sent)
        self.assertEqual(plan.notes, [])

    def test_v2_defaults(self) -> None:
        plan = build(V2)
        self.assertEqual(plan.channel, "from-stdin")
        self.assertEqual(plan.stdin_payload, (PASSWORD + "\n").encode())
        self.assertEqual(plan.argv[0], "/usr/bin/xfreerdp")
        self.assertEqual(plan.argv[-1], "/from-stdin:force")
        self.assertIn("/cert:deny,fingerprint:sha256:" + ":".join(["ab"] * 32), plan.argv)

    def test_everything_v3(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            s = RdpSettings(security="nla", tls_security_level="0", admin_session=True, restricted_admin=True,
                            remote_credential_guard=True, size_mode="fixed", width=1280, height=720, smart_sizing=True,
                            color_depth=16, scale_percent=140, desktop_scale_factor=150, device_scale_factor=180,
                            preset="custom", wallpaper=False, menu_animations=True, gfx="avc444", compression=False,
                            keyboard_layout="fa-IR", unicode_input=True, grab_keyboard=False, mouse_motion=False,
                            touch=True, clipboard="from_remote", drives=[SharedFolder("work", tmp)], home_drive=True,
                            printers=True, audio_out="remote", audio_in=True, usb=["auto", "id:054c:0268"],
                            smartcard=True, connect_timeout_s=30, auto_reconnect=False,
                            load_balance_info="tsv://MS Terminal Services Plugin.1.Pool",
                            initial_program="C:\\Tools\\a.exe", working_dir="C:\\Work",
                            remote_app=RemoteApp("||notepad", "Notepad", "x.txt"),
                            gateway=Gateway("gw.acme.test", 8443, "bob", "CORP", False, "http"),
                            extra_args="+async-update /gdi:hw")
            args = args_of(build(settings=s, gateway_password="gw-secret"))
        for expected in ("/sec:nla", "/tls:seclevel:0", "/admin", "/restricted-admin", "+remoteGuard",
                         "/size:1280x720", "/smart-sizing", "/bpp:16", "/scale:140", "/scale-desktop:150",
                         "/scale-device:180", "-wallpaper", "+menu-anims", "/gfx:AVC444", "-compression",
                         "/kbd:layout:0x00000429,unicode:on", "-grab-keyboard", "-mouse-motion", "+multitouch",
                         "/clipboard:direction-to:local", "+home-drive", "/printer", "/audio-mode:1", "/microphone",
                         "/usb:auto", "/usb:id:054c:0268", "/smartcard", "/timeout:30000",
                         "/load-balance-info:tsv://MS Terminal Services Plugin.1.Pool", "/shell:C:\\Tools\\a.exe",
                         "/shell-dir:C:\\Work", "/app:program:||notepad,name:Notepad,cmd:x.txt",
                         "/gateway:g:gw.acme.test:8443,u:bob,d:CORP,p:gw-secret,type:http", "+async-update",
                         "/gdi:hw"):
            self.assertIn(expected, args, expected)
        self.assertTrue(any(a.startswith("/drive:work,") for a in args))
        self.assertNotIn("/network:auto", args)  # custom: the toggles themselves
        self.assertNotIn("+auto-reconnect", args)

    def test_v2_spellings_and_downgrades(self) -> None:
        s = RdpSettings(tls_security_level="1", keyboard_layout="de", unicode_input=True, clipboard="to_remote",
                        remote_credential_guard=True, remote_app=RemoteApp("||calc", "Calc", "-x"),
                        gateway=Gateway("gw.acme.test", 443, "bob", "CORP", False, "rpc"))
        plan = build(V2, settings=s, gateway_password="gw-secret")
        args = plan.argv
        for expected in ("/tls-seclevel:1", "/kbd:0x00000407", "-clipboard", "/app:||calc", "/app-name:Calc",
                         "/app-cmd:-x", "/g:gw.acme.test:443", "/gu:bob", "/gd:CORP", "/gt:rpc"):
            self.assertIn(expected, args, expected)
        self.assertFalse(any(a.startswith("/gp:") for a in args))
        self.assertNotIn("+remoteGuard", args)
        notes = " ".join(plan.notes)
        for words in ("Unicode", "one direction", "gateway password", "Remote Credential Guard"):
            self.assertIn(words, notes)

    def test_fullscreen_window_and_embedding(self) -> None:
        full = args_of(build(settings=RdpSettings(size_mode="fullscreen", multimon=True, monitors=[0, 2])))
        self.assertIn("/f", full)
        self.assertIn("/multimon", full)
        self.assertIn("/monitors:0,2", full)
        window = args_of(build(window_size=(1000, 700), parent_window=12345))
        self.assertIn("/size:1000x700", window)
        self.assertIn("/parent-window:12345", window)
        self.assertIn("/dynamic-resolution", window)


class SecretTests(unittest.TestCase):
    def test_password_never_in_argv_or_display(self) -> None:
        for client in (V3, V2, NO_CHANNEL):
            plan = build(client, settings=RdpSettings(gateway=Gateway("gw.acme.test", use_same_credentials=False)),
                         gateway_password="gw-secret")
            joined = " ".join(plan.argv) + " ".join(plan.display_args) + repr(plan)
            self.assertNotIn(PASSWORD, joined, client.name)
            self.assertNotIn("gw-secret", joined, client.name)
            self.assertFalse(plan.env)

    def test_no_channel_never_passes_the_password(self) -> None:
        plan = build(NO_CHANNEL)
        self.assertEqual((plan.channel, plan.stdin_payload, plan.password_sent), ("none", b"", False))
        self.assertIn("type it in the remote sign-in screen", " ".join(plan.notes))

    def test_short_passwords_never_block_a_launch(self) -> None:
        """Regression: the leak check once matched '32' in /bpp:32 and 'pw' in the client's path."""

        for password in ("32", "bin", "pw", "a", "x", "/", ":"):
            client = ClientBinary("xfreerdp", f"/opt/{password}bin/xfreerdp", (2, 11, 7), "2.11.7", V2.options)
            self.assertTrue(build(client, password=password).password_sent, password)
            self.assertTrue(build(password=password).password_sent, password)

    def test_leak_check_catches_password_positions(self) -> None:
        from ssh_manager.rdp.command import _leaks

        self.assertTrue(_leaks(["/p:hunter2"], ["hunter2"]))
        self.assertTrue(_leaks(["/gateway:g:gw,u:bob,p:hunter2"], ["hunter2"]))
        self.assertTrue(_leaks(["/gp:hunter2"], ["hunter2"]))
        self.assertFalse(_leaks(["/bpp:32", "/size:1920x1080"], ["32"]))

    def test_unsafe_passwords(self) -> None:
        for bad in ("line\nbreak", "cr\rhere", "nul\x00", "x" * 600):
            with self.assertRaises(LaunchError):
                build(password=bad)
        self.assertFalse(build(password=None).password_sent)
        self.assertFalse(build(password="").password_sent)

    def test_gateway_password_with_comma_is_not_sent(self) -> None:
        s = RdpSettings(gateway=Gateway("gw.acme.test", use_same_credentials=False, user="bob"))
        plan = build(settings=s, gateway_password="a,b")
        self.assertNotIn("a,b", plan.stdin_payload.decode())
        self.assertIn("comma or a quote", " ".join(plan.notes))


class ValidationTests(unittest.TestCase):
    def test_targets(self) -> None:
        self.assertEqual(target_address("fe80::1", 3389), "[fe80::1]:3389")
        self.assertEqual(target_address("[fe80::1]", 3390), "[fe80::1]:3390")
        for bad in ("", "-oProxy", "host name", "a\nb", "evil/path", "x,y", "a;b", "user@host", "a" * 300):
            with self.assertRaises(LaunchError, msg=bad):
                target_address(bad, 3389)
        with self.assertRaises(LaunchError):
            target_address("ok", 0)

    def test_injection_attempts(self) -> None:
        plan = build(title="win\n/p:leak\x1b[31m")
        self.assertIn("/t:win/p:leak[31m", args_of(plan))  # control characters dropped: one line, one argument
        for identity in (RdpIdentity("alice\n/v:evil"), RdpIdentity("alice", "CORP\n/sec:rdp")):
            with self.assertRaises(LaunchError):
                build(identity=identity)
        with self.assertRaises(LaunchError):
            build(identity=RdpIdentity("a\\b\\c", problem="Use DOMAIN\\user with a single backslash."))
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(LaunchError):
                build(settings=RdpSettings(drives=[SharedFolder("bad,name", tmp)]))
            comma = Path(tmp) / "a,b"
            comma.mkdir()
            plan = build(settings=RdpSettings(drives=[SharedFolder("x", str(comma))]))  # left out, never split
            self.assertFalse(any(a.startswith("/drive:") for a in args_of(plan)))
            self.assertIn("comma", " ".join(plan.notes))
        forbidden = build(settings=RdpSettings(extra_args="/p:x /v:evil:1"))
        self.assertNotIn("/v:evil:1", args_of(forbidden))
        self.assertIn("extra options are not valid", " ".join(forbidden.notes))
        with self.assertRaises(LaunchError):
            build(settings=RdpSettings(remote_app=RemoteApp("a,b")))

    def test_drives_read_only_and_missing_are_not_shared(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan = build(settings=RdpSettings(drives=[SharedFolder("ro", tmp, True), SharedFolder("gone", tmp + "/x")]))
        self.assertFalse(any(a.startswith("/drive:") for a in args_of(plan)))
        notes = " ".join(plan.notes)
        self.assertIn("read-only", notes)
        self.assertIn("does not exist", notes)

    def test_unsupported_options_are_dropped_with_a_note(self) -> None:
        trimmed = ClientBinary("xfreerdp3", "/x", (3, 15, 0), "3.15.0", V3.options - {"usb", "smartcard", "printer"})
        plan = build(trimmed, settings=RdpSettings(usb=["auto"], smartcard=True, printers=True))
        args = args_of(plan)
        self.assertFalse(any(a.startswith(("/usb", "/smartcard", "/printer")) for a in args))
        self.assertEqual(len([n for n in plan.notes if "left out" in n]), 3)
        bad_usb = build(settings=RdpSettings(usb=["id:zz"]))
        self.assertIn("USB selector", " ".join(bad_usb.notes))

    def test_certificate_modes(self) -> None:
        self.assertIn("/cert:ignore", args_of(build(accepted_fingerprints=[], ignore_certificate=True)))
        self.assertIn("/cert:deny", args_of(build(accepted_fingerprints=[])))  # never a hidden console prompt
        standard = args_of(build(settings=RdpSettings(security="rdp"), accepted_fingerprints=[]))
        self.assertFalse(any(a.startswith("/cert") for a in standard))
        self.assertIn("/sec:rdp", standard)
        self.assertIn("/sec:nla", args_of(build(settings=RdpSettings(nla_fallback=False))))

    def test_unusable_client(self) -> None:
        with self.assertRaises(LaunchError):
            build(ClientBinary("x", "/x", (0, 0, 0), "", frozenset(), error="broken"))


if __name__ == "__main__":
    unittest.main()
