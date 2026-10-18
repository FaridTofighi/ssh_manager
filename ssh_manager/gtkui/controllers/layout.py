"""Sidebar visibility, focus mode, density and responsive tweaks."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import GLib, Gtk  # type: ignore  # noqa: E402

from ..theme import density as density_tokens  # noqa: E402
from ..theme.tokens import Layout, Size  # noqa: E402
from .ui_state import DEFAULT_SIDEBAR_WIDTH  # noqa: E402


if TYPE_CHECKING:  # pragma: no cover
    from ...gtk_app import SSHManagerWindow


LOGGER = logging.getLogger(__name__)


def default_sidebar_width() -> int:
    """Pane position of the active density's default sidebar (its edge + 1px separator)."""

    return Size.SIDEBAR - 1


def resolve_sidebar_width(saved: int) -> int:
    """``saved`` clamped to the active density's [min, max]; the stored default follows the density."""

    if saved <= 0 or saved == DEFAULT_SIDEBAR_WIDTH:
        return default_sidebar_width()
    return max(Size.SIDEBAR_MIN, min(Size.SIDEBAR_MAX, int(saved)))


class LayoutController:
    """Sidebar visibility, focus mode (``maximized``), density and responsive tweaks."""

    def __init__(self, win: "SSHManagerWindow") -> None:
        self.win = win
        state = win._ui
        self.sidebar_visible = state.sidebar_visible
        self.sidebar_width_saved = resolve_sidebar_width(state.sidebar_width)
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
            header.set_brand_width(self.win._paned.get_position() + Layout.Topbar.BRAND_DIVIDER_OFFSET)
        else:
            header.set_brand_width(None)

    def on_paned_position(self, *_args) -> None:
        if not self.sidebar_visible:
            return
        position = self.win._paned.get_position()
        if position > Size.SIDEBAR_MAX:
            GLib.idle_add(self.win._paned.set_position, Size.SIDEBAR_MAX)  # GtkPaned has no maximum
        self.win._header.set_brand_width(position + Layout.Topbar.BRAND_DIVIDER_OFFSET)

    def set_sidebar_visible(self, visible: bool, *, initial: bool = False, persist: bool = True) -> None:
        if self.win._sidebar_container is None:
            self.sidebar_visible = visible
            return
        if visible == self.sidebar_visible and not initial:
            self.update_sidebar_toggle_ui()
            return
        if visible:
            self.win._sidebar_container.show()
            width = max(density_tokens.MIN_SIDEBAR_PX, self.sidebar_width_saved)
            GLib.idle_add(self.win._paned.set_position, width)
        else:
            if self.sidebar_visible:
                self.sidebar_width_saved = max(density_tokens.MIN_SIDEBAR_PX, self.win._paned.get_position())
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
        # Focus mode: the top bar, sidebar, host bar and status bar go away;
        # the tabs / session row, the broadcast strip and the terminal stay.
        if maximized:
            self.sidebar_visible_before_max = self.sidebar_visible
            self.win.host_page.set_chrome_visible(False)
            self.win._header.hide()
            if self.sidebar_visible_before_max:
                self.set_sidebar_visible(False, persist=False)
        else:
            self.win.host_page.set_chrome_visible(True)
            self.win._header.show()
            if self.sidebar_visible_before_max:
                self.set_sidebar_visible(True, persist=False)
        self.update_maximize_ui()
        self.update_sidebar_toggle_ui()
        if not initial:
            self.win._save_ui_state()

    def on_size_allocate(self, _widget: Gtk.Widget, allocation: Gtk.Allocation) -> None:
        self.set_narrow(allocation.width < Layout.Page.NARROW_WIDTH)

    def set_narrow(self, narrow: bool, *, force: bool = False) -> None:
        """Narrow window: secondary labels hide, sessions get their own strip, the sidebar shrinks."""

        if narrow == self.compact and not force:
            return
        self.compact = narrow
        self.win._header.set_compact(narrow)
        self.win.host_page.set_narrow(narrow)
        narrow_width = max(density_tokens.MIN_SIDEBAR_PX, Size.SIDEBAR_MIN)
        if narrow and self.sidebar_visible and self.win._paned.get_position() > narrow_width:
            # Give the workspace room; the user can still drag the divider.
            GLib.idle_add(self.win._paned.set_position, narrow_width)

    def on_density_changed(self, previous_default: int) -> None:
        """Runtime density switch: a default-width sidebar follows the new default."""

        position = self.win._paned.get_position() if self.sidebar_visible else self.sidebar_width_saved
        if abs(position - previous_default) <= 2:
            position = default_sidebar_width()
        position = max(Size.SIDEBAR_MIN, min(Size.SIDEBAR_MAX, position))
        self.sidebar_width_saved = position
        if self.sidebar_visible and not self.maximized:
            self.win._paned.set_position(position)
        self.update_sidebar_toggle_ui()
