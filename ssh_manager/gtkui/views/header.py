"""Main window header bar.

Layout (start → end)::

    [☰ app menu] [+ New Host | ▾] [sidebar]   SSH Manager / <inventory>   [🔍] [🕭 history] [⚙] [⛶]

All buttons are bound to ``win.*`` actions, so sensitivity and accelerators
come from the action system. The app menu is a ``Gio.Menu`` model, which lets
GTK show the accelerator next to each entry automatically.
"""

from __future__ import annotations

from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gio, Gtk  # type: ignore  # noqa: E402

from ..constants import Icons  # noqa: E402
from ..controllers.notifications import MessageHistory  # noqa: E402
from ..i18n import _  # noqa: E402
from ..theme.tokens import Space  # noqa: E402
from ..widgets.icon_button import IconButton, set_accessible_name  # noqa: E402
from ..widgets.toast import MessageHistoryButton  # noqa: E402


def _app_menu_model(git_updates: bool = False) -> Gio.Menu:
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

    help_section = Gio.Menu()
    help_section.append(_("Keyboard Shortcuts"), "win.shortcuts")
    help_section.append(_("About SSH Manager"), "win.about")
    menu.append_section(None, help_section)
    return menu


def _new_menu_model(template_items) -> Gio.Menu:
    menu = Gio.Menu()
    hosts = Gio.Menu()
    hosts.append(_("New Host…"), "win.new-host")
    templates = Gio.Menu()
    for label, detailed_action in template_items:
        templates.append(label, detailed_action)
    if template_items:
        hosts.append_submenu(_("New Host from Template"), templates)
    hosts.append(_("Quick Connect…"), "win.quick-connect")
    menu.append_section(None, hosts)
    groups = Gio.Menu()
    groups.append(_("New Group…"), "win.new-group")
    groups.append(_("New Child Group…"), "win.new-child-group")
    menu.append_section(None, groups)
    return menu


class HeaderView(Gtk.HeaderBar):
    """Header bar with global actions."""

    def __init__(self, history: MessageHistory, *, template_items=()) -> None:
        Gtk.HeaderBar.__init__(self)
        self.set_show_close_button(True)
        self.set_title(_("SSH Manager"))
        self.get_style_context().add_class("sm-headerbar")

        # ---- start: app menu, New split button, sidebar toggle
        self.app_menu_button = Gtk.MenuButton()
        self.app_menu_button.set_tooltip_text(_("Main menu (F10)"))
        set_accessible_name(self.app_menu_button, _("Main menu"))
        overlay = Gtk.Overlay()
        overlay.add(Gtk.Image.new_from_icon_name(Icons.APP_MENU, Gtk.IconSize.BUTTON))
        self._git_badge = Gtk.Label(label="1")
        self._git_badge.get_style_context().add_class("badge")
        self._git_badge.set_halign(Gtk.Align.END)
        self._git_badge.set_valign(Gtk.Align.START)
        self._git_badge.set_no_show_all(True)
        self._git_badge.set_tooltip_text(_("Git updates available"))
        overlay.add_overlay(self._git_badge)
        self.app_menu_button.add(overlay)
        self._app_popover = Gtk.Popover.new_from_model(self.app_menu_button, _app_menu_model())
        self.app_menu_button.set_popover(self._app_popover)
        self.pack_start(self.app_menu_button)

        new_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        new_box.get_style_context().add_class("linked")
        self.new_button = Gtk.Button()
        new_content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.XS)
        new_content.pack_start(Gtk.Image.new_from_icon_name(Icons.NEW, Gtk.IconSize.BUTTON), False, False, 0)
        self._new_label = Gtk.Label(label=_("New Host"))
        new_content.pack_start(self._new_label, False, False, 0)
        self.new_button.add(new_content)
        self.new_button.set_action_name("win.new-host")
        self.new_button.set_tooltip_text(_("Add a new host (Ctrl+N)"))
        self.new_button.get_style_context().add_class("suggested-action")
        new_box.pack_start(self.new_button, False, False, 0)
        self.new_menu_button = Gtk.MenuButton()
        self.new_menu_button.add(Gtk.Image.new_from_icon_name(Icons.DROPDOWN, Gtk.IconSize.BUTTON))
        self.new_menu_button.set_tooltip_text(_("More ways to add hosts and groups"))
        set_accessible_name(self.new_menu_button, _("More new items"))
        self.new_menu_button.get_style_context().add_class("suggested-action")
        self.new_menu_button.set_menu_model(_new_menu_model(list(template_items)))
        new_box.pack_start(self.new_menu_button, False, False, 0)
        self.pack_start(new_box)

        self.sidebar_button = Gtk.ToggleButton()
        self.sidebar_button.add(Gtk.Image.new_from_icon_name(Icons.SIDEBAR, Gtk.IconSize.BUTTON))
        self.sidebar_button.set_tooltip_text(_("Toggle sidebar (Ctrl+B)"))
        set_accessible_name(self.sidebar_button, _("Toggle sidebar"))
        self.sidebar_button.set_active(True)
        self.pack_start(self.sidebar_button)

        # ---- end (packed right-to-left): maximize, settings, history, search
        self.maximize_button = Gtk.ToggleButton()
        self._maximize_image = Gtk.Image.new_from_icon_name(Icons.MAXIMIZE, Gtk.IconSize.BUTTON)
        self.maximize_button.add(self._maximize_image)
        self.maximize_button.set_tooltip_text(_("Maximize workspace (F11)"))
        set_accessible_name(self.maximize_button, _("Maximize workspace"))
        self.pack_end(self.maximize_button)

        self.settings_button = IconButton(Icons.SETTINGS, _("Settings (Ctrl+,)"), action_name="win.settings")
        self.pack_end(self.settings_button)

        self.history_button = MessageHistoryButton(history)
        self.pack_end(self.history_button)

        self.search_button = IconButton(Icons.SEARCH, _("Search hosts (Ctrl+F)"), action_name="win.focus-search")
        self.pack_end(self.search_button)
        self._subtitle = ""

    # ---------------------------------------------------------------- api
    def set_inventory_path(self, path: str) -> None:
        home = str(Path.home())
        self._subtitle = "~" + path[len(home):] if path.startswith(home) else path
        self.set_subtitle(self._subtitle)
        self.set_tooltip_text(path)

    def set_git_updates(self, available: bool) -> None:
        self._git_badge.set_visible(available)
        self._app_popover.bind_model(_app_menu_model(git_updates=available), None)
        tip = _("Main menu — Git updates available (F10)") if available else _("Main menu (F10)")
        self.app_menu_button.set_tooltip_text(tip)

    def set_maximized(self, maximized: bool) -> None:
        self._maximize_image.set_from_icon_name(Icons.RESTORE if maximized else Icons.MAXIMIZE, Gtk.IconSize.BUTTON)
        tip = _("Restore panels (F11)") if maximized else _("Maximize workspace (F11)")
        self.maximize_button.set_tooltip_text(tip)
        set_accessible_name(self.maximize_button, tip)

    def set_compact(self, compact: bool) -> None:
        """Hide secondary labels at narrow widths."""

        self._new_label.set_visible(not compact)
        self.set_subtitle(None if compact else self._subtitle)

    def open_app_menu(self) -> None:
        self.app_menu_button.set_active(True)
