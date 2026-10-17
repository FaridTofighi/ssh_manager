import unittest

from ssh_manager.password_change import PasswordChangeTracker


class PasswordChangeTrackerTests(unittest.TestCase):
    def test_detects_successful_new_password_flow(self) -> None:
        tracker = PasswordChangeTracker()

        self.assertIsNone(tracker.observe_input("Current password:", "old\r"))
        self.assertIsNone(tracker.observe_input("New password:", "new-secret\r"))
        candidate = tracker.observe_input("Retype new password:", "new-secret\r")

        self.assertEqual(candidate, "new-secret")
        self.assertEqual(
            tracker.observe_output("passwd: all authentication tokens updated successfully."),
            "new-secret",
        )

    def test_detects_unix_prompt_variant(self) -> None:
        tracker = PasswordChangeTracker()

        tracker.observe_input("Enter new UNIX password:", "new-secret\r")
        candidate = tracker.observe_input("Retype new UNIX password:", "new-secret\r")

        self.assertEqual(candidate, "new-secret")

    def test_infers_new_password_after_expiration_message_without_prompt_tail(self) -> None:
        tracker = PasswordChangeTracker()

        tracker.observe_output("WARNING: Your password has expired.\nYou must change your password now.")
        self.assertIsNone(tracker.observe_input("", "old-secret\r"))
        self.assertIsNone(tracker.observe_input("", "new-secret\r"))
        candidate = tracker.observe_input("", "new-secret\r")

        self.assertEqual(candidate, "new-secret")

    def test_infers_new_password_when_current_password_was_auto_supplied(self) -> None:
        tracker = PasswordChangeTracker()

        tracker.observe_output("You are required to change your password immediately.")
        self.assertIsNone(tracker.observe_input("", "new-secret\r"))
        candidate = tracker.observe_input("", "new-secret\r")

        self.assertEqual(candidate, "new-secret")

    def test_rejects_local_retype_mismatch(self) -> None:
        tracker = PasswordChangeTracker()

        tracker.observe_input("New password:", "first\r")
        candidate = tracker.observe_input("Retype new password:", "second\r")

        self.assertIsNone(candidate)
        self.assertIsNone(tracker.pending_candidate)
        self.assertFalse(tracker.watching_output)

    def test_remote_rejection_clears_candidate(self) -> None:
        tracker = PasswordChangeTracker()

        tracker.observe_input("New password:", "short\r")
        tracker.observe_input("Retype new password:", "short\r")
        self.assertEqual(tracker.pending_candidate, "short")

        self.assertIsNone(tracker.observe_output("BAD PASSWORD: it is too short\nNew password:"))
        self.assertIsNone(tracker.pending_candidate)
        self.assertEqual(tracker.state, "capture_new")

    def test_backspace_updates_captured_password(self) -> None:
        tracker = PasswordChangeTracker()

        tracker.observe_input("New password:", "bad\x7f\x7f\x7fgood\r")
        candidate = tracker.observe_input("Retype new password:", "good\r")

        self.assertEqual(candidate, "good")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
