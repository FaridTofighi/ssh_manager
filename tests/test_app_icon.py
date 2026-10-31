"""The application icon (``ophion``): bundled hicolor files, the default window icon, About, packaging."""

import shutil
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from tests.gtk_window_fixture import display_available

REPO = Path(__file__).resolve().parent.parent


@unittest.skipUnless(display_available(), "needs a display")
class AppIconTests(unittest.TestCase):
    def setUp(self) -> None:
        from ssh_manager.gtkui.widgets import icons

        self.icons = icons
        self.addCleanup(icons.install_app_icon)  # the real default again for the next tests

    def test_the_bundled_theme_has_every_size(self) -> None:
        from gi.repository import GdkPixbuf, Gtk

        theme = Gtk.IconTheme.new()
        theme.append_search_path(str(self.icons.APP_ICON_DIR))
        self.assertTrue(theme.has_icon("ophion"))
        info = theme.lookup_icon("ophion", 48, 0)
        self.assertTrue(info.get_filename().endswith("hicolor/48x48/apps/ophion.png"))
        for size in self.icons.APP_ICON_SIZES:
            pixbuf = GdkPixbuf.Pixbuf.new_from_file(str(self.icons.app_icon_file(size)))
            self.assertEqual((pixbuf.get_width(), pixbuf.get_height()), (size, size))

    def test_windows_and_dialogs_get_the_default_icon(self) -> None:
        from gi.repository import Gtk

        from tests.gtk_window_fixture import WindowSandbox

        self.assertEqual(self.icons.install_app_icon(), "name")
        self.assertEqual(Gtk.Window.get_default_icon_name(), "ophion")
        sandbox = WindowSandbox()
        self.addCleanup(sandbox.close)
        dialog = Gtk.Dialog(transient_for=sandbox.window)
        self.addCleanup(dialog.destroy)
        for window in (sandbox.window, dialog):  # nothing sets an icon of its own: they all inherit
            self.assertIn(window.get_icon_name(), (None, "ophion"))
            self.assertIsNone(window.get_icon())

    def test_the_welcome_state_shows_the_mark_at_128_px(self) -> None:
        from gi.repository import Gtk

        from tests import gtk_window_fixture
        from tests.gtk_window_fixture import WindowSandbox

        self.icons.install_app_icon()
        with mock.patch.dict(gtk_window_fixture.SAMPLE, clear=True):  # no hosts: the welcome
            sandbox = WindowSandbox()
        self.addCleanup(sandbox.close)
        sandbox.window.host_page.refresh()
        ready = sandbox.window.host_page.terminal_page.ready
        Gtk.Box.show_all(ready)  # what showing the window does: must not bring the card back
        self.assertEqual(ready.title.get_text(), "Add your first host")
        self.assertTrue(ready.logo.get_visible())
        self.assertFalse(ready._card.get_visible())
        self.assertEqual((ready.logo.get_icon_name()[0], ready.logo.get_pixel_size()), ("ophion", 128))
        info = Gtk.IconTheme.get_default().lookup_icon("ophion", 128, 0)
        self.assertTrue(info.get_filename().endswith("128x128/apps/ophion.png"))  # its own size, never stretched
        ready.set_content("Ready to connect", "", "Connect", lambda: None)  # any other state: the card again
        self.assertEqual((ready.logo.get_visible(), ready._card.get_visible()), (False, True))

    def test_about_shows_the_logo(self) -> None:
        from gi.repository import Gtk

        from tests.gtk_window_fixture import WindowSandbox

        sandbox = WindowSandbox()
        self.addCleanup(sandbox.close)
        shown = []
        with mock.patch.object(Gtk.AboutDialog, "run", lambda about: shown.append(about.get_logo_icon_name()) or 0,
                               create=True):
            sandbox.window.app_dialogs.show_about()
        self.assertEqual(shown, ["ophion"])

    def test_a_missing_theme_icon_falls_back_to_the_file(self) -> None:
        from gi.repository import Gtk

        theme = Gtk.IconTheme.new()
        self.assertEqual(self.icons.install_app_icon(theme, Path("/nonexistent/icons")), "file")
        self.assertEqual([p.get_width() for p in Gtk.Window.get_default_icon_list()], [256])
        with mock.patch.object(self.icons, "app_icon_file", lambda size=256: Path("/nonexistent/ophion.png")):
            self.assertEqual(self.icons.install_app_icon(Gtk.IconTheme.new(), Path("/nonexistent/icons")), "")


class PackagingTests(unittest.TestCase):
    def test_every_icon_file_is_shipped(self) -> None:
        """The AppImage ships ``git archive HEAD -- ssh_manager``: the files must exist and be tracked."""

        from ssh_manager.gtkui.widgets import icons

        required = [icons.app_icon_file(size) for size in icons.APP_ICON_SIZES]
        for path in required:
            self.assertTrue(path.is_file(), path)
        if shutil.which("git") is None or not (REPO / ".git").exists():
            self.skipTest("not a git checkout")
        tracked = set(subprocess.run(["git", "ls-files", "--", "ssh_manager/gtkui/assets/app-icon"], cwd=REPO,
                                     capture_output=True, text=True, check=True).stdout.split())
        for path in required:
            self.assertIn(str(path.relative_to(REPO)), tracked)


if __name__ == "__main__":
    unittest.main()
