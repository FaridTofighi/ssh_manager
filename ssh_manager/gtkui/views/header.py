"""Main window top bar (Relay layout).

Layout (start → end)::

    [▣ SSH Manager] │ WORKSPACE <inventory> ▾          ● All systems operational  [pulse] [?] (AF) [– □ ×]

* The brand block is as wide as the sidebar so its divider lines up with the
  sidebar edge; when the sidebar is hidden it shrinks and shows an expand button.
* The workspace switcher lists the loaded inventories.
* The health indicator and the pulse button open the message history.
* The help button holds *Keyboard Shortcuts*, *Documentation* and *About*.
* The avatar menu holds the former application-menu items.

All menu entries are bound to ``win.*`` actions, so sensitivity and
accelerators come from the action system.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional, Sequence

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gio, Gtk, Pango  # type: ignore  # noqa: E402

from ..constants import Icons  # noqa: E402
from ..controllers.notifications import MessageHistory  # noqa: E402
from ..health import Health  # noqa: E402
from ..i18n import _  # noqa: E402
from ..theme.tokens import Layout, Level, Size, Space  # noqa: E402
from ..widgets.dot import Dot  # noqa: E402
from ..widgets.icon_button import IconButton, set_accessible_name  # noqa: E402
from ..widgets.icons import make_icon  # noqa: E402
from ..widgets.toast import MessageHistoryButton  # noqa: E402
from ..workspaces import Workspace  # noqa: E402

BRAND_PADDING = Layout.Topbar.BRAND_INSET
WORKSPACE_GAP = Layout.Topbar.WORKSPACE_GAP


def _avatar_menu_model(*, git_updates: bool = False, maximized: bool = False) -> Gio.Menu:
    menu = Gio.Menu()
    main = Gio.Menu()
    main.append(_("Settings"), "win.settings")
    main.append(_("Credentials"), "win.credentials")
    main.append(_("Credential Vault"), "win.vault")
    main.append(_("SSH Config File"), "win.config-file")
    menu.append_section(None, main)

    inventory = Gio.Menu()
    inventory.append(_("Open Shared Inventory…"), "win.open-shared")
    inventory.append(_("Git Updates Available…") if git_updates else _("Check for Git Updates"), "win.git-check")
    inventory.append(_("Reload Configuration"), "win.refresh")
    menu.append_section(None, inventory)

    view = Gio.Menu()
    view.append(_("Restore Panels") if maximized else _("Maximize Workspace"), "win.toggle-maximize")
    view.append(_("Toggle Sidebar"), "win.toggle-sidebar")
    menu.append_section(None, view)

    help_section = Gio.Menu()
    help_section.append(_("Keyboard Shortcuts"), "win.shortcuts")
    help_section.append(_("About SSH Manager"), "win.about")
    menu.append_section(None, help_section)

    quit_section = Gio.Menu()
    quit_section.append(_("Quit"), "win.quit")
    menu.append_section(None, quit_section)
    return menu


def _help_menu_model() -> Gio.Menu:
    menu = Gio.Menu()
    menu.append(_("Keyboard Shortcuts"), "win.shortcuts")
    menu.append(_("Documentation"), "win.documentation")
    menu.append(_("About SSH Manager"), "win.about")
    return menu


class HeaderView(Gtk.HeaderBar):
    """Top bar with brand, workspace switcher, health and the user menu."""

    def __init__(self, history: MessageHistory) -> None:
        Gtk.HeaderBar.__init__(self)
        self.set_show_close_button(True)
        self.set_title(_("SSH Manager"))
        self.set_custom_title(Gtk.Box())  # no centred title: the wordmark is the title
        self.set_property("spacing", 0)
        self.get_style_context().add_class("sm-topbar")
        self.set_size_request(-1, Size.TOPBAR)

        self.on_workspace_selected: Callable[[Workspace], None] = lambda _w: None
        self._git_updates = False
        self._maximized = False
        self._compact = False
        self._workspace_path = ""

        self._build_brand()
        self._build_workspace_switcher()
        self._build_end_cluster(history)
        self.set_compact(False)

    # ------------------------------------------------------------ building
    def _build_brand(self) -> None:
        self.brand = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self.brand.get_style_context().add_class("topbar-brand")
        logo = Gtk.Box()
        logo.get_style_context().add_class("logo-tile")
        logo.set_size_request(Size.LOGO_TILE, Size.LOGO_TILE)
        logo.set_valign(Gtk.Align.CENTER)
        logo.set_margin_start(BRAND_PADDING)
        glyph = make_icon(Icons.TERMINAL, 22)
        glyph.set_valign(Gtk.Align.CENTER)
        logo.set_center_widget(glyph)
        self.brand.pack_start(logo, False, False, 0)
        self.wordmark = Gtk.Label(label=_("SSH Manager"))
        self.wordmark.get_style_context().add_class("wordmark")
        self.wordmark.set_margin_start(Layout.Topbar.WORDMARK_GAP)
        self.wordmark.set_ellipsize(Pango.EllipsizeMode.END)
        self.brand.pack_start(self.wordmark, False, False, 0)

        self.sidebar_expand_button = IconButton(
            Icons.SIDEBAR, _("Show sidebar (Ctrl+B)"), action_name="win.toggle-sidebar", css_classes=("topbar-icon",)
        )
        self.sidebar_expand_button.set_margin_start(Space.M)
        self.sidebar_expand_button.set_no_show_all(True)
        self.brand.pack_start(self.sidebar_expand_button, False, False, 0)

        self.divider = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        self.divider.get_style_context().add_class("topbar-divider")
        self.divider.set_valign(Gtk.Align.CENTER)
        self.divider.set_size_request(1, Layout.Topbar.DIVIDER_HEIGHT)
        self.brand.pack_end(self.divider, False, False, 0)
        self.pack_start(self.brand)

    def _build_workspace_switcher(self) -> None:
        self.workspace_button = Gtk.MenuButton()
        self.workspace_button.get_style_context().add_class("workspace-switcher")
        self.workspace_button.set_relief(Gtk.ReliefStyle.NONE)
        self.workspace_button.set_valign(Gtk.Align.CENTER)
        self.workspace_button.set_margin_start(WORKSPACE_GAP)
        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self._workspace_caption = Gtk.Label(label=_("WORKSPACE"))
        self._workspace_caption.get_style_context().add_class("workspace-label")
        self._workspace_caption.set_no_show_all(True)  # visibility belongs to set_compact()
        content.pack_start(self._workspace_caption, False, False, 0)
        self._workspace_name = Gtk.Label(label="")
        self._workspace_name.get_style_context().add_class("workspace-name")
        self._workspace_name.set_margin_start(Layout.Topbar.WORKSPACE_NAME_GAP)
        self._workspace_name.set_ellipsize(Pango.EllipsizeMode.END)
        self._workspace_name.set_max_width_chars(32)
        content.pack_start(self._workspace_name, False, False, 0)
        chevron = make_icon(Icons.CHEVRON_DOWN, 16, css_class="workspace-chevron")
        chevron.set_margin_start(Space.M)
        content.pack_start(chevron, False, False, 0)
        self.workspace_button.add(content)
        set_accessible_name(self.workspace_button, _("Switch workspace"))
        self.workspace_button.set_tooltip_text(_("Switch workspace"))

        self._workspace_popover = Gtk.Popover()
        self._workspace_popover.get_style_context().add_class("sm-popover")
        self._workspace_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._workspace_list.set_size_request(280, -1)
        self._workspace_popover.add(self._workspace_list)
        self.workspace_button.set_popover(self._workspace_popover)
        self.pack_start(self.workspace_button)
        self.set_workspaces([])

    def _build_end_cluster(self, history: MessageHistory) -> None:
        # pack_end packs right-to-left: avatar, help, activity, health.
        self.avatar_button = Gtk.MenuButton()
        self.avatar_button.get_style_context().add_class("avatar")
        self.avatar_button.set_valign(Gtk.Align.CENTER)
        self.avatar_button.set_halign(Gtk.Align.CENTER)
        self.avatar_button.set_size_request(Size.AVATAR, Size.AVATAR)
        self.avatar_button.set_margin_start(Layout.Topbar.AVATAR_GAP)
        self._avatar_label = Gtk.Label(label="U")
        self.avatar_button.add(self._avatar_label)
        self.avatar_button.set_tooltip_text(_("Account and application menu (F10)"))
        set_accessible_name(self.avatar_button, _("Application menu"))
        self._avatar_popover = Gtk.Popover.new_from_model(self.avatar_button, _avatar_menu_model())
        self._avatar_popover.get_style_context().add_class("sm-popover")
        self.avatar_button.set_popover(self._avatar_popover)
        self.pack_end(self.avatar_button)

        self.help_button = Gtk.MenuButton()
        self.help_button.get_style_context().add_class("topbar-icon")
        self.help_button.set_relief(Gtk.ReliefStyle.NONE)
        self.help_button.set_valign(Gtk.Align.CENTER)
        self.help_button.add(make_icon(Icons.HELP, 20))
        self.help_button.set_tooltip_text(_("Help"))
        set_accessible_name(self.help_button, _("Help"))
        self.help_button.set_margin_start(Layout.Topbar.HELP_GAP)
        help_popover = Gtk.Popover.new_from_model(self.help_button, _help_menu_model())
        help_popover.get_style_context().add_class("sm-popover")
        self.help_button.set_popover(help_popover)
        self.pack_end(self.help_button)

        self.history_button = MessageHistoryButton(history)
        self.history_button.get_style_context().add_class("topbar-icon")
        self.history_button.set_valign(Gtk.Align.CENTER)
        self.pack_end(self.history_button)

        self.health_button = Gtk.Button()
        self.health_button.get_style_context().add_class("flat")
        self.health_button.get_style_context().add_class("health")
        self.health_button.set_relief(Gtk.ReliefStyle.NONE)
        self.health_button.set_valign(Gtk.Align.CENTER)
        self.health_button.set_margin_end(Layout.Topbar.HEALTH_GAP)
        health = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Layout.Topbar.HEALTH_DOT_GAP)
        self._health_dot = Dot(9, css_class="health-dot")
        health.pack_start(self._health_dot, False, False, 0)
        self._health_label = Gtk.Label(label=_("All systems operational"))
        self._health_label.set_no_show_all(True)  # visibility belongs to set_compact()
        health.pack_start(self._health_label, False, False, 0)
        self.health_button.add(health)
        self.health_button.connect("clicked", lambda *_: self.history_button.set_active(True))
        self.pack_end(self.health_button)
        self.set_health(Health(Level.SUCCESS, _("All systems operational"), _("Operational")))

    # ---------------------------------------------------------------- api
    def set_brand_width(self, width: Optional[int]) -> None:
        """Line the brand divider up with the sidebar edge (``None`` = sidebar hidden)."""

        if width is None:
            self.brand.set_size_request(-1, -1)
            self.sidebar_expand_button.show()
            self.divider.set_margin_start(Space.L)
        else:
            self.brand.set_size_request(max(1, int(width)), -1)
            self.sidebar_expand_button.hide()
            self.divider.set_margin_start(0)

    def set_workspaces(self, workspaces: Sequence[Workspace]) -> None:
        for child in list(self._workspace_list.get_children()):
            self._workspace_list.remove(child)
        title = Gtk.Label(label=_("WORKSPACES"))
        title.set_xalign(0.0)
        title.get_style_context().add_class("caps")
        title.set_margin_start(Space.M)
        title.set_margin_top(Space.XS)
        title.set_margin_bottom(Space.XS)
        self._workspace_list.pack_start(title, False, False, 0)
        for workspace in workspaces:
            self._workspace_list.pack_start(self._workspace_row(workspace), False, False, 0)
            if workspace.current and not workspace.read_only:
                self._workspace_name.set_text(workspace.name)
                self._workspace_path = str(workspace.path)
                self.workspace_button.set_tooltip_text(_("Workspace: {path}").format(path=workspace.path))
        self._workspace_list.pack_start(Gtk.Separator(), False, False, Space.XS)
        for label, action in ((_("Open Shared Inventory…"), "win.open-shared"), (_("Open Config Folder…"), "win.open-folder")):
            button = Gtk.ModelButton()
            button.set_property("text", label)
            button.set_action_name(action)
            button.get_style_context().add_class("popover-menu-item")
            self._workspace_list.pack_start(button, False, False, 0)
        self._workspace_list.show_all()

    def _workspace_row(self, workspace: Workspace) -> Gtk.Widget:
        button = Gtk.Button()
        button.set_relief(Gtk.ReliefStyle.NONE)
        button.get_style_context().add_class("popover-menu-item")
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.S)
        box.pack_start(make_icon(Icons.FOLDER, 16, css_class="menu-icon"), False, False, 0)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        name = Gtk.Label(label=workspace.name)
        name.set_xalign(0.0)
        texts.pack_start(name, False, False, 0)
        detail = Gtk.Label(label=_("Shared · read-only") if workspace.read_only else str(workspace.path))
        detail.set_xalign(0.0)
        detail.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        detail.set_max_width_chars(36)
        detail.get_style_context().add_class("caption")
        texts.pack_start(detail, False, False, 0)
        box.pack_start(texts, True, True, 0)
        if workspace.current:
            box.pack_end(make_icon(Icons.CHECK, 16, css_class="accent-icon"), False, False, 0)
        button.add(box)
        button.set_tooltip_text(str(workspace.path))
        if workspace.read_only or workspace.current:
            button.set_sensitive(not workspace.read_only)
        button.connect("clicked", lambda *_: self._select_workspace(workspace))
        return button

    def _select_workspace(self, workspace: Workspace) -> None:
        self._workspace_popover.popdown()
        if not workspace.current:
            self.on_workspace_selected(workspace)

    @property
    def workspace_name(self) -> str:
        return self._workspace_name.get_text()

    def set_inventory_path(self, path: str) -> None:
        """Back-compat: show ``path`` as the only workspace."""

        from ..workspaces import workspace_list

        self.set_workspaces(workspace_list(Path(path)))

    def set_health(self, health: Health) -> None:
        self._health_label.set_text(health.text)
        state = {Level.ERROR: "error", Level.WARNING: "warning"}.get(health.level, "")
        self._health_dot.set_state_class(state, choices=("error", "warning"))
        self.health_button.set_tooltip_text(_("{status} — show messages").format(status=health.text))
        set_accessible_name(self.health_button, health.text)

    @property
    def health_text(self) -> str:
        return self._health_label.get_text()

    def set_user_initials(self, initials: str) -> None:
        self._avatar_label.set_text(initials or "U")

    def set_git_updates(self, available: bool) -> None:
        self._git_updates = available
        self._rebuild_avatar_menu()
        tip = _("Account and application menu — Git updates available (F10)") if available else _("Account and application menu (F10)")
        self.avatar_button.set_tooltip_text(tip)
        ctx = self.avatar_button.get_style_context()
        (ctx.add_class if available else ctx.remove_class)("attention")

    def set_maximized(self, maximized: bool) -> None:
        self._maximized = maximized
        self._rebuild_avatar_menu()

    def _rebuild_avatar_menu(self) -> None:
        self._avatar_popover.bind_model(_avatar_menu_model(git_updates=self._git_updates, maximized=self._maximized), None)

    def set_compact(self, compact: bool) -> None:
        """Narrow windows: dot-only health and no ``WORKSPACE`` caption."""

        self._compact = compact
        self._health_label.set_visible(not compact)
        self._workspace_caption.set_visible(not compact)

    def open_app_menu(self) -> None:
        self.avatar_button.set_active(True)
