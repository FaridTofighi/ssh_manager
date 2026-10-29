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

The accelerators here are the defaults: the user may change them
(``shortcut_registry.ShortcutRegistry``, Preferences → Keyboard Shortcuts).
Once the window sets the active registry, :func:`accel_label`,
:func:`register_actions` and the shortcuts window use its effective bindings.
"""

from __future__ import annotations

import weakref
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
G_MACROS = _("Macros")

ACTIONS: List[ActionSpec] = [
    # General
    ActionSpec("new-host", _("New host"), G_GENERAL, ("<Primary>n",)),
    ActionSpec("new-host-template", _("New host from template"), G_GENERAL, parameter="s"),
    ActionSpec("new-group", _("New group"), G_GENERAL),
    ActionSpec("new-child-group", _("New child group"), G_GENERAL),
    ActionSpec("quick-connect", _("Quick connect"), G_GENERAL, ("<Primary><Alt>t",)),
    ActionSpec("focus-search", _("Search hosts"), G_GENERAL, ("<Primary>k", "<Primary>f")),
    ActionSpec("open-folder", _("Open config folder"), G_GENERAL),
    ActionSpec("import-mobaxterm", _("Import from MobaXterm"), G_GENERAL),
    ActionSpec("import-openssh", _("Import from OpenSSH config"), G_GENERAL),
    ActionSpec("import-rdp-files", _("Import .rdp files"), G_GENERAL),
    ActionSpec("export-rdp-file", _("Export the selected RDP host as a .rdp file"), G_GENERAL),
    ActionSpec("export-openssh", _("Export to OpenSSH config"), G_GENERAL),
    ActionSpec("export-native", _("Export as SSH Manager file (.sshm)"), G_GENERAL),
    ActionSpec("backup-inventory", _("Back up the inventory"), G_GENERAL),
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
    # Ctrl+Shift+E / C are Split pane / Copy: O(pen) and L for the sidebar groups.
    ActionSpec("expand-all-groups", _("Expand all groups"), G_HOSTS, ("<Primary><Shift>o",)),
    ActionSpec("collapse-all-groups", _("Collapse all groups"), G_HOSTS, ("<Primary><Shift>l",)),
    # Sessions
    ActionSpec("new-local-tab", _("New local terminal"), G_SESSIONS, ("<Primary><Shift>t",)),
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
    ActionSpec("copy", _("Copy"), G_TERMINAL, ("<Primary><Shift>c", "<Primary>Insert")),
    ActionSpec("paste", _("Paste"), G_TERMINAL, ("<Primary><Shift>v",)),
    ActionSpec("paste-primary", _("Paste selection"), G_TERMINAL, ("<Shift>Insert",)),
    ActionSpec("terminal-menu", _("Open terminal context menu"), G_TERMINAL),
    ActionSpec("select-all-terminal", _("Select all"), G_TERMINAL),  # Ctrl+Shift+A is "Show all sessions"
    ActionSpec("clear-scrollback", _("Clear scrollback"), G_TERMINAL),
    ActionSpec("reset-terminal", _("Reset terminal"), G_TERMINAL),
    ActionSpec("interrupt", _("Send interrupt (Ctrl+C)"), G_TERMINAL, ("<Primary><Shift>k",)),
    ActionSpec("font-increase", _("Zoom in"), G_TERMINAL, ("<Primary>plus", "<Primary>equal", "<Primary>KP_Add")),
    ActionSpec("font-decrease", _("Zoom out"), G_TERMINAL, ("<Primary>minus", "<Primary>KP_Subtract")),
    ActionSpec("font-reset", _("Reset zoom"), G_TERMINAL, ("<Primary>0", "<Primary>KP_0")),
    # View
    ActionSpec("toggle-sidebar", _("Toggle sidebar"), G_VIEW, ("<Primary>b",)),
    ActionSpec("toggle-details", _("Host details"), G_VIEW, ("<Primary>i", "F9")),
    ActionSpec("host-activity", _("Host activity"), G_VIEW),
    ActionSpec("toggle-maximize", _("Focus mode (terminal only)"), G_VIEW, ("F11", "<Primary><Shift>m")),
    # Macros (each macro's own shortcut is an extra registry entry: win.run-macro('<id>'))
    ActionSpec("macro-record-toggle", _("Record macro / stop recording"), G_MACROS),
    ActionSpec("macro-manage", _("Manage macros"), G_MACROS),
    ActionSpec("macro-stop-playback", _("Stop the running macro"), G_MACROS),
    ActionSpec("run-macro", _("Run macro"), G_MACROS, parameter="s"),
]

GOTO_TAB_ACCELS = tuple((f"<Alt>{n}", n) for n in range(1, 10))

ACTION_BY_NAME: Dict[str, ActionSpec] = {spec.name: spec for spec in ACTIONS}

_ACTIVE_REGISTRY = None  # the window's ShortcutRegistry (set_active_registry); None = the defaults


def set_active_registry(registry) -> None:
    global _ACTIVE_REGISTRY
    _ACTIVE_REGISTRY = registry


def effective_accels(spec: ActionSpec) -> Tuple[str, ...]:
    """The accelerators ``spec`` has now (the user's, else the defaults)."""

    registry = _ACTIVE_REGISTRY
    if registry is not None and registry.spec(spec.name) is not None:
        return tuple(registry.effective_accels(spec.name))
    return spec.accels


def accel_label(spec_name: str) -> str:
    """Human-readable label of an action's first accelerator (for menu hints)."""

    spec = ACTION_BY_NAME.get(spec_name)
    if spec is None:
        return ""
    accels = effective_accels(spec) or spec.display_accels
    if not accels:
        return ""
    key, mods = Gtk.accelerator_parse(accels[0])
    return Gtk.accelerator_get_label(key, mods) if key else ""


_TOOLTIPS: List[Tuple["weakref.ref", str, str, str]] = []  # (widget, text, action, suffix): refresh_accel_tooltips


def accel_tooltip(text: str, action: str, suffix: str = "") -> str:
    """``text (Ctrl+B)`` with the action's current shortcut (just ``text`` when it has none), then ``suffix``."""

    label = accel_label(action)
    head = f"{text} ({label})" if label else text
    return f"{head}. {suffix}" if suffix else head


def set_accel_tooltip(widget: Gtk.Widget, text: str, action: str, suffix: str = "") -> None:
    """Tooltip naming ``action``'s shortcut; it follows the user's changes (:func:`refresh_accel_tooltips`)."""

    widget.set_tooltip_text(accel_tooltip(text, action, suffix))
    _TOOLTIPS.append((weakref.ref(widget), text, action, suffix))


def refresh_accel_tooltips() -> None:
    alive = []
    for ref, text, action, suffix in _TOOLTIPS:
        widget = ref()
        if widget is not None:
            widget.set_tooltip_text(accel_tooltip(text, action, suffix))
            alive.append((ref, text, action, suffix))
    _TOOLTIPS[:] = alive


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
            if spec.name in created and effective_accels(spec):
                app.set_accels_for_action(spec.detailed_name, list(effective_accels(spec)))
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
        if effective_accels(spec) or spec.display_accels:
            grouped.setdefault(spec.group, []).append(spec)
    return grouped


def _shortcut_accelerator(spec: ActionSpec) -> str:
    if spec.name == "goto-tab":
        return "<Alt>1...9"
    return " ".join(list(effective_accels(spec)) + list(spec.display_accels))


def build_shortcuts_window(parent: Optional[Gtk.Window],
                           on_customize: Optional[Callable[[], None]] = None) -> Gtk.ShortcutsWindow:
    """Build the ``Gtk.ShortcutsWindow`` from :data:`ACTIONS` (the effective bindings).

    Closing it only hides it, and callers reuse one window (never destroy it).
    GTK 3.24 leaves dangling CSS nodes behind a destroyed ``ShortcutsWindow``,
    and the next theme or font change restyles them and crashes the process.
    So it is never rebuilt: :func:`refresh_shortcuts_window` updates it in place.
    """

    window = Gtk.ShortcutsWindow(modal=True)
    window.get_style_context().add_class("sm-dialog")
    window.connect("delete-event", lambda widget, _event: widget.hide_on_delete())
    if parent is not None:
        window.set_transient_for(parent)
    section = Gtk.ShortcutsSection(section_name="main", title=_("SSH Manager"), max_height=12)
    section.show()
    rows: Dict[str, Gtk.ShortcutsShortcut] = {}
    groups: Dict[str, Gtk.ShortcutsGroup] = {}
    grouped = _groups(ACTIONS)
    for group_title in dict.fromkeys(spec.group for spec in ACTIONS):
        group = Gtk.ShortcutsGroup(title=group_title)
        group.show()
        groups[group_title] = group
        for spec in grouped.get(group_title, []):
            shortcut = Gtk.ShortcutsShortcut(title=spec.title, accelerator=_shortcut_accelerator(spec))
            shortcut.show()
            group.add(shortcut)
            rows[spec.name] = shortcut
        if spec_extra := _EXTRA_SHORTCUTS.get(group_title):
            for title, accel in spec_extra:
                shortcut = Gtk.ShortcutsShortcut(title=title, accelerator=accel)
                shortcut.show()
                group.add(shortcut)
        section.add(group)
    window.add(section)
    window._sshm_rows, window._sshm_groups = rows, groups  # type: ignore[attr-defined]
    _sync_extra_rows(window)
    titlebar = window.get_titlebar()
    if on_customize is not None and isinstance(titlebar, Gtk.HeaderBar):
        button = Gtk.Button.new_with_mnemonic(_("_Customize…"))
        button.set_tooltip_text(_("Change the keyboard shortcuts"))
        button.connect("clicked", lambda _b: (window.hide(), on_customize()))
        button.show()
        titlebar.pack_end(button)
    return window


def refresh_shortcuts_window(window: Gtk.ShortcutsWindow) -> None:
    """Show the current bindings (after the user changed them) without rebuilding the window."""

    rows: Dict[str, Gtk.ShortcutsShortcut] = getattr(window, "_sshm_rows", {})
    groups: Dict[str, Gtk.ShortcutsGroup] = getattr(window, "_sshm_groups", {})
    for spec in ACTIONS:
        accelerator = _shortcut_accelerator(spec)
        row = rows.get(spec.name)
        if row is None:
            if not accelerator or spec.group not in groups:
                continue
            row = Gtk.ShortcutsShortcut(title=spec.title, accelerator=accelerator)
            groups[spec.group].add(row)
            rows[spec.name] = row
        row.set_property("accelerator", accelerator)
        row.set_visible(bool(accelerator))
    _sync_extra_rows(window)


def _sync_extra_rows(window: Gtk.ShortcutsWindow) -> None:
    """The macros' shortcuts (registry extras): one row each; rows of deleted macros are hidden."""

    rows: Dict[str, Gtk.ShortcutsShortcut] = getattr(window, "_sshm_rows", {})
    groups: Dict[str, Gtk.ShortcutsGroup] = getattr(window, "_sshm_groups", {})
    registry = _ACTIVE_REGISTRY
    extras = registry.extras() if registry is not None else {}
    for name, entry in extras.items():
        group = groups.get(entry.group)
        if group is None:
            continue
        row = rows.get(name)
        if row is None:
            row = rows[name] = Gtk.ShortcutsShortcut(title=entry.title, accelerator=" ".join(entry.accels))
            group.add(row)
        row.set_property("title", entry.title)
        row.set_property("accelerator", " ".join(entry.accels))
        row.set_visible(bool(entry.accels))
    for name, row in rows.items():
        if name.startswith("macro:") and name not in extras:
            row.set_visible(False)
    macros = groups.get(G_MACROS)
    if macros is not None:
        visible = [spec.name for spec in ACTIONS if spec.group == G_MACROS and effective_accels(spec)]
        visible += [name for name, entry in extras.items() if entry.accels]
        macros.set_visible(bool(visible))


_EXTRA_SHORTCUTS: Dict[str, List[Tuple[str, str]]] = {
    G_GENERAL: [(_("Clear search / leave a field"), "Escape")],
    G_SESSIONS: [
        (_("Stop broadcasting (outside the terminal)"), "Escape"),
        (_("Focus the pane or tile to the left / right / above / below"), "<Alt>Left <Alt>Right <Alt>Up <Alt>Down"),
    ],
}
