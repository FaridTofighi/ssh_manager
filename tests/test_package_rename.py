"""The package is ``ophion``; ``python3 -m ssh_manager`` (the command before the rename) still starts it."""

import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


class PackageRenameTests(unittest.TestCase):
    def run_help(self, *argv):
        return subprocess.run([sys.executable, *argv, "--help"], cwd=REPO, capture_output=True, text=True,
                              timeout=60, env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent"})

    def test_every_way_to_start_it(self) -> None:
        for argv in (["main.py"], ["-m", "ophion"], ["-m", "ssh_manager"]):
            result = self.run_help(*argv)
            self.assertEqual(result.returncode, 0, (argv, result.stderr[-500:]))
            self.assertTrue(result.stdout.startswith("usage: ophion"), argv)
        self.assertIn("python3 -m ophion", self.run_help("-m", "ssh_manager").stderr)

    def test_the_old_package_only_forwards(self) -> None:
        self.assertEqual(sorted(p.name for p in (REPO / "ssh_manager").iterdir() if p.suffix == ".py"),
                         ["__init__.py", "__main__.py"])
        with self.assertRaises(ImportError):
            __import__("ssh_manager.config")  # code imports ophion now


if __name__ == "__main__":
    unittest.main()
