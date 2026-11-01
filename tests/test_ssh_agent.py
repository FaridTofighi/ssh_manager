"""ssh-agent: status and ``ssh-add`` through a pty, against a private agent in a temporary directory."""

import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from ophion import ssh_keys as K

PASS = "Agent-passphrase-sentinel"


@unittest.skipUnless(shutil.which("ssh-agent") and shutil.which("ssh-add"), "needs ssh-agent and ssh-add")
class AgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.sock = str(self.root / "agent.sock")
        agent = subprocess.Popen(["ssh-agent", "-D", "-a", self.sock], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
        self.addCleanup(agent.wait)
        self.addCleanup(agent.terminate)
        deadline = time.monotonic() + 10
        while not os.path.exists(self.sock) and time.monotonic() < deadline:
            time.sleep(0.05)
        patcher = mock.patch.dict(os.environ, {"SSH_AUTH_SOCK": self.sock})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_status_without_an_agent(self) -> None:
        with mock.patch.dict(os.environ, {"SSH_AUTH_SOCK": ""}):
            status = K.agent_status()
        self.assertFalse(status.reachable)
        self.assertIn("No ssh-agent", status.detail)

    def test_add_a_protected_key_typed_at_the_prompt(self) -> None:
        self.assertEqual(K.agent_status(), K.AgentStatus(True, 0, ""))
        key = K.generate_key(self.root / "id_ed25519", comment="t", passphrase=PASS)
        environ = dict(os.environ)
        ok, message = K.agent_add(key.path, PASS)
        self.assertTrue(ok, message)
        self.assertEqual(K.agent_status().keys, 1)
        self.assertEqual(dict(os.environ), environ)  # nothing put in the environment

    def test_a_wrong_passphrase_fails_without_echoing_it(self) -> None:
        key = K.generate_key(self.root / "id_ed25519", comment="t", passphrase=PASS)
        ok, message = K.agent_add(key.path, PASS + "-wrong", timeout=10)
        self.assertFalse(ok)
        self.assertNotIn(PASS, message)
        self.assertEqual(K.agent_status().keys, 0)

    def test_the_passphrase_is_not_in_argv_or_env(self) -> None:
        key = K.generate_key(self.root / "id_ed25519", comment="t", passphrase=PASS)

        fake = self.root / "ssh-add"  # a stand-in that records what it was started with
        fake.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$0.argv\"\nenv > \"$0.env\"\nexit 1\n")
        fake.chmod(0o700)
        with mock.patch("shutil.which", lambda name: str(fake)):
            ok, _message = K.agent_add(key.path, PASS, timeout=5)
        self.assertFalse(ok)
        argv = (self.root / "ssh-add.argv").read_text()
        env = (self.root / "ssh-add.env").read_text()
        self.assertEqual(argv, f"{key.path}\n")
        self.assertNotIn(PASS, argv + env)
        self.assertNotIn("SSH_ASKPASS=", env)
        self.assertIn("SSH_ASKPASS_REQUIRE=never", env)

    def test_an_unprotected_key(self) -> None:
        key = K.generate_key(self.root / "plain", comment="t")
        ok, message = K.agent_add(key.path)
        self.assertTrue(ok, message)
        self.assertEqual(K.agent_status().keys, 1)


if __name__ == "__main__":
    unittest.main()
