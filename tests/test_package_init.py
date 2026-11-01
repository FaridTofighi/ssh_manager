import subprocess
import sys
import unittest


class PackageInitTests(unittest.TestCase):
    def test_package_import_is_lazy_and_exports_core_names(self) -> None:
        code = (
            "import sys, ophion\n"
            "print('ophion.password_dialog' in sys.modules)\n"
            "from ophion import ConfigManager, build_ssh_command, split_config_directive\n"
            "print(ConfigManager.__name__)\n"
            "print(build_ssh_command.__name__)\n"
            "print(split_config_directive('Host=app'))\n"
            "print('ophion.password_dialog' in sys.modules)\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            proc.stdout.strip().splitlines(),
            ["False", "ConfigManager", "build_ssh_command", "('Host', 'app')", "False"],
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
