"""Terminal macro dialogs: save / edit a macro, the Macros window, and the macro file's problems.

:class:`MacroDialog` is one dialog for both cases: after a recording (Save /
Discard Recording / Keep Recording, steps read-only) and from the Macros window
(Save / Cancel, with the step editor). It edits a copy; the caller stores
:meth:`MacroDialog.values` only after *Save*. Secret steps are always shown as
``••••••`` unless the user re-enters the master password (*Reveal Secrets*);
nothing secret is put into a selectable label, a tooltip or the clipboard.
"""

from __future__ import annotations

import time
from typing import Callable, List, Optional, Sequence, Tuple

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk, Pango  # type: ignore  # noqa: E402

from ...macros import KEY_NAMES, MAX_NAME, Macro, duration_ms  # noqa: E402
from ..i18n import _, ngettext  # noqa: E402
from ..theme.tokens import Space  # noqa: E402
from ..widgets.icon_button import set_accessible_name  # noqa: E402
from .base import BaseDialog, DialogSize, is_accept  # noqa: E402
from .confirm import confirm  # noqa: E402
from .prompt import prompt_text  # noqa: E402
from .secrets import UnlockDialog  # noqa: E402
from .shortcuts_page import accel_text, steals_terminal_input  # noqa: E402

MASK = "••••••"
RESPONSE_DISCARD = 10
RESPONSE_KEEP_RECORDING = 11
RESPONSE_RUN = 12

Conflicts = Callable[[str], List[Tuple[str, str]]]  # accelerator → [(owner, title)] already using it


def _visible(text: str, limit: int = 120) -> str:
    shown = text.replace("\r", "⏎").replace("\n", "⏎").replace("\t", "⇥")
    shown = "".join(c if c.isprintable() else "·" for c in shown)
    return shown if len(shown) <= limit else shown[: limit - 1] + "…"


def describe_step(step: dict, *, reveal: bool = False) -> str:
    """One line for the step list (secrets masked unless ``reveal``)."""

    kind = step.get("type")
    secret = bool(step.get("secret")) and not reveal
    if kind == "text":
        text = MASK if secret else f"“{_visible(str(step.get('text', '')))}”"
        if step.get("secret"):
            return (_("Paste secret: {text}") if step.get("paste") else _("Type secret: {text}")).format(text=text)
        return (_("Paste: {text}") if step.get("paste") else _("Type: {text}")).format(text=text)
    if kind == "key":
        return _("Key: {key}").format(key=step.get("key", "?"))
    if kind == "raw":
        data = MASK if secret else " ".join(str(step.get("hex", ""))[i:i + 2] for i in range(0, len(str(step.get("hex", ""))), 2))
        return _("Raw bytes: {bytes}").format(bytes=data)
    if kind == "delay":
        return _("Wait {ms} ms").format(ms=step.get("ms", 0))
    return _("Unknown step (kept, not played)")


def _capture_result(event: Gdk.EventKey) -> Tuple[str, str]:
    """``(kind, accel)``: kind is wait, cancel, clear, invalid or accel."""

    mods = event.state & Gtk.accelerator_get_default_mod_mask()
    keyval = Gdk.keyval_to_lower(event.keyval)
    if event.is_modifier:
        return "wait", ""
    if keyval == Gdk.KEY_Escape and not mods:
        return "cancel", ""
    if keyval == Gdk.KEY_BackSpace and not mods:
        return "clear", ""
    if not Gtk.accelerator_valid(keyval, mods):
        return "invalid", ""
    return "accel", Gtk.accelerator_name(keyval, mods)


class MacroDialog(BaseDialog):
    """Name, shortcut, options and steps of one macro (``recording``: right after Stop)."""

    def __init__(self, parent: Optional[Gtk.Window], macro: Macro, *, recording: bool,
                 name_error: Callable[[str], Optional[str]], conflicts: Conflicts,
                 verify_master: Optional[Callable[[str], bool]] = None) -> None:
        BaseDialog.__init__(self, parent, _("Save Macro") if recording else _("Edit Macro"),
                            size=DialogSize.LARGE, height=640)
        self.macro = macro.copy()  # the stored macro stays as it is until Save
        self.recording = recording
        self.replace_owners: List[str] = []
        self._name_error = name_error
        self._conflicts = conflicts
        self._verify_master = verify_master
        self._accel = self.macro.shortcut[0] if self.macro.shortcut else ""
        self._pending: List[Tuple[str, str]] = []  # unresolved conflicts of the shortcut
        self._capturing = False
        self._revealed = False

        grid = Gtk.Grid(column_spacing=Space.M, row_spacing=Space.S)
        self.add_body(grid)
        row = 0

        def label(text: str, widget: Gtk.Widget) -> Gtk.Label:
            caption = Gtk.Label.new_with_mnemonic(text)
            caption.set_xalign(0.0)
            caption.set_mnemonic_widget(widget)
            return caption

        # 1. Name
        self.name_entry = Gtk.Entry()
        self.name_entry.set_max_length(MAX_NAME)
        self.name_entry.set_text(self.macro.name)
        self.name_entry.set_hexpand(True)
        self.name_entry.set_activates_default(True)
        self.name_entry.connect("changed", lambda *_: self._sync())
        grid.attach(label(_("_Name"), self.name_entry), 0, row, 1, 1)
        grid.attach(self.name_entry, 1, row, 1, 1)
        row += 1
        self.name_message = Gtk.Label()
        self.name_message.set_xalign(0.0)
        self.name_message.get_style_context().add_class("form-error")
        grid.attach(self.name_message, 1, row, 1, 1)
        row += 1

        # 2. Shortcut
        shortcut_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.S)
        self.capture_button = Gtk.Button()
        self.capture_button.connect("clicked", lambda *_: self.start_capture())
        shortcut_box.pack_start(self.capture_button, False, False, 0)
        self.clear_shortcut_button = Gtk.Button.new_with_mnemonic(_("C_lear"))
        self.clear_shortcut_button.connect("clicked", lambda *_: self.set_shortcut(""))
        shortcut_box.pack_start(self.clear_shortcut_button, False, False, 0)
        grid.attach(label(_("_Shortcut"), self.capture_button), 0, row, 1, 1)
        grid.attach(shortcut_box, 1, row, 1, 1)
        row += 1
        self.shortcut_hint = Gtk.Label(label=_("Optional: a macro can also be run from the menu."))
        self.shortcut_hint.set_xalign(0.0)
        self.shortcut_hint.set_line_wrap(True)
        self.shortcut_hint.get_style_context().add_class("dim-label")
        grid.attach(self.shortcut_hint, 1, row, 1, 1)
        row += 1
        self.conflict_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.S)
        self.conflict_label = Gtk.Label()
        self.conflict_label.set_xalign(0.0)
        self.conflict_label.set_line_wrap(True)
        self.conflict_label.get_style_context().add_class("form-error")
        self.conflict_box.pack_start(self.conflict_label, True, True, 0)
        self.replace_button = Gtk.Button.new_with_mnemonic(_("_Replace"))
        self.replace_button.connect("clicked", lambda *_: self.replace_conflict())
        self.another_button = Gtk.Button.new_with_mnemonic(_("Choose _Another"))
        self.another_button.connect("clicked", lambda *_: (self.set_shortcut(""), self.start_capture()))
        self.conflict_box.pack_start(self.replace_button, False, False, 0)
        self.conflict_box.pack_start(self.another_button, False, False, 0)
        self.conflict_box.set_no_show_all(True)
        grid.attach(self.conflict_box, 1, row, 1, 1)
        row += 1

        # Options
        self.description_entry = Gtk.Entry()
        self.description_entry.set_text(self.macro.description)
        self.description_entry.set_placeholder_text(_("Optional"))
        grid.attach(label(_("_Description"), self.description_entry), 0, row, 1, 1)
        grid.attach(self.description_entry, 1, row, 1, 1)
        row += 1
        self.timing_combo = Gtk.ComboBoxText()
        self.timing_combo.append("recorded", _("As recorded (with the recorded pauses)"))
        self.timing_combo.append("fast", _("Fast (100 ms between steps)"))
        self.timing_combo.set_active_id(self.macro.timing)
        self.timing_combo.connect("changed", lambda *_: self._sync_summary())
        grid.attach(label(_("_Timing"), self.timing_combo), 0, row, 1, 1)
        grid.attach(self.timing_combo, 1, row, 1, 1)
        row += 1
        self.confirm_check = Gtk.CheckButton.new_with_mnemonic(_("_Ask before running it"))
        self.confirm_check.set_active(self.macro.confirm_before_run)
        grid.attach(self.confirm_check, 1, row, 1, 1)
        row += 1

        # 3. Steps
        steps_title = Gtk.Label(label=_("Steps"))
        steps_title.set_xalign(0.0)
        steps_title.get_style_context().add_class("section-title")
        self.add_body(steps_title)
        self.summary = Gtk.Label()
        self.summary.set_xalign(0.0)
        self.summary.get_style_context().add_class("dim-label")
        self.add_body(self.summary)
        self.step_list = Gtk.ListBox()
        self.step_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        set_accessible_name(self.step_list, _("Macro steps"))
        self.step_list.connect("row-selected", lambda *_: self._sync_editor())
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_min_content_height(160)
        scroller.set_vexpand(True)
        scroller.add(self.step_list)
        self.add_body(scroller, expand=True)
        self.editor = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.XS)
        self.editor_buttons = {}
        for key, mnemonic, callback in (
            ("up", _("Move _Up"), lambda: self.move_step(-1)),
            ("down", _("Move Do_wn"), lambda: self.move_step(1)),
            ("edit", _("_Edit…"), self.edit_step),
            ("delete", _("De_lete"), self.delete_step),
            ("text", _("Insert _Text…"), lambda: self.insert_step("text")),
            ("key", _("Insert _Key…"), lambda: self.insert_step("key")),
            ("delay", _("Insert _Pause…"), lambda: self.insert_step("delay")),
            ("secret", _("Replace _Secret…"), self.replace_secret),
        ):
            button = Gtk.Button.new_with_mnemonic(mnemonic)
            button.connect("clicked", lambda _b, cb=callback: cb())
            self.editor.pack_start(button, False, False, 0)
            self.editor_buttons[key] = button
        self.reveal_button = Gtk.ToggleButton.new_with_mnemonic(_("Re_veal Secrets"))
        self.reveal_button.connect("toggled", lambda *_: self._on_reveal_toggled())
        self.editor.pack_end(self.reveal_button, False, False, 0)
        if not recording:
            self.add_body(self.editor)

        if recording:
            self.add_secondary(_("_Discard Recording"), RESPONSE_DISCARD)
            self.add_secondary(_("_Keep Recording"), RESPONSE_KEEP_RECORDING)
        else:
            self.add_cancel()
        self.add_primary(_("_Save"))
        self.set_initial_focus(self.name_entry)
        self.set_response_validator(self._validate)
        self.connect("key-press-event", self._on_key)
        self.connect("destroy", lambda *_: self._forget())
        self._rebuild_steps()
        self.set_shortcut(self._accel, check=False)
        self._sync()

    # ------------------------------------------------------------- values
    def values(self) -> Macro:
        macro = self.macro.copy()
        macro.name = self.name_entry.get_text().strip()
        macro.shortcut = [self._accel] if self._accel else []
        macro.description = self.description_entry.get_text().strip()
        macro.timing = self.timing_combo.get_active_id() or "recorded"
        macro.confirm_before_run = self.confirm_check.get_active()
        return macro

    @property
    def shortcut(self) -> str:
        return self._accel

    @property
    def conflict_pending(self) -> bool:
        return bool(self._pending)

    def can_save(self) -> bool:
        has_steps = any(step.get("type") != "delay" for step in self.macro.steps)
        return self._name_error(self.name_entry.get_text()) is None and not self._pending and has_steps

    # ------------------------------------------------------------ shortcut
    def start_capture(self) -> None:
        self._capturing = True
        self.capture_button.set_label(_("Press the keys…"))
        self.shortcut_hint.set_text(_("Esc cancels, Backspace removes the shortcut."))
        self.capture_button.grab_focus()

    def _on_key(self, _dialog, event: Gdk.EventKey) -> bool:
        if not self._capturing:
            return False
        kind, accel = _capture_result(event)
        if kind == "wait":
            return True
        if kind == "invalid":
            self.shortcut_hint.set_text(_("This key cannot be used as a shortcut."))
            return True
        self._capturing = False
        if kind == "clear":
            self.set_shortcut("")
        elif kind == "accel":
            self.set_shortcut(accel)
        else:
            self.set_shortcut(self._accel, check=False)
        return True

    def set_shortcut(self, accel: str, *, check: bool = True) -> None:
        """Use ``accel`` (``""``: none); a conflict blocks Save until it is replaced or changed."""

        self._capturing = False
        self._accel = accel
        self.replace_owners = [] if check else self.replace_owners
        self._pending = self._conflicts(accel) if accel else []
        self.capture_button.set_label(accel_text([accel]) if accel else _("Set Shortcut…"))
        set_accessible_name(self.capture_button, _("Shortcut: {keys}").format(keys=accel_text([accel]) or _("none")))
        self.clear_shortcut_button.set_sensitive(bool(accel))
        if self._pending:
            titles = ", ".join(f"“{title}”" for _owner, title in self._pending)
            self.conflict_label.set_text(_("This shortcut is already assigned to {owners}.").format(owners=titles))
            self.conflict_box.show_all()
        else:
            self.conflict_box.hide()
        if accel and steals_terminal_input(accel):
            self.shortcut_hint.set_text(_("Programs running in the terminal will no longer receive this key."))
        else:
            self.shortcut_hint.set_text(_("Optional: a macro can also be run from the menu."))
        self._sync()

    def replace_conflict(self) -> None:
        """Take the shortcut away from the other action / macro when the macro is saved."""

        titles = ", ".join(f"“{title}”" for _owner, title in self._pending)
        self.replace_owners = [owner for owner, _title in self._pending]
        self._pending = []
        self.conflict_box.hide()
        self.shortcut_hint.set_text(_("It will be removed from {owners}.").format(owners=titles))
        self._sync()

    # --------------------------------------------------------------- steps
    def _rebuild_steps(self, select: Optional[int] = None) -> None:
        for child in self.step_list.get_children():
            self.step_list.remove(child)
        for index, step in enumerate(self.macro.steps):
            row = Gtk.ListBoxRow()
            text = Gtk.Label(label=f"{index + 1}. {describe_step(step, reveal=self._revealed)}")
            text.set_xalign(0.0)
            text.set_ellipsize(Pango.EllipsizeMode.END)
            text.set_selectable(False)  # never a selectable label for a secret
            row.add(text)
            row.step_index = index
            self.step_list.add(row)
        self.step_list.show_all()
        if select is not None and 0 <= select < len(self.macro.steps):
            self.step_list.select_row(self.step_list.get_row_at_index(select))
        self._sync_summary()
        self._sync_editor()
        self._sync()

    def step_rows(self) -> List[str]:
        return [row.get_child().get_text() for row in self.step_list.get_children()]

    def _selected(self) -> Optional[int]:
        row = self.step_list.get_selected_row()
        return row.step_index if row is not None else None

    def select_step(self, index: int) -> None:
        self.step_list.select_row(self.step_list.get_row_at_index(index))

    def _sync_summary(self) -> None:
        steps = [s for s in self.macro.steps if s.get("type") != "delay"]
        seconds = duration_ms(self.macro.steps, self.timing_combo.get_active_id() or "recorded") / 1000.0
        secrets = len([s for s in self.macro.steps if s.get("secret")])
        text = ngettext("{n} step", "{n} steps", len(steps)).format(n=len(steps))
        text += " · " + _("about {s:.1f} s").format(s=seconds)
        if secrets:
            text += " · " + ngettext("{n} secret step (masked)", "{n} secret steps (masked)", secrets).format(n=secrets)
        self.summary.set_text(text)

    def _sync_editor(self) -> None:
        index = self._selected()
        step = self.macro.steps[index] if index is not None else None
        kind = step.get("type") if step else None
        buttons = self.editor_buttons
        buttons["up"].set_sensitive(bool(index))
        buttons["down"].set_sensitive(index is not None and index < len(self.macro.steps) - 1)
        buttons["delete"].set_sensitive(step is not None)
        buttons["edit"].set_sensitive(kind in ("text", "delay", "key") and not (step or {}).get("secret"))
        buttons["secret"].set_sensitive(bool(step and step.get("secret") and kind == "text"))
        self.reveal_button.set_sensitive(self.macro.has_secrets and self._verify_master is not None)

    def move_step(self, offset: int) -> None:
        index = self._selected()
        if index is None or not 0 <= index + offset < len(self.macro.steps):
            return
        steps = self.macro.steps
        steps[index], steps[index + offset] = steps[index + offset], steps[index]
        self._rebuild_steps(index + offset)

    def delete_step(self) -> None:
        index = self._selected()
        if index is not None:
            self.macro.steps.pop(index)
            self._rebuild_steps(min(index, len(self.macro.steps) - 1))

    def _ask_text(self, title: str, default: str = "") -> Optional[str]:
        return prompt_text(self, title, default=default, accept_label=_("_OK"))

    def _ask_delay(self, default: int = 500) -> Optional[int]:
        def check(value: str) -> Optional[str]:
            return None if value.strip().isdigit() and int(value) <= 3_600_000 else _("Enter milliseconds (0–3600000).")

        text = prompt_text(self, _("Pause"), _("Milliseconds to wait"), str(default), validator=check)
        return int(text) if text else None

    def _ask_key(self, default: str = "Enter") -> Optional[str]:
        dialog = BaseDialog(self, _("Insert Key"), size=DialogSize.SMALL, resizable=False)
        combo = Gtk.ComboBoxText()
        for name in KEY_NAMES:
            combo.append(name, name)
        combo.set_active_id(default if default in KEY_NAMES else "Enter")
        set_accessible_name(combo, _("Key"))
        dialog.add_body(combo)
        dialog.add_cancel()
        dialog.add_primary(_("_OK"))
        dialog.show_all()
        try:
            return combo.get_active_id() if is_accept(int(dialog.run())) else None
        finally:
            dialog.destroy()

    def edit_step(self) -> None:
        index = self._selected()
        if index is None:
            return
        step = self.macro.steps[index]
        kind = step.get("type")
        if step.get("secret"):
            return
        if kind == "text":
            value = self._ask_text(_("Edit Text"), str(step.get("text", "")))
            if value is not None:
                step["text"] = value
        elif kind == "delay":
            ms = self._ask_delay(int(step.get("ms", 0)))
            if ms is not None:
                step["ms"] = ms
        elif kind == "key":
            key = self._ask_key(str(step.get("key", "")))
            if key is not None:
                step.clear()
                step.update({"type": "key", "key": key})
        self._rebuild_steps(index)

    def insert_step(self, kind: str) -> None:
        index = self._selected()
        position = len(self.macro.steps) if index is None else index + 1
        if kind == "text":
            value = self._ask_text(_("Insert Text"))
            step = {"type": "text", "text": value} if value else None
        elif kind == "key":
            key = self._ask_key()
            step = {"type": "key", "key": key} if key else None
        else:
            ms = self._ask_delay()
            step = {"type": "delay", "ms": ms} if ms is not None else None
        if step is not None:
            self.macro.steps.insert(position, step)
            self._rebuild_steps(position)

    def replace_secret(self) -> None:
        index = self._selected()
        if index is None or not self.macro.steps[index].get("secret"):
            return
        value = prompt_text(self, _("Replace Secret"), _("The new text is sent where the old one was."),
                            secret=True, accept_label=_("_Replace"))
        if value is not None:
            self.macro.steps[index]["text"] = value
            value = ""
        self._rebuild_steps(index)

    def _on_reveal_toggled(self) -> None:
        if self.reveal_button.get_active() and not self._revealed:
            if self._verify_master is None or UnlockDialog(
                    self, _("Reveal Secrets"), _("Enter your master password"),
                    _("The secret steps of this macro are shown until the dialog closes."),
                    self._verify_master, accept_label=_("_Reveal"),
                    error_text=_("Incorrect master password. Please try again.")).ask() is None:
                self.reveal_button.set_active(False)
                return
            self._revealed = True
        elif not self.reveal_button.get_active():
            self._revealed = False
        self._rebuild_steps(self._selected())

    # ----------------------------------------------------------- validation
    def _sync(self) -> None:
        error = self._name_error(self.name_entry.get_text())
        self.name_message.set_text(error or "")
        if self.primary_button is not None:
            self.primary_button.set_sensitive(self.can_save())

    def _validate(self, response: int) -> bool:
        if response == RESPONSE_DISCARD and any(s.get("type") != "delay" for s in self.macro.steps):
            return confirm(self, _("Discard the recording?"), _("The recorded steps are deleted."),
                           confirm_label=_("_Discard"), destructive=True)
        return not is_accept(response) or self.can_save()

    def _forget(self) -> None:
        self._revealed = False


def run_macro_dialog(dialog: MacroDialog) -> Tuple[int, Optional[Macro]]:
    """``(response, values)``; values only after Save. The dialog is destroyed."""

    dialog.show_all()
    dialog.conflict_box.set_visible(dialog.conflict_pending)
    try:
        response = int(dialog.run())
        values = dialog.values() if is_accept(response) else None
        return response, values
    finally:
        dialog.destroy()


def _modified_text(stamp: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(stamp)) if stamp else ""


class MacrosWindow(BaseDialog):
    """Search, then one row per macro: name, shortcut, steps, last modified. Run / Edit / Duplicate / Delete."""

    COLUMNS = (_("Name"), _("Shortcut"), _("Steps"), _("Modified"))

    def __init__(self, parent: Optional[Gtk.Window], list_macros: Callable[[], Sequence[Macro]], *,
                 on_edit: Callable[[str, Gtk.Window], None], on_duplicate: Callable[[str], None],
                 on_delete: Callable[[str, Gtk.Window], None]) -> None:
        BaseDialog.__init__(self, parent, _("Macros"), size=DialogSize.LARGE, height=520)
        self._list_macros = list_macros
        self._on_edit, self._on_duplicate, self._on_delete = on_edit, on_duplicate, on_delete
        self.run_request: Optional[str] = None
        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.S)
        self.search = Gtk.SearchEntry()
        self.search.set_placeholder_text(_("Filter macros"))
        set_accessible_name(self.search, _("Filter macros"))
        self.search.connect("search-changed", lambda *_: self.filter.refilter())
        top.pack_start(self.search, True, True, 0)
        self.buttons = {}
        for key, mnemonic, callback in (
            ("run", _("_Run"), self._run),
            ("edit", _("_Edit…"), lambda: self._with_selected(lambda mid: self._on_edit(mid, self))),
            ("duplicate", _("D_uplicate"), lambda: self._with_selected(self._on_duplicate)),
            ("delete", _("_Delete…"), lambda: self._with_selected(lambda mid: self._on_delete(mid, self))),
        ):
            button = Gtk.Button.new_with_mnemonic(mnemonic)
            button.connect("clicked", lambda _b, cb=callback: cb())
            top.pack_start(button, False, False, 0)
            self.buttons[key] = button
        self.add_body(top)
        self.store = Gtk.ListStore(str, str, str, str, str)  # id, name, shortcut, steps, modified
        self.filter = self.store.filter_new()
        self.filter.set_visible_func(self._visible)
        self.view = Gtk.TreeView(model=self.filter)
        set_accessible_name(self.view, _("Macros"))
        for column, title in enumerate(self.COLUMNS, start=1):
            renderer = Gtk.CellRendererText()
            renderer.set_property("ellipsize", Pango.EllipsizeMode.END)
            col = Gtk.TreeViewColumn(title, renderer, text=column)
            col.set_expand(column == 1)
            col.set_resizable(True)
            self.view.append_column(col)
        self.view.get_selection().connect("changed", lambda *_: self._sync_buttons())
        self.view.connect("row-activated", lambda *_: self._with_selected(lambda mid: self._on_edit(mid, self)))
        scroller = Gtk.ScrolledWindow()
        scroller.set_vexpand(True)
        scroller.add(self.view)
        self.add_body(scroller, expand=True)
        self.empty = Gtk.Label(label=_("No macros yet. Record one with the Record button in the session toolbar."))
        self.empty.get_style_context().add_class("dim-label")
        self.empty.set_no_show_all(True)
        self.add_body(self.empty)
        self.add_button(_("_Close"), Gtk.ResponseType.CLOSE)
        self.set_initial_focus(self.search)
        self.refresh()

    def refresh(self, select: Optional[str] = None) -> None:
        current = select or self.selected_id()
        self.store.clear()
        for macro in self._list_macros():
            steps = len([s for s in macro.steps if s.get("type") != "delay"])
            self.store.append([macro.id, macro.name, accel_text(macro.shortcut), str(steps),
                               _modified_text(macro.modified)])
        self.empty.set_visible(len(self.store) == 0)
        if current:
            self.select(current)
        self._sync_buttons()

    def _visible(self, model, it, _data) -> bool:
        needle = self.search.get_text().strip().casefold()
        return not needle or needle in f"{model[it][1]} {model[it][2]}".casefold()

    def names(self) -> List[str]:
        return [row[1] for row in self.filter]

    def select(self, macro_id: str) -> None:
        for row in self.filter:
            if row[0] == macro_id:
                self.view.get_selection().select_iter(row.iter)
                return

    def selected_id(self) -> Optional[str]:
        model, it = self.view.get_selection().get_selected()
        return model[it][0] if it is not None else None

    def _sync_buttons(self) -> None:
        selected = self.selected_id() is not None
        for button in self.buttons.values():
            button.set_sensitive(selected)

    def _with_selected(self, callback: Callable[[str], None]) -> None:
        macro_id = self.selected_id()
        if macro_id is not None:
            callback(macro_id)
            GLib.idle_add(lambda: self.refresh() and False if self.get_visible() else False)

    def _run(self) -> None:
        self.run_request = self.selected_id()
        if self.run_request is not None:
            self.response(RESPONSE_RUN)


def ask_damaged_file(parent: Optional[Gtk.Window], kind: str) -> str:
    """``"keep"`` (no macros this session, the file untouched) or ``"empty"`` (moved aside, start empty)."""

    details = {
        "wrong_key": _("It was not encrypted with the current master password, or it was changed."),
        "tampered": _("Its header was changed: it is not used, so that a weaker protection cannot be forced."),
        "unsupported": _("It was written by a newer SSH Manager."),
        "unreadable": _("It cannot be read (permissions)."),
    }
    dialog = Gtk.MessageDialog(transient_for=parent, modal=True, message_type=Gtk.MessageType.WARNING,
                               text=_("The macros file cannot be opened"))
    dialog.format_secondary_text(details.get(kind, _("It is damaged or incomplete.")) + "\n\n" + _(
        "Keep it and continue without macros: macros are off until the next start and the file is not changed.\n"
        "Start empty: the file is renamed to macros.enc.damaged-<time> and a new one is started."))
    dialog.add_button(_("_Keep It and Continue Without Macros"), 1)
    dialog.add_button(_("_Start Empty"), 2)
    dialog.set_default_response(1)
    try:
        return "empty" if dialog.run() == 2 else "keep"
    finally:
        dialog.destroy()


def ask_master_password_needed(parent: Optional[Gtk.Window]) -> bool:
    """Macros are never stored unencrypted: ``True`` = set a master password, ``False`` = discard."""

    dialog = Gtk.MessageDialog(transient_for=parent, modal=True, message_type=Gtk.MessageType.INFO,
                               text=_("Macros need a master password"))
    dialog.format_secondary_text(_(
        "Macros can contain passwords, so they are always stored encrypted with the master password. "
        "Set a master password to keep this macro, or discard it."))
    dialog.add_button(_("_Discard Macro"), 2)
    dialog.add_button(_("_Set Master Password…"), 1)
    dialog.set_default_response(1)
    try:
        return dialog.run() == 1
    finally:
        dialog.destroy()


__all__ = ["MacroDialog", "MacrosWindow", "run_macro_dialog", "describe_step", "ask_damaged_file",
           "ask_master_password_needed", "MASK", "RESPONSE_DISCARD", "RESPONSE_KEEP_RECORDING", "RESPONSE_RUN"]
