"""The application icon (``ophion``): bundled hicolor files, the default window icon, About, packaging."""

import shutil
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from tests.gtk_window_fixture import display_available

REPO = Path(__file__).resolve().parent.parent
APP_ID = "io.github.faridtofighi.Ophion"


@unittest.skipUnless(display_available(), "needs a display")
class AppIconTests(unittest.TestCase):
    def setUp(self) -> None:
        from ophion.gtkui.widgets import icons

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
            self.assertGreaterEqual(max(alpha(x, y) for x in range(pixels) for y in range(pixels)), 250)  # artwork
        from ophion.gtkui.widgets.app_logo import logo_pixbuf

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
        theme.set_search_path([])  # nothing installed counts (the app's own launcher puts ophion in ~/.local)
        self.assertEqual(self.icons.install_app_icon(theme, Path("/nonexistent/icons")), "file")
        self.assertEqual([p.get_width() for p in Gtk.Window.get_default_icon_list()],
                         [16, 22, 24, 32, 48, 64, 96, 128, 256])
        with mock.patch.object(self.icons, "app_icon_file", lambda size=256: Path("/nonexistent/ophion.png")):
            empty = Gtk.IconTheme.new()
            empty.set_search_path([])
            self.assertEqual(self.icons.install_app_icon(empty, Path("/nonexistent/icons")), "")


# Before: in the light theme the header, welcome and About marks sat on a dark plate (#212226) and the
# header mark was inset to 24 px.
# The brand block, measured before the change (identical in both themes at 700, 900 and 1366 px).
LOGO_BOX = (12, 9, 28, 28)


@unittest.skipUnless(display_available(), "needs a display")
class TransparentMarkTests(unittest.TestCase):
    """The mark is drawn as it is, transparent, in light and dark theme: no tile, no plate (owner's decision)."""

    def setUp(self) -> None:
        from ophion.gtkui.theme.loader import get_theme_manager

        self.theme = get_theme_manager()
        self.addCleanup(self.theme.apply, self.theme.mode)

    def header(self, width: int):
        from gi.repository import Gtk

        from ophion.gtkui.controllers.notifications import MessageHistory
        from ophion.gtkui.views.header import HeaderView
        from tests.test_local_terminal import pump

        top = Gtk.OffscreenWindow()
        self.addCleanup(top.destroy)
        header = HeaderView(MessageHistory())
        for child in header.get_children():  # a GtkPopover needs a real toplevel: offscreen, GDK reports criticals
            if isinstance(child, Gtk.MenuButton):
                child.set_popover(None)
        header.set_size_request(width, -1)
        top.add(header)
        top.show_all()
        Gtk.Window.show(top)
        pump(0.2)
        return header

    @staticmethod
    def box(widget, header):
        a, h = widget.get_allocation(), header.get_allocation()
        return a.x - h.x, a.y - h.y, a.width, a.height

    def test_header_mark_has_no_plate_in_either_theme(self) -> None:
        from unittest import mock as _mock

        from ophion.gtkui.theme.loader import ThemeMode
        from ophion.gtkui.theme.tokens import Size

        from tests.gtk_window_fixture import WindowSandbox

        sandbox = WindowSandbox()
        self.addCleanup(sandbox.close)
        header = sandbox.window._header
        context = header.logo.get_style_context()
        self.assertFalse(context.has_class("logo-tile"))
        flips = [(ThemeMode.LIGHT, None), (ThemeMode.DARK, None), (ThemeMode.SYSTEM, True), (ThemeMode.SYSTEM, False),
                 (ThemeMode.LIGHT, None)]
        for mode, system_dark in flips:
            with _mock.patch.object(type(self.theme), "system_prefers_dark", return_value=bool(system_dark)):
                self.theme.apply(mode)
            label = f"{mode.value} (system dark: {system_dark})"
            self.assertFalse(context.has_class("brand-plate"), label)
            self.assertEqual(context.get_property("background-color", context.get_state()).alpha, 0.0, label)
            self.assertEqual(header.logo_image.size, Size.LOGO_TILE, label)  # the full slot, never inset
            self.assertEqual(header.logo.get_size_request(), (Size.LOGO_TILE, Size.LOGO_TILE))

    def test_brand_block_does_not_move(self) -> None:
        from ophion.gtkui.theme.loader import ThemeMode

        for width in (700, 900, 1366):
            places = {}
            for mode in (ThemeMode.DARK, ThemeMode.LIGHT):
                self.theme.apply(mode)
                header = self.header(width)
                places[mode] = [self.box(widget, header) for widget in
                                (header.logo, header.wordmark, header.sidebar_toggle_button)]
                self.assertEqual(places[mode][0], LOGO_BOX, f"{mode.value} {width}px")
            self.assertEqual(places[ThemeMode.DARK], places[ThemeMode.LIGHT], f"{width}px")

    def test_transparent_pixels_show_the_header_background(self) -> None:
        from ophion.gtkui.theme.loader import ThemeMode
        from ophion.gtkui.widgets.app_logo import logo_pixbuf

        self.theme.apply(ThemeMode.LIGHT)
        header = self.header(900)
        shot = header.get_toplevel().get_pixbuf()
        data, stride, channels = shot.get_pixels(), shot.get_rowstride(), shot.get_n_channels()

        def pixel(x, y):
            offset = y * stride + x * channels
            return tuple(data[offset:offset + 3])

        lx, ly, _lw, _lh = self.box(header.logo, header)
        background = pixel(lx - 6, ly + 2)  # the header, left of the mark
        mark = logo_pixbuf(28)
        mdata, mstride = mark.get_pixels(), mark.get_rowstride()
        leaks = [(x, y) for y in range(28) for x in range(28)
                 if mdata[y * mstride + x * 4 + 3] == 0 and pixel(lx + x, ly + y) != background]
        self.assertEqual(leaks, [], f"{len(leaks)} transparent pixels are not the header background {background}")

    def test_welcome_and_about_marks_have_no_plate(self) -> None:
        from gi.repository import Gtk

        from ophion.gtkui.theme.loader import ThemeMode

        from tests import gtk_window_fixture
        from tests.gtk_window_fixture import WindowSandbox

        with mock.patch.dict(gtk_window_fixture.SAMPLE, clear=True):
            sandbox = WindowSandbox()
        self.addCleanup(sandbox.close)
        welcome = sandbox.window.host_page.terminal_page.ready.logo
        logos = []

        def capture(about):
            def walk(widget):
                if isinstance(widget, Gtk.Image) and widget.get_icon_name()[0] == "ophion":
                    context = widget.get_style_context()
                    logos.append((context.has_class("brand-plate"), context.has_class("icon-dropshadow"),
                                  widget.get_pixel_size()))
                elif isinstance(widget, Gtk.Container):
                    widget.forall(walk)
            walk(about)
            return 0

        for mode in (ThemeMode.LIGHT, ThemeMode.DARK):
            self.theme.apply(mode)
            self.assertFalse(welcome.get_style_context().has_class("brand-plate"), mode)
            logos.clear()
            with mock.patch.object(Gtk.AboutDialog, "run", capture, create=True):
                sandbox.window.app_dialogs.show_about()
            self.assertEqual(logos, [(False, False, 128)], mode)  # the mark at 128 px, no plate, no shadow

    def test_no_plate_left_in_code_or_docs(self) -> None:
        if shutil.which("git") is None or not (REPO / ".git").exists():
            self.skipTest("not a git checkout")
        found = subprocess.run(["git", "grep", "-l", "-e", "brand-plate", "-e", "follow_theme_plate", "-e", "PLATE_CLASS",
                                "-e", "brand_plate"], cwd=REPO, capture_output=True, text=True).stdout.split()
        self.assertEqual([f for f in found if f not in ("CHANGELOG-UI.md", "tests/test_app_icon.py")], [])


class IconAssetTests(unittest.TestCase):
    """The prepared spiral mark (tools/prepare_ophion_icon.py): crop + resample of the approved artwork only."""

    def opaque_box(self, path: Path):
        from gi.repository import GdkPixbuf

        pixbuf = GdkPixbuf.Pixbuf.new_from_file(str(path))
        self.assertTrue(pixbuf.get_has_alpha() and pixbuf.get_n_channels() == 4, path)
        data, stride, size = pixbuf.get_pixels(), pixbuf.get_rowstride(), pixbuf.get_width()
        self.assertEqual(pixbuf.get_height(), size, path)
        self.assertEqual(data[3], 0, f"{path}: the corner is not transparent")
        xs, ys = [], []
        for y in range(size):
            for x in range(size):
                if data[y * stride + x * 4 + 3] >= 128:
                    xs.append(x)
                    ys.append(y)
        return size, min(xs), min(ys), max(xs) + 1, max(ys) + 1

    def test_every_size_is_filled_by_the_centred_mark(self) -> None:
        from ophion.gtkui.widgets import icons

        for size in icons.APP_ICON_SIZES:
            with self.subTest(size=size):
                path = icons.app_icon_file(size)
                self.assertGreater(path.stat().st_size, 0)
                canvas, x0, y0, x1, y1 = self.opaque_box(path)
                self.assertEqual(canvas, size)
                cover = max(x1 - x0, y1 - y0) / size
                self.assertTrue(0.86 <= cover <= 0.94, f"{size} px: the mark covers {cover:.1%}")
                self.assertLessEqual(abs((x0 + x1) / 2 - size / 2), 1, f"{size} px: off centre horizontally")
                self.assertLessEqual(abs((y0 + y1) / 2 - size / 2), 1, f"{size} px: off centre vertically")

    def test_the_source_and_master_are_kept(self) -> None:
        import hashlib

        source = REPO / "data" / "icons" / "source" / "SOURCE_ORIGINAL_UNMODIFIED.png"
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(),
                         "239940402af44de0ad68d4440bf694804de4ffcce683e7f755958f0a77cd4196")  # the approved file
        self.assertEqual(self.opaque_box(REPO / "data" / "icons" / "ophion-master-1024.png")[0], 1024)

    def test_no_fake_vector_and_no_old_artwork(self) -> None:
        if shutil.which("git") is None or not (REPO / ".git").exists():
            self.skipTest("not a git checkout")
        files = subprocess.run(["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=True).stdout
        self.assertNotIn("ophion.svg", files)  # there is no vector: an SVG-wrapped PNG would mislead
        found = subprocess.run(["git", "grep", "-l", "-e", "Ophion_wordmark", "-e", "Ophion_App_Icon_Package",
                                "-e", "snake-and-loop"], cwd=REPO, capture_output=True, text=True).stdout.split()
        self.assertEqual([f for f in found if f not in ("CHANGELOG-UI.md", "tests/test_app_icon.py")], [])


@unittest.skipUnless(display_available(), "needs a display")
class WindowClassTests(unittest.TestCase):
    def test_wm_class_is_the_desktop_files_startup_wm_class(self) -> None:
        from gi.repository import Gdk, GLib

        from tests.gtk_window_fixture import WindowSandbox

        from ophion.gtk_app import APPLICATION_ID, SSHManagerApplication
        from ophion.identity import APP_ID, APP_SLUG

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
        self.assertIn('APP_ICONS="$DATA/ophion/gtkui/assets/app-icon/hicolor"', script)
        self.assertIn('/usr/share/icons/hicolor/$(basename "$size_dir")/apps/ophion.png', script)
        self.assertIn('ln -s ophion.png "$APPDIR/.DirIcon"', script)
        self.assertIn("gtk-update-icon-cache -f -t", script)
        self.assertNotIn("ssh-manager.svg", script)  # the old icon is gone
        self.assertIn('NAME="Ophion-$VERSION-x86_64.AppImage"', script)
        self.assertIn('DESKTOP_ID="io.github.faridtofighi.Ophion"', script)
        apprun = (REPO / "packaging" / "appimage" / "AppRun").read_text(encoding="utf-8")
        self.assertIn('export OPHION_APPDIR="$HERE"', apprun)
        self.assertIn("${OPHION_APPIMAGE_EXEC:-${SSH_MANAGER_APPIMAGE_EXEC:-}}", apprun)  # the old name works

    def test_every_icon_file_is_shipped(self) -> None:
        """The AppImage ships ``git archive HEAD -- ophion``: the files must exist and be tracked."""

        from ophion.gtkui.widgets import icons

        required = [icons.app_icon_file(size) for size in icons.APP_ICON_SIZES]
        for path in required:
            self.assertTrue(path.is_file(), path)
        if shutil.which("git") is None or not (REPO / ".git").exists():
            self.skipTest("not a git checkout")
        tracked = set(subprocess.run(["git", "ls-files", "--", "ophion/gtkui/assets/app-icon"], cwd=REPO,
                                     capture_output=True, text=True, check=True).stdout.split())
        for path in required:
            self.assertIn(str(path.relative_to(REPO)), tracked)


if __name__ == "__main__":
    unittest.main()
