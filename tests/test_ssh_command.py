import unittest

from ssh_manager.config import HostEntry
from ssh_manager.ssh_command import build_ssh_command


class SshCommandTests(unittest.TestCase):
    def test_builds_basic_connection_arguments(self) -> None:
        host = HostEntry(
            alias="app",
            options={
                "hostname": "app.internal",
                "user": "deploy",
                "port": "2222",
                "identityfile": "~/.ssh/id_ed25519",
            },
        )

        self.assertEqual(
            build_ssh_command(host),
            [
                "ssh",
                "-p",
                "2222",
                "-i",
                "~/.ssh/id_ed25519",
                "deploy@app.internal",
            ],
        )

    def test_proxy_command_takes_precedence_over_proxy_jump(self) -> None:
        host = HostEntry(
            alias="db",
            options={
                "hostname": "db.internal",
                "proxyjump": "bastion",
                "proxycommand": "ssh -W %h:%p gateway",
                "Compression": "yes",
            },
        )

        command = build_ssh_command(host)

        self.assertIn("-o", command)
        self.assertIn("ProxyCommand=ssh -W %h:%p gateway", command)
        self.assertNotIn("-J", command)
        self.assertIn("Compression=yes", command)
        self.assertEqual(command[-1], "db.internal")

    def test_filters_app_only_and_literal_secret_options(self) -> None:
        host = HostEntry(
            alias="app",
            options={
                "hostname": "app.internal",
                "user": "root",
                "tags": "prod,web",
                "vault_id": "vault-entry",
                "password": "secret",
                "PasswordAuthentication": "no",
                "NumberOfPasswordPrompts": 1,
                "ForwardAgent": True,
                "EmptyOption": "",
                "UnsetOption": None,
            },
        )

        command = build_ssh_command(host)
        rendered = " ".join(command)

        self.assertNotIn("tags=", rendered)
        self.assertNotIn("vault_id=", rendered)
        self.assertNotIn("password=secret", rendered)
        self.assertIn("PasswordAuthentication=no", command)
        self.assertIn("NumberOfPasswordPrompts=1", command)
        self.assertIn("ForwardAgent=yes", command)
        self.assertNotIn("EmptyOption=", rendered)
        self.assertNotIn("UnsetOption=", rendered)

    def test_option_lookup_is_case_insensitive(self) -> None:
        host = HostEntry(
            alias="fallback",
            options={
                "HostName": "case.internal",
                "User": "ops",
                "Port": 2200,
                "ProxyJump": "jumpbox",
            },
        )

        self.assertEqual(
            build_ssh_command(host),
            ["ssh", "-p", "2200", "-J", "jumpbox", "ops@case.internal"],
        )

    def test_invalid_port_is_not_emitted(self) -> None:
        host = HostEntry(
            alias="app",
            options={"hostname": "app.internal", "port": "70000"},
        )

        self.assertEqual(build_ssh_command(host), ["ssh", "app.internal"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
