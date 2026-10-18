"""Tree rows rendered to cairo surfaces: icon size, shape, color, badge and cut-out ring.

Each row goes through ``RelayRowRenderer.render()`` (the tree's own painter) onto
an image surface pre-filled with ``bg_surface``; normal, hover (PRELIT) and
selected rows get their real backgrounds from the renderer.
"""

import os
import time
import unittest
from contextlib import contextmanager
from unittest import mock


def _display_available() -> bool:
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return False
    try:
        import gi

        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk  # type: ignore

        return Gtk.init_check(None)[0]
    except Exception:
        return False


def _hex(value: str):
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


def _dist(a, b) -> int:
    return max(abs(x - y) for x, y in zip(a, b))


class Raster:
    """RGB access to a cairo ARGB32 surface (opaque, so premultiplication is moot)."""

    def __init__(self, surface) -> None:
        surface.flush()
        self.width, self.height = surface.get_width(), surface.get_height()
        self.stride = surface.get_stride()
        self.data = bytes(surface.get_data())

    def rgb(self, x: int, y: int):
        offset = int(y) * self.stride + int(x) * 4
        b, g, r = self.data[offset], self.data[offset + 1], self.data[offset + 2]
        return r, g, b

    def box_pixels(self, x0, y0, w, h):
        for y in range(max(0, int(y0)), min(self.height, int(y0 + h))):
            for x in range(max(0, int(x0)), min(self.width, int(x0 + w))):
                yield x, y, self.rgb(x, y)


@unittest.skipUnless(_display_available(), "needs a display")
class TreeIconRenderTests(unittest.TestCase):
    USER_COLOR = "#3366CC"

    def setUp(self) -> None:
        from gi.repository import Gdk  # type: ignore

        from ssh_manager.config import HostEntry
        from ssh_manager.gtkui.constants import RowKind
        from ssh_manager.gtkui.controllers.tree_controller import TreeRow
        from ssh_manager.gtkui.views.session_tree import SessionTreeView

        def host(alias, status="unknown", read_only=False):
            entry = HostEntry(alias=alias, options={"hostname": alias}, parent_group="prod", child_group="web.conf")
            return TreeRow(RowKind.HOST, alias, "prod", child="web.conf", alias=alias, host=entry,
                           status=status, read_only=read_only)

        self.hosts = {
            "up": host("up", "connected"), "down": host("down", "failed"), "wait": host("wait", "connecting"),
            "idle": host("idle"), "locked": host("locked", read_only=True),
            "locked-up": host("locked-up", "connected", read_only=True),
        }
        web = TreeRow(RowKind.CHILD, "web.conf", "prod", child="web.conf", count=6, children=list(self.hosts.values()))
        db_conf = TreeRow(RowKind.CHILD, "primary.conf", "prod", child="db/primary.conf", count=1,
                          children=[host("db-1")])
        db = TreeRow(RowKind.CHILD_FOLDER, "db", "prod", child="db", count=1, children=[db_conf])
        prod = TreeRow(RowKind.PARENT, "prod", "prod", count=7, children=[db, web])
        legacy = TreeRow(RowKind.PARENT, "legacy", "legacy", count=1, color=self.USER_COLOR,
                         children=[TreeRow(RowKind.CHILD, "old.conf", "legacy", child="old.conf", count=1,
                                           children=[host("old")])])
        self.tree = SessionTreeView()
        self.tree.show()  # an unshown tree keeps a 1 px allocation
        self.tree.populate([prod, legacy])
        self.tree.set_row_collapsed("parent", "legacy", "", True)
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = 0, 0, 320, 900
        self.tree.size_allocate(rect)

    def tearDown(self) -> None:
        self.tree.destroy()

    # ------------------------------------------------------------ helpers
    def path_of(self, text: str):
        found = []
        self.tree.store.foreach(lambda model, path, it: found.append(path.copy())
                                if model.get_value(it, 1) == text else False)
        self.assertTrue(found, text)
        return found[0]

    def render(self, text: str, flags=None, *, scale: int = 1):
        """Render the row ``text``: ``(raster, geometry, renderer props, icon surfaces drawn)``."""

        import cairo
        from gi.repository import Gdk, Gtk  # type: ignore

        from ssh_manager.gtkui.theme.loader import current_palette
        from ssh_manager.gtkui.views import session_tree

        tree = self.tree
        path = self.path_of(text)
        renderer = tree.row_renderer
        tree._row_data(None, renderer, tree.store, tree.store.get_iter(path))
        height = renderer.do_get_preferred_height(tree)[0]
        width = tree.get_allocated_width()
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width * scale, height * scale)
        surface.set_device_scale(scale, scale)
        cr = cairo.Context(surface)
        cr.set_source_rgb(*(c / 255.0 for c in _hex(current_palette().bg_surface)))
        cr.paint()
        area = Gdk.Rectangle()
        area.x, area.y, area.width, area.height = 0, 0, width, height
        drawn = []
        real = session_tree.render_tree_icon

        def spy(*args, **kwargs):
            result = real(*args, **kwargs)
            drawn.append((args, kwargs, result))
            return result

        with mock.patch.object(session_tree, "render_tree_icon", side_effect=spy), \
                mock.patch.object(type(tree), "get_scale_factor", return_value=scale):
            renderer.render(cr, tree, area, area, flags or Gtk.CellRendererState(0))
        geometry = session_tree.row_geometry(renderer.props.kind, renderer.props.depth)
        return Raster(surface), geometry, height, drawn

    def icon_box(self, geometry, height, scale=1):
        from ssh_manager.gtkui.theme.tokens import Layout

        size = Layout.Tree.ICON
        top = round(height / 2.0 - size / 2.0)
        return geometry["icon"] * scale, top * scale, size * scale

    def background(self, flags):
        from gi.repository import Gtk  # type: ignore

        from ssh_manager.gtkui.theme.loader import current_palette

        palette = current_palette()
        if flags & Gtk.CellRendererState.SELECTED:
            return _hex(palette.bg_selected)
        if flags & Gtk.CellRendererState.PRELIT:
            return _hex(palette.bg_hover)
        return _hex(palette.bg_surface)

    @contextmanager
    def densities(self):
        from ssh_manager.gtkui.theme import density

        for name in ("compact", "comfortable"):
            with density.using(name), self.subTest(density=name):
                self.tree.clear_icon_cache()
                yield_value = name
                yield yield_value
                break

    def each_density(self):
        from ssh_manager.gtkui.theme import density

        for name in ("compact", "comfortable"):
            with density.using(name):
                self.tree.clear_icon_cache()
                yield name

    def ink(self, raster, box, bg, color):
        """Pixels of ``box`` that carry at least half of the icon color's contrast (outline, not duotone)."""

        x0, y0, size = box
        full = _dist(_hex(color), bg)
        return [(x, y, rgb) for x, y, rgb in raster.box_pixels(x0, y0, size, size) if _dist(rgb, bg) >= full * 0.5]

    # -------------------------------------------------------------- tests
    def test_icon_names_per_row(self) -> None:
        from ssh_manager.gtkui.constants import Icons

        expected = {"prod": Icons.FOLDER_OPEN, "db": Icons.FOLDER_OPEN, "primary.conf": Icons.FILE_CONF,
                    "web.conf": Icons.FILE_CONF, "legacy": Icons.FOLDER, "up": Icons.HOST}
        for text, name in expected.items():
            with self.subTest(row=text):
                _raster, _geometry, _height, drawn = self.render(text)
                self.assertEqual(len(drawn), 1)
                self.assertEqual(drawn[0][0][0], name)

    def test_icon_size_centering_and_shape(self) -> None:
        from ssh_manager.gtkui.theme.loader import current_palette
        from ssh_manager.gtkui.theme.tokens import Layout

        palette = current_palette()
        for density_name in self.each_density():
            for text, color in (("prod", palette.icon_group), ("legacy", self.USER_COLOR), ("web.conf", palette.icon_file),
                                ("idle", palette.icon_host)):
                with self.subTest(density=density_name, row=text):
                    raster, geometry, height, drawn = self.render(text)
                    size = Layout.Tree.ICON
                    self.assertEqual(size, {"compact": 20, "comfortable": 24}[density_name])
                    surface = drawn[0][2]
                    self.assertEqual((surface.get_width(), surface.get_height()), (size, size))
                    bg = _hex(palette.bg_surface)
                    box = self.icon_box(geometry, height)
                    ink = self.ink(raster, box, bg, color)
                    ys = [y for _x, y, _c in ink]
                    ink_height = max(ys) - min(ys) + 1
                    # The glyphs keep the 24-grid margins of the approved sheet (folder ≈ 17/24).
                    self.assertGreaterEqual(ink_height, size * 0.65)
                    self.assertLessEqual(ink_height, size)
                    self.assertLessEqual(abs((max(ys) + min(ys)) / 2.0 - height / 2.0), 2.0, "not centered on the row")
                    ratio = len(ink) / float(size * size)
                    self.assertGreater(ratio, 0.08, "empty icon")
                    self.assertLess(ratio, 0.55, "solid blob: a stroke rendered as a fill")

    def test_colors_selected_and_user_tint(self) -> None:
        from gi.repository import Gtk  # type: ignore

        from ssh_manager.gtkui.theme.loader import current_palette

        palette = current_palette()
        selected = Gtk.CellRendererState.SELECTED
        for text, flags, color in (("idle", selected, palette.icon_host_selected), ("idle", 0, palette.icon_host),
                                   ("prod", selected, palette.icon_group), ("legacy", 0, self.USER_COLOR),
                                   ("legacy", selected, self.USER_COLOR)):
            with self.subTest(row=text, selected=bool(flags)):
                raster, geometry, height, _drawn = self.render(text, Gtk.CellRendererState(flags))
                bg = self.background(Gtk.CellRendererState(flags))
                ink = self.ink(raster, self.icon_box(geometry, height), bg, color)
                strongest = max(ink, key=lambda item: _dist(item[2], bg))[2]
                self.assertLessEqual(_dist(strongest, _hex(color)), 10, f"{strongest} is not {color}")

    def test_badge_color_and_cutout_ring_on_every_background(self) -> None:
        from gi.repository import Gtk  # type: ignore

        from ssh_manager.gtkui.theme.loader import current_palette
        from ssh_manager.gtkui.theme.tokens import Layout
        from ssh_manager.gtkui.tree_icons import badge_box, cutout_ring

        palette = current_palette()
        states = {"up": palette.badge_connected, "down": palette.danger, "idle": palette.badge_unknown,
                  "wait": palette.warning}
        flag_sets = (Gtk.CellRendererState(0), Gtk.CellRendererState.PRELIT, Gtk.CellRendererState.SELECTED)
        for density_name in self.each_density():
            size = Layout.Tree.ICON
            badge = badge_box(size)
            ring = cutout_ring(size)
            for text, color in states.items():
                for flags in flag_sets:
                    with self.subTest(density=density_name, row=text, flags=int(flags)):
                        # Scale 2: the 2 px ring is 4 device px wide, so a whole pixel sits inside it.
                        k = 2
                        with mock.patch("ssh_manager.gtkui.views.session_tree.animations_enabled", return_value=False):
                            raster, geometry, height, _drawn = self.render(text, flags, scale=k)
                        x0, y0, _size = self.icon_box(geometry, height, k)
                        cx, cy = x0 + badge.center[0] * k, y0 + badge.center[1] * k
                        # The ring badge is hollow on the right: sample the filled left half.
                        sample_x = cx - badge.size * k / 4.0 if text == "wait" else cx
                        # The failed badge has an × cut through its centre (24 px): sample between its arms.
                        sample_y = cy + badge.size * k / 4.0 if text == "down" else cy
                        self.assertLessEqual(_dist(raster.rgb(sample_x, sample_y), _hex(color)), 12)
                        # Straight above the badge, inside the cut-out ring: the row's own background.
                        ring_y = cy - (badge.size / 2.0 + ring / 2.0) * k
                        self.assertLessEqual(_dist(raster.rgb(cx, ring_y), self.background(flags)), 6)

    def test_cutout_removes_icon_ink(self) -> None:
        from ssh_manager.gtkui.constants import Icons
        from ssh_manager.gtkui.tree_icons import badge_box, cutout_ring
        from ssh_manager.gtkui.widgets.icons import render_tree_icon

        k = 2  # device pixels per logical pixel
        for size in (20, 24):
            with self.subTest(size=size):
                plain = Raster(render_tree_icon(Icons.HOST, "#FFFFFF", size, k, cache={}))
                badged = Raster(render_tree_icon(Icons.HOST, "#FFFFFF", size, k, badge=Icons.BADGE_DOT,
                                                 badge_color="#FF0000", cache={}))
                badge = badge_box(size)
                cx, cy = (v * k for v in badge.center)
                y = cy - (badge.size / 2.0 + cutout_ring(size) / 2.0) * k
                alpha = lambda raster: raster.data[int(y) * raster.stride + int(cx) * 4 + 3]  # noqa: E731
                self.assertGreater(alpha(plain), 100, "no icon ink to cut at the probe point")
                self.assertEqual(alpha(badged), 0)

    def test_read_only_lock(self) -> None:
        from ssh_manager.gtkui.constants import Icons

        _raster, _geometry, _height, drawn = self.render("locked")
        self.assertTrue(drawn[0][1]["lock"])
        self.assertEqual(drawn[0][1]["badge"], "")  # status unknown: the lock alone
        _raster, _geometry, _height, drawn = self.render("locked-up")
        self.assertTrue(drawn[0][1]["lock"])
        self.assertEqual(drawn[0][1]["badge"], Icons.BADGE_DOT)
        _raster, _geometry, _height, drawn = self.render("up")
        self.assertFalse(drawn[0][1]["lock"])

    def test_right_edge_shows_counts_only(self) -> None:
        from ssh_manager.gtkui.theme.loader import current_palette

        palette = current_palette()
        bg = _hex(palette.bg_surface)
        for text in ("up", "down", "wait"):
            with self.subTest(row=text):
                raster, _geometry, height, _drawn = self.render(text)
                right = [rgb for _x, _y, rgb in raster.box_pixels(raster.width - 40, 0, 40, height)]
                self.assertTrue(all(_dist(rgb, bg) <= 2 for rgb in right), "something is drawn at the right edge")
        raster, _geometry, height, _drawn = self.render("web.conf")
        right = [rgb for _x, _y, rgb in raster.box_pixels(raster.width - 40, 0, 40, height)]
        self.assertTrue(any(_dist(rgb, bg) > 20 for rgb in right), "group count missing")

    def test_hidpi_icons_double_and_stay_crisp(self) -> None:
        from ssh_manager.gtkui.theme.loader import current_palette

        palette = current_palette()
        bg = _hex(palette.bg_surface)
        full = _dist(_hex(palette.icon_host), bg)
        results = {}
        for scale in (1, 2):
            raster, geometry, height, drawn = self.render("idle", scale=scale)
            surface = drawn[0][2]
            box = self.icon_box(geometry, height, scale)
            self.assertEqual(surface.get_width(), box[2])  # rendered at device pixels, not scaled up
            ink = self.ink(raster, box, bg, palette.icon_host)
            ys = [y for _x, y, _c in ink]
            sharpest = 0
            x0, y0, size = box
            for y in range(int(y0), int(y0 + size)):
                for x in range(int(x0), int(x0 + size) - 1):
                    sharpest = max(sharpest, _dist(raster.rgb(x, y), raster.rgb(x + 1, y)))
            results[scale] = (max(ys) - min(ys) + 1, sharpest)
        self.assertLessEqual(abs(results[2][0] - 2 * results[1][0]), 2, results)
        self.assertGreaterEqual(results[2][1], full * 0.8, "blurry at scale 2")

    def test_connecting_badge_pulses_only_with_animations(self) -> None:
        from ssh_manager.gtkui.views import session_tree

        renderer = self.tree.row_renderer
        alphas = {}
        for enabled in (True, False):
            seen = set()
            for phase in (0.0, 0.3, 0.6):
                renderer.pulse_phase = phase
                with mock.patch.object(session_tree, "animations_enabled", return_value=enabled):
                    _raster, _geometry, _height, drawn = self.render("wait")
                seen.add(drawn[0][1]["badge_alpha"])
            alphas[enabled] = seen
        self.assertGreater(len(alphas[True]), 1)
        self.assertEqual(alphas[False], {1.0})
        self.assertTrue(renderer.saw_connecting)

    def test_pulse_timer_runs_only_while_a_host_is_connecting(self) -> None:
        from ssh_manager.gtkui.views import session_tree

        tree = self.tree
        with mock.patch.object(session_tree, "animations_enabled", return_value=True):
            tree.row_renderer.saw_connecting = True
            tree._after_draw()
            self.assertTrue(tree.pulsing)
            tree.row_renderer.saw_connecting = False
            tree._after_draw()
            self.assertFalse(tree.pulsing)
        with mock.patch.object(session_tree, "animations_enabled", return_value=False):
            tree.row_renderer.saw_connecting = True
            tree._after_draw()
            self.assertFalse(tree.pulsing)

    def test_drag_icon_uses_the_new_row_painter(self) -> None:
        from gi.repository import Gtk  # type: ignore

        from ssh_manager.gtkui.views import session_tree

        tree = self.tree
        window = Gtk.OffscreenWindow()  # a drag icon needs a realized tree
        window.add(tree)
        window.show_all()
        for _ in range(300):
            if not Gtk.events_pending():
                break
            Gtk.main_iteration_do(False)
        drawn = []
        real = session_tree.render_tree_icon
        with mock.patch.object(session_tree, "render_tree_icon",
                               side_effect=lambda *a, **k: drawn.append(a[0]) or real(*a, **k)):
            surface = tree.create_row_drag_icon(self.path_of("up"))
        self.assertIsNotNone(surface)
        self.assertIn("sshm-server", drawn)
        window.remove(tree)
        window.destroy()

    def test_theme_switch_recolors_without_restart(self) -> None:
        from ssh_manager.gtkui.theme.tokens import DARK, LIGHT

        colors = {}
        for palette in (DARK, LIGHT):
            with mock.patch("ssh_manager.gtkui.views.session_tree.current_palette", return_value=palette), \
                    mock.patch("ssh_manager.gtkui.theme.loader.current_palette", return_value=palette):
                _raster, _geometry, _height, drawn = self.render("prod")
                colors[palette.name] = drawn[0][0][1]
        self.assertEqual(colors, {"dark": DARK.icon_group, "light": LIGHT.icon_group})


@unittest.skipUnless(_display_available(), "needs a display")
class GtkSymbolicPathTests(unittest.TestCase):
    """The tree family also survives GTK's own symbolic loader (the recoloring pitfall)."""

    def test_not_a_blob_and_not_empty_through_gtk(self) -> None:
        from gi.repository import Gdk, Gtk  # type: ignore

        from ssh_manager.gtkui.constants import Icons
        from ssh_manager.gtkui.widgets.icons import register_icon_path

        register_icon_path()
        theme = Gtk.IconTheme.get_default()
        fg = Gdk.RGBA()
        fg.parse("#202020")
        for name in (Icons.FOLDER, Icons.FOLDER_OPEN, Icons.FILE_CONF, Icons.HOST):
            for size in (20, 24):
                with self.subTest(icon=name, size=size):
                    info = theme.lookup_icon(f"{name}-symbolic", size, Gtk.IconLookupFlags.FORCE_SIZE)
                    self.assertIsNotNone(info)
                    pixbuf, was_symbolic = info.load_symbolic(fg, None, None, None)
                    self.assertTrue(was_symbolic)
                    pixels = pixbuf.get_pixels()
                    stride, channels = pixbuf.get_rowstride(), pixbuf.get_n_channels()
                    opaque = sum(1 for y in range(pixbuf.get_height()) for x in range(pixbuf.get_width())
                                 if pixels[y * stride + x * channels + 3] >= 128)
                    ratio = opaque / float(pixbuf.get_width() * pixbuf.get_height())
                    self.assertGreater(ratio, 0.08)
                    self.assertLess(ratio, 0.55)


@unittest.skipUnless(_display_available(), "needs a display")
class TreePerformanceTests(unittest.TestCase):
    def test_2000_hosts_render_from_cache(self) -> None:
        import cairo
        from gi.repository import Gdk, Gtk, Rsvg  # type: ignore

        from ssh_manager.gtkui.constants import RowKind
        from ssh_manager.gtkui.controllers.tree_controller import TreeRow
        from ssh_manager.gtkui.views.session_tree import SessionTreeView

        states = ("connected", "failed", "connecting", "unknown")
        groups = []
        for g in range(20):
            hosts = [TreeRow(RowKind.HOST, f"h{g}-{i}", f"g{g}", child="hosts.conf", alias=f"h{g}-{i}",
                             status=states[i % 4], read_only=i % 7 == 0) for i in range(100)]
            conf = TreeRow(RowKind.CHILD, "hosts.conf", f"g{g}", child="hosts.conf", count=100, children=hosts)
            groups.append(TreeRow(RowKind.PARENT, f"g{g}", f"g{g}", count=100, children=[conf]))
        tree = SessionTreeView()
        tree.show()
        tree.populate(groups)
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = 0, 0, 320, 900
        tree.size_allocate(rect)
        iters = []
        tree.store.foreach(lambda model, path, it: iters.append(it.copy()) or False)
        self.assertEqual(len(iters), 2040)
        renderer = tree.row_renderer
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 320, 41)
        cr = cairo.Context(surface)
        area = Gdk.Rectangle()
        area.x, area.y, area.width, area.height = 0, 0, 320, 41

        def paint_all():
            start = time.perf_counter()
            for it in iters:
                tree._row_data(None, renderer, tree.store, it)
                renderer.render(cr, tree, area, area, Gtk.CellRendererState(0))
            return time.perf_counter() - start

        paint_all()  # warm the icon cache
        with mock.patch.object(Rsvg.Handle, "new_from_data", side_effect=AssertionError("SVG parsed while drawing")):
            elapsed = paint_all()
        self.assertLess(len(tree.icon_cache), 80, "icon cache grows per row")
        self.assertLess(elapsed / len(iters), 0.002, f"{elapsed:.2f}s for {len(iters)} rows")
        tree.destroy()


@unittest.skipUnless(_display_available(), "needs a display")
class TreeIconWindowTests(unittest.TestCase):
    def setUp(self) -> None:
        from tests.gtk_window_fixture import WindowSandbox

        self.sandbox = WindowSandbox()
        self.win = self.sandbox.window

    def tearDown(self) -> None:
        self.sandbox.close()

    def test_status_change_updates_the_tooltip(self) -> None:
        from ssh_manager.gtkui.constants import TreeColumn

        host = self.win.tree.find_host_by_alias("web-01")
        key = self.win.creds.host_key(host)
        for state, text in (("connected", "connected"), ("failed", "failed"), ("connecting", "connecting")):
            with self.subTest(state=state):
                self.win.tree.set_host_status(host, state)
                tooltip = self.win.tree_store.get_value(self.win.tree.host_iters[key], TreeColumn.TOOLTIP)
                self.assertIn(text, tooltip.lower())

    def test_group_picker_rows_carry_the_tree_icons(self) -> None:
        from gi.repository import Gtk  # type: ignore

        from ssh_manager.gtkui.dialogs.group_picker import GroupPickerDialog

        dialog = GroupPickerDialog(self.win, self.win.manager, "Move")
        try:
            for combo in (dialog._parent_combo, dialog._child_combo):
                with self.subTest(combo=combo):
                    cells = combo.get_cells()
                    self.assertIsInstance(cells[0], Gtk.CellRendererPixbuf)
                    model = combo.get_model()
                    tree_iter = model.get_iter_first()
                    self.assertIsNotNone(tree_iter)
                    combo.get_area().apply_attributes(model, tree_iter, False, False)
                    self.assertIsNotNone(cells[0].get_property("surface"))
        finally:
            dialog.destroy()


if __name__ == "__main__":
    unittest.main()
