import subprocess
import sys
import unittest


class FaridScriptTests(unittest.TestCase):
    def test_import_has_no_side_effect_output(self) -> None:
        proc = subprocess.run(
            [sys.executable, "-c", "import farid; print(farid.parse_args(['--base-dir', '/tmp']).base_dir)"],
            check=True,
            capture_output=True,
            text=True,
        )

        self.assertEqual(proc.stdout.strip(), "/tmp")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
