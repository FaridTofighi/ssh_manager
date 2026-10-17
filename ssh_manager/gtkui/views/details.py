"""Details drawer for the selected host, group or multi-selection.

The header row (status, title, subtitle, chips and actions) is always visible;
the facts grid below it collapses (F9). Host facts use a fixed set of rows and
show "—" for empty values, so the layout does not jump between hosts, and the
title/subtitle facts (alias, user@host) are not repeated in the grid.
"""

from __future__ import annotations

from typing import Callable, List, Tuple

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Pango  # type: ignore  # noqa: E402

from ..constants import Icons  # noqa: E402
from ..i18n import _  # noqa: E402
from ..menu_spec import MenuItemSpec, MenuSection  # noqa: E402
from ..theme.loader import get_theme_manager  # noqa: E402
from ..theme.tokens import Motion, Space  # noqa: E402
from ..view_models import DetailAction, DetailContent, placeholder_content  # noqa: E402
from ..widgets.chips import ChipFlow  # noqa: E402
from ..widgets.icon_button import IconButton, IconMenuButton, set_accessible_name  # noqa: E402
from ..widgets.menus import build_menu  # noqa: E402
from ..widgets.status_dot import StatusDot  # noqa: E402

EMPTY_VALUE = "—"


class DetailsView(Gtk.Box):
    """Collapsible details drawer."""

    def __init__(self) -> None:
        Gtk.Box.__init__(self, orientation=Gtk.Orientation.VERTICAL, spacing=Space.S)
        self.get_style_context().add_class("details-drawer")
        self.on_expanded_changed: Callable[[bool], None] = lambda _e: None

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.M)
        self.status_dot = StatusDot(size=12)
        self.status_dot.set_no_show_all(True)
        header.pack_start(self.status_dot, False, False, 0)

        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.title_label = Gtk.Label()
        self.title_label.set_xalign(0.0)
        self.title_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.title_label.set_selectable(True)
        self.title_label.set_can_focus(False)
        self.title_label.get_style_context().add_class("section-title")
        titles.pack_start(self.title_label, False, False, 0)
        self.subtitle_label = Gtk.Label()
        self.subtitle_label.set_xalign(0.0)
        self.subtitle_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.subtitle_label.set_selectable(True)
        self.subtitle_label.set_can_focus(False)
        self.subtitle_label.get_style_context().add_class("dim-label")
        titles.pack_start(self.subtitle_label, False, False, 0)
        header.pack_start(titles, False, False, 0)

        self.chips = ChipFlow()
        self.chips.set_valign(Gtk.Align.CENTER)
        header.pack_start(self.chips, True, True, 0)

        self._actions_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.XS)
        self._actions_box.set_valign(Gtk.Align.CENTER)
        header.pack_start(self._actions_box, False, False, 0)

        self._expand_button = IconButton("pan-down-symbolic", _("Hide details (F9)"), self.toggle_expanded)
        header.pack_end(self._expand_button, False, False, 0)
        self.pack_start(header, False, False, 0)

        self._revealer = Gtk.Revealer()
        self._revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_UP)
        self._revealer.set_transition_duration(get_theme_manager().motion_ms(Motion.NORMAL))
        self._grid = Gtk.Grid(column_spacing=Space.M, row_spacing=Space.XS)
        self._grid.set_column_homogeneous(False)
        self._revealer.add(self._grid)
        self._revealer.set_reveal_child(True)
        self.pack_start(self._revealer, False, False, 0)
        self._expanded = True
        self.set_content(placeholder_content())

    # ------------------------------------------------------------------ api
    @property
    def expanded(self) -> bool:
        return self._expanded

    def set_expanded(self, expanded: bool, *, notify: bool = True) -> None:
        self._expanded = bool(expanded)
        self._revealer.set_reveal_child(self._expanded)
        icon = "pan-down-symbolic" if self._expanded else "pan-up-symbolic"
        tip = _("Hide details (F9)") if self._expanded else _("Show details (F9)")
        self._expand_button.set_icon(icon)
        self._expand_button.set_tooltip_text(tip)
        set_accessible_name(self._expand_button, tip)
        if notify:
            self.on_expanded_changed(self._expanded)

    def toggle_expanded(self) -> None:
        self.set_expanded(not self._expanded)

    def set_content(self, content: DetailContent) -> None:
        self.title_label.set_text(content.title)
        self.subtitle_label.set_text(content.subtitle)
        self.subtitle_label.set_visible(bool(content.subtitle))
        if content.status is None:
            self.status_dot.hide()
        else:
            self.status_dot.set_status(content.status)
            self.status_dot.show()
        self.chips.set_chips(content.chips, accent=content.accent_chips)
        self._render_facts(content.facts)
        self._render_actions(content.actions)

    # ------------------------------------------------------------ internal
    def _render_facts(self, facts: List[Tuple[str, str]]) -> None:
        for child in list(self._grid.get_children()):
            self._grid.remove(child)
        columns = 2
        for index, (key, value) in enumerate(facts):
            row, col = divmod(index, columns)
            key_label = Gtk.Label(label=key)
            key_label.set_xalign(0.0)
            key_label.get_style_context().add_class("detail-key")
            value_label = Gtk.Label(label=value or EMPTY_VALUE)
            value_label.set_xalign(0.0)
            value_label.set_selectable(True)
            value_label.set_can_focus(False)
            value_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
            value_label.set_hexpand(True)
            value_label.set_tooltip_text(value or None)
            if not value:
                value_label.get_style_context().add_class("dim-label")
            self._grid.attach(key_label, col * 2, row, 1, 1)
            self._grid.attach(value_label, col * 2 + 1, row, 1, 1)
        self._grid.show_all()

    def _render_actions(self, actions: List[DetailAction]) -> None:
        for child in list(self._actions_box.get_children()):
            self._actions_box.remove(child)
        inline = [a for a in actions if not a.overflow]
        overflow = [a for a in actions if a.overflow]
        for action in inline:
            if action.primary:
                button = Gtk.Button.new_with_mnemonic(action.label)
                button.get_style_context().add_class("destructive-action" if action.destructive else "suggested-action")
            elif action.icon:
                button = IconButton(action.icon, action.tooltip or action.label.replace("_", ""))
            else:
                button = Gtk.Button.new_with_mnemonic(action.label)
            if action.tooltip and action.primary:
                button.set_tooltip_text(action.tooltip)
            button.set_sensitive(action.sensitive)
            callback = action.callback
            button.connect("clicked", lambda _b, cb=callback: cb())
            self._actions_box.pack_start(button, False, False, 0)
        if overflow:
            sections = [
                MenuSection("more", [self._menu_item(a) for a in overflow if not a.destructive]),
                MenuSection("danger", [self._menu_item(a) for a in overflow if a.destructive]),
            ]
            more = IconMenuButton(Icons.MORE, _("More actions"), menu=build_menu(sections))
            self._actions_box.pack_start(more, False, False, 0)
        self._actions_box.show_all()

    @staticmethod
    def _menu_item(action: DetailAction) -> MenuItemSpec:
        return MenuItemSpec(
            action.label,
            action.callback,
            icon=action.icon,
            sensitive=action.sensitive,
            destructive=action.destructive,
            tooltip=action.tooltip,
        )
