import tempfile
import unittest
from pathlib import Path

from ophion.config import ConfigManager


class CliDetectionTests(unittest.TestCase):
    def test_config_file_base_dir_picks_up_confd_hosts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            ssh_dir = Path(tmpdir) / ".ssh"
            confd = ssh_dir / "conf.d"
            confd.mkdir(parents=True)

            include_target = confd / "servers.conf"
            include_target.write_text(
                "Host api\n  HostName api.internal\n",
                encoding="utf-8",
            )

            config_file = ssh_dir / "config"
            config_file.write_text(
                "Include conf.d/*.conf\n"
                "Host root\n  HostName root.internal\n",
                encoding="utf-8",
            )

            manager = ConfigManager(config_file)
            tree = manager.tree.get(ssh_dir.name, {})
            group = tree.get(config_file.name)
            self.assertIsNotNone(group, "config file should become a child group")
            assert group is not None
            aliases = {h.alias for h in group.hosts}
            self.assertTrue({"api", "root"} <= aliases)
            api_host = next(h for h in group.hosts if h.alias == "api")
            self.assertEqual(api_host.source_file, include_target)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
