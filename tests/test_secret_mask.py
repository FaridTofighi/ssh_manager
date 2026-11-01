"""Masking secrets in program output: long ones everywhere, very short ones never (they would show)."""

import types
import unittest

from ophion.secret_mask import MIN_MASKED_LENGTH, mask_secrets

LINE = ("[17:45:50:344] [1792398:0001b5996] [WARN][com.freerdp.client.x11] - [load_map_from_xkbfile]: "
        "keycode: 0x08 -> no RDP scancode found")


class SecretMaskTests(unittest.TestCase):
    def test_a_one_character_password_leaves_the_log_alone(self) -> None:
        # Before: every "1" became "***" — unreadable, and it showed the password was "1".
        self.assertEqual(mask_secrets(LINE, ["1"]), LINE)
        self.assertEqual(mask_secrets(LINE, ["x" * (MIN_MASKED_LENGTH - 1)]), LINE)

    def test_real_passwords_are_masked_everywhere(self) -> None:
        self.assertEqual(mask_secrets("login hunter22 ok hunter22", ["hunter22"]), "login *** ok ***")
        self.assertEqual(mask_secrets("a s3cret! b", ["", None or "", "s3cret!"], "••"), "a •• b")

    def test_every_output_path_uses_it(self) -> None:
        from ophion.rdp.process import RdpProcess
        from ophion.transfer.backend import redact

        process = types.SimpleNamespace(_secrets=("1", "long-password"))
        self.assertEqual(RdpProcess.mask(process, LINE + " long-password"), LINE + " ***")
        self.assertEqual(redact("x11 hunter22", {"user": "1", "password": "hunter22"}), "x11 ********")
        self.assertEqual(redact("PASS 1"), "PASS ********")  # the FTP PASS line is masked whole, any length


if __name__ == "__main__":
    unittest.main()
