"""RDP engine discovery: fake FreeRDP clients on a temporary PATH, help parsing, capabilities, choice, report."""

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ssh_manager.rdp import engines
from ssh_manager.rdp.engines import (
    CAPABILITIES, ClientBinary, Platform, build_engines, choose_engine, detect_platform, discover, find_binaries,
    inspect_binary, install_hint, parse_help_options, parse_version,
)

FIXTURES = Path(__file__).parent / "fixtures" / "freerdp"
HELP3 = (FIXTURES / "help-3.15.txt").read_text()
HELP2 = (FIXTURES / "help-2.11.txt").read_text()
X11 = Platform("x11", True, "", "", False, "debian")
WAYLAND = Platform("wayland", False, "", "", False, "fedora")


def fake_client(directory: Path, name: str, version: str = "3.15.0", help_file: str = "help-3.15.txt",
                body: str = "") -> Path:
    """A shell script that answers /version and /help like a FreeRDP client (and records nothing else)."""

    path = directory / name
    script = body or f"""#!/bin/sh
case "$1" in
  /version|--version) echo "This is FreeRDP version {version} (n/a)";;
  /help) cat "{FIXTURES / help_file}";;
  *) echo "unexpected $*" >&2; exit 64;;
esac
"""
    path.write_text(script)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def binary(name="xfreerdp3", version=(3, 15, 0), help_text=HELP3) -> ClientBinary:
    return ClientBinary(name, f"/usr/bin/{name}", version, ".".join(map(str, version)), parse_help_options(help_text))


class ParsingTests(unittest.TestCase):
    def test_versions(self) -> None:
        self.assertEqual(parse_version("This is FreeRDP version 3.15.0 (n/a)"), ((3, 15, 0), "3.15.0"))
        self.assertEqual(parse_version("This is FreeRDP version 2.11.7 (2.11.7)"), ((2, 11, 7), "2.11.7"))
        self.assertEqual(parse_version("garbage"), ((0, 0, 0), ""))

    def test_help_options_of_both_versions(self) -> None:
        three, two = parse_help_options(HELP3), parse_help_options(HELP2)
        for option in ("v", "u", "d", "p", "args-from", "from-stdin", "cert", "parent-window", "clipboard", "gateway",
                       "dynamic-resolution", "t", "kbd", "tls", "remoteguard", "sec"):
            self.assertIn(option, three, option)
        for option in ("v", "u", "d", "p", "from-stdin", "cert", "cert-ignore", "parent-window", "clipboard", "g",
                       "gp", "dynamic-resolution", "t", "kbd", "tls-seclevel", "sec"):
            self.assertIn(option, two, option)
        self.assertNotIn("args-from", two)  # 2.x has no /args-from
        self.assertNotIn("gateway", two)
        self.assertNotIn("help", parse_help_options("Usage: xfreerdp [file] [options]\nSyntax:\n  /flag"))

    def test_secret_channels(self) -> None:
        self.assertEqual(binary().secret_channel, "args-from")
        self.assertEqual(binary("xfreerdp", (2, 11, 7), HELP2).secret_channel, "from-stdin")
        self.assertEqual(ClientBinary("x", "/x", (3, 0, 0), "3.0.0", frozenset({"v"})).secret_channel, "")


class FakeBinaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_finds_and_inspects_3x_and_2x(self) -> None:
        fake_client(self.dir, "xfreerdp3")
        fake_client(self.dir, "xfreerdp", "2.11.7", "help-2.11.txt")
        found = find_binaries(str(self.dir))
        self.assertEqual([name for name, _path in found], ["xfreerdp3", "xfreerdp"])
        three = inspect_binary(*found[0])
        two = inspect_binary(*found[1])
        self.assertEqual((three.major, three.usable, two.major, two.usable), (3, True, 2, True))
        self.assertIn("args-from", three.options)

    def test_broken_unsupported_and_slow_clients(self) -> None:
        fake_client(self.dir, "xfreerdp3", body="#!/bin/sh\necho 'segmentation fault' >&2\nexit 139\n")
        fake_client(self.dir, "xfreerdp", "4.0.0")
        fake_client(self.dir, "sdl-freerdp3", body="#!/bin/sh\nsleep 5\n")
        found = dict(find_binaries(str(self.dir)))
        broken = inspect_binary("xfreerdp3", found["xfreerdp3"])
        self.assertFalse(broken.usable)
        self.assertIn("did not report", broken.error)
        future = inspect_binary("xfreerdp", found["xfreerdp"])
        self.assertIn("only FreeRDP 2.x and 3.x", future.error)
        with mock.patch.object(engines, "VERSION_TIMEOUT", 0.3):
            slow = inspect_binary("sdl-freerdp3", found["sdl-freerdp3"])
        self.assertIn("in time", slow.error)

    def test_override_and_missing(self) -> None:
        self.assertEqual(find_binaries(str(self.dir)), [])
        custom = fake_client(self.dir, "my-freerdp")
        self.assertEqual(find_binaries("", override=str(custom)), [("my-freerdp", str(custom))])
        self.assertEqual(find_binaries("", override=str(self.dir / "missing")), [])

    def test_discovery_never_passes_credentials_or_environment(self) -> None:
        log = self.dir / "argv.log"
        fake_client(self.dir, "xfreerdp3", body=f"""#!/bin/sh
echo "$@" >> "{log}"
env >> "{log}"
case "$1" in /version) echo "This is FreeRDP version 3.15.0 (n/a)";; /help) cat "{FIXTURES / 'help-3.15.txt'}";; esac
""")
        with mock.patch.dict(os.environ, {"SSHM_TEST_SECRET": "hunter2"}):
            report = discover(path_env=str(self.dir), platform=X11)
        recorded = log.read_text()
        self.assertNotIn("hunter2", recorded)
        self.assertIn("/version", recorded)
        self.assertIn("/help", recorded)
        self.assertEqual(report.chosen.id, "xfreerdp-embed")


class EngineTests(unittest.TestCase):
    def test_capability_matrix(self) -> None:
        engines_ = build_engines([binary()], X11)
        by_id = {engine.id: engine for engine in engines_}
        self.assertFalse(by_id["frdp"].available)
        embed, external = by_id["xfreerdp-embed"], by_id["external"]
        self.assertTrue(embed.available and embed.embedded and not external.embedded)
        for capability in ("clipboard", "drives", "gateway", "dynamic_resize", "multimon", "remoteapp", "usb"):
            self.assertTrue(external.supports(capability), capability)
        self.assertFalse(external.supports("screenshot") or external.supports("send_keys"))
        self.assertEqual(set(external.capabilities), set(CAPABILITIES))
        frdp = {e.id: e for e in build_engines([], Platform("wayland", False, "0.1", "", False, ""))}["frdp"]
        self.assertTrue(frdp.available and frdp.embedded)

    def test_build_specific_capabilities(self) -> None:
        no_usb = ClientBinary("xfreerdp3", "/x", (3, 15, 0), "3.15.0", parse_help_options(HELP3) - {"usb"})
        self.assertFalse({e.id: e for e in build_engines([no_usb], X11)}["external"].supports("usb"))
        wl = ClientBinary("wlfreerdp3", "/x", (3, 15, 0), "3.15.0", parse_help_options(HELP3))
        caps = {e.id: e for e in build_engines([wl], WAYLAND)}["external"].capabilities
        self.assertFalse(caps["keyboard_grab"] or caps["multimon"])

    def test_wayland_has_no_x11_embedding(self) -> None:
        by_id = {e.id: e for e in build_engines([binary()], WAYLAND)}
        self.assertFalse(by_id["xfreerdp-embed"].available)
        self.assertIn("X11", by_id["xfreerdp-embed"].reason)
        self.assertTrue(by_id["external"].available)

    def test_sdl_and_wayland_clients_cannot_embed(self) -> None:
        sdl = binary("sdl-freerdp3")
        by_id = {e.id: e for e in build_engines([sdl], X11)}
        self.assertFalse(by_id["xfreerdp-embed"].available)  # only xfreerdp / xfreerdp3 take /parent-window
        self.assertEqual(by_id["external"].binary.name, "sdl-freerdp3")

    def test_external_preference_order(self) -> None:
        clients = [binary("xfreerdp", (2, 11, 7), HELP2), binary("xfreerdp3"), binary("sdl-freerdp3"),
                   binary("wlfreerdp")]
        self.assertEqual(engines.preferred_external(clients, X11).name, "xfreerdp3")
        wayland_only = [binary("wlfreerdp"), binary("sdl-freerdp3")]
        self.assertEqual(engines.preferred_external(wayland_only, WAYLAND).name, "sdl-freerdp3")

    def test_choose_engine(self) -> None:
        all_ = build_engines([binary()], X11)
        self.assertEqual(choose_engine(all_, "auto")[0].id, "xfreerdp-embed")
        self.assertEqual(choose_engine(all_, "external")[0].id, "external")
        self.assertEqual(choose_engine(all_, "auto", host_engine="external")[0].id, "external")
        wayland = build_engines([binary()], WAYLAND)
        engine, note = choose_engine(wayland, "embedded")
        self.assertEqual(engine.id, "external")
        self.assertIn("gtk-frdp", note)
        none, why = choose_engine(build_engines([], WAYLAND), "auto")
        self.assertIsNone(none)
        self.assertTrue(why)


class PlatformAndReportTests(unittest.TestCase):
    def test_detect_platform(self) -> None:
        p = detect_platform({"XDG_SESSION_TYPE": "wayland"}, gdk_x11=False, frdp_version="",
                            os_release='ID=ubuntu\nID_LIKE="debian"\n')
        self.assertEqual((p.session_type, p.gdk_x11, p.distro), ("wayland", False, "ubuntu debian"))
        self.assertEqual(detect_platform({"DISPLAY": ":0"}, gdk_x11=True, frdp_version="", os_release="").session_type,
                         "x11")

    def test_install_hints(self) -> None:
        self.assertIn("apt install freerdp3-x11", install_hint(Platform(distro="ubuntu debian")))
        self.assertIn("dnf", install_hint(Platform(distro="fedora")))
        self.assertIn("pacman", install_hint(Platform(distro="arch")))
        self.assertIn("package manager", install_hint(Platform(distro="gentoo")))

    def test_report_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_client(Path(tmp), "xfreerdp", "2.11.7", "help-2.11.txt")
            report = discover(path_env=tmp, platform=WAYLAND, preference="embedded")
        text = report.text()
        self.assertIn("FreeRDP 2.11.7", text)
        self.assertIn("password channel: from-stdin", text)
        self.assertIn("Chosen: External window", text)
        self.assertIn("unavailable", text)
        empty = discover(path_env="", platform=Platform(distro="fedora", flatpak=True)).text()
        self.assertIn("none found", empty)
        self.assertIn("dnf install freerdp", empty)
        self.assertIn("Flatpak", empty)

    def test_cache_and_async(self) -> None:
        import threading

        done = threading.Event()
        results = []
        engines.discover_async(lambda report: (results.append(report), done.set()), path_env="", platform=X11)
        self.assertTrue(done.wait(5))
        self.assertIs(engines.cached_report(), results[0])
        engines.store_report(None)


if __name__ == "__main__":
    unittest.main()
