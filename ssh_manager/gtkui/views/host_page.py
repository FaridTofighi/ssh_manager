"""Host page: the right-hand side of the main window (Relay layout).

Four bands stacked in one column::

    page bar     Hosts › api-prod-01                              [···] [Connect]
    host header  [▣] api-prod-01 PRODUCTION          LAST CONNECTED   LATENCY
                     deploy@10.60.110.98             2 hours ago      ● 24 ms
    tabs         Terminal   Details   Activity
    content      (stack: terminal workspace / details card / activity card)
    info bar     CONNECTION  AUTH  PROXY   ·live session stats·             [⧉]

The view only renders :class:`HostPageContent`; the window's
``HostPageController`` builds that content and handles the callbacks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, List, Sequence, Tuple

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Pango  # type: ignore  # noqa: E402

from ..constants import Icons  # noqa: E402
from ..host_facts import ActivityEvent, Crumb, LatencyLevel, format_relative_time  # noqa: E402
from ..i18n import _  # noqa: E402
from ..theme.tokens import Level, Size, Space, environment_kind  # noqa: E402
from ..widgets.dot import Dot  # noqa: E402
from ..widgets.icon_button import IconButton, set_accessible_name  # noqa: E402
from ..widgets.icons import make_icon  # noqa: E402
from ..widgets.toast import level_icon  # noqa: E402

GUTTER = Space.GUTTER  # 36px horizontal padding of every band
STAT_GAP = 40  # between the two header stat columns
INFO_GAP = 48  # between info-bar fields
TAB_GAP = 29  # between page tabs


class PageTab(str, Enum):
    TERMINAL = "terminal"
    DETAILS = "details"
    ACTIVITY = "activity"


@dataclass
class HostPageContent:
    """Everything the host page shows for the current selection."""

    kind: str = "empty"  # "host" | "group" | "multi" | "empty"
    crumbs: List[Crumb] = field(default_factory=list)
    title: str = ""
    environment: str = ""
    address: str = ""  # mono line under the title
    icon: str = Icons.HOST
    stats: List[Tuple[str, str, str]] = field(default_factory=list)  # (caption, value, level or "")
    primary_label: str = ""
    primary_outlined: bool = False
    primary_sensitive: bool = True
    primary_tooltip: str = ""
    info: List[Tuple[str, str, bool]] = field(default_factory=list)  # (caption, value, mono)
    copy_text: str = ""
    facts: List[Tuple[str, str, bool]] = field(default_factory=list)  # (label, value, mono)
    chips: List[str] = field(default_factory=list)
    activity: List[ActivityEvent] = field(default_factory=list)
    now: float = 0.0


def _caps(text: str, css: str) -> Gtk.Label:
    label = Gtk.Label(label=text.upper())
    label.set_xalign(0.0)
    label.get_style_context().add_class(css)
    return label


class PageBar(Gtk.Box):
    def __init__(self) -> None:
        Gtk.Box.__init__(self, orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.M)
        self.get_style_context().add_class("page-bar")
        self.set_size_request(-1, Size.PAGE_BAR)
        self.on_crumb: Callable[[Tuple[str, ...]], None] = lambda _t: None
        self.on_primary: Callable[[], None] = lambda: None
        self.on_more: Callable[[Gtk.Widget], None] = lambda _w: None

        self.crumbs = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.S + 2)
        self.crumbs.set_valign(Gtk.Align.CENTER)
        set_accessible_name(self.crumbs, _("Location"))
        self.pack_start(self.crumbs, True, True, 0)

        self.primary = Gtk.Button()
        self.primary.get_style_context().add_class("page-primary")
        self.primary.set_valign(Gtk.Align.CENTER)
        self.primary.connect("clicked", lambda *_: self.on_primary())
        self.pack_end(self.primary, False, False, 0)

        self.more = IconButton(Icons.MORE, _("More actions"), css_classes=("outlined", "page-more"))
        self.more.get_style_context().remove_class("flat")
        self.more.get_style_context().remove_class("toolbar-icon")
        self.more.set_relief(Gtk.ReliefStyle.NORMAL)
        self.more.connect("clicked", lambda button: self.on_more(button))
        self.pack_end(self.more, False, False, 0)

    def set_crumbs(self, crumbs: Sequence[Crumb]) -> None:
        for child in list(self.crumbs.get_children()):
            self.crumbs.remove(child)
        for index, crumb in enumerate(crumbs):
            if index:
                self.crumbs.pack_start(make_icon(Icons.CHEVRON_RIGHT, 14, css_class="breadcrumb-sep"), False, False, 0)
            if crumb.current:
                label = Gtk.Label(label=crumb.label)
                label.get_style_context().add_class("breadcrumb-current")
                label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
                label.set_selectable(True)
                label.set_can_focus(False)
                self.crumbs.pack_start(label, False, False, 0)
            else:
                button = Gtk.Button(label=crumb.label)
                button.get_style_context().add_class("breadcrumb-link")
                button.get_style_context().add_class("breadcrumb-parent")
                button.set_relief(Gtk.ReliefStyle.NONE)
                button.set_tooltip_text(_("Show {name}").format(name=crumb.label))
                button.connect("clicked", lambda _b, target=crumb.target: self.on_crumb(target))
                self.crumbs.pack_start(button, False, False, 0)
        self.crumbs.show_all()

    def set_primary(self, label: str, *, outlined: bool, sensitive: bool, tooltip: str) -> None:
        self.primary.set_label(label)
        self.primary.set_use_underline(True)
        self.primary.set_visible(bool(label))
        self.primary.set_sensitive(sensitive)
        self.primary.set_tooltip_text(tooltip or None)
        ctx = self.primary.get_style_context()
        if outlined:
            ctx.remove_class("primary")
            ctx.add_class("outlined")
        else:
            ctx.remove_class("outlined")
            ctx.add_class("primary")


class HostHeader(Gtk.Box):
    def __init__(self) -> None:
        Gtk.Box.__init__(self, orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.L + 2)
        self.get_style_context().add_class("host-header")
        self.set_size_request(-1, Size.HOST_HEADER)

        self.tile = Gtk.Box()
        self.tile.get_style_context().add_class("host-tile")
        self.tile.set_size_request(Size.HOST_TILE, Size.HOST_TILE)
        self.tile.set_valign(Gtk.Align.CENTER)
        self._tile_icon = make_icon(Icons.HOST, 24)
        self._tile_icon.set_valign(Gtk.Align.CENTER)
        self.tile.set_center_widget(self._tile_icon)
        self.pack_start(self.tile, False, False, 0)

        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=Space.XS)
        titles.set_valign(Gtk.Align.CENTER)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.M)
        self.title = Gtk.Label()
        self.title.get_style_context().add_class("host-title")
        self.title.set_xalign(0.0)
        self.title.set_ellipsize(Pango.EllipsizeMode.END)
        self.title.set_selectable(True)
        self.title.set_can_focus(False)
        row.pack_start(self.title, False, False, 0)
        self.badge = Gtk.Label()
        self.badge.get_style_context().add_class("env-badge")
        self.badge.set_valign(Gtk.Align.CENTER)
        self.badge.set_no_show_all(True)
        row.pack_start(self.badge, False, False, 0)
        titles.pack_start(row, False, False, 0)
        self.address = Gtk.Label()
        self.address.get_style_context().add_class("host-address")
        self.address.set_xalign(0.0)
        self.address.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.address.set_selectable(True)
        self.address.set_can_focus(False)
        titles.pack_start(self.address, False, False, 0)
        self.pack_start(titles, True, True, 0)

        self.stats = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=STAT_GAP)
        self.stats.set_valign(Gtk.Align.CENTER)
        self.pack_end(self.stats, False, False, 0)
        self._badge_class = ""

    def set_content(self, content: HostPageContent) -> None:
        self.title.set_text(content.title)
        self._tile_icon.set_icon(content.icon)
        self.address.set_text(content.address)
        self.address.set_visible(bool(content.address))
        if self._badge_class:
            self.badge.get_style_context().remove_class(self._badge_class)
        if content.environment:
            self._badge_class = f"env-{environment_kind(content.environment)}"
            self.badge.get_style_context().add_class(self._badge_class)
            self.badge.set_text(content.environment.upper())
            self.badge.show()
        else:
            self._badge_class = ""
            self.badge.hide()
        for child in list(self.stats.get_children()):
            self.stats.remove(child)
        for caption, value, level in content.stats:
            column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=Space.S - 1)
            column.pack_start(_caps(caption, "stat-label"), False, False, 0)
            line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.S)
            if level:
                dot = Dot(7, css_class=f"latency-dot {level}")
                line.pack_start(dot, False, False, 0)
            label = Gtk.Label(label=value)
            label.set_xalign(0.0)
            label.get_style_context().add_class("stat-value")
            if level:
                label.get_style_context().add_class(f"latency-{level}")
            line.pack_start(label, False, False, 0)
            column.pack_start(line, False, False, 0)
            set_accessible_name(column, f"{caption}: {value}")
            self.stats.pack_start(column, False, False, 0)
        self.stats.show_all()


class PageTabs(Gtk.Box):
    def __init__(self) -> None:
        Gtk.Box.__init__(self, orientation=Gtk.Orientation.HORIZONTAL, spacing=TAB_GAP)
        self.get_style_context().add_class("page-tabs")
        self.set_size_request(-1, Size.TABS)
        self.on_changed: Callable[[PageTab], None] = lambda _t: None
        self.buttons = {}
        self._syncing = False
        for tab, label, accel in (
            (PageTab.TERMINAL, _("Terminal"), "Ctrl+1"),
            (PageTab.DETAILS, _("Details"), "Ctrl+2"),
            (PageTab.ACTIVITY, _("Activity"), "Ctrl+3"),
        ):
            button = Gtk.ToggleButton(label=label)
            button.get_style_context().add_class("page-tab")
            button.set_relief(Gtk.ReliefStyle.NONE)
            button.set_tooltip_text(f"{label} ({accel})")
            button.connect("toggled", self._on_toggled, tab)
            self.buttons[tab] = button
            self.pack_start(button, False, False, 0)
        self.current = PageTab.TERMINAL
        self.buttons[PageTab.TERMINAL].set_active(True)

    def _on_toggled(self, button: Gtk.ToggleButton, tab: PageTab) -> None:
        if self._syncing:
            return
        if not button.get_active():
            if tab is self.current:  # the active tab cannot be switched off
                self._set_active(tab)
            return
        self.select(tab)
        self.on_changed(tab)

    def _set_active(self, tab: PageTab) -> None:
        self._syncing = True
        try:
            for other, button in self.buttons.items():
                button.set_active(other is tab)
        finally:
            self._syncing = False

    def select(self, tab: PageTab) -> None:
        self.current = PageTab(tab)
        self._set_active(self.current)


class DetailsCard(Gtk.ScrolledWindow):
    """Definition list of host facts inside a white card on the canvas."""

    def __init__(self) -> None:
        Gtk.ScrolledWindow.__init__(self)
        self.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.get_style_context().add_class("page-canvas")
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        outer.set_border_width(GUTTER)
        self.card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=Space.L)
        self.card.get_style_context().add_class("card")
        self.card.set_valign(Gtk.Align.START)
        outer.pack_start(self.card, False, False, 0)
        self.add(outer)

    def set_content(self, content: HostPageContent) -> None:
        for child in list(self.card.get_children()):
            self.card.remove(child)
        grid = Gtk.Grid(column_spacing=Space.XXXL, row_spacing=Space.L)
        columns = 2
        for index, (label, value, mono) in enumerate(content.facts):
            cell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=Space.XS)
            cell.pack_start(_caps(label, "detail-key"), False, False, 0)
            text = Gtk.Label(label=value or "—")
            text.set_xalign(0.0)
            text.set_line_wrap(True)
            text.set_selectable(True)
            text.set_can_focus(False)
            text.get_style_context().add_class("detail-value")
            if mono:
                text.get_style_context().add_class("mono")
            cell.pack_start(text, False, False, 0)
            cell.set_hexpand(True)
            grid.attach(cell, index % columns, index // columns, 1, 1)
        self.card.pack_start(grid, False, False, 0)
        if content.chips:
            from ..widgets.chips import ChipFlow

            chips_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=Space.XS)
            chips_box.pack_start(_caps(_("Tags"), "detail-key"), False, False, 0)
            flow = ChipFlow()
            flow.set_halign(Gtk.Align.START)
            flow.set_chips(content.chips)
            chips_box.pack_start(flow, False, False, 0)
            self.card.pack_start(chips_box, False, False, 0)
        if not content.facts and not content.chips:
            empty = Gtk.Label(label=_("Select a host to see its details."))
            empty.get_style_context().add_class("dim-label")
            self.card.pack_start(empty, False, False, 0)
        self.card.show_all()


class ActivityCard(Gtk.ScrolledWindow):
    def __init__(self) -> None:
        Gtk.ScrolledWindow.__init__(self)
        self.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.get_style_context().add_class("page-canvas")
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        outer.set_border_width(GUTTER)
        self.card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.card.get_style_context().add_class("card")
        self.card.set_valign(Gtk.Align.START)
        outer.pack_start(self.card, False, False, 0)
        self.add(outer)

    def set_events(self, events: Sequence[ActivityEvent], now: float) -> None:
        for child in list(self.card.get_children()):
            self.card.remove(child)
        if not events:
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=Space.S)
            box.set_margin_top(Space.XXL)
            box.set_margin_bottom(Space.XXL)
            box.pack_start(make_icon(Icons.ACTIVITY, 28, css_class="empty-icon"), False, False, 0)
            title = Gtk.Label(label=_("No activity yet"))
            title.get_style_context().add_class("section-title")
            box.pack_start(title, False, False, 0)
            hint = Gtk.Label(label=_("Connections, disconnections and authentication events appear here."))
            hint.get_style_context().add_class("section-description")
            hint.set_line_wrap(True)
            box.pack_start(hint, False, False, 0)
            self.card.pack_start(box, False, False, 0)
        for event in events:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.M)
            row.get_style_context().add_class("activity-row")
            try:
                level = Level(event.level)
            except ValueError:
                level = Level.INFO
            row.pack_start(level_icon(level), False, False, 0)
            text = Gtk.Label(label=event.text)
            text.set_xalign(0.0)
            text.set_line_wrap(True)
            text.set_selectable(True)
            text.set_can_focus(False)
            row.pack_start(text, True, True, 0)
            stamp = Gtk.Label(label=format_relative_time(event.timestamp, now))
            stamp.get_style_context().add_class("activity-time")
            row.pack_end(stamp, False, False, 0)
            self.card.pack_start(row, False, False, 0)
        self.card.show_all()


class InfoBar(Gtk.Box):
    def __init__(self) -> None:
        Gtk.Box.__init__(self, orientation=Gtk.Orientation.HORIZONTAL, spacing=INFO_GAP)
        self.get_style_context().add_class("info-bar")
        self.set_size_request(-1, Size.INFO_BAR)
        self.on_copy: Callable[[], None] = lambda: None
        self.fields = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=INFO_GAP)
        self.fields.set_valign(Gtk.Align.CENTER)
        self.pack_start(self.fields, False, False, 0)
        # The current session's status bar is docked here while a session is live.
        self.live = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self.live.get_style_context().add_class("info-live")
        self.live.set_valign(Gtk.Align.CENTER)
        self.pack_start(self.live, True, True, 0)
        self.copy_button = IconButton(Icons.COPY, _("Copy SSH command"), lambda: self.on_copy(), css_classes=("info-copy",))
        self.copy_button.set_valign(Gtk.Align.CENTER)
        self.pack_end(self.copy_button, False, False, 0)

    def set_content(self, content: HostPageContent) -> None:
        for child in list(self.fields.get_children()):
            self.fields.remove(child)
        for caption, value, mono in content.info:
            column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=Space.S - 2)
            column.pack_start(_caps(caption, "info-label"), False, False, 0)
            label = Gtk.Label(label=value)
            label.set_xalign(0.0)
            label.set_selectable(True)
            label.set_can_focus(False)
            label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
            label.set_max_width_chars(40)
            label.get_style_context().add_class("info-value")
            if mono:
                label.get_style_context().add_class("mono")
            column.pack_start(label, False, False, 0)
            self.fields.pack_start(column, False, False, 0)
        self.fields.show_all()
        self.copy_button.set_visible(bool(content.copy_text))
        self.copy_button.set_tooltip_text(
            _("Copy “{command}”").format(command=content.copy_text) if content.copy_text else _("Copy SSH command")
        )


class HostPageView(Gtk.Box):
    """Page bar, host header, tabs, content stack and info bar."""

    def __init__(self, terminal_page: Gtk.Widget) -> None:
        Gtk.Box.__init__(self, orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.get_style_context().add_class("host-page")
        self.page_bar = PageBar()
        self.header = HostHeader()
        self.tabs = PageTabs()
        self.pack_start(self.page_bar, False, False, 0)
        self.pack_start(self.header, False, False, 0)
        self.pack_start(self.tabs, False, False, 0)

        self.stack = Gtk.Stack()
        self.stack.set_hhomogeneous(False)
        self.stack.set_vhomogeneous(False)
        self.stack.get_style_context().add_class("page-canvas")
        self.details = DetailsCard()
        self.activity = ActivityCard()
        self.stack.add_named(terminal_page, PageTab.TERMINAL.value)
        self.stack.add_named(self.details, PageTab.DETAILS.value)
        self.stack.add_named(self.activity, PageTab.ACTIVITY.value)
        self.pack_start(self.stack, True, True, 0)

        self.info_bar = InfoBar()
        self.pack_start(self.info_bar, False, False, 0)
        self.tabs.on_changed = self._show_tab
        self.content = HostPageContent()

    def _show_tab(self, tab: PageTab) -> None:
        child = self.stack.get_child_by_name(tab.value)
        if child is not None:
            child.show()
        self.stack.set_visible_child_name(tab.value)

    @property
    def current_tab(self) -> PageTab:
        return self.tabs.current

    def select_tab(self, tab: PageTab) -> None:
        self.tabs.select(tab)
        self._show_tab(PageTab(tab))

    def set_content(self, content: HostPageContent) -> None:
        self.content = content
        self.page_bar.set_crumbs(content.crumbs)
        self.page_bar.set_primary(
            content.primary_label,
            outlined=content.primary_outlined,
            sensitive=content.primary_sensitive,
            tooltip=content.primary_tooltip,
        )
        self.header.set_content(content)
        self.info_bar.set_content(content)
        self.details.set_content(content)
        self.activity.set_events(content.activity, content.now)


def latency_css(level: LatencyLevel) -> str:
    return {LatencyLevel.GOOD: "good", LatencyLevel.WARN: "warn", LatencyLevel.BAD: "bad"}.get(level, "")
