"""Characterization (§2.4.1): what a session spawns, for a matrix of hosts, against committed goldens.

Each case runs the real spawn path of ``TerminalTab`` (``_spawn`` → ``_spawn_command`` → pre-flight or
``_start_child`` → ``Vte.Terminal.spawn_async``) with ``spawn_async``, the host-key pre-flight, the
password prompt and ``shutil.which`` patched, in a fixed environment. The golden records the path taken,
argv, the environment (in its order: with duplicate names the order decides), the spawn and pty flags and
the log lines; the session's passwords are replaced by ``<SECRET>`` (the outer ``SSHPASS`` of the fixed
environment is a fixture value and stays visible). R8 is asserted for every case: a password never in argv, only as ``SSHPASS`` in the env,
with ``sshpass -e``.
"""

import os
import unittest
from pathlib import Path
from unittest import mock

from ophion.config import HostEntry
from tests.gtk_window_fixture import display_available
from tests.refactor import golden

ENVIRON = {
    "HOME": "/tmp", "USER": "tester", "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8", "PATH": "/usr/bin:/bin",
    "DISPLAY": ":99", "SSH_AUTH_SOCK": "/run/user/1000/agent.sock", "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/bus",
    "TERM": "tmux-256color", "COLORTERM": "outer", "TMUX": "/tmp/tmux-1000/default,1,0", "TMUX_PANE": "%1",
    "STY": "1.pts", "WINDOW": "0", "SSHPASS": "outer-sshpass-value", "OPHION_DEBUG": "1",
    "SSH_MANAGER_APPDIR": "/opt/app", "SSHM_DENSITY": "compact",
}
MARKER = Path("/tmp/ophion-markers/0123456789abcdef.ok")
SECRETS = ("s3cret-credential", "typed-password", "legacy-stored")  # the session's passwords
NEVER_IN_ARGV = SECRETS + ("outer-sshpass-value",)


def host(alias="web", **options) -> HostEntry:
    return HostEntry(alias=alias, options=dict(options), parent_group="prod", child_group="web")


# name: (host options, password resolution (password, source) or None, sshpass installed, override, profile env)
CASES = {
    "credential_password": (dict(hostname="10.0.0.5", user="deploy", port="2222"), ("s3cret-credential", "credential"),
                            True, None, None),
    "prompted_password": (dict(hostname="10.0.0.5", user="deploy"), None, True, "typed-password", None),
    "legacy_password": (dict(hostname="db.internal", user="root"), ("legacy-stored", "legacy"), True, None, None),
    "password_without_sshpass": (dict(hostname="10.0.0.5", user="deploy"), ("s3cret-credential", "credential"),
                                 False, None, None),
    "key_file": (dict(hostname="10.0.0.6", user="ops", identityfile="~/.ssh/id_ed25519"), None, False, None, None),
    "key_file_with_sshpass_asks": (dict(hostname="10.0.0.6", user="ops", identityfile="~/.ssh/id_ed25519"), None,
                                   True, None, None),
    "agent": (dict(hostname="10.0.0.7", user="ops", forwardagent="yes", identitiesonly="no"), None, False, None, None),
    "proxy_jump": (dict(hostname="10.1.0.9", user="app", proxyjump="jump@bastion:2200"),
                   ("s3cret-credential", "credential"), True, None, None),
    "proxy_command_wins": (dict(hostname="10.1.0.9", proxyjump="ignored", proxycommand="ssh -W %h:%p gw"), None,
                           False, None, None),
    "port_forwards": (dict(hostname="10.0.0.8", localforward="8080 localhost:80", remoteforward="9090 localhost:90",
                           dynamicforward="1080"), None, False, None, None),
    "extra_options": (dict(hostname="10.0.0.9", serveraliveinterval="30", stricthostkeychecking="accept-new",
                           compression="yes", tags="prod,web", user="u"), None, False, None, None),
    "permit_local_command_no": (dict(hostname="10.0.0.10", permitlocalcommand="no"), None, False, None, None),
    "own_local_command": (dict(hostname="10.0.0.11", localcommand="echo 100% done"), None, False, None, None),
    "alias_only": (dict(), None, False, None, None),
    "profile_env": (dict(hostname="10.0.0.12"), None, False, None, {"EDITOR": "vim", "LANG": "C.UTF-8"}),
}


@unittest.skipUnless(display_available(), "needs a display")
class SpawnGoldenTests(unittest.TestCase):
    def run_case(self, options, resolution, sshpass, override, profile_env, alias="web"):
        from ophion import gtk_terminal as GT
        from ophion.auth_flow import NO_PASSWORD, PasswordResolution
        from ophion.profiles import SessionProfile

        logs = []
        provider = (lambda: PasswordResolution(resolution[0], resolution[1], "c1")) if resolution else (lambda: NO_PASSWORD)
        profile = SessionProfile(id="p", name="P", env_vars=profile_env) if profile_env else None
        record = {"path": "none", "asked_password": False}
        with mock.patch.dict(os.environ, ENVIRON, clear=True), \
                mock.patch("ophion.gtk_terminal.shutil.which",
                           side_effect=lambda name: f"/usr/bin/{name}" if (name != "sshpass" or sshpass) else None), \
                mock.patch("ophion.gtk_terminal.new_marker_path", return_value=MARKER), \
                mock.patch("ophion.gtk_terminal.Vte.Terminal.spawn_async") as spawn, \
                mock.patch("ophion.gtk_terminal.TerminalTab._preflight_then_start") as preflight, \
                mock.patch("ophion.gtk_terminal.TerminalTab._ask_password",
                           side_effect=lambda *a, **k: record.update(asked_password=True)):
            tab = GT.TerminalTab(host(alias, **options), lambda text, level=None: logs.append(
                [text, getattr(level, "value", level)]), autostart=False, password_provider=provider, profile=profile)
            self.addCleanup(tab.destroy)
            if override is not None:
                tab._spawn(override, password_origin="prompt")
            else:
                tab._spawn()
            if preflight.called:
                argv, envv = preflight.call_args[0]
                record.update(path="preflight", argv=list(argv), env=list(envv))
            elif spawn.called:
                args = spawn.call_args[0]
                record.update(path="spawn", pty_flags=int(args[0]), workdir=args[1], argv=list(args[2]),
                              env=list(args[3]), spawn_flags=int(args[4]))
        record.update(auth_method=tab._auth_method, password_source=tab._password_source, logs=logs)
        return record

    def assert_r8(self, record, password):
        argv = record.get("argv", [])
        for secret in NEVER_IN_ARGV:
            self.assertFalse(any(secret in item for item in argv), f"a secret in argv: {argv}")
        sshpass_env = [item for item in record.get("env", []) if item.startswith("SSHPASS=")]
        if password and record["path"] != "none":
            self.assertEqual(argv[:2], ["sshpass", "-e"])
            self.assertIn(f"SSHPASS={password}", sshpass_env)
        else:
            self.assertNotEqual(argv[:1], ["sshpass"])

    def test_spawn_matrix(self) -> None:
        for name, (options, resolution, sshpass, override, profile_env) in CASES.items():
            with self.subTest(case=name):
                record = self.run_case(options, resolution, sshpass, override, profile_env)
                password = override or (resolution[0] if resolution and sshpass else None)
                self.assert_r8(record, password)
                golden.check(self, f"spawn_{name}", golden.mask(record, SECRETS))

    def test_secondary_pane(self) -> None:
        """A split pane spawns the same ssh (no password) with the same flags."""

        from ophion import gtk_terminal as GT

        with mock.patch.dict(os.environ, ENVIRON, clear=True), \
                mock.patch("ophion.gtk_terminal.Vte.Terminal.spawn_async") as spawn:
            tab = GT.TerminalTab(host(hostname="10.0.0.5", user="deploy", port="2200"), lambda *a: None,
                                 autostart=False)
            self.addCleanup(tab._terminal.destroy)
            self.addCleanup(tab.destroy)
            tab._spawn_secondary(tab._terminal)
        args = spawn.call_args[0]
        record = {"pty_flags": int(args[0]), "workdir": args[1], "argv": list(args[2]), "env": list(args[3]),
                  "spawn_flags": int(args[4])}
        golden.check(self, "spawn_secondary_pane", golden.mask(record, SECRETS))

    def test_local_shell(self) -> None:
        from ophion.gtkui.local_terminal import LocalTerminalTab
        from ophion.settings import LocalShellSettings

        settings = LocalShellSettings()
        settings.command = "/bin/sh"
        with mock.patch.dict(os.environ, ENVIRON, clear=True), \
                mock.patch("ophion.gtkui.local_terminal.Vte.Terminal.spawn_async") as spawn:
            tab = LocalTerminalTab(lambda *a, **k: None, 1.0, settings=settings, cwd=None)
            self.addCleanup(tab.destroy)
            tab._spawn()
        args = spawn.call_args[0]
        record = {"pty_flags": int(args[0]), "workdir": args[1], "argv": list(args[2]), "env": list(args[3]),
                  "spawn_flags": int(args[4])}
        self.assertFalse([item for item in record["env"] if item.startswith(("SSHPASS=", "OPHION_", "SSHM_"))])
        golden.check(self, "spawn_local_shell", record)

    def test_protocol_variants(self) -> None:
        """Only ssh hosts build an ssh command; the others are refused with the same message."""

        from ophion.ssh_command import build_ssh_command

        result = {}
        for protocol in ("ssh", "sftp", "ftp", "ftps", "rdp"):
            entry = HostEntry(alias="h", options={"hostname": "10.0.0.1", "user": "u"}, protocol=protocol)
            try:
                result[protocol] = {"argv": build_ssh_command(entry)}
            except ValueError as exc:
                result[protocol] = {"error": str(exc)}
        golden.check(self, "spawn_protocol_variants", result)


if __name__ == "__main__":
    unittest.main()
