"""The Credential Vault was removed: only its migration module may still mention it."""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ALLOWED_FILES = {"legacy_vault.py"}  # ssh_manager/legacy_vault.py and its GTK dialogs (migration only)
# Code may refer to the migration module by name (imports, LEGACY_METADATA_KEYS …), nothing else.
MODULE_TOKEN = re.compile(r"legacy_vault", re.IGNORECASE)
WORD = re.compile(r"vault", re.IGNORECASE)


class NoVaultLeftTests(unittest.TestCase):
    def test_code_and_ui_strings(self) -> None:
        found = []
        for path in sorted((ROOT / "ssh_manager").rglob("*.py")):
            if path.name in ALLOWED_FILES:
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if WORD.search(MODULE_TOKEN.sub("", line)):
                    found.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()}")
        self.assertEqual(found, [], "\n".join(found))


if __name__ == "__main__":
    unittest.main()
