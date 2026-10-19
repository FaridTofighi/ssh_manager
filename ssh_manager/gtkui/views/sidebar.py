"""Sidebar: "Hosts" header with *Add host*, search, filter popover, tree and footer.

Relay layout (top → bottom); Compact puts "Hosts", a host count chip and
*Add host* on one 40px row and hides the eyebrow::

    CONNECTION MANAGER
    Hosts                                   [+ Add host]
    [🔍 Search hosts…            Ctrl K] [⚙ filter]
    <tree>
    ───────────────────────────────────────────────
    ⚙ Settings   ⚷ Keys                          ‹

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
from gi.repository import Gdk, Gio, GLib, Gtk, Pango  # type: ignore  # noqa: E402

from ..constants import Icons  # noqa: E402
from ..i18n import _, ngettext  # noqa: E402
from ..theme import density as density_tokens  # noqa: E402
from ..theme.tokens import Layout, Size, Space  # noqa: E402
from ..widgets.dot import Dot  # noqa: E402
from ..widgets.empty_state import EmptyState  # noqa: E402
from ..widgets.icon_button import IconButton, set_accessible_name  # noqa: E402
from ..widgets.icons import make_icon  # noqa: E402

FILTER_HELP = _(
    "Combine <b>key:value</b> terms with <b>AND</b>.\n"
    "Keys: <tt>tag</tt>, <tt>environment</tt>, <tt>owner</tt>, <tt>service</tt>, "
    "or any SSH option such as <tt>user</tt> or <tt>port</tt>.\n"
    "Example: <tt>tag:prod AND owner:alice</tt>"
)



def _add_menu_model(template_items) -> Gio.Menu:
    """Secondary *Add host* menu: templates, Quick Connect and groups."""

    menu = Gio.Menu()
    hosts = Gio.Menu()
    hosts.append(_("New Host…"), "win.new-host")
    if template_items:
        templates = Gio.Menu()
        for label, detailed_action in template_items:
            templates.append(label, detailed_action)
        hosts.append_submenu(_("New Host from Template"), templates)
    hosts.append(_("Quick Connect…"), "win.quick-connect")
    menu.append_section(None, hosts)
    groups = Gio.Menu()
    groups.append(_("New Group…"), "win.new-group")
    groups.append(_("New Child Group…"), "win.new-child-group")
    menu.append_section(None, groups)
    return menu


def _footer_link(icon: str, label: str, action: str, tooltip: str) -> Gtk.Button:
    button = Gtk.Button()
    button.get_style_context().add_class("footer-link")
    button.set_relief(Gtk.ReliefStyle.NONE)
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
    button.icon = make_icon(icon, Size.ICON_FOOTER)  # type: ignore[attr-defined]
    box.pack_start(button.icon, False, False, 0)  # type: ignore[attr-defined]
    box.pack_start(Gtk.Label(label=label), False, False, 0)
    button.add(box)
    button.set_action_name(action)
    button.set_tooltip_text(tooltip)
    button.set_valign(Gtk.Align.CENTER)
    return button


class TreePage(str, Enum):
    """Which page of the sidebar stack is visible."""

    TREE = "tree"
    NO_RESULTS = "no-results"
    EMPTY = "empty"


class SidebarView(Gtk.Box):
    """Left pane of the main window."""

    def __init__(self, tree_view: Gtk.TreeView, *, template_items: Sequence = ()) -> None:
        Gtk.Box.__init__(self, orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.get_style_context().add_class("sidebar-pane")
        self._host_count = 0

        self.on_search_changed: Callable[[str], None] = lambda _t: None
        self.on_filter_changed: Callable[[str], None] = lambda _t: None
        self.on_smart_group_activated: Callable[[int], None] = lambda _i: None
        self.on_save_smart_group: Callable[[], None] = lambda: None
        self.on_delete_smart_group: Callable[[int], None] = lambda _i: None
        self.on_add_host: Callable[[], None] = lambda: None
        self.on_open_folder: Callable[[], None] = lambda: None
        self.on_import_mobaxterm: Callable[[], None] = lambda: None
        self.on_escape_to_tree: Callable[[], None] = lambda: None
        self.on_bulk_connect: Callable[[], None] = lambda: None
        self.on_bulk_move: Callable[[], None] = lambda: None
        self.on_bulk_tag: Callable[[], None] = lambda: None
        self.on_bulk_clear: Callable[[], None] = lambda: None

        self.pack_start(self._build_header(list(template_items)), False, False, 0)
        self.pack_start(self._build_search_row(), False, False, 0)
        self.pack_start(self._build_filter_chip(), False, False, 0)

        self.tree_view = tree_view
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_overlay_scrolling(True)
        scroller.add(tree_view)
        self._scroller = scroller

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
            action_label=_("_Clear search and filter"),
            on_action=self.clear_all,
            compact=True,
        )
        self.stack.add_named(self.no_results, TreePage.NO_RESULTS.value)
        self.first_run = EmptyState(
            Icons.SERVER,
            _("No hosts yet"),
            _("Import your sessions from MobaXterm, add your first host, or open a folder that contains SSH "
              "config files."),
            action_label=_("_Import from MobaXterm…"),
            on_action=lambda: self.on_import_mobaxterm(),
            secondary_label=_("_Add host"),
            on_secondary=lambda: self.on_add_host(),
            link_label=_("Import from _OpenSSH config…"),
            on_link=lambda: self.on_open_folder(),
            compact=True,
        )
        self.stack.add_named(self.first_run, TreePage.EMPTY.value)
        self.pack_start(self.stack, True, True, 0)
        self.pack_end(self._build_footer(), False, False, 0)
        self.pack_end(self._build_bulk_bar(), False, False, 0)
        self.apply_density()

    # ------------------------------------------------------------- density
    def apply_density(self) -> None:
        """Lay the sidebar out with the active density preset (also on a runtime switch)."""

        tokens = density_tokens.active()
        side, size = Layout.Sidebar, Size
        single_row = tokens.variant.SINGLE_ROW_SIDEBAR_HEADER
        self.set_size_request(max(density_tokens.MIN_SIDEBAR_PX, size.SIDEBAR_MIN - 80), -1)

        header = self._header
        header.set_spacing(Space.M)
        header.set_margin_start(side.SIDE)
        header.set_margin_end(side.SIDE)
        header.set_margin_top(side.HEADER_TOP)
        header.set_size_request(-1, size.SIDEBAR_HEADER if single_row else -1)
        self._eyebrow.set_visible(not single_row)
        self.title_label.set_margin_top(side.TITLE_GAP)
        self._title_row.set_spacing(Space.S)
        self._titles.set_valign(Gtk.Align.CENTER if single_row else Gtk.Align.START)
        self._sync_count_chip()
        self.add_host_button.set_valign(Gtk.Align.CENTER if single_row else Gtk.Align.START)
        self.add_host_button.set_margin_top(side.ADD_HOST_TOP)
        self._add_host_content.set_spacing(Space.M if not single_row else Space.S)
        self._add_host_icon.set_pixel_size(size.ICON_BUTTON_GLYPH)

        row = self._search_row
        row.set_spacing(side.SEARCH_GAP)
        row.set_margin_start(side.SIDE)
        row.set_margin_end(side.SEARCH_END)
        row.set_margin_top(side.SEARCH_TOP)
        self._search_icon.set_margin_start(side.SEARCH_ICON_INSET)
        self.keycap.set_margin_end(Space.M)
        self._filter_icon.set_pixel_size(size.ICON_FOOTER)
        self._filter_badge.set_margin_top(Space.S)
        self._filter_badge.set_margin_end(Space.XS + 2)
        self._scroller.set_margin_top(side.TREE_TOP_GAP)

        for state in (self.no_results, self.first_run):
            state.apply_density()
        self._footer.set_spacing(side.FOOTER_GAP)
        self._footer.set_size_request(-1, size.SIDEBAR_FOOTER)
        for link in (self.settings_link, self.keys_link):
            link.icon.set_pixel_size(size.ICON_FOOTER)
            link.get_child().set_spacing(Space.M - 2 if not tokens.compact else Space.S)

    def set_host_count(self, count: int) -> None:
        """Number shown in the Compact header's count chip."""

        self._host_count = max(0, int(count))
        self._sync_count_chip()

    def _sync_count_chip(self) -> None:
        single_row = density_tokens.active().variant.SINGLE_ROW_SIDEBAR_HEADER
        self.count_chip.set_text(str(self._host_count))
        self.count_chip.set_tooltip_text(ngettext("{n} host", "{n} hosts", self._host_count).format(n=self._host_count))
        self.count_chip.set_visible(single_row and self._host_count > 0)

    # ------------------------------------------------------------ building
    def _build_header(self, template_items) -> Gtk.Widget:
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        header.get_style_context().add_class("sidebar-header")
        self._header = header
        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._titles = titles
        self._eyebrow = eyebrow = Gtk.Label(label=_("CONNECTION MANAGER"))
        eyebrow.set_xalign(0.0)
        eyebrow.set_ellipsize(Pango.EllipsizeMode.END)
        eyebrow.get_style_context().add_class("sidebar-eyebrow")
        eyebrow.set_no_show_all(True)  # visibility belongs to apply_density()
        titles.pack_start(eyebrow, False, False, 0)
        self._title_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self.title_label = Gtk.Label(label=_("Hosts"))
        self.title_label.set_xalign(0.0)
        self.title_label.get_style_context().add_class("sidebar-title")
        self._title_row.pack_start(self.title_label, False, False, 0)
        self.count_chip = Gtk.Label()
        self.count_chip.get_style_context().add_class("host-count")
        self.count_chip.set_valign(Gtk.Align.CENTER)
        self.count_chip.set_no_show_all(True)  # Compact only, see _sync_count_chip()
        self._title_row.pack_start(self.count_chip, False, False, 0)
        titles.pack_start(self._title_row, False, False, 0)
        # The title's line box is taller than its glyphs; the stylesheet pulls
        # the search row up over that slack (.sidebar-title margin-bottom), so
        # the Comfortable block is 52px tall at the default font and still grows with it.
        header.pack_start(titles, True, True, 0)

        self.add_host_button = Gtk.Button()
        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self._add_host_content = content
        self._add_host_icon = make_icon(Icons.NEW, Size.ICON_BUTTON_GLYPH)
        content.pack_start(self._add_host_icon, False, False, 0)
        content.pack_start(Gtk.Label(label=_("Add host")), False, False, 0)
        self.add_host_button.add(content)
        ctx = self.add_host_button.get_style_context()
        ctx.add_class("suggested-action")
        ctx.add_class("add-host")
        self.add_host_button.set_tooltip_text(
            _("Add a new host (Ctrl+N). Right-click or Shift+click for templates, groups and Quick Connect.")
        )
        set_accessible_name(self.add_host_button, _("Add host"))
        self.add_host_button.connect("clicked", self._on_add_host_clicked)
        self.add_host_button.connect("button-press-event", self._on_add_host_pressed)
        self.add_host_button.connect("popup-menu", lambda *_: self.show_add_menu() or True)
        self._long_press = Gtk.GestureLongPress.new(self.add_host_button)
        self._long_press.connect("pressed", lambda *_: self.show_add_menu())
        self.add_menu = Gtk.Popover.new_from_model(self.add_host_button, _add_menu_model(template_items))
        self.add_menu.get_style_context().add_class("sm-popover")
        header.pack_end(self.add_host_button, False, False, 0)
        return header

    def _build_search_row(self) -> Gtk.Widget:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        row.get_style_context().add_class("sidebar-search")
        self._search_row = row

        overlay = Gtk.Overlay()
        self.search_entry = Gtk.SearchEntry()
        self.search_entry.get_style_context().add_class("sidebar-search-entry")
        self.search_entry.set_icon_from_icon_name(Gtk.EntryIconPosition.PRIMARY, None)
        self.search_entry.set_placeholder_text(_("Search hosts…"))
        self.search_entry.set_hexpand(True)
        self.search_entry.set_width_chars(6)  # expands; a small minimum keeps narrow windows usable
        set_accessible_name(self.search_entry, _("Search hosts"))
        self.search_entry.connect("search-changed", lambda e: self.on_search_changed(e.get_text()))
        self.search_entry.connect("key-press-event", self._on_search_key)
        for signal in ("changed", "focus-in-event", "focus-out-event"):
            self.search_entry.connect(signal, lambda *_: self._sync_keycap() or False)
        # Visibility changes are ignored while GTK is allocating: re-check afterwards.
        self.search_entry.connect("size-allocate", lambda *_: GLib.idle_add(self._sync_keycap_idle))
        overlay.add(self.search_entry)
        search_icon = make_icon(Icons.SEARCH, Size.ICON, css_class="search-icon")
        search_icon.set_halign(Gtk.Align.START)
        search_icon.set_valign(Gtk.Align.CENTER)
        self._search_icon = search_icon
        overlay.add_overlay(search_icon)
        overlay.set_overlay_pass_through(search_icon, True)
        self.keycap = Gtk.Label(label=_("Ctrl K"))
        self.keycap.get_style_context().add_class("keycap")
        self.keycap.set_halign(Gtk.Align.END)
        self.keycap.set_valign(Gtk.Align.CENTER)
        self.keycap.set_no_show_all(True)
        overlay.add_overlay(self.keycap)
        overlay.set_overlay_pass_through(self.keycap, True)
        row.pack_start(overlay, True, True, 0)

        self.filter_button = Gtk.MenuButton()
        self.filter_button.get_style_context().add_class("filter-button")
        filter_overlay = Gtk.Overlay()
        self._filter_icon = make_icon(Icons.FILTER, Size.ICON_FOOTER)
        filter_overlay.add(self._filter_icon)
        self._filter_badge = Dot(Size.DOT_BADGE, css_class="filter-dot")
        self._filter_badge.set_halign(Gtk.Align.END)
        self._filter_badge.set_valign(Gtk.Align.START)
        self._filter_badge.set_no_show_all(True)
        filter_overlay.add_overlay(self._filter_badge)
        self.filter_button.add(filter_overlay)
        self.filter_button.set_tooltip_text(_("Advanced filter and smart groups"))
        set_accessible_name(self.filter_button, _("Advanced filter"))
        self.filter_button.set_popover(self._build_filter_popover())
        row.pack_start(self.filter_button, False, False, 0)
        self._sync_keycap()
        return row

    def _build_bulk_bar(self) -> Gtk.Widget:
        """Slim accent-tint bar docked under the tree while ≥ 2 hosts are selected."""

        self.bulk_revealer = Gtk.Revealer()
        self.bulk_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_UP)
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.XS)
        bar.get_style_context().add_class("bulk-bar")
        self.bulk_label = Gtk.Label()
        self.bulk_label.set_xalign(0.0)
        self.bulk_label.set_ellipsize(Pango.EllipsizeMode.END)
        bar.pack_start(self.bulk_label, True, True, 0)
        for icon, tip, action in (
            (Icons.CONNECT, _("Connect selected hosts"), lambda: self.on_bulk_connect()),
            (Icons.MOVE, _("Move selected hosts…"), lambda: self.on_bulk_move()),
            (Icons.TAG, _("Tag or color selected hosts…"), lambda: self.on_bulk_tag()),
            (Icons.CLEAR, _("Clear selection"), lambda: self.on_bulk_clear()),
        ):
            bar.pack_start(IconButton(icon, tip, action), False, False, 0)
        self.bulk_revealer.add(bar)
        return self.bulk_revealer

    def set_bulk_count(self, count: int) -> None:
        active = count >= 2
        if active:
            self.bulk_label.set_text(ngettext("{n} host selected", "{n} hosts selected", count).format(n=count))
        self.bulk_revealer.set_reveal_child(active)

    def _build_footer(self) -> Gtk.Widget:
        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        footer.get_style_context().add_class("sidebar-footer")
        self._footer = footer
        self.settings_link = _footer_link(Icons.SETTINGS, _("Settings"), "win.settings", _("Settings (Ctrl+,)"))
        footer.pack_start(self.settings_link, False, False, 0)
        self.keys_link = _footer_link(Icons.KEY, _("Keys"), "win.credentials", _("Credentials and keys"))
        footer.pack_start(self.keys_link, False, False, 0)
        self.collapse_button = IconButton(
            Icons.CHEVRON_LEFT, _("Hide sidebar (Ctrl+B)"), action_name="win.toggle-sidebar", css_classes=("footer-icon",)
        )
        footer.pack_end(self.collapse_button, False, False, 0)
        return footer

    def _build_filter_popover(self) -> Gtk.Popover:
        popover = Gtk.Popover()
        popover.get_style_context().add_class("sm-popover")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=Space.S)
        box.set_border_width(Space.M)
        box.set_size_request(Size.POPOVER_WIDTH, -1)

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

    def show_add_menu(self) -> None:
        self.add_menu.popup()

    def _on_add_host_clicked(self, _button: Gtk.Button) -> None:
        event = Gtk.get_current_event()
        state = event.get_state()[1] if event is not None and event.get_state()[0] else 0
        if state & Gdk.ModifierType.SHIFT_MASK:
            self.show_add_menu()
        else:
            self.on_add_host()

    def _on_add_host_pressed(self, _button: Gtk.Button, event: Gdk.EventButton) -> bool:
        if event.type == Gdk.EventType.BUTTON_PRESS and event.button == Gdk.BUTTON_SECONDARY:
            self.show_add_menu()
            return True
        return False

    def _sync_keycap(self) -> None:
        idle = not self.search_entry.get_text() and not self.search_entry.has_focus()
        self.keycap.set_visible(idle and self._keycap_fits())

    def _sync_keycap_idle(self) -> bool:
        self._sync_keycap()
        return GLib.SOURCE_REMOVE

    def _keycap_fits(self) -> bool:
        """The keycap hint only shows when it does not cover the placeholder (narrow sidebars)."""

        width = self.search_entry.get_allocated_width()
        if width <= 1:  # not allocated yet
            return True
        placeholder = self.search_entry.create_pango_layout(self.search_entry.get_placeholder_text() or "")
        # Measure the keycap's text, not the widget: a hidden widget reports width 0.
        keycap = self.keycap.create_pango_layout(self.keycap.get_text()).get_pixel_size()[0] + 2 * Space.XS + 2
        needed = Layout.Sidebar.SEARCH_TEXT_INSET + placeholder.get_pixel_size()[0] + Space.M + keycap + Space.M
        return width >= needed

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
