import hashlib
import unittest

from ophion.master_auth import (
    hash_master_password,
    master_hash_needs_upgrade,
    verify_master_password,
)


class MasterAuthTests(unittest.TestCase):
    def test_pbkdf2_hash_verifies_and_uses_random_salt(self) -> None:
        first = hash_master_password("secret")
        second = hash_master_password("secret")

        self.assertNotEqual(first, second)
        self.assertTrue(first.startswith("pbkdf2_sha256$"))
        self.assertTrue(verify_master_password("secret", first))
        self.assertFalse(verify_master_password("wrong", first))
        self.assertFalse(master_hash_needs_upgrade(first))

    def test_legacy_sha256_hash_still_verifies_and_needs_upgrade(self) -> None:
        legacy = hashlib.sha256(b"secret").hexdigest()

        self.assertTrue(verify_master_password("secret", legacy))
        self.assertFalse(verify_master_password("wrong", legacy))
        self.assertTrue(master_hash_needs_upgrade(legacy))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
