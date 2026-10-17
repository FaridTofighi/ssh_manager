import unittest
from pathlib import Path

from ssh_manager.gtk_main import parse_args


class GtkMainTests(unittest.TestCase):
    def test_parse_args_separates_gtk_extras(self) -> None:
        args, extras = parse_args(["--base-dir", "~/ssh", "--gapplication-service"])

        self.assertEqual(args.base_dir, "~/ssh")
        self.assertEqual(extras, ["--gapplication-service"])
        self.assertEqual(Path(args.base_dir).expanduser(), Path("~/ssh").expanduser())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
