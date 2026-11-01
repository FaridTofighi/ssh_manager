import unittest

from ophion.config import HostEntry
from ophion.credentials import Credential
from ophion.gtkui.controllers.details_presenter import parse_quick_connect
from ophion.gtkui.dialogs.host_dialog import parse_option_lines, validate_option_lines
from ophion.gtkui.widgets.secret_entry import generate_password, password_strength


def _display_available() -> bool:
    try:
        import gi

        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk  # type: ignore

        return bool(Gtk.init_check(None)[0])
    except Exception:
        return False


HAS_DISPLAY = _display_available()


class OptionLineTests(unittest.TestCase):
    def test_parse_option_lines(self) -> None:
        options, invalid = parse_option_lines("ServerAliveInterval=30\n# comment\n\nProxyCommand = nc %h %p\nbad line\nkey with space=1")
        self.assertEqual(options, {"ServerAliveInterval": "30", "ProxyCommand": "nc %h %p"})
        self.assertEqual(invalid, ["bad line", "key with space=1"])
        self.assertIsNotNone(validate_option_lines("oops"))
        self.assertIsNone(validate_option_lines("A=1\nB=2"))


class QuickConnectTests(unittest.TestCase):
    def test_parse_quick_connect(self) -> None:
        self.assertEqual(parse_quick_connect("web"), ("", "web", ""))
        self.assertEqual(parse_quick_connect("deploy@web.example.com:2222"), ("deploy", "web.example.com", "2222"))
        self.assertEqual(parse_quick_connect("root@[fe80::1]:22"), ("root", "fe80::1", "22"))
        self.assertEqual(parse_quick_connect("fe80::1"), ("", "fe80::1", ""))
        for bad in ("", "a b", "@host", "host:0", "host:99999", "[::1", "host:x"):
            self.assertIsNone(parse_quick_connect(bad), bad)


class SecretHelperTests(unittest.TestCase):
    def test_password_strength_scale(self) -> None:
        self.assertEqual(password_strength(""), 0)
        self.assertLess(password_strength("abc"), password_strength("Abcdefgh12!x"))
        self.assertEqual(password_strength("Abcdefghijklmno1!"), 4)

    def test_generated_passwords_are_long_and_varied(self) -> None:
        first, second = generate_password(), generate_password()
        self.assertEqual(len(first), 20)
        self.assertNotEqual(first, second)


@unittest.skipUnless(HAS_DISPLAY, "GTK display not available")
class HostDialogTests(unittest.TestCase):
    def _dialog(self, host=None, **kwargs):
        from ophion.gtkui.dialogs.host_dialog import HostDialog

        return HostDialog(None, host, **kwargs)

    def test_round_trip_keeps_proxycommand_and_metadata(self) -> None:
        host = HostEntry(
            alias="db",
            options={"hostname": "10.0.0.5", "port": "2222", "proxycommand": "nc -X 5 %h %p", "tags": "prod, db"},
            metadata={"environment": "prod", "permissions": {"can_view": True, "can_connect": True, "can_edit": False}},
        )
        dialog = self._dialog(host)
        try:
            self.assertEqual(dialog.proxy_entry.get_text(), "")
            built = dialog.build_host("p", "c.conf")
        finally:
            dialog.destroy()
        self.assertEqual(built.options["proxycommand"], "nc -X 5 %h %p")
        self.assertNotIn("proxyjump", built.options)
        self.assertEqual(built.options["tags"], "prod,db")
        self.assertEqual(built.metadata["environment"], "prod")
        self.assertFalse(built.metadata["permissions"]["can_edit"])

    def test_validation_blocks_bad_port_and_alias(self) -> None:
        dialog = self._dialog()
        try:
            dialog.alias_entry.set_text("bad alias")
            dialog.port_entry.set_text("70000")
            self.assertFalse(dialog.validate())
            self.assertEqual(dialog.layout.current_page, "general")
            dialog.alias_entry.set_text("good")
            dialog.port_entry.set_text("22")
            dialog.extras_view.get_buffer().set_text("not-an-option")
            self.assertFalse(dialog.validate())
            self.assertEqual(dialog.layout.current_page, "ssh_options")
            dialog.extras_view.get_buffer().set_text("ServerAliveInterval=30")
            self.assertTrue(dialog.validate())
        finally:
            dialog.destroy()

    def test_credential_username_overrides_user(self) -> None:
        cred = Credential(id="c1", name="Prod", username="ops")
        dialog = self._dialog(credentials=[cred], selected_credential_id="c1")
        try:
            dialog.alias_entry.set_text("web")
            self.assertFalse(dialog.user_entry.get_sensitive())
            built = dialog.build_host("p", "c")
            self.assertEqual(built.options["user"], "ops")
            self.assertEqual(dialog.selected_credential_id(), "c1")
        finally:
            dialog.destroy()


@unittest.skipUnless(HAS_DISPLAY, "GTK display not available")
class PreferencesDialogTests(unittest.TestCase):
    def test_values_round_trip_settings(self) -> None:
        from ophion.gtkui.dialogs.preferences import PreferencesDialog
        from ophion.gtkui.theme.loader import ThemeMode
        from ophion.settings import AppSettings

        settings = AppSettings()
        settings.host_key_policy = "auto-add"
        settings.allowed_ciphers = ["aes128-ctr"]
        settings.git_watch_interval = 900
        dialog = PreferencesDialog(None, ThemeMode.LIGHT, 1.2, 18, settings)
        try:
            values = dialog.values()
        finally:
            dialog.destroy()
        self.assertIs(values.theme_mode, ThemeMode.LIGHT)
        self.assertFalse(values.prefer_dark)
        self.assertEqual(values.ui_font_size, 18)
        self.assertAlmostEqual(values.terminal_font_scale, 1.2)
        self.assertEqual(values.host_key_policy, "auto-add")
        self.assertEqual(values.allowed_ciphers, ["aes128-ctr"])
        self.assertEqual(values.git_watch_interval, 900)

    def test_legacy_bool_theme_argument(self) -> None:
        from ophion.gtkui.dialogs.preferences import PreferencesDialog
        from ophion.gtkui.theme.loader import ThemeMode
        from ophion.settings import AppSettings

        dialog = PreferencesDialog(None, True, 1.0, 14, AppSettings())
        try:
            self.assertIs(dialog.values().theme_mode, ThemeMode.DARK)
        finally:
            dialog.destroy()


@unittest.skipUnless(HAS_DISPLAY, "GTK display not available")
class SecretDialogTests(unittest.TestCase):
    def test_new_secret_requires_match_and_current_password(self) -> None:
        from gi.repository import Gtk  # type: ignore

        from ophion.gtkui.dialogs.secrets import NewSecretDialog

        dialog = NewSecretDialog(None, "t", "h", verify_current=lambda value: value == "old")
        try:
            dialog.current.set_text("wrong")
            dialog.new.set_text("new-pass")
            dialog.confirm.set_text("different")
            self.assertFalse(dialog._validate(Gtk.ResponseType.OK))
            dialog.current.set_text("old")
            dialog.confirm.set_text("new-pass")
            self.assertTrue(dialog._validate(Gtk.ResponseType.OK))
            self.assertEqual((dialog.current_password, dialog.new_password), ("old", "new-pass"))
        finally:
            dialog.destroy()

    def test_unlock_dialog_uses_verifier(self) -> None:
        from gi.repository import Gtk  # type: ignore

        from ophion.gtkui.dialogs.secrets import UnlockDialog

        dialog = UnlockDialog(None, "t", "h", "d", lambda value: value == "secret")
        try:
            dialog.secret.set_text("nope")
            self.assertFalse(dialog._validate(Gtk.ResponseType.OK))
            self.assertEqual(dialog.secret.get_text(), "")
            dialog.secret.set_text("secret")
            self.assertTrue(dialog._validate(Gtk.ResponseType.OK))
            self.assertEqual(dialog.accepted_password, "secret")
        finally:
            dialog.destroy()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
