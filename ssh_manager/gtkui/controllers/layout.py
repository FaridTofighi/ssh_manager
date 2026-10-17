"""Sidebar visibility, workspace maximization and responsive tweaks."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import GLib, Gtk  # type: ignore  # noqa: E402

from ..theme.tokens import Layout  # noqa: E402


if TYPE_CHECKING:  # pragma: no cover
    from ...gtk_app import SSHManagerWindow


LOGGER = logging.getLogger(__name__)


COMPACT_SIDEBAR_WIDTH = 300
BRAND_DIVIDER_OFFSET = Layout.Topbar.BRAND_DIVIDER_OFFSET


class LayoutController:
    """Sidebar visibility, workspace maximization and responsive tweaks."""

    def __init__(self, win: "SSHManagerWindow") -> None:
        self.win = win
        state = win._ui
        self.sidebar_visible = state.sidebar_visible
        self.sidebar_width_saved = state.sidebar_width
        self.sidebar_visible_before_max = self.sidebar_visible
        self.maximized = False
        self.details_visible_before_max = True
        self.compact = False

    def toggle_sidebar(self) -> None:
        if self.maximized:
            self.set_maximized(False)
        self.set_sidebar_visible(not self.sidebar_visible)

    def update_sidebar_toggle_ui(self) -> None:
        """Keep the top-bar brand block aligned with the sidebar edge."""

        header = self.win._header
        if self.sidebar_visible:
            header.set_brand_width(self.win._paned.get_position() + BRAND_DIVIDER_OFFSET)
        else:
            header.set_brand_width(None)

    def on_paned_position(self, *_args) -> None:
        if self.sidebar_visible:
            self.win._header.set_brand_width(self.win._paned.get_position() + BRAND_DIVIDER_OFFSET)

    def set_sidebar_visible(self, visible: bool, *, initial: bool = False, persist: bool = True) -> None:
        if self.win._sidebar_container is None:
            self.sidebar_visible = visible
            return
        if visible == self.sidebar_visible and not initial:
            self.update_sidebar_toggle_ui()
            return
        if visible:
            self.win._sidebar_container.show()
            width = max(220, self.sidebar_width_saved)
            GLib.idle_add(self.win._paned.set_position, width)
        else:
            if self.sidebar_visible:
                self.sidebar_width_saved = max(220, self.win._paned.get_position())
            self.win._sidebar_container.hide()
        self.sidebar_visible = visible
        if not self.maximized and persist:
            self.sidebar_visible_before_max = visible
        self.update_sidebar_toggle_ui()
        if not initial and persist:
            self.win._save_ui_state()

    def toggle_maximized(self) -> None:
        self.set_maximized(not self.maximized)

    def update_maximize_ui(self) -> None:
        self.win._header.set_maximized(self.maximized)

    def set_maximized(self, maximized: bool, *, initial: bool = False) -> None:
        if maximized == self.maximized and not initial:
            self.update_maximize_ui()
            return
        self.maximized = maximized
        if maximized:
            self.sidebar_visible_before_max = self.sidebar_visible
            self.win.host_page.set_chrome_visible(False)
            if self.sidebar_visible_before_max:
                self.set_sidebar_visible(False, persist=False)
        else:
            self.win.host_page.set_chrome_visible(True)
            if self.sidebar_visible_before_max:
                self.set_sidebar_visible(True, persist=False)
        self.update_maximize_ui()
        self.update_sidebar_toggle_ui()
        if not initial:
            self.win._save_ui_state()

    def on_size_allocate(self, _widget: Gtk.Widget, allocation: Gtk.Allocation) -> None:
        compact = allocation.width < 1100
        if compact != self.compact:
            self.compact = compact
            self.win._header.set_compact(compact)
            if compact and self.sidebar_visible and self.win._paned.get_position() > COMPACT_SIDEBAR_WIDTH:
                # Narrow window: give the workspace room; the user can still drag the divider.
                GLib.idle_add(self.win._paned.set_position, COMPACT_SIDEBAR_WIDTH)
