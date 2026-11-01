"""No new code reads the old product's ids or folders: every quoted ``ssh-manager`` / ``org.codex.SSHManager``
literal in ophion/ is in tests/rename_allowlist.txt with its reason (docs/rename-audit.md)."""

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
EXEMPT = {"ophion/identity.py", "ophion/legacy_paths.py"}
LITERAL = re.compile(r"""["']([^"'\n]*(?:ssh-manager|org\.codex\.SSHManager)[^"'\n]*)["']""")


def allowlist():
    entries = set()
    for line in (REPO / "tests" / "rename_allowlist.txt").read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith("#"):
            path, literal, reason = (part.strip() for part in line.split(" | ", 2))
            assert reason, line
            entries.add((path, literal))
    return entries


def found():
    hits = set()
    for path in sorted((REPO / "ophion").rglob("*.py")):
        relative = str(path.relative_to(REPO))
        if relative in EXEMPT:
            continue
        for match in LITERAL.finditer(path.read_text(encoding="utf-8")):
            hits.add((relative, match.group(1)))
    return hits


class RenameGuardTests(unittest.TestCase):
    def test_no_unlisted_old_name(self) -> None:
        unlisted = sorted(found() - allowlist())
        self.assertEqual(unlisted, [], "old-name literals outside identity.py / legacy_paths.py: add the new name "
                                       "from ophion.identity, or list them with a reason")

    def test_no_stale_allowlist_entry(self) -> None:
        self.assertEqual(sorted(allowlist() - found()), [])

    def test_the_old_application_id_lives_in_identity_only(self) -> None:
        self.assertFalse([hit for hit in found() if "org.codex" in hit[1]])


if __name__ == "__main__":
    unittest.main()
