"""Host page: the right-hand side of the main window (Relay layout).

Comfortable density — four bands stacked in one column::

    page bar     Hosts › api-prod-01                              [···] [Connect]
    host header  [▣] api-prod-01 PRODUCTION          LAST CONNECTED   LATENCY
                     deploy@10.60.110.98             2 hours ago      ● 24 ms
    tabs         Terminal   Details   Activity
    content      (stack: terminal workspace / details card / activity card)
    info bar     CONNECTION  AUTH  PROXY   ·live session stats·             [⧉]

Compact density — the page bar and host header merge into one host bar (row
A); the tabs (row B) share their row with the session chips and toolbar while
the Terminal tab shows sessions; the info bar is a single line::

    row A        [▣] api-prod-01 PROD deploy@10.60.110.98   Last connected 2 h ago  ● 24 ms  [···] [Reconnect]
    row B        Terminal  Details  Activity │ ● api-prod-01 ×                     [+ ▾ │ toolbar]
    content      terminal, edge to edge
    status       ● Connected · 118×23 · CPU 3 % …            deploy@10.60.110.98:22 · Credential: x  [⧉]

The same widgets serve both layouts: :meth:`HostPageView.apply_density`
re-parents them, so a density switch at runtime keeps every session alive.

The view only renders :class:`HostPageContent`; the window's
``HostPageController`` builds that content and handles the callbacks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, List, Sequence, Tuple

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk, Pango  # type: ignore  # noqa: E402

from ..constants import Icons  # noqa: E402
from ..host_facts import ActivityEvent, Crumb, LatencyLevel, format_relative_time  # noqa: E402
from ..i18n import _  # noqa: E402
from ..theme import density as density_tokens  # noqa: E402
from ..theme.tokens import Layout, Level, Size, Space, environment_kind  # noqa: E402
from ..widgets.dot import Dot  # noqa: E402
from ..widgets.icon_button import IconButton, set_accessible_name  # noqa: E402
from ..widgets.icons import make_icon  # noqa: E402
from ..widgets.toast import level_icon  # noqa: E402


def _compact() -> bool:
    return density_tokens.active().variant.MERGED_PAGE_HEADER


def _sentence_case_tail(value: str) -> str:
    """``"Never"`` → ``"never"`` so it reads on after a caption; ``"10.0.0.1"``/``"OK"`` stay."""

    if len(value) > 1 and value[0].isupper() and value[1].islower():
        return value[0].lower() + value[1:]
    return value


def _move(widget: Gtk.Widget, target: Gtk.Box, *, end: bool = False, expand: bool = False) -> None:
    """Re-parent ``widget`` into ``target`` (no-op when it is already there)."""

    parent = widget.get_parent()
    if parent is target:
        return
    if parent is not None:
        parent.remove(widget)
    (target.pack_end if end else target.pack_start)(widget, expand, expand, 0)


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
        self.on_crumb: Callable[[Tuple[str, ...]], None] = lambda _t: None
        self.on_primary: Callable[[], None] = lambda: None
        self.on_more: Callable[[Gtk.Widget], None] = lambda _w: None

        self.crumbs = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
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

    def apply_density(self) -> None:
        self.set_size_request(-1, Size.PAGE_BAR)
        self.set_spacing(Space.M)
        self.crumbs.set_spacing(Layout.Page.CRUMB_GAP)

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
    """Comfortable: the host header band. Compact: the whole host bar (row A)."""

    def __init__(self) -> None:
        Gtk.Box.__init__(self, orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self.get_style_context().add_class("host-header")
        self.on_crumb: Callable[[Tuple[str, ...]], None] = lambda _t: None

        self.tile = Gtk.Box()
        self.tile.get_style_context().add_class("host-tile")
        self.tile.set_valign(Gtk.Align.CENTER)
        self._tile_icon = make_icon(Icons.HOST, 24)
        self._tile_icon.set_valign(Gtk.Align.CENTER)
        self.tile.set_center_widget(self._tile_icon)
        self.pack_start(self.tile, False, False, 0)

        # Compact only: "pardis ›" in front of a group title.
        self.prefix = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self.prefix.set_valign(Gtk.Align.CENTER)
        self.prefix.set_no_show_all(True)
        self.pack_start(self.prefix, False, False, 0)

        self.titles = titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        titles.set_valign(Gtk.Align.CENTER)
        self._title_row = row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
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

        # Compact only: the page bar's [···] and primary button move in here.
        self.actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self.actions.set_valign(Gtk.Align.CENTER)
        self.actions.set_no_show_all(True)  # Compact only (an empty box would still take a spacing slot)
        self.pack_end(self.actions, False, False, 0)
        self.stats = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self.stats.set_valign(Gtk.Align.CENTER)
        self.pack_end(self.stats, False, False, 0)
        self._badge_class = ""
        self._content = HostPageContent()
        self._narrow = False

    def set_narrow(self, narrow: bool) -> None:
        """Narrow window: the host bar drops the address (the Details tab has it)."""

        self._narrow = narrow
        self.set_content(self._content)

    def apply_density(self) -> None:
        compact = _compact()
        page = Layout.Page
        self.set_size_request(-1, Size.HOST_HEADER)
        self.set_spacing(page.HEADER_GAP)
        ctx = self.get_style_context()
        (ctx.add_class if compact else ctx.remove_class)("host-bar")
        self.tile.set_size_request(Size.HOST_TILE, Size.HOST_TILE)
        self._tile_icon.set_pixel_size(Size.HOST_TILE_ICON)
        # Comfortable stacks title over address; Compact puts them on one line.
        self.titles.set_orientation(Gtk.Orientation.HORIZONTAL if compact else Gtk.Orientation.VERTICAL)
        self.titles.set_spacing(page.TITLE_GAP if compact else Space.XS)
        self.titles.set_valign(Gtk.Align.CENTER)
        self.address.set_valign(Gtk.Align.CENTER)
        self._title_row.set_spacing(page.TITLE_GAP if compact else Space.M)
        self.stats.set_spacing(page.STAT_GAP)
        self.actions.set_spacing(Space.M)
        self.actions.set_margin_start(Space.M if compact else 0)
        self.actions.set_visible(compact)
        self.set_content(self._content)

    def set_content(self, content: HostPageContent) -> None:
        self._content = content
        self.title.set_text(content.title)
        self._tile_icon.set_icon(content.icon)
        self.address.set_text(content.address)
        self.address.set_visible(bool(content.address) and not (self._narrow and _compact()))
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
        self._set_prefix(content)
        for child in list(self.stats.get_children()):
            self.stats.remove(child)
        if _compact():
            self._inline_stats(content)
            return
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

    def _inline_stats(self, content: HostPageContent) -> None:
        """Compact: ``Last connected 2 hours ago``  ``● 24 ms`` — captions move to tooltips."""

        for caption, value, level in content.stats:
            line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.S)
            if level:
                line.pack_start(Dot(Layout.Tree.DOT, css_class=f"latency-dot {level}"), False, False, 0)
                text = value
            else:
                text = f"{caption} {_sentence_case_tail(value)}"
            label = Gtk.Label(label=text)
            label.get_style_context().add_class("host-stat-inline")
            if level:
                label.get_style_context().add_class(f"latency-{level}")
            line.pack_start(label, False, False, 0)
            line.set_tooltip_text(f"{caption}: {value}")
            set_accessible_name(line, f"{caption}: {value}")
            self.stats.pack_start(line, False, False, 0)
        self.stats.show_all()

    def _set_prefix(self, content: HostPageContent) -> None:
        """Compact group pages: the parent location as muted prefix links (hosts: none)."""

        for child in list(self.prefix.get_children()):
            self.prefix.remove(child)
        parents = [c for c in content.crumbs if not c.current and c.target] if content.kind == "group" else []
        if not _compact() or not parents:
            self.prefix.hide()
            return
        for crumb in parents:
            button = Gtk.Button(label=crumb.label)
            ctx = button.get_style_context()
            for css in ("breadcrumb-link", "crumb-prefix"):
                ctx.add_class(css)
            button.set_relief(Gtk.ReliefStyle.NONE)
            button.set_valign(Gtk.Align.CENTER)
            button.set_tooltip_text(_("Show {name}").format(name=crumb.label))
            button.connect("clicked", lambda _b, target=crumb.target: self.on_crumb(target))
            self.prefix.pack_start(button, False, False, 0)
            self.prefix.pack_start(make_icon(Icons.CHEVRON_RIGHT, 12, css_class="breadcrumb-sep"), False, False, 0)
        for child in self.prefix.get_children():  # no_show_all: show_all() would skip them
            child.show_all()
        self.prefix.show()


class PageTabs(Gtk.Box):
    """Terminal / Details / Activity. ``band=False`` builds the copy shown in the session strip."""

    def __init__(self, *, band: bool = True) -> None:
        Gtk.Box.__init__(self, orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self._band = band
        self.get_style_context().add_class("page-tabs" if band else "strip-tabs")
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

    def apply_density(self) -> None:
        self.set_spacing(Layout.Page.TAB_GAP)
        if self._band:
            self.set_size_request(-1, Size.TABS)

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
        self._outer = outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.card.get_style_context().add_class("card")
        self.card.set_valign(Gtk.Align.START)
        outer.pack_start(self.card, False, False, 0)
        self.add(outer)
        self._content = HostPageContent()

    def apply_density(self) -> None:
        self._outer.set_border_width(Space.GUTTER)
        self.card.set_spacing(Space.L)
        self.set_content(self._content)

    def set_content(self, content: HostPageContent) -> None:
        self._content = content
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
        self._outer = outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.card.get_style_context().add_class("card")
        self.card.set_valign(Gtk.Align.START)
        outer.pack_start(self.card, False, False, 0)
        self.add(outer)
        self._events: Tuple[Sequence[ActivityEvent], float] = ((), 0.0)

    def apply_density(self) -> None:
        self._outer.set_border_width(Space.GUTTER)
        self.set_events(*self._events)

    def set_events(self, events: Sequence[ActivityEvent], now: float) -> None:
        self._events = (events, now)
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
    """Comfortable: caption-over-value fields, then the live session stats.

    Compact: one line — live session stats on the left, the fields as muted
    inline text on the right (captions in tooltips).
    """

    def __init__(self) -> None:
        Gtk.Box.__init__(self, orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self.get_style_context().add_class("info-bar")
        self.on_copy: Callable[[], None] = lambda: None
        self.fields = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self.fields.set_valign(Gtk.Align.CENTER)
        self.pack_start(self.fields, False, False, 0)
        # The current session's status bar is docked here while a session is live.
        self.live = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self.live.get_style_context().add_class("info-live")
        self.live.set_valign(Gtk.Align.CENTER)
        self.pack_start(self.live, True, True, 0)
        self.copy_button = IconButton(Icons.COPY, _("Copy SSH command"), lambda: self.on_copy(), css_classes=("info-copy",))
        self.copy_button.set_valign(Gtk.Align.CENTER)
        self.copy_button.set_no_show_all(True)  # shown only when there is a command to copy
        self.pack_end(self.copy_button, False, False, 0)
        self._compact = False
        self._content = HostPageContent()
        self.connect("size-allocate", self._on_size_allocate)

    @property
    def COMPACT_WIDTH(self) -> int:  # noqa: N802 - kept from the constant it replaces
        """Live session and a narrower bar: hide the secondary fields (the Details tab has them)."""

        return Layout.Page.INFO_COMPACT_WIDTH

    def apply_density(self) -> None:
        single_line = density_tokens.active().variant.SINGLE_LINE_STATUS
        self.set_size_request(-1, Size.INFO_BAR)
        self.set_spacing(Layout.Page.INFO_GAP if not single_line else Space.L)
        self.fields.set_spacing(Layout.Page.INFO_GAP)
        pack = Gtk.PackType.END if single_line else Gtk.PackType.START
        self.set_child_packing(self.fields, False, False, 0, pack)
        self.reorder_child(self.copy_button, 0)  # first end-packed child = right edge
        self.set_content(self._content)

    def _on_size_allocate(self, _widget, allocation) -> None:
        self._compact = allocation.width < self.COMPACT_WIDTH
        hide = self._want_hidden()
        columns = self.fields.get_children()[1:]
        if any(column.get_visible() == hide for column in columns):  # also undoes a parent's show_all()
            self._sync_compact()  # converges: only acts while the visibility is wrong

    def _want_hidden(self) -> bool:
        return self._compact and bool(self.live.get_children())

    def _sync_compact(self) -> bool:
        hide = self._want_hidden()
        for index, column in enumerate(self.fields.get_children()):
            column.set_visible(index == 0 or not hide)
        return GLib.SOURCE_REMOVE

    def set_content(self, content: HostPageContent) -> None:
        self._content = content
        for child in list(self.fields.get_children()):
            self.fields.remove(child)
        single_line = density_tokens.active().variant.SINGLE_LINE_STATUS
        for index, (caption, value, mono) in enumerate(content.info):
            if single_line:
                self.fields.pack_start(self._inline_field(index, caption, value, mono), False, False, 0)
                continue
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
        self._sync_compact()
        self.copy_button.set_visible(bool(content.copy_text))
        self.copy_button.set_tooltip_text(
            _("Copy “{command}”").format(command=content.copy_text) if content.copy_text else _("Copy SSH command")
        )


    @staticmethod
    def _inline_field(index: int, caption: str, value: str, mono: bool) -> Gtk.Widget:
        """``user@host:22`` · ``Credential: x`` · ``Proxy: None`` — the caption is the tooltip."""

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.S)
        if index:
            sep = Gtk.Label(label="·")
            sep.get_style_context().add_class("status-sep")
            box.pack_start(sep, False, False, 0)
        text = value if index == 0 or ":" in value else f"{caption}: {value}"
        label = Gtk.Label(label=text)
        label.set_selectable(True)
        label.set_can_focus(False)
        label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        label.set_max_width_chars(40)
        label.get_style_context().add_class("status-field")
        if mono:
            label.get_style_context().add_class("mono")
        box.pack_start(label, False, False, 0)
        box.set_tooltip_text(f"{caption}: {value}")
        set_accessible_name(label, f"{caption}: {value}")
        return box


class HostPageView(Gtk.Box):
    """Page bar, host header, tabs, content stack and info bar."""

    def __init__(self, terminal_page: Gtk.Widget) -> None:
        Gtk.Box.__init__(self, orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.get_style_context().add_class("host-page")
        self.page_bar = PageBar()
        self.header = HostHeader()
        self.header.on_crumb = lambda target: self.page_bar.on_crumb(target)
        self.tabs = PageTabs()
        # Copy of the tabs that sits in the session strip while row B is merged.
        self.strip_tabs = PageTabs(band=False)
        self.on_strip_merged: Callable[[bool], None] = lambda _merged: None
        self._sessions_strip = False
        self._narrow = False
        self._chrome_visible = True
        self.merged = False
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
        self.tabs.on_changed = self._on_tab_clicked
        self.strip_tabs.on_changed = self._on_tab_clicked
        self.on_tab_changed: Callable[[PageTab], None] = self._show_tab
        self.content = HostPageContent()
        # _sync_bands() owns the bands' visibility: show their contents once,
        # then keep a window-level show_all() from re-showing hidden bands.
        for band in (self.page_bar, self.header, self.tabs, self.strip_tabs, self.info_bar):
            band.show_all()
            band.set_no_show_all(True)
        self.apply_density()

    # ------------------------------------------------------------- density
    def apply_density(self) -> None:
        """Arrange the bands for the active density (re-parents; sessions stay alive)."""

        compact = _compact()
        bar, header = self.page_bar, self.header
        if compact:
            _move(bar.crumbs, bar)  # stays in the (hidden) page bar; groups use header.prefix
            _move(bar.more, header.actions)
            _move(bar.primary, header.actions)
        else:
            _move(bar.crumbs, bar, expand=True)
            _move(bar.primary, bar, end=True)
            _move(bar.more, bar, end=True)
        for band in (bar, header, self.tabs, self.strip_tabs, self.details, self.activity, self.info_bar):
            band.apply_density()
        self._sync_bands()

    def set_narrow(self, narrow: bool) -> None:
        """Window narrower than ``Layout.Page.NARROW_WIDTH``: sessions get their own strip."""

        if narrow != self._narrow:
            self._narrow = narrow
            self.header.set_narrow(narrow)
            self._sync_bands()

    def set_sessions_strip(self, visible: bool) -> None:
        """Whether the Terminal tab currently shows the session chips (tabs mode with sessions)."""

        if visible != self._sessions_strip:
            self._sessions_strip = visible
            self._sync_bands()

    def set_chrome_visible(self, visible: bool) -> None:
        """Focus mode: only row B (tabs / sessions) and the terminal stay."""

        self._chrome_visible = visible
        self._sync_bands()

    def _sync_bands(self) -> None:
        compact = _compact()
        chrome = self._chrome_visible
        self.page_bar.set_visible(chrome and not compact)
        self.header.set_visible(chrome)
        self.info_bar.set_visible(chrome)
        merged = (
            compact
            and not self._narrow
            and self._sessions_strip
            and self.tabs.current is PageTab.TERMINAL
        )
        self.tabs.set_visible(not merged)
        self.strip_tabs.set_visible(merged)
        if merged != self.merged:
            self.merged = merged
            self.on_strip_merged(merged)

    # ---------------------------------------------------------------- tabs
    def _on_tab_clicked(self, tab: PageTab) -> None:
        self.tabs.select(tab)
        self.strip_tabs.select(tab)
        self.on_tab_changed(tab)

    def _show_tab(self, tab: PageTab) -> None:
        child = self.stack.get_child_by_name(tab.value)
        if child is not None:
            child.show()
        self.stack.set_visible_child_name(tab.value)
        self._sync_bands()

    @property
    def current_tab(self) -> PageTab:
        return self.tabs.current

    def select_tab(self, tab: PageTab) -> None:
        self.tabs.select(tab)
        self.strip_tabs.select(tab)
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
