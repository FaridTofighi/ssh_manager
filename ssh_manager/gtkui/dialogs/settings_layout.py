"""Sidebar + stack layout shared by the Host and Preferences dialogs."""

from __future__ import annotations

from typing import Dict, List, Optional

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # type: ignore  # noqa: E402

from ..theme.loader import get_theme_manager  # noqa: E402
from ..theme.tokens import Motion, Size, Space  # noqa: E402


class SettingsPage(Gtk.ScrolledWindow):
    """One scrollable page with a consistent header."""

    def __init__(self, page_id: str, title: str, description: str = "") -> None:
        Gtk.ScrolledWindow.__init__(self)
        self.page_id = page_id
        self.title = title
        self.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=Space.M)
        self.body.set_border_width(Space.L)
        heading = Gtk.Label(label=title)
        heading.set_xalign(0.0)
        heading.get_style_context().add_class("title-1")
        self.body.pack_start(heading, False, False, 0)
        if description:
            desc = Gtk.Label(label=description)
            desc.set_xalign(0.0)
            desc.set_line_wrap(True)
            desc.get_style_context().add_class("section-description")
            self.body.pack_start(desc, False, False, 0)
        Gtk.ScrolledWindow.add(self, self.body)

    def append(self, widget: Gtk.Widget, *, expand: bool = False) -> Gtk.Widget:
        """Add ``widget`` below the page header."""

        self.body.pack_start(widget, expand, expand, 0)
        return widget


class SettingsPageLayout(Gtk.Box):
    """Navigation sidebar on the start side, pages in a stack."""

    def __init__(self) -> None:
        Gtk.Box.__init__(self, orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self._stack = Gtk.Stack()
        self._stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self._stack.set_transition_duration(get_theme_manager().motion_ms(Motion.FAST))
        self._sidebar = Gtk.ListBox()
        self._sidebar.set_selection_mode(Gtk.SelectionMode.BROWSE)
        self._sidebar.get_style_context().add_class("settings-sidebar")
        self._sidebar.connect("row-selected", self._on_row_selected)
        self._sidebar.set_size_request(Size.SETTINGS_NAV, -1)
        nav_scroller = Gtk.ScrolledWindow()
        nav_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        nav_scroller.add(self._sidebar)
        nav_scroller.get_style_context().add_class("settings-sidebar")
        self.pack_start(nav_scroller, False, False, 0)
        self.pack_start(self._stack, True, True, 0)
        self._pages: Dict[str, SettingsPage] = {}
        self._rows: Dict[str, Gtk.ListBoxRow] = {}
        self._order: List[str] = []

    def add_page(self, page: SettingsPage) -> SettingsPage:
        self._stack.add_named(page, page.page_id)
        row = Gtk.ListBoxRow()
        label = Gtk.Label(label=page.title)
        label.set_xalign(0.0)
        row.add(label)
        row.page_id = page.page_id  # type: ignore[attr-defined]
        self._sidebar.add(row)
        self._pages[page.page_id] = page
        self._rows[page.page_id] = row
        self._order.append(page.page_id)
        if len(self._order) == 1:
            self._sidebar.select_row(row)
        return page

    def new_page(self, page_id: str, title: str, description: str = "") -> SettingsPage:
        return self.add_page(SettingsPage(page_id, title, description))

    def show_page(self, page_id: str) -> None:
        row = self._rows.get(page_id)
        if row is not None:
            self._sidebar.select_row(row)

    def set_page_visible(self, page_id: str, visible: bool) -> None:
        """Show or hide a page and its navigation row (survives ``show_all``)."""

        row, page = self._rows.get(page_id), self._pages.get(page_id)
        if row is None or page is None:
            return
        for widget in (row, page):
            widget.set_no_show_all(not visible)
            if visible:
                widget.show_all()
            else:
                widget.hide()
        if not visible and self.current_page == page_id:
            first = next((pid for pid in self._order if self.page_visible(pid)), None)
            if first is not None:
                self.show_page(first)

    def page_visible(self, page_id: str) -> bool:
        row = self._rows.get(page_id)
        return row is not None and not row.get_no_show_all()

    @property
    def current_page(self) -> Optional[str]:
        return self._stack.get_visible_child_name()

    def page(self, page_id: str) -> SettingsPage:
        return self._pages[page_id]

    def _on_row_selected(self, _listbox: Gtk.ListBox, row: Optional[Gtk.ListBoxRow]) -> None:
        if row is not None:
            self._stack.set_visible_child_name(row.page_id)  # type: ignore[attr-defined]
