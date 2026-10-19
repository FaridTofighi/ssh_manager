"""Window actions, keyboard accelerators and the shortcuts window.

:data:`ACTIONS` is the single list of named window actions. The window
supplies a callback per action name; this module registers the
``Gio.SimpleAction`` objects, sets the accelerators on the application and
builds the ``Gtk.ShortcutsWindow`` from the same data, so the help window can
never drift from the real bindings.

Terminal-friendly choices: copy/paste/interrupt use Ctrl+Shift so plain Ctrl
combinations still reach programs running in the terminal; ``Delete`` and
``Esc`` are handled by the focused widget (tree, search, broadcast strip)
instead of global accelerators so they keep working inside terminals.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gio, GLib, Gtk  # type: ignore  # noqa: E402

from ..i18n import _  # noqa: E402


@dataclass(frozen=True)
class ActionSpec:
    """A named window action and its default accelerators."""

    name: str
    title: str
    group: str
    accels: Tuple[str, ...] = ()
    # Accelerators shown in the help window for bindings handled outside the
    # action system (e.g. Delete in the tree).
    display_accels: Tuple[str, ...] = ()
    parameter: str = ""  # GVariant type string for parameterised actions

    @property
    def detailed_name(self) -> str:
        return f"win.{self.name}"


G_GENERAL = _("General")
G_HOSTS = _("Hosts and Groups")
G_SESSIONS = _("Sessions")
G_TERMINAL = _("Terminal")
G_VIEW = _("View")

ACTIONS: List[ActionSpec] = [
    # General
    ActionSpec("new-host", _("New host"), G_GENERAL, ("<Primary>n",)),
    ActionSpec("new-host-template", _("New host from template"), G_GENERAL, parameter="s"),
    ActionSpec("new-group", _("New group"), G_GENERAL),
    ActionSpec("new-child-group", _("New child group"), G_GENERAL),
    ActionSpec("quick-connect", _("Quick connect"), G_GENERAL, ("<Primary><Shift>t",)),
    ActionSpec("focus-search", _("Search hosts"), G_GENERAL, ("<Primary>k", "<Primary>f")),
    ActionSpec("open-folder", _("Open config folder"), G_GENERAL),
    ActionSpec("import-mobaxterm", _("Import from MobaXterm"), G_GENERAL),
    ActionSpec("refresh", _("Reload configuration"), G_GENERAL, ("F5",)),
    ActionSpec("settings", _("Settings"), G_GENERAL, ("<Primary>comma",)),
    ActionSpec("app-menu", _("Open the application menu"), G_GENERAL, ("F10",)),
    ActionSpec("shortcuts", _("Keyboard shortcuts"), G_GENERAL, ("<Primary>question", "F1")),
    ActionSpec("credentials", _("Credentials"), G_GENERAL),
    ActionSpec("vault", _("Credential vault"), G_GENERAL),
    ActionSpec("config-file", _("SSH config file"), G_GENERAL),
    ActionSpec("open-shared", _("Open shared inventory"), G_GENERAL),
    ActionSpec("git-check", _("Check for Git updates"), G_GENERAL),
    ActionSpec("about", _("About"), G_GENERAL),
    ActionSpec("documentation", _("Documentation"), G_GENERAL),
    ActionSpec("quit", _("Quit"), G_GENERAL, ("<Primary>q",)),
    # Hosts
    ActionSpec("host-connect", _("Connect to selected host"), G_HOSTS, ("<Primary>t",), ("Return",)),
    ActionSpec("host-edit", _("Edit selected host"), G_HOSTS),
    ActionSpec("host-duplicate", _("Duplicate selected host"), G_HOSTS),
    ActionSpec("host-move", _("Move selected host"), G_HOSTS),
    ActionSpec("host-delete", _("Delete selected item"), G_HOSTS, (), ("Delete",)),
    ActionSpec("copy-ssh", _("Copy SSH command"), G_HOSTS),
    ActionSpec("copy-scp", _("Copy SCP command"), G_HOSTS),
    ActionSpec("rename", _("Rename selected item or tab"), G_HOSTS, ("F2",)),
    ActionSpec("context-menu", _("Open context menu"), G_HOSTS, (), ("<Shift>F10", "Menu")),
    # Sessions
    ActionSpec("close-tab", _("Close session"), G_SESSIONS, ("<Primary>w",)),
    ActionSpec("next-tab", _("Next session"), G_SESSIONS, ("<Primary>Tab", "<Primary>Page_Down")),
    ActionSpec("prev-tab", _("Previous session"), G_SESSIONS, ("<Primary><Shift>Tab", "<Primary>Page_Up")),
    ActionSpec("goto-tab", _("Go to session 1…9"), G_SESSIONS, (), ("<Alt>1",), parameter="i"),
    ActionSpec("duplicate-session", _("Duplicate session"), G_SESSIONS, ("<Primary><Shift>d",)),
    ActionSpec("reconnect", _("Reconnect session"), G_SESSIONS, ("<Primary><Shift>r",)),
    ActionSpec("save-transcript", _("Save transcript"), G_SESSIONS, ("<Primary><Shift>s",)),
    ActionSpec("toggle-broadcast", _("Toggle broadcast input"), G_SESSIONS, ("<Primary><Shift>b",)),
    ActionSpec("toggle-tile", _("Show all sessions"), G_SESSIONS, ("<Primary><Shift>a",)),
    ActionSpec("split-pane", _("Split pane"), G_SESSIONS, ("<Primary><Shift>e",)),
    ActionSpec("toggle-stats", _("Show remote statistics"), G_SESSIONS),
    ActionSpec("toggle-strip-ansi", _("Copy as plain text"), G_SESSIONS),
    # Terminal
    ActionSpec("copy", _("Copy"), G_TERMINAL, ("<Primary><Shift>c",)),
    ActionSpec("paste", _("Paste"), G_TERMINAL, ("<Primary><Shift>v",)),
    ActionSpec("interrupt", _("Send interrupt (Ctrl+C)"), G_TERMINAL, ("<Primary><Shift>k",)),
    ActionSpec("font-increase", _("Zoom in"), G_TERMINAL, ("<Primary>plus", "<Primary>equal", "<Primary>KP_Add")),
    ActionSpec("font-decrease", _("Zoom out"), G_TERMINAL, ("<Primary>minus", "<Primary>KP_Subtract")),
    ActionSpec("font-reset", _("Reset zoom"), G_TERMINAL, ("<Primary>0", "<Primary>KP_0")),
    # View
    ActionSpec("toggle-sidebar", _("Toggle sidebar"), G_VIEW, ("<Primary>b",)),
    ActionSpec("toggle-details", _("Host details"), G_VIEW, ("<Primary>i", "F9")),
    ActionSpec("host-activity", _("Host activity"), G_VIEW),
    ActionSpec("toggle-maximize", _("Focus mode (terminal only)"), G_VIEW, ("F11", "<Primary><Shift>m")),
]

GOTO_TAB_ACCELS = tuple((f"<Alt>{n}", n) for n in range(1, 10))

ACTION_BY_NAME: Dict[str, ActionSpec] = {spec.name: spec for spec in ACTIONS}


def accel_label(spec_name: str) -> str:
    """Human-readable label of an action's first accelerator (for menu hints)."""

    spec = ACTION_BY_NAME.get(spec_name)
    if spec is None:
        return ""
    accels = spec.accels or spec.display_accels
    if not accels:
        return ""
    key, mods = Gtk.accelerator_parse(accels[0])
    return Gtk.accelerator_get_label(key, mods) if key else ""


def register_actions(
    window: Gtk.ApplicationWindow,
    callbacks: Dict[str, Callable[..., None]],
    application: Optional[Gtk.Application] = None,
) -> Dict[str, Gio.SimpleAction]:
    """Create ``win.*`` actions for every spec with a callback and set accelerators."""

    created: Dict[str, Gio.SimpleAction] = {}
    for spec in ACTIONS:
        callback = callbacks.get(spec.name)
        if callback is None:
            continue
        param_type = GLib.VariantType.new(spec.parameter) if spec.parameter else None
        action = Gio.SimpleAction.new(spec.name, param_type)
        if spec.parameter:
            action.connect("activate", lambda _a, param, cb=callback: cb(param.unpack()))
        else:
            action.connect("activate", lambda _a, _p, cb=callback: cb())
        window.add_action(action)
        created[spec.name] = action
    app = application or window.get_application()
    if app is not None:
        for spec in ACTIONS:
            if spec.name in created and spec.accels:
                app.set_accels_for_action(spec.detailed_name, list(spec.accels))
        if "goto-tab" in created:
            for accel, number in GOTO_TAB_ACCELS:
                app.set_accels_for_action(f"win.goto-tab({number})", [accel])
    return created


def set_enabled(actions: Dict[str, Gio.SimpleAction], names: Iterable[str], enabled: bool) -> None:
    for name in names:
        action = actions.get(name)
        if action is not None:
            action.set_enabled(enabled)


def _groups(specs: Sequence[ActionSpec]) -> Dict[str, List[ActionSpec]]:
    grouped: Dict[str, List[ActionSpec]] = {}
    for spec in specs:
        if spec.accels or spec.display_accels:
            grouped.setdefault(spec.group, []).append(spec)
    return grouped


def build_shortcuts_window(parent: Optional[Gtk.Window]) -> Gtk.ShortcutsWindow:
    """Build the ``Gtk.ShortcutsWindow`` from :data:`ACTIONS`.

    Closing it only hides it, and callers reuse one window (never destroy it).
    GTK 3.24 leaves dangling CSS nodes behind a destroyed ``ShortcutsWindow``,
    and the next theme or font change restyles them and crashes the process.
    """

    window = Gtk.ShortcutsWindow(modal=True)
    window.get_style_context().add_class("sm-dialog")
    window.connect("delete-event", lambda widget, _event: widget.hide_on_delete())
    if parent is not None:
        window.set_transient_for(parent)
    section = Gtk.ShortcutsSection(section_name="main", title=_("SSH Manager"), max_height=12)
    section.show()
    for group_title, specs in _groups(ACTIONS).items():
        group = Gtk.ShortcutsGroup(title=group_title)
        group.show()
        for spec in specs:
            accels = list(spec.accels or spec.display_accels)
            if spec.name == "goto-tab":
                accels = ["<Alt>1...9"]
            shortcut = Gtk.ShortcutsShortcut(title=spec.title, accelerator=" ".join(accels))
            shortcut.show()
            group.add(shortcut)
        if spec_extra := _EXTRA_SHORTCUTS.get(group_title):
            for title, accel in spec_extra:
                shortcut = Gtk.ShortcutsShortcut(title=title, accelerator=accel)
                shortcut.show()
                group.add(shortcut)
        section.add(group)
    window.add(section)
    return window


_EXTRA_SHORTCUTS: Dict[str, List[Tuple[str, str]]] = {
    G_GENERAL: [(_("Clear search / leave a field"), "Escape")],
    G_SESSIONS: [
        (_("Stop broadcasting (outside the terminal)"), "Escape"),
        (_("Focus the pane or tile to the left / right / above / below"), "<Alt>Left <Alt>Right <Alt>Up <Alt>Down"),
    ],
}
