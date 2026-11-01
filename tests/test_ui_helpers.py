import unittest

from ophion.ui import format_extra_options, parse_extra_options


class UiHelperTests(unittest.TestCase):
    def test_parse_extra_options_preserves_commas_inside_values(self) -> None:
        parsed, error = parse_extra_options(
            "ProxyJump=bastion1,bastion2, Compression=yes, LocalForward=8080 localhost:80"
        )

        self.assertIsNone(error)
        self.assertEqual(
            parsed,
            {
                "proxyjump": "bastion1,bastion2",
                "compression": "yes",
                "localforward": "8080 localhost:80",
            },
        )

    def test_parse_extra_options_reports_invalid_chunks(self) -> None:
        parsed, error = parse_extra_options("Compression=yes, missing-value")

        self.assertEqual(parsed, {})
        self.assertEqual(error, "Invalid extra option 'missing-value'. Use key=value.")

    def test_parse_extra_options_rejects_bad_keys_and_line_breaks(self) -> None:
        parsed, error = parse_extra_options("Bad Key=value")
        self.assertEqual(parsed, {})
        self.assertIn("must not contain whitespace", error or "")

        parsed, error = parse_extra_options("ProxyCommand=ssh host\nUser=root")
        self.assertEqual(parsed, {})
        self.assertIn("must not contain line breaks", error or "")

    def test_format_extra_options_excludes_base_fields_case_insensitively(self) -> None:
        self.assertEqual(
            format_extra_options(
                {
                    "HostName": "app.internal",
                    "User": "alice",
                    "IdentityFile": "~/.ssh/id",
                    "Compression": "yes",
                }
            ),
            "Compression=yes",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
