"""Preferences → Keyboard Shortcuts: change, add, clear and reset the window's shortcuts.

:class:`ShortcutEditor` holds the logic (no widgets, so it is testable): it edits
a working copy of the window's :class:`ShortcutRegistry`, and Preferences →
Apply hands its :meth:`ShortcutEditor.to_dict` to the real registry (Cancel
discards it). :class:`ShortcutsPage` is the view; :class:`ShortcutCaptureDialog`
reads one key combination ("Press the new shortcut, Esc to cancel, Backspace to
clear").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gtk  # type: ignore  # noqa: E402

from ..controllers.shortcut_registry import ShortcutRegistry, canonical  # noqa: E402
from ..controllers.shortcuts import ACTIONS, ActionSpec  # noqa: E402
from ..i18n import _  # noqa: E402
from ..theme.tokens import Space  # noqa: E402
from ..widgets.icon_button import set_accessible_name  # noqa: E402
from .confirm import confirm  # noqa: E402


def show_message(parent: Optional[Gtk.Window], title: str, detail: str) -> None:
    dialog = Gtk.MessageDialog(transient_for=parent, modal=True, message_type=Gtk.MessageType.INFO,
                               buttons=Gtk.ButtonsType.OK, text=title)
    dialog.format_secondary_text(detail)
    try:
        dialog.run()
    finally:
        dialog.destroy()

TERMINAL_INPUT_MODS = Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.MOD1_MASK | Gdk.ModifierType.SUPER_MASK


def accel_text(accels) -> str:
    labels = []
    for accel in accels:
        key, mods = Gtk.accelerator_parse(accel)
        if key:
            labels.append(Gtk.accelerator_get_label(key, mods))
    return ", ".join(labels)


def steals_terminal_input(accel: str) -> bool:
    """A plain key, or Ctrl+letter without Shift / Alt: programs in the terminal would never get it."""

    key, mods = Gtk.accelerator_parse(accel)
    if not key:
        return False
    if not mods & TERMINAL_INPUT_MODS:
        return True
    letter = Gdk.keyval_to_unicode(key)
    return (mods & (TERMINAL_INPUT_MODS | Gdk.ModifierType.SHIFT_MASK)) == Gdk.ModifierType.CONTROL_MASK and \
        bool(letter) and chr(letter).isalpha()


@dataclass
class AssignResult:
    ok: bool
    conflicts: List[str] = field(default_factory=list)  # action names already using the shortcut
    steals_input: bool = False  # plain key / Ctrl+letter: asks before using it
    invalid: bool = False


class ShortcutEditor:
    """Edits a working copy of the registry; nothing changes in the window until Apply."""

    def __init__(self, registry: ShortcutRegistry) -> None:
        self.registry = ShortcutRegistry(overrides=registry.to_dict())
        self.registry.set_extras(registry.extras(), notify=False)  # macros: for conflicts, changed elsewhere

    def title(self, name: str) -> str:
        return self.registry.title(name)

    def assign(self, name: str, accel: str, *, append: bool = False, replace: bool = False,
               force: bool = False) -> AssignResult:
        """Give ``name`` the shortcut ``accel`` (``append``: as an additional one).

        Without ``replace`` a conflict is reported instead of applied; with it the shortcut is
        taken away from the other actions. Without ``force`` a shortcut that would take keys
        away from programs in the terminal is reported first.
        """

        value = canonical(accel)
        if not value:
            return AssignResult(False, invalid=True)
        conflicts = self.registry.conflicts(value, ignoring=name)
        if conflicts and (not replace or any(self.registry.is_extra(c) for c in conflicts)):
            return AssignResult(False, conflicts=conflicts)  # a macro's shortcut changes only in Manage Macros
        if steals_terminal_input(value) and not force:
            return AssignResult(False, conflicts=conflicts, steals_input=True)
        for other in conflicts:
            self.registry.set_accels(other, [a for a in self.registry.effective_accels(other) if a != value])
        current = list(self.registry.effective_accels(name)) if append else []
        if value not in current:
            current.append(value)
        self.registry.set_accels(name, current)
        return AssignResult(True, conflicts=conflicts)

    def clear(self, name: str) -> None:
        self.registry.set_accels(name, [])

    def reset(self, name: str) -> None:
        self.registry.reset(name)

    def reset_all(self) -> None:
        self.registry.reset_all()

    def to_dict(self) -> Dict[str, List[str]]:
        return self.registry.to_dict()


FIXED_SHORTCUTS: List[Tuple[str, str, str]] = [  # (group, title, accelerators) handled by widgets, not actions
    (spec.group, spec.title, "Alt+1…9" if spec.name == "goto-tab" else accel_text(spec.display_accels))
    for spec in ACTIONS if spec.display_accels
]


class ShortcutCaptureDialog(Gtk.Dialog):
    """Reads one shortcut: :meth:`ask` returns it, ``""`` for "clear" (Backspace) or ``None`` (Esc)."""

    def __init__(self, parent: Optional[Gtk.Window], action_title: str) -> None:
        Gtk.Dialog.__init__(self, title=_("Set Shortcut"), transient_for=parent, modal=True)
        self.result: Optional[str] = None
        box = self.get_content_area()
        box.set_spacing(Space.M)
        box.set_border_width(Space.L)
        prompt = Gtk.Label(label=_("Press the new shortcut for “{action}”.").format(action=action_title))
        prompt.set_line_wrap(True)
        hint = Gtk.Label(label=_("Esc cancels, Backspace removes the shortcut."))
        hint.get_style_context().add_class("dim-label")
        self.shown = Gtk.Label(label=_("Waiting for a key…"))
        self.shown.get_style_context().add_class("section-title")
        for widget in (prompt, self.shown, hint):
            box.pack_start(widget, False, False, 0)
        self.add_button(_("_Cancel"), Gtk.ResponseType.CANCEL)
        self.connect("key-press-event", self._on_key)
        set_accessible_name(self, _("Set shortcut for {action}").format(action=action_title))
        self.show_all()

    def _on_key(self, _dialog, event: Gdk.EventKey) -> bool:
        mods = event.state & Gtk.accelerator_get_default_mod_mask()
        keyval = Gdk.keyval_to_lower(event.keyval)
        if event.is_modifier:
            return True  # wait for the real key
        if keyval == Gdk.KEY_Escape and not mods:
            self.response(Gtk.ResponseType.CANCEL)
        elif keyval == Gdk.KEY_BackSpace and not mods:
            self.result = ""
            self.response(Gtk.ResponseType.OK)
        elif not Gtk.accelerator_valid(keyval, mods):
            self.shown.set_text(_("This key cannot be used as a shortcut."))
        else:
            self.result = Gtk.accelerator_name(keyval, mods)
            self.shown.set_text(Gtk.accelerator_get_label(keyval, mods))
            self.response(Gtk.ResponseType.OK)
        return True

    def ask(self) -> Optional[str]:
        try:
            return self.result if self.run() == Gtk.ResponseType.OK else None
        finally:
            self.destroy()


class ShortcutsPage(Gtk.Box):
    """Search, then one row per action: shortcut, Change / Add / Clear / Reset; fixed bindings read-only."""

    def __init__(self, editor: ShortcutEditor, parent: Callable[[], Optional[Gtk.Window]]) -> None:
        Gtk.Box.__init__(self, orientation=Gtk.Orientation.VERTICAL, spacing=Space.S)
        self.editor = editor
        self._parent = parent
        self.search = Gtk.SearchEntry()
        self.search.set_placeholder_text(_("Filter shortcuts"))
        set_accessible_name(self.search, _("Filter shortcuts"))
        self.search.connect("search-changed", lambda *_: self.listbox.invalidate_filter())
        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.S)
        top.pack_start(self.search, True, True, 0)
        reset_all = Gtk.Button.new_with_mnemonic(_("Reset _All"))
        reset_all.connect("clicked", lambda *_: self._reset_all())
        top.pack_end(reset_all, False, False, 0)
        self.pack_start(top, False, False, 0)
        self.listbox = Gtk.ListBox()
        self.listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        self.listbox.set_filter_func(self._filter)
        self.listbox.set_header_func(self._header)
        self.listbox.connect("row-activated", lambda _l, row: self.change(row.name) if row.name else None)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)
        scroller.add(self.listbox)
        self.pack_start(scroller, True, True, 0)
        self.rows: Dict[str, Gtk.ListBoxRow] = {}
        self._build_rows()

    # ------------------------------------------------------------------- rows
    def _build_rows(self) -> None:
        for spec in ACTIONS:
            if self.editor.registry.spec(spec.name) is not None:
                self.listbox.add(self._row(spec))
        for name, entry in self.editor.registry.extras().items():  # macros: changed in Manage Macros
            row = Gtk.ListBoxRow()
            row.name, row.group, row.title = "", entry.group, entry.title
            row.extra = name
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.S)
            box.set_border_width(Space.XS)
            label = Gtk.Label(label=entry.title)
            label.set_xalign(0.0)
            box.pack_start(label, True, True, 0)
            hint = Gtk.Label(label=_("{keys} (change it in Manage Macros)").format(
                keys=accel_text(entry.accels) or _("No shortcut")))
            hint.get_style_context().add_class("dim-label")
            box.pack_end(hint, False, False, 0)
            row.add(box)
            row.set_activatable(False)
            self.listbox.add(row)
        for group, title, accels in FIXED_SHORTCUTS:
            row = Gtk.ListBoxRow()
            row.name, row.group, row.title = "", group, title
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.S)
            box.set_border_width(Space.XS)
            label = Gtk.Label(label=title)
            label.set_xalign(0.0)
            box.pack_start(label, True, True, 0)
            fixed = Gtk.Label(label=_("{keys} (fixed)").format(keys=accels))
            fixed.get_style_context().add_class("dim-label")
            box.pack_end(fixed, False, False, 0)
            row.add(box)
            row.set_activatable(False)
            self.listbox.add(row)
        self.listbox.show_all()

    def _row(self, spec: ActionSpec) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        row.name, row.group, row.title = spec.name, spec.group, spec.title
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.S)
        box.set_border_width(Space.XS)
        title = Gtk.Label(label=spec.title)
        title.set_xalign(0.0)
        box.pack_start(title, True, True, 0)
        row.accel_label = Gtk.Label()
        box.pack_start(row.accel_label, False, False, 0)
        for mnemonic, tooltip, callback in (
            (_("_Change…"), _("Set a new shortcut for “{action}”"), self.change),
            (_("A_dd…"), _("Add another shortcut for “{action}”"), self.add),
            (_("C_lear"), _("Remove the shortcuts of “{action}”"), self.clear),
            (_("_Reset"), _("Restore the default shortcut of “{action}”"), self.reset),
        ):
            button = Gtk.Button.new_with_mnemonic(mnemonic)
            button.get_style_context().add_class("flat")
            button.set_tooltip_text(tooltip.format(action=spec.title))
            set_accessible_name(button, tooltip.format(action=spec.title))
            button.connect("clicked", lambda _b, name=spec.name, cb=callback: cb(name))
            box.pack_start(button, False, False, 0)
            if mnemonic == _("_Reset"):
                row.reset_button = button
        row.add(box)
        self.rows[spec.name] = row
        self._sync_row(spec.name)
        return row

    def _sync_row(self, name: str) -> None:
        row = self.rows[name]
        accels = self.editor.registry.effective_accels(name)
        row.accel_label.set_text(accel_text(accels) or _("Disabled"))
        row.reset_button.set_sensitive(not self.editor.registry.is_default(name))
        set_accessible_name(row, f"{row.title}: {accel_text(accels) or _('Disabled')}")

    def refresh(self) -> None:
        for name in self.rows:
            self._sync_row(name)

    def _filter(self, row: Gtk.ListBoxRow) -> bool:
        needle = self.search.get_text().strip().casefold()
        if not needle:
            return True
        accels = accel_text(self.editor.registry.effective_accels(row.name)) if row.name else ""
        return needle in f"{row.title} {row.group} {accels}".casefold()

    @staticmethod
    def _header(row: Gtk.ListBoxRow, before: Optional[Gtk.ListBoxRow]) -> None:
        if before is not None and before.group == row.group:
            row.set_header(None)
            return
        label = Gtk.Label(label=row.group)
        label.set_xalign(0.0)
        label.get_style_context().add_class("section-title")
        label.set_margin_top(Space.M)
        row.set_header(label)

    # ---------------------------------------------------------------- actions
    def change(self, name: str) -> None:
        self._capture(name, append=False)

    def add(self, name: str) -> None:
        self._capture(name, append=True)

    def clear(self, name: str) -> None:
        self.editor.clear(name)
        self.refresh()

    def reset(self, name: str) -> None:
        self.editor.reset(name)
        self.refresh()

    def _reset_all(self) -> None:
        if confirm(self._parent(), _("Reset all keyboard shortcuts?"),
                   _("Every shortcut you changed goes back to its default."), confirm_label=_("_Reset All"),
                   destructive=True):
            self.editor.reset_all()
            self.refresh()

    def _capture(self, name: str, *, append: bool) -> None:
        accel = ShortcutCaptureDialog(self._parent(), self.editor.title(name)).ask()
        if accel is None:
            return
        if accel == "":
            self.clear(name)
            return
        self.assign_interactively(name, accel, append=append)

    def assign_interactively(self, name: str, accel: str, *, append: bool = False) -> AssignResult:
        """Assign, asking about a conflict (Replace / Cancel) and about keys programs need."""

        result = self.editor.assign(name, accel, append=append)
        replace = force = False
        while not result.ok and not result.invalid:
            label = accel_text([canonical(accel)])
            macros = [c for c in result.conflicts if self.editor.registry.is_extra(c)]
            if macros:
                show_message(self._parent(), _("{key} is already used").format(key=label),
                             _("The macro “{macro}” uses this shortcut. Change the macro's shortcut in "
                               "Manage Macros first, or choose another one.").format(
                                 macro=", ".join(self.editor.title(m) for m in macros)))
                return result
            if result.conflicts and not replace:
                others = ", ".join(self.editor.title(other) for other in result.conflicts)
                if not confirm(self._parent(), _("{key} is already used").format(key=label),
                               _("“{others}” uses this shortcut. Replace it, so that it runs “{action}”?").format(
                                   others=others, action=self.editor.title(name)), confirm_label=_("_Replace")):
                    return result
                replace = True
            elif result.steals_input and not force:
                if not confirm(self._parent(), _("Use {key}?").format(key=label),
                               _("Programs running in the terminal will no longer receive this key."),
                               confirm_label=_("_Use It")):
                    return result
                force = True
            else:
                break
            result = self.editor.assign(name, accel, append=append, replace=replace, force=force)
        self.refresh()
        return result


__all__ = ["ShortcutEditor", "ShortcutsPage", "ShortcutCaptureDialog", "AssignResult", "steals_terminal_input"]
