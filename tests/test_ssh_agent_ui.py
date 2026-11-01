"""Agent options in the window: ForwardAgent in the host dialog, Preferences → Security → SSH agent."""

import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.gtk_window_fixture import display_available
from tests.test_local_terminal import pump

PASS = "Agent-ui-passphrase-sentinel"


@unittest.skipUnless(display_available(), "needs a display")
class ForwardAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        from tests.gtk_window_fixture import WindowSandbox

        self.sandbox = WindowSandbox(store=True)
        self.addCleanup(self.sandbox.close)
        self.win = self.sandbox.window

    def host(self):
        return next(h for h in self.win.ssh_keys.hosts() if h.alias == "web-01")

    def dialog(self, host):
        dialog = self.win.hosts.host_dialog(host)
        self.addCleanup(dialog.destroy)
        return dialog

    def test_off_by_default_and_written_only_when_ticked(self) -> None:
        from ophion.ssh_command import build_ssh_command

        host = self.host()
        dialog = self.dialog(host)
        self.assertFalse(dialog.forward_agent_check.get_active())
        self.assertFalse(dialog.forward_agent_warning.get_visible())
        built = dialog.build_host(host.parent_group, host.child_group)
        self.assertFalse(built.option("forwardagent"))
        dialog.forward_agent_check.set_active(True)
        self.assertTrue(dialog.forward_agent_warning.get_visible())
        self.assertIn("root on that server", dialog.forward_agent_warning.get_text())
        built = dialog.build_host(host.parent_group, host.child_group)
        self.assertEqual(built.option("forwardagent"), "yes")
        self.assertIn("forwardagent=yes", build_ssh_command(built))

    def test_an_existing_option_shows_checked_and_is_not_duplicated(self) -> None:
        host = self.host()
        host.options["ForwardAgent"] = "yes"
        dialog = self.dialog(host)
        self.assertTrue(dialog.forward_agent_check.get_active())
        self.assertNotIn("ForwardAgent", dialog._extras_text())  # shown by the checkbox, not in SSH options
        dialog.forward_agent_check.set_active(False)
        built = dialog.build_host(host.parent_group, host.child_group)
        self.assertFalse(built.option("forwardagent"))
        self.assertFalse(any(str(k).lower() == "forwardagent" for k in built.options))


@unittest.skipUnless(display_available() and shutil.which("ssh-agent") and shutil.which("ssh-add"),
                     "needs a display, ssh-agent and ssh-add")
class AgentSectionTests(unittest.TestCase):
    def setUp(self) -> None:
        from tests.gtk_window_fixture import WindowSandbox

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        sock = str(Path(self.tmp.name) / "agent.sock")
        agent = subprocess.Popen(["ssh-agent", "-D", "-a", sock], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
        self.addCleanup(agent.wait)
        self.addCleanup(agent.terminate)
        deadline = time.monotonic() + 10
        while not os.path.exists(sock) and time.monotonic() < deadline:
            time.sleep(0.05)
        patcher = mock.patch.dict(os.environ, {"SSH_AUTH_SOCK": sock})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.sandbox = WindowSandbox(store=True)
        self.addCleanup(self.sandbox.close)
        self.win = self.sandbox.window
        self.sandbox.use_keybox("pw")
        self.win.ssh_keys.ssh_dir = self.sandbox.root / "home" / ".ssh"

    def section(self):
        from ophion.gtkui.dialogs.ssh_agent_section import AgentSection

        section = AgentSection(self.win.ssh_keys)
        self.addCleanup(section.destroy)
        return section

    def test_status_and_adding_with_the_saved_passphrase(self) -> None:
        from ophion import ssh_keys as K

        info = K.generate_key(self.win.ssh_keys.ssh_dir / "id_ed25519", comment="t", passphrase=PASS)
        self.win.ssh_keys.remember_passphrase(info.path, PASS)
        section = self.section()
        self.assertEqual(section.status_label.get_text(), "An ssh-agent is running with 0 keys.")
        self.assertTrue(section.add_button.get_sensitive())
        section.ask_passphrase = lambda info: self.fail("the saved passphrase is used")
        self.assertTrue(section.add_key())
        self.assertTrue(pump(20, until=lambda: not section.busy))
        self.assertEqual(section.last_result, (True, "The key was added to the agent."))
        self.assertEqual(section.status_label.get_text(), "An ssh-agent is running with 1 key.")

    def test_cancelled_passphrase_runs_nothing_and_no_agent_disables_adding(self) -> None:
        from ophion import ssh_keys as K

        K.generate_key(self.win.ssh_keys.ssh_dir / "id_ed25519", comment="t", passphrase=PASS)
        section = self.section()
        section.ask_passphrase = lambda info: None
        self.assertFalse(section.add_key())
        self.assertFalse(section.busy)
        with mock.patch.dict(os.environ, {"SSH_AUTH_SOCK": ""}):
            section.refresh()
        self.assertFalse(section.add_button.get_sensitive())
        self.assertIn("No ssh-agent", section.status_label.get_text())

    def test_preferences_show_the_section(self) -> None:
        from ophion.gtkui.dialogs.preferences import PreferencesDialog

        dialog = PreferencesDialog(None, self.win._theme_mode, 1.0, 11, self.win._settings,
                                   security=getattr(self.win, "security", None), ssh_keys=self.win.ssh_keys)
        self.addCleanup(dialog.destroy)
        self.assertIsNotNone(dialog.agent_section)


if __name__ == "__main__":
    unittest.main()
