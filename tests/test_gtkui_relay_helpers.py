import tempfile
import unittest
from pathlib import Path

from ophion.gtkui.health import compute_health
from ophion.gtkui.identity import user_initials
from ophion.gtkui.theme.tokens import ConnectionStatus, Level
from ophion.gtkui.workspaces import (
    RECENT_LIMIT,
    WorkspaceKind,
    remember_workspace,
    workspace_list,
    workspace_name,
)


class HealthTests(unittest.TestCase):
    def test_operational_without_sessions_or_problems(self) -> None:
        health = compute_health([])
        self.assertEqual((health.level, health.text), (Level.SUCCESS, "All systems operational"))

    def test_active_sessions_are_counted(self) -> None:
        health = compute_health([ConnectionStatus.CONNECTED, "connected", ConnectionStatus.CONNECTING])
        self.assertEqual((health.level, health.text), (Level.SUCCESS, "2 sessions active"))
        self.assertEqual(compute_health(["connected"]).text, "1 session active")

    def test_failed_sessions_win_over_everything(self) -> None:
        health = compute_health(["connected", "failed"], warnings=3, errors=1)
        self.assertEqual((health.level, health.text), (Level.ERROR, "1 session failed"))

    def test_unread_problems(self) -> None:
        self.assertEqual(compute_health([], errors=2).text, "2 errors")
        self.assertEqual(compute_health(["connected"], warnings=1).level, Level.WARNING)
        self.assertEqual(compute_health([], warnings=1).text, "1 warning")


class IdentityTests(unittest.TestCase):
    def test_initials(self) -> None:
        self.assertEqual(user_initials("Ada Fernandez", "ada"), "AF")
        self.assertEqual(user_initials("Ada Maria Fernandez"), "AF")
        self.assertEqual(user_initials("", "farid"), "F")
        self.assertEqual(user_initials("cher", ""), "C")
        self.assertEqual(user_initials("", ""), "U")
        self.assertEqual(user_initials(",,,", "_svc"), "U")


class WorkspaceTests(unittest.TestCase):
    def test_names(self) -> None:
        self.assertEqual(workspace_name(Path("/srv/Infrastructure")), "Infrastructure")
        self.assertEqual(workspace_name(Path("/srv/shared/inventory.yaml")), "inventory")

    def test_remember_dedupes_and_bounds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = [Path(tmp) / f"inv{i}" for i in range(RECENT_LIMIT + 2)]
            recent: list = []
            for path in paths:
                recent = remember_workspace(recent, path)
            recent = remember_workspace(recent, paths[-3])
            self.assertEqual(len(recent), RECENT_LIMIT)
            self.assertEqual(recent[0], str(paths[-3].resolve()))
            self.assertEqual(len(set(recent)), len(recent))

    def test_list_puts_current_first_and_shared_last(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            current, other = Path(tmp) / "Infrastructure", Path(tmp) / "Lab"
            shared = Path(tmp) / "team.yaml"
            items = workspace_list(current, [str(current), str(other), ""], shared)
            self.assertEqual([w.name for w in items], ["Infrastructure", "Lab", "team"])
            self.assertTrue(items[0].current)
            self.assertFalse(items[1].current)
            self.assertIs(items[2].kind, WorkspaceKind.SHARED)
            self.assertTrue(items[2].read_only)


if __name__ == "__main__":
    unittest.main()
