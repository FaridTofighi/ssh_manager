"""Sidebar: always-visible search, advanced-filter popover and the session tree.

The sidebar owns no inventory logic. It exposes widgets and emits callbacks:

* ``on_search_changed(text)``
* ``on_filter_changed(text)``
* ``on_smart_group_activated(index)``
* ``on_save_smart_group()`` / ``on_delete_smart_group(index)``
* ``on_add_host()`` / ``on_open_folder()`` (first-run empty state)
"""

from __future__ import annotations

from enum import Enum
from typing import Callable, Optional, Sequence

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gtk, Pango  # type: ignore  # noqa: E402

from ..constants import Icons  # noqa: E402
from ..i18n import _  # noqa: E402
from ..theme.tokens import Space  # noqa: E402
from ..widgets.empty_state import EmptyState  # noqa: E402
from ..widgets.icon_button import IconButton, set_accessible_name  # noqa: E402

FILTER_HELP = _(
    "Combine <b>key:value</b> terms with <b>AND</b>.\n"
    "Keys: <tt>tag</tt>, <tt>environment</tt>, <tt>owner</tt>, <tt>service</tt>, "
    "or any SSH option such as <tt>user</tt> or <tt>port</tt>.\n"
    "Example: <tt>tag:prod AND owner:alice</tt>"
)


class TreePage(str, Enum):
    """Which page of the sidebar stack is visible."""

    TREE = "tree"
    NO_RESULTS = "no-results"
    EMPTY = "empty"


class SidebarView(Gtk.Box):
    """Left pane of the main window."""

    def __init__(self, tree_view: Gtk.TreeView) -> None:
        Gtk.Box.__init__(self, orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.get_style_context().add_class("sidebar-pane")
        self.set_size_request(220, -1)

        self.on_search_changed: Callable[[str], None] = lambda _t: None
        self.on_filter_changed: Callable[[str], None] = lambda _t: None
        self.on_smart_group_activated: Callable[[int], None] = lambda _i: None
        self.on_save_smart_group: Callable[[], None] = lambda: None
        self.on_delete_smart_group: Callable[[int], None] = lambda _i: None
        self.on_add_host: Callable[[], None] = lambda: None
        self.on_open_folder: Callable[[], None] = lambda: None
        self.on_escape_to_tree: Callable[[], None] = lambda: None

        self.pack_start(self._build_search_row(), False, False, 0)
        self.pack_start(self._build_filter_chip(), False, False, 0)

        self.tree_view = tree_view
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.add(tree_view)

        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.NONE)
        # Size to the visible page only, so hidden empty states don't widen the sidebar.
        self.stack.set_hhomogeneous(False)
        self.stack.set_vhomogeneous(False)
        self.stack.add_named(scroller, TreePage.TREE.value)
        self.no_results = EmptyState(
            Icons.SEARCH,
            _("No matching hosts"),
            _("Try a different search or clear the filter."),
            action_label=_("_Clear Search and Filter"),
            on_action=self.clear_all,
            compact=True,
        )
        self.stack.add_named(self.no_results, TreePage.NO_RESULTS.value)
        self.first_run = EmptyState(
            Icons.SERVER,
            _("No hosts yet"),
            _("Add your first host, or open a folder that contains SSH config files."),
            action_label=_("_Add Host"),
            on_action=lambda: self.on_add_host(),
            secondary_label=_("_Open Config Folder…"),
            on_secondary=lambda: self.on_open_folder(),
            compact=True,
        )
        self.stack.add_named(self.first_run, TreePage.EMPTY.value)
        self.pack_start(self.stack, True, True, 0)

    # ------------------------------------------------------------ building
    def _build_search_row(self) -> Gtk.Widget:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.XS)
        row.get_style_context().add_class("sidebar-search")
        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_placeholder_text(_("Search hosts, users, tags…"))
        self.search_entry.set_hexpand(True)
        set_accessible_name(self.search_entry, _("Search hosts"))
        self.search_entry.connect("search-changed", lambda e: self.on_search_changed(e.get_text()))
        self.search_entry.connect("key-press-event", self._on_search_key)
        row.pack_start(self.search_entry, True, True, 0)

        self.filter_button = Gtk.MenuButton()
        overlay = Gtk.Overlay()
        overlay.add(Gtk.Image.new_from_icon_name(Icons.FILTER, Gtk.IconSize.BUTTON))
        self._filter_badge = Gtk.Label(label="•")
        self._filter_badge.get_style_context().add_class("badge")
        self._filter_badge.set_halign(Gtk.Align.END)
        self._filter_badge.set_valign(Gtk.Align.START)
        self._filter_badge.set_no_show_all(True)
        overlay.add_overlay(self._filter_badge)
        self.filter_button.add(overlay)
        self.filter_button.set_tooltip_text(_("Advanced filter and smart groups"))
        set_accessible_name(self.filter_button, _("Advanced filter"))
        self.filter_button.set_popover(self._build_filter_popover())
        row.pack_start(self.filter_button, False, False, 0)
        return row

    def _build_filter_popover(self) -> Gtk.Popover:
        popover = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=Space.S)
        box.set_border_width(Space.M)
        box.set_size_request(320, -1)

        title = Gtk.Label(label=_("Advanced filter"))
        title.set_xalign(0.0)
        title.get_style_context().add_class("section-title")
        box.pack_start(title, False, False, 0)

        self.filter_entry = Gtk.Entry()
        self.filter_entry.set_placeholder_text(_("tag:prod AND owner:alice"))
        set_accessible_name(self.filter_entry, _("Filter expression"))
        self.filter_entry.connect("changed", lambda e: self.on_filter_changed(e.get_text().strip()))
        self.filter_entry.connect("activate", lambda *_: popover.popdown())
        box.pack_start(self.filter_entry, False, False, 0)

        self.filter_error = Gtk.Label()
        self.filter_error.set_xalign(0.0)
        self.filter_error.set_line_wrap(True)
        self.filter_error.get_style_context().add_class("form-error")
        self.filter_error.set_no_show_all(True)
        box.pack_start(self.filter_error, False, False, 0)

        help_label = Gtk.Label()
        help_label.set_markup(FILTER_HELP)
        help_label.set_xalign(0.0)
        help_label.set_line_wrap(True)
        help_label.set_max_width_chars(40)
        help_label.get_style_context().add_class("form-help")
        box.pack_start(help_label, False, False, 0)

        box.pack_start(Gtk.Separator(), False, False, Space.XS)
        self._build_smart_group_section(box)
        box.show_all()
        popover.add(box)
        popover.connect("show", lambda *_: self.filter_entry.grab_focus())
        self._update_smart_buttons()
        return popover

    def _build_smart_group_section(self, box: Gtk.Box) -> None:
        """Saved filters (smart groups) inside the filter popover."""

        sg_title = Gtk.Label(label=_("Smart groups"))
        sg_title.set_xalign(0.0)
        sg_title.get_style_context().add_class("section-title")
        box.pack_start(sg_title, False, False, 0)
        sg_desc = Gtk.Label(label=_("Saved filters. Click one to apply it."))
        sg_desc.set_xalign(0.0)
        sg_desc.get_style_context().add_class("section-description")
        box.pack_start(sg_desc, False, False, 0)

        self.smart_group_list = Gtk.ListBox()
        self.smart_group_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.smart_group_list.set_activate_on_single_click(True)
        placeholder = Gtk.Label(label=_("No smart groups saved."))
        placeholder.get_style_context().add_class("dim-label")
        placeholder.set_margin_top(Space.S)
        placeholder.set_margin_bottom(Space.S)
        placeholder.show()
        self.smart_group_list.set_placeholder(placeholder)
        self.smart_group_list.connect("row-activated", lambda _l, row: self.on_smart_group_activated(row.get_index()))
        self.smart_group_list.connect("row-selected", lambda *_: self._update_smart_buttons())
        frame = Gtk.Frame()
        frame.add(self.smart_group_list)
        box.pack_start(frame, False, False, 0)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.S)
        self.save_smart_button = Gtk.Button.new_with_mnemonic(_("_Save Filter…"))
        self.save_smart_button.connect("clicked", lambda *_: self.on_save_smart_group())
        actions.pack_start(self.save_smart_button, False, False, 0)
        self.delete_smart_button = Gtk.Button.new_with_mnemonic(_("_Delete"))
        self.delete_smart_button.get_style_context().add_class("destructive-action")
        self.delete_smart_button.connect("clicked", lambda *_: self._delete_selected_smart_group())
        actions.pack_start(self.delete_smart_button, False, False, 0)
        clear = Gtk.Button.new_with_mnemonic(_("C_lear Filter"))
        clear.connect("clicked", lambda *_: self.filter_entry.set_text(""))
        actions.pack_end(clear, False, False, 0)
        box.pack_start(actions, False, False, 0)

    def _build_filter_chip(self) -> Gtk.Widget:
        self._filter_chip_revealer = Gtk.Revealer()
        chip_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.XS)
        chip_row.set_margin_start(Space.S)
        chip_row.set_margin_end(Space.S)
        chip_row.set_margin_top(Space.XS)
        chip_row.set_margin_bottom(Space.XS)
        self._filter_chip_label = Gtk.Label()
        self._filter_chip_label.set_xalign(0.0)
        self._filter_chip_label.set_ellipsize(Pango.EllipsizeMode.END)
        self._filter_chip_label.get_style_context().add_class("chip")
        self._filter_chip_label.get_style_context().add_class("accent")
        chip_row.pack_start(self._filter_chip_label, True, True, 0)
        clear = IconButton(Icons.CLEAR, _("Clear filter"), lambda: self.filter_entry.set_text(""))
        chip_row.pack_start(clear, False, False, 0)
        self._filter_chip_revealer.add(chip_row)
        return self._filter_chip_revealer

    # ------------------------------------------------------------------ api
    @property
    def search_text(self) -> str:
        return self.search_entry.get_text().strip()

    @property
    def filter_text(self) -> str:
        return self.filter_entry.get_text().strip()

    def set_filter_text(self, text: str) -> None:
        if self.filter_entry.get_text() != text:
            self.filter_entry.set_text(text)

    def set_filter_error(self, message: str) -> None:
        self.filter_error.set_text(message)
        self.filter_error.set_visible(bool(message))

    def set_filter_active(self, text: str) -> None:
        active = bool(text)
        self._filter_badge.set_visible(active)
        self._filter_chip_label.set_text(_("Filter: {expr}").format(expr=text) if active else "")
        self._filter_chip_label.set_tooltip_text(text or None)
        self._filter_chip_revealer.set_reveal_child(active)

    def set_page(self, page: TreePage) -> None:
        self.stack.set_visible_child_name(TreePage(page).value)

    def set_smart_groups(self, names: Sequence[str]) -> None:
        for row in list(self.smart_group_list.get_children()):
            self.smart_group_list.remove(row)
        for name in names:
            label = Gtk.Label(label=name)
            label.set_xalign(0.0)
            label.set_margin_start(Space.S)
            label.set_margin_end(Space.S)
            label.set_margin_top(Space.XS)
            label.set_margin_bottom(Space.XS)
            row = Gtk.ListBoxRow()
            row.add(label)
            self.smart_group_list.add(row)
        self.smart_group_list.show_all()
        self._update_smart_buttons()

    def selected_smart_group(self) -> Optional[int]:
        row = self.smart_group_list.get_selected_row()
        return row.get_index() if row is not None else None

    def focus_search(self) -> None:
        self.search_entry.grab_focus()
        self.search_entry.select_region(0, -1)

    def clear_all(self) -> None:
        self.search_entry.set_text("")
        self.filter_entry.set_text("")

    # ------------------------------------------------------------- internal
    def _update_smart_buttons(self) -> None:
        self.delete_smart_button.set_sensitive(self.selected_smart_group() is not None)

    def _delete_selected_smart_group(self) -> None:
        index = self.selected_smart_group()
        if index is not None:
            self.on_delete_smart_group(index)

    def _on_search_key(self, entry: Gtk.SearchEntry, event: Gdk.EventKey) -> bool:
        if event.keyval == Gdk.KEY_Escape:
            if entry.get_text():
                entry.set_text("")
            else:
                self.on_escape_to_tree()
            return True
        if event.keyval in (Gdk.KEY_Down, Gdk.KEY_Return, Gdk.KEY_KP_Enter) and entry.get_text():
            self.on_escape_to_tree()
            return event.keyval == Gdk.KEY_Down
        return False
