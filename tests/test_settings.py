import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from ophion.settings import AppSettings


class AppSettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.config_path = Path(self.tmpdir.name) / "settings.json"

    def test_load_defaults_when_missing(self) -> None:
        settings = AppSettings.load(self.config_path)
        self.assertTrue(settings.remember_password_default)
        self.assertEqual(settings.host_key_policy, "reject")
        self.assertEqual(settings.config_path, self.config_path)
        self.assertFalse(settings.use_paramiko_vte)
        self.assertFalse(settings.master_password_disabled)
        self.assertEqual(settings.allowed_ciphers, [])
        self.assertEqual(settings.allowed_kex, [])

    def test_save_and_reload(self) -> None:
        settings = AppSettings.load(self.config_path)
        settings.remember_password_default = False
        settings.host_key_policy = "auto-add"
        settings.connect_timeout = 20.5
        settings.auth_timeout = 25.0
        settings.banner_timeout = 12.0
        settings.use_paramiko_vte = True
        settings.master_password_disabled = True
        settings.allowed_ciphers = ["aes256-ctr"]
        settings.allowed_kex = ["curve25519-sha256"]
        settings.save()

        self.assertEqual(stat.S_IMODE(os.stat(self.config_path).st_mode), 0o600)
        loaded = AppSettings.load(self.config_path)
        self.assertFalse(loaded.remember_password_default)
        self.assertEqual(loaded.host_key_policy, "auto-add")
        self.assertEqual(loaded.connect_timeout, 20.5)
        self.assertEqual(loaded.auth_timeout, 25.0)
        self.assertEqual(loaded.banner_timeout, 12.0)
        self.assertTrue(loaded.use_paramiko_vte)
        self.assertTrue(loaded.master_password_disabled)
        self.assertEqual(loaded.allowed_ciphers, ["aes256-ctr"])
        self.assertEqual(loaded.allowed_kex, ["curve25519-sha256"])

    def test_invalid_json_returns_defaults(self) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text("not json", encoding="utf-8")
        settings = AppSettings.load(self.config_path)
        self.assertTrue(settings.remember_password_default)

    def test_wrong_json_shape_returns_defaults(self) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text("[]", encoding="utf-8")

        settings = AppSettings.load(self.config_path)

        self.assertTrue(settings.remember_password_default)
        self.assertEqual(settings.host_key_policy, "reject")

    def test_non_finite_numeric_settings_return_defaults(self) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(
            '{"timeouts": {"connect": NaN, "auth": Infinity, "banner": -Infinity}, '
            '"git_watch_interval": NaN, '
            '"terminal_appearance": {"font_size": Infinity}}',
            encoding="utf-8",
        )

        settings = AppSettings.load(self.config_path)

        self.assertEqual(settings.connect_timeout, 10.0)
        self.assertEqual(settings.auth_timeout, 10.0)
        self.assertEqual(settings.banner_timeout, 10.0)
        self.assertEqual(settings.git_watch_interval, 600)
        self.assertIsNone(settings.terminal_appearance.font_size)

    def test_settings_resilient_to_wrong_types(self) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "remember_password_default": "yes",
            "host_key_policy": "accept-anything",
            "timeouts": {
                "connect": -1,
                "auth": 30,
                "banner": 700.0,
            },
            "use_paramiko_vte": "true",
            "master_password_disabled": "yes",
            "allowed_ciphers": [" ", "aes256-ctr"],
            "allowed_kex": [123, " good "],
            "git_watch_interval": 10,
        }
        self.config_path.write_text(json.dumps(data), encoding="utf-8")
        settings = AppSettings.load(self.config_path)
        self.assertTrue(settings.remember_password_default)
        self.assertEqual(settings.host_key_policy, "reject")
        self.assertEqual(settings.connect_timeout, 10.0)
        self.assertEqual(settings.auth_timeout, 30.0)
        self.assertEqual(settings.banner_timeout, 10.0)
        self.assertFalse(settings.use_paramiko_vte)
        self.assertFalse(settings.master_password_disabled)
        self.assertEqual(settings.allowed_ciphers, ["aes256-ctr"])
        self.assertEqual(settings.allowed_kex, ["good"])
        self.assertEqual(settings.git_watch_interval, 600)

    def test_terminal_appearance_ignores_wrong_boolean_types(self) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "terminal_appearance": {
                "use_system_mono_font": "false",
                "use_custom_colors": "true",
                "cursor_blink": "false",
                "bold_is_bright": "false",
                "custom_font": 123,
                "font_size": -1,
                "color_scheme": "light",
                "palette": [" #000000 ", "", 123, "#ffffff"],
            }
        }
        self.config_path.write_text(json.dumps(data), encoding="utf-8")

        settings = AppSettings.load(self.config_path)
        appearance = settings.terminal_appearance
        self.assertTrue(appearance.use_system_mono_font)
        self.assertFalse(appearance.use_custom_colors)
        self.assertTrue(appearance.cursor_blink)
        self.assertTrue(appearance.bold_is_bright)
        self.assertIsNone(appearance.custom_font)
        self.assertIsNone(appearance.font_size)
        self.assertEqual(appearance.color_scheme, "light")
        self.assertEqual(appearance.palette, ["#000000", "#ffffff"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
