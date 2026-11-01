"""The application icon (``ophion``): bundled hicolor files, the default window icon, About, packaging."""

import shutil
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from tests.gtk_window_fixture import display_available

REPO = Path(__file__).resolve().parent.parent
APP_ID = "io.github.ophion.Ophion"


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

    def test_header_logo_renders_at_1x_and_2x(self) -> None:
        import cairo

        from tests.gtk_window_fixture import WindowSandbox

        sandbox = WindowSandbox()
        self.addCleanup(sandbox.close)
        header = sandbox.window._header
        logo = header.logo_image
        self.assertEqual(header.logo.get_accessible().get_name(), "Ophion")
        self.assertTrue(header.logo.get_style_context().has_class("app-logo"))
        for scale in (1, 2):
            pixels = logo.size * scale
            self.assertEqual(logo.pixbuf_for(scale).get_width(), pixels)
            surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, pixels, pixels)
            surface.set_device_scale(scale, scale)
            self.assertTrue(logo.paint(cairo.Context(surface), scale))
            data, stride = surface.get_data(), surface.get_stride()

            def alpha(x, y):
                return data[y * stride + x * 4 + 3]

            self.assertEqual(alpha(0, 0), 0)  # rounded corner: clipped
            self.assertEqual(alpha(pixels // 2, pixels // 2), 255)  # the opaque artwork
        from ssh_manager.gtkui.widgets.app_logo import logo_pixbuf

        self.assertEqual(logo_pixbuf(600).get_width(), 512)  # never enlarged past the largest file

    def test_about_shows_the_logo(self) -> None:
        from gi.repository import Gtk

        from tests.gtk_window_fixture import WindowSandbox

        sandbox = WindowSandbox()
        self.addCleanup(sandbox.close)
        shown = []
        with mock.patch.object(Gtk.AboutDialog, "run", lambda about: shown.append(
                (about.get_program_name(), about.get_logo_icon_name(), about.get_comments())) or 0, create=True):
            sandbox.window.app_dialogs.show_about()
        self.assertEqual(shown, [("Ophion", "ophion", "Connection manager for SSH, SFTP, FTP and RDP")])
        self.assertEqual(sandbox.window.get_title(), "Ophion")
        self.assertEqual(sandbox.window._header.wordmark.get_text(), "Ophion")

    def test_a_missing_theme_icon_falls_back_to_the_file(self) -> None:
        from gi.repository import Gtk

        theme = Gtk.IconTheme.new()
        self.assertEqual(self.icons.install_app_icon(theme, Path("/nonexistent/icons")), "file")
        self.assertEqual([p.get_width() for p in Gtk.Window.get_default_icon_list()],
                         [16, 22, 24, 32, 48, 64, 96, 128, 256])
        with mock.patch.object(self.icons, "app_icon_file", lambda size=256: Path("/nonexistent/ophion.png")):
            self.assertEqual(self.icons.install_app_icon(Gtk.IconTheme.new(), Path("/nonexistent/icons")), "")


@unittest.skipUnless(display_available(), "needs a display")
class WindowClassTests(unittest.TestCase):
    def test_wm_class_is_the_desktop_files_startup_wm_class(self) -> None:
        from gi.repository import Gdk, GLib

        from tests.gtk_window_fixture import WindowSandbox

        from ssh_manager.gtk_app import APPLICATION_ID, SSHManagerApplication
        from ssh_manager.identity import APP_ID, APP_SLUG

        sandbox = WindowSandbox()  # a temporary HOME: the application reads its settings there
        self.addCleanup(sandbox.close)
        SSHManagerApplication()
        self.assertEqual(APPLICATION_ID, APP_ID)
        self.assertEqual((GLib.get_prgname(), Gdk.get_program_class()), (APP_SLUG, APP_ID))  # X11 WM_CLASS
        self.assertEqual(desktop_entry()["StartupWMClass"], APPLICATION_ID)


def desktop_entry():
    import configparser

    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str  # keys are case-sensitive
    parser.read(REPO / "packaging" / "appimage" / f"{APP_ID}.desktop", encoding="utf-8")
    return parser["Desktop Entry"]


class PackagingTests(unittest.TestCase):
    def test_desktop_entry(self) -> None:
        entry = desktop_entry()
        for key in ("Type", "Name", "Exec", "Icon", "Terminal", "Categories", "StartupWMClass"):
            self.assertTrue(entry.get(key), key)
        self.assertEqual(entry["Icon"], "ophion")
        self.assertEqual(entry["Exec"].split()[0], "ophion")  # the command (the AppImage integration rewrites it)
        self.assertEqual((entry["Name"], entry["StartupWMClass"]), ("Ophion", APP_ID))
        self.assertEqual(entry["GenericName"], "Connection manager for SSH, SFTP, FTP and RDP")
        self.assertEqual(entry["Terminal"], "false")
        if shutil.which("desktop-file-validate"):
            result = subprocess.run(["desktop-file-validate", str(REPO / f"packaging/appimage/{APP_ID}.desktop")],
                                    capture_output=True, text=True)
            self.assertEqual((result.returncode, result.stdout.strip()), (0, ""))

    def test_appstream_metainfo(self) -> None:
        import xml.etree.ElementTree as ET

        root = ET.parse(REPO / "packaging" / "appimage" / f"{APP_ID}.metainfo.xml").getroot()
        self.assertEqual((root.findtext("id"), root.findtext("name")), (APP_ID, "Ophion"))
        self.assertEqual(root.find("launchable").text, f"{APP_ID}.desktop")
        self.assertEqual(root.findtext("summary"), "Connection manager for SSH, SFTP, FTP and RDP")
        self.assertEqual(root.findtext("provides/binary"), "ophion")
        self.assertEqual(root.findtext("provides/id"), "org.codex.SSHManager")  # the id before the rename
        self.assertEqual(root.findtext("icon"), "ophion")

    def test_the_appimage_installs_every_size(self) -> None:
        script = (REPO / "packaging" / "appimage" / "build.sh").read_text(encoding="utf-8")
        self.assertIn('APP_ICONS="$DATA/ssh_manager/gtkui/assets/app-icon/hicolor"', script)
        self.assertIn('/usr/share/icons/hicolor/$(basename "$size_dir")/apps/ophion.png', script)
        self.assertIn('ln -s ophion.png "$APPDIR/.DirIcon"', script)
        self.assertIn("gtk-update-icon-cache -f -t", script)
        self.assertNotIn("ssh-manager.svg", script)  # the old icon is gone
        self.assertIn('NAME="Ophion-$VERSION-x86_64.AppImage"', script)
        self.assertIn('DESKTOP_ID="io.github.ophion.Ophion"', script)
        apprun = (REPO / "packaging" / "appimage" / "AppRun").read_text(encoding="utf-8")
        self.assertIn('export OPHION_APPDIR="$HERE"', apprun)
        self.assertIn("${OPHION_APPIMAGE_EXEC:-${SSH_MANAGER_APPIMAGE_EXEC:-}}", apprun)  # the old name works

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
