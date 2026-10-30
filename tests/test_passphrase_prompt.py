"""Filling a saved key passphrase at ssh's own prompt: exactly once, only for the host's key, never elsewhere."""

import unittest

from ssh_manager.auth_flow import PassphraseFill, passphrase_prompt

KEY = "/home/u/.ssh/id_ed25519"


class PromptTests(unittest.TestCase):
    def test_real_prompts(self) -> None:
        for tail in (f"Enter passphrase for key '{KEY}': ", f"\x1b[0mEnter passphrase for key '{KEY}':",
                     f"some banner\r\nEnter passphrase for key '{KEY}': "):
            with self.subTest(tail=tail):
                self.assertEqual(passphrase_prompt(tail), KEY)

    def test_look_alikes(self) -> None:
        for tail in (f"$ echo \"Enter passphrase for key '{KEY}': \"\r\nEnter passphrase for key '{KEY}': \r\nu@h:~$ ",
                     f"Enter passphrase for key '{KEY}': typed\r\n", "Enter passphrase: ", "password: ",
                     f"cat notes: Enter passphrase for key '{KEY}' maybe"):
            with self.subTest(tail=tail):
                self.assertIsNone(passphrase_prompt(tail))


class FillTests(unittest.TestCase):
    def test_once_per_attempt_and_only_for_the_hosts_key(self) -> None:
        fill = PassphraseFill(KEY)
        self.assertTrue(fill.should_fill(KEY))
        fill.filled(KEY)
        self.assertFalse(fill.should_fill(KEY))  # never twice (a wrong saved passphrase: the user types)
        self.assertFalse(PassphraseFill(KEY).should_fill("/home/u/.ssh/other"))
        self.assertFalse(PassphraseFill("").should_fill(KEY))  # no identity file configured: nothing to fill
        fill.reset()
        self.assertTrue(fill.should_fill(KEY))  # a new connection attempt


if __name__ == "__main__":
    unittest.main()
