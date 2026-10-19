import re
import unittest
from pathlib import Path

from ssh_manager.gtkui.constants import Icons, RowKind
from ssh_manager.gtkui.theme.density import COMFORTABLE, COMPACT
from ssh_manager.gtkui.theme.tokens import DARK, LIGHT, contrast_ratio
from ssh_manager.gtkui.tree_icons import (
    TreeIcon,
    badge_box,
    badge_diameter,
    cutout_ring,
    icon_color,
    lock_box,
    lock_size,
    pulse_alpha,
    quantize_alpha,
    show_status_badge,
    status_badge,
    tree_icon_for,
)

ICON_DIR = Path(__file__).resolve().parent.parent / "ssh_manager" / "gtkui" / "assets" / "icons"
TREE_FAMILY = (Icons.FOLDER, Icons.FOLDER_OPEN, Icons.FILE_CONF, Icons.HOST, Icons.BADGE_DOT, Icons.BADGE_RING,
               Icons.BADGE_FAILED, Icons.BADGE_CUTOUT, Icons.BADGE_LOCK)


class TreeIconForTests(unittest.TestCase):
    def test_groups(self) -> None:
        for kind in (RowKind.PARENT, RowKind.CHILD_FOLDER, "parent", "child-folder"):
            with self.subTest(kind=kind):
                self.assertEqual(tree_icon_for(kind), TreeIcon(Icons.FOLDER, "icon_default"))
                self.assertEqual(tree_icon_for(kind, expanded=True), TreeIcon(Icons.FOLDER_OPEN, "icon_default"))

    def test_empty_group_keeps_the_closed_folder(self) -> None:
        self.assertEqual(tree_icon_for("parent", expanded=True, has_children=False).name, Icons.FOLDER)

    def test_config_file_group(self) -> None:
        self.assertEqual(tree_icon_for(RowKind.CHILD), TreeIcon(Icons.FILE_CONF, "icon_default"))
        self.assertEqual(tree_icon_for(RowKind.CHILD, expanded=True).name, Icons.FILE_CONF)  # never "opens"
        self.assertEqual(tree_icon_for(RowKind.PARENT, file_backed_group=True).name, Icons.FILE_CONF)
        self.assertEqual(tree_icon_for(RowKind.CHILD, file_backed_group=False).name, Icons.FOLDER)

    def test_hosts(self) -> None:
        self.assertEqual(tree_icon_for("host"), TreeIcon(Icons.HOST, "icon_default"))
        self.assertEqual(tree_icon_for("host", selected=True), tree_icon_for("host"))  # no state color
        self.assertEqual(tree_icon_for("host", read_only=True), TreeIcon(Icons.HOST, "icon_default", lock=True))
        self.assertFalse(tree_icon_for("parent", read_only=True).lock)  # the lock is a host overlay

    def test_all_four_roles_are_distinct_icons(self) -> None:
        names = {tree_icon_for("parent").name, tree_icon_for("parent", True).name,
                 tree_icon_for("child").name, tree_icon_for("host").name}
        self.assertEqual(len(names), 4)

    def test_user_color_wins_and_clearing_restores_the_default(self) -> None:
        for kind in ("parent", "child", "child-folder", "host"):
            with self.subTest(kind=kind):
                self.assertEqual(icon_color(DARK, tree_icon_for(kind), "#123456"), "#123456")
                self.assertEqual(icon_color(DARK, tree_icon_for(kind, selected=True), "#123456"), "#123456")
                self.assertEqual(icon_color(DARK, tree_icon_for(kind), ""), "#FFFFFF")  # "Clear Color"
                self.assertEqual(icon_color(LIGHT, tree_icon_for(kind), ""), "#18231F")


class DensityTests(unittest.TestCase):
    def test_sizes_per_density(self) -> None:
        # (icon, row, chevron, icon ↔ label gap) from the brief.
        self.assertEqual((COMPACT.tree.ICON, COMPACT.size.TREE_ROW, COMPACT.tree.CHEVRON, COMPACT.tree.ICON_GAP),
                         (20, 30, 14, 8))
        self.assertEqual((COMFORTABLE.tree.ICON, COMFORTABLE.size.TREE_ROW, COMFORTABLE.tree.CHEVRON,
                          COMFORTABLE.tree.ICON_GAP), (24, 41, 16, 10))
        self.assertEqual(COMPACT.size.TREE_GROUP_ROW, 30)

    def test_icon_fits_the_row_with_room(self) -> None:
        for preset in (COMPACT, COMFORTABLE):
            with self.subTest(density=preset.density):
                self.assertLessEqual(preset.tree.ICON + 8, preset.size.TREE_ROW)

    def test_host_bar_tile_variant(self) -> None:
        self.assertEqual((COMPACT.size.HOST_TILE_ICON, COMFORTABLE.size.HOST_TILE_ICON), (24, 28))
        for preset in (COMPACT, COMFORTABLE):
            self.assertLessEqual(preset.size.HOST_TILE_ICON, preset.size.HOST_TILE)


class TokenTests(unittest.TestCase):
    ICON_DEFAULT = {"dark": "#FFFFFF", "light": "#18231F"}  # Phase 9b
    LEGACY = ("icon_group", "icon_file", "icon_host", "icon_host_selected")

    def test_one_icon_default_per_palette(self) -> None:
        for palette in (DARK, LIGHT):
            with self.subTest(palette=palette.name):
                self.assertEqual(palette.icon_default.upper(), self.ICON_DEFAULT[palette.name])
                for role in self.LEGACY:
                    self.assertEqual(getattr(palette, role), palette.icon_default, role)
        self.assertEqual(LIGHT.icon_default, LIGHT.text_primary)

    def test_no_literal_icon_colors_outside_the_tokens(self) -> None:
        root = Path(__file__).resolve().parent.parent / "ssh_manager"
        banned = re.compile(r"#(FFFFFF|18231F|E2B04F|B07A1E|D9A84E|A57828|A3A8B0|5B6B64|5CCB9C|176B4D)\b", re.I)
        found = []
        for path in root.rglob("*.py"):
            if path.parent.name == "theme":  # the token modules (palettes, terminal color schemes)
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if banned.search(line):
                    found.append(f"{path.relative_to(root)}:{number}")
        self.assertEqual(found, [])

    def test_icon_and_badge_colors_reach_3_to_1_on_every_row_background(self) -> None:
        roles = ("icon_default", "badge_connected", "warning", "danger", "badge_unknown")
        for palette in (DARK, LIGHT):
            for role in roles:
                for background in ("bg_surface", "bg_hover", "bg_selected"):
                    with self.subTest(palette=palette.name, role=role, background=background):
                        ratio = contrast_ratio(getattr(palette, role), getattr(palette, background))
                        self.assertGreaterEqual(ratio, 3.0)

    def test_white_icons_on_dark_are_far_above_the_floor(self) -> None:
        for background in ("bg_surface", "bg_hover", "bg_selected"):
            self.assertGreaterEqual(contrast_ratio(DARK.icon_default, getattr(DARK, background)), 13.0)
            self.assertGreaterEqual(contrast_ratio(LIGHT.icon_default, getattr(LIGHT, background)), 13.0)


def _triple(badge):
    return badge.name, badge.color_role, badge.shape


class BadgeTests(unittest.TestCase):
    def test_status_to_badge(self) -> None:
        for size in (20, 24):
            with self.subTest(size=size):
                self.assertEqual(_triple(status_badge("connected", size)), (Icons.BADGE_DOT, "badge_connected", "dot"))
                connecting = status_badge("connecting", size)
                self.assertEqual(_triple(connecting), (Icons.BADGE_RING, "warning", "ring"))
                self.assertTrue(connecting.pulse)
                self.assertEqual(_triple(status_badge("unknown", size)), (Icons.BADGE_DOT, "badge_unknown", "dot"))
                self.assertEqual(status_badge("bogus", size).color_role, "badge_unknown")
        self.assertEqual(_triple(status_badge("failed", 20)), (Icons.BADGE_DOT, "danger", "dot"))  # too small for ×
        self.assertEqual(_triple(status_badge("failed", 24)), (Icons.BADGE_FAILED, "danger", "cross"))
        self.assertFalse(status_badge("connected", 20).pulse)

    def test_shapes_differ_where_size_allows(self) -> None:
        shapes = {status_badge(s, 24).shape for s in ("connected", "connecting", "failed")}
        self.assertEqual(shapes, {"dot", "ring", "cross"})

    def test_geometry(self) -> None:
        self.assertEqual((badge_diameter(20), badge_diameter(24)), (8, 10))  # ≈ 42 %
        self.assertEqual((lock_size(20), lock_size(24)), (10, 12))  # 50 %
        self.assertEqual((cutout_ring(16), cutout_ring(20), cutout_ring(24)), (1.5, 2.0, 2.0))
        box = badge_box(20)
        self.assertEqual((box.x + box.size, box.y + box.size), (20, 20))  # bottom-right, inside the icon
        self.assertEqual(lock_box(20, with_badge=False).y, 10)
        self.assertEqual(lock_box(20, with_badge=True).y, 0)

    def test_badge_visibility(self) -> None:
        self.assertTrue(show_status_badge("host", "unknown"))
        self.assertTrue(show_status_badge("host", "connected", read_only=True))
        self.assertFalse(show_status_badge("host", "unknown", read_only=True))  # the lock alone
        self.assertFalse(show_status_badge("parent", "connected"))

    def test_pulse_is_slow_and_quantized(self) -> None:
        samples = [pulse_alpha(t / 100.0) for t in range(0, 250)]
        self.assertGreaterEqual(min(samples), 0.44)
        self.assertLessEqual(max(samples), 1.0)
        # ≤ 1 Hz: one full period takes at least a second.
        self.assertAlmostEqual(pulse_alpha(0.0), pulse_alpha(1.25), places=6)
        self.assertNotAlmostEqual(pulse_alpha(0.0), pulse_alpha(0.5), places=2)
        self.assertEqual(len({quantize_alpha(a) for a in samples}), 5)


class SvgSourceTests(unittest.TestCase):
    """GTK's symbolic recoloring rewrites ``fill`` and ignores ``stroke``: ship filled paths only."""

    def test_tree_family_is_fill_only_and_tintable(self) -> None:
        for name in TREE_FAMILY:
            with self.subTest(icon=name):
                text = (ICON_DIR / f"{name}-symbolic.svg").read_text(encoding="utf-8")
                self.assertNotRegex(text, r"\bstroke[-=:\"]", "stroked outline (renders as a blob in GTK)")
                self.assertIn("currentColor", text)
                self.assertNotIn("<style", text)
                self.assertIn('viewBox="0 0 24 24"' if not name.startswith("sshm-badge") else 'viewBox="0 0 12 12"', text)

    def test_duotone_fill_is_a_second_path_at_20_percent(self) -> None:
        for name in (Icons.FOLDER, Icons.FOLDER_OPEN, Icons.FILE_CONF, Icons.HOST):
            with self.subTest(icon=name):
                text = (ICON_DIR / f"{name}-symbolic.svg").read_text(encoding="utf-8")
                self.assertIn('opacity="0.2"', text)
                self.assertGreaterEqual(len(re.findall(r"<path", text)), 3)

    def test_old_drafts_and_monitor_host_icon_are_gone(self) -> None:
        for draft in ("sshm-folder.svg", "sshm-folder-open.svg", "sshm-file-conf.svg", "sshm-server.svg"):
            self.assertFalse((ICON_DIR / draft).exists(), draft)
        self.assertNotEqual(Icons.HOST, "sshm-monitor")


class TreeRowReadOnlyTests(unittest.TestCase):
    def test_shared_and_permission_restricted_hosts_are_read_only(self) -> None:
        from ssh_manager.config import GroupConfig, HostEntry
        from ssh_manager.gtkui.controllers.tree_controller import TreeCallbacks, build_tree_rows

        hosts = [HostEntry(alias=a, options={}, parent_group="prod", child_group="web.conf") for a in ("a", "b")]
        group = GroupConfig(parent="prod", name="web.conf", path=Path("web.conf"), hosts=hosts)
        tree = {"prod": {"web.conf": group}}
        callbacks = TreeCallbacks(read_only=lambda host: host.alias == "b")
        rows = build_tree_rows([(tree, False)], callbacks=callbacks)
        flags = {row.alias: row.read_only for row in rows[0].walk() if row.kind is RowKind.HOST}
        self.assertEqual(flags, {"a": False, "b": True})
        shared = build_tree_rows([(tree, True)])
        self.assertTrue(all(row.read_only for row in shared[0].walk() if row.kind is RowKind.HOST))


if __name__ == "__main__":
    unittest.main()
