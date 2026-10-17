import unittest

from ssh_manager.config import HostEntry
from ssh_manager.host_utils import build_scp_command, host_config_key, host_connection_params, host_option, with_user


class HostUtilsTests(unittest.TestCase):
    def test_host_connection_params_defaults_and_invalid_port(self) -> None:
        host = HostEntry(
            alias="app",
            options={"hostname": "app.internal", "port": "not-a-number"},
            parent_group="prod",
            child_group="web.conf",
        )
        self.assertEqual(
            host_connection_params(host, default_user="alice"),
            ("app.internal", 22, "alice"),
        )

    def test_host_entry_display_helpers_are_case_insensitive(self) -> None:
        host = HostEntry(
            alias="app",
            options={
                "HostName": "app.internal",
                "User": "deploy",
                "IdentityFile": "~/.ssh/id",
            },
        )

        self.assertEqual(host.display_title(), "app.internal")
        self.assertEqual(
            dict(host.summary_pairs()),
            {
                "Hostname": "app.internal",
                "User": "deploy",
                "Identityfile": "~/.ssh/id",
            },
        )

    def test_host_connection_params_are_case_insensitive_and_validate_port_range(self) -> None:
        host = HostEntry(
            alias="app",
            options={"HostName": "app.internal", "User": "deploy", "Port": "70000"},
        )
        self.assertEqual(host_connection_params(host), ("app.internal", 22, "deploy"))

        host.options["Port"] = "2200"
        self.assertEqual(host_connection_params(host), ("app.internal", 2200, "deploy"))
        self.assertEqual(host_option(host, "hostname"), "app.internal")

    def test_host_config_key_and_with_user(self) -> None:
        host = HostEntry(
            alias="db",
            options={"hostname": "db.internal", "User": "old"},
            metadata={"owner": "platform", "permissions": {"can_edit": False}},
            parent_group="prod",
            child_group="db.conf",
        )
        updated = with_user(host, "postgres")

        self.assertEqual(host_config_key(host), "prod::db.conf::db")
        self.assertEqual(updated.options["user"], "postgres")
        self.assertEqual(host.options["User"], "old")
        self.assertNotIn("User", updated.options)
        self.assertEqual(updated.metadata, host.metadata)
        updated.metadata["permissions"]["can_edit"] = True
        self.assertFalse(host.metadata["permissions"]["can_edit"])

    def test_build_scp_command_uses_case_insensitive_connection_options(self) -> None:
        host = HostEntry(
            alias="app",
            options={
                "HostName": "app.internal",
                "User": "deploy",
                "Port": "2222",
                "IdentityFile": "~/.ssh/id_ed25519",
                "ProxyJump": "bastion",
            },
        )

        self.assertEqual(
            build_scp_command(host),
            [
                "scp",
                "-P",
                "2222",
                "-i",
                "~/.ssh/id_ed25519",
                "-J",
                "bastion",
                "deploy@app.internal:/remote/path",
                "/local/path",
            ],
        )

    def test_build_scp_command_filters_invalid_port_and_prefers_proxy_command(self) -> None:
        host = HostEntry(
            alias="db",
            options={
                "hostname": "db.internal",
                "port": "99999",
                "proxyjump": "bastion",
                "proxycommand": "ssh -W %h:%p gateway",
            },
        )

        self.assertEqual(
            build_scp_command(host),
            ["scp", "-o", "ProxyCommand=ssh -W %h:%p gateway", "db.internal:/remote/path", "/local/path"],
        )

    def test_build_scp_command_includes_safe_extra_options_and_filters_secrets(self) -> None:
        host = HostEntry(
            alias="app",
            options={
                "hostname": "app.internal",
                "ForwardAgent": True,
                "Compression": "yes",
                "password": "secret",
                "tags": "prod",
            },
        )

        command = build_scp_command(host, "/var/log/app.log", "./app.log")
        rendered = " ".join(command)

        self.assertIn("ForwardAgent=yes", command)
        self.assertIn("Compression=yes", command)
        self.assertNotIn("password=secret", rendered)
        self.assertNotIn("tags=prod", rendered)
        self.assertEqual(command[-2:], ["app.internal:/var/log/app.log", "./app.log"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
