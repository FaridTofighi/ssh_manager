"""R8: security invariants the refactor must keep (run by the gate on its own as well as in the suite).

* the key and crypto modules are not touched: byte-identical to the ``pre-refactor`` tag (and not moved);
* no secret in exception text: a wrong export passphrase or master password never shows up in an error;
* the spawn goldens (``test_spawn_golden``) assert that a password is never in argv and only in ``SSHPASS``,
  the persistence goldens (``test_persistence_golden``) the 0600 / 0700 modes and the frozen identifiers;
  ``test_secret_mask``, ``test_auth_flow`` and the store tests cover masking, logs and atomic writes.
"""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UNTOUCHED = ("ophion/aead.py", "ophion/keybox.py", "ophion/secure_store.py", "ophion/unlock.py")
TAG = "pre-refactor"


def git(*args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True)


@unittest.skipUnless(shutil.which("git") and git("rev-parse", "--verify", f"{TAG}^{{commit}}").returncode == 0,
                     f"needs a git checkout with the {TAG} tag")
class UntouchedFilesTests(unittest.TestCase):
    def test_key_and_crypto_modules_are_byte_identical_to_the_tag(self) -> None:
        for name in UNTOUCHED:
            with self.subTest(file=name):
                before = git("show", f"{TAG}:{name}")
                self.assertEqual(before.returncode, 0, f"{name} is not in {TAG}")
                self.assertTrue((ROOT / name).is_file(), f"{name} moved or was removed")
                self.assertEqual((ROOT / name).read_bytes(), before.stdout, f"{name} changed since {TAG}")


class NoSecretInErrorsTests(unittest.TestCase):
    def test_a_wrong_export_passphrase_is_not_in_the_error(self) -> None:
        from ophion.importers.native import read_native
        from tests.refactor.make_fixtures import FIXTURES

        wrong = "Wr0ng-passphrase-value"
        with self.assertRaises(Exception) as caught:
            read_native((FIXTURES / "rich-encrypted.ophion").read_bytes(), wrong)
        self.assertNotIn(wrong, str(caught.exception))
        self.assertNotIn(wrong, repr(caught.exception.args))

    def test_a_wrong_master_password_is_refused_without_an_error_text(self) -> None:
        from ophion.keybox import Keybox
        from tests.refactor.make_fixtures import FIXTURES

        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "keys"
            shutil.copytree(FIXTURES / "keybox", folder)
            folder.chmod(0o700)
            for path in folder.iterdir():
                path.chmod(0o600)
            keybox = Keybox.load(folder / "keybox.json")
            self.assertFalse(keybox.unlock("Wr0ng-master-password"))


if __name__ == "__main__":
    unittest.main()
