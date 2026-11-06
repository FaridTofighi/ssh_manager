"""Form building blocks with consistent alignment and inline validation."""

from __future__ import annotations

from typing import Callable, List, Optional

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Pango  # type: ignore  # noqa: E402

from ..theme.tokens import Space  # noqa: E402
from ..validation import Validator  # noqa: E402
from .icon_button import set_accessible_name  # noqa: E402


def _control_text(control: Gtk.Widget) -> str:
    getter = getattr(control, "get_text", None)
    if callable(getter):
        return getter()
    if isinstance(control, Gtk.ComboBoxText):
        return control.get_active_text() or ""
    return ""


def _entry_of(control: Gtk.Widget) -> Optional[Gtk.Entry]:
    if isinstance(control, Gtk.Entry):
        return control
    entry = getattr(control, "entry", None)
    return entry if isinstance(entry, Gtk.Entry) else None


class FormRow:
    """Label + control + optional help text + inline error message."""

    def __init__(
        self,
        label: str,
        control: Gtk.Widget,
        *,
        help_text: str = "",
        validator: Optional[Validator] = None,
        value_getter: Optional[Callable[[], str]] = None,
    ) -> None:
        self.label = Gtk.Label.new_with_mnemonic(label)
        self.label.set_xalign(1.0)
        self.label.set_valign(Gtk.Align.CENTER if not help_text else Gtk.Align.START)
        self.label.get_style_context().add_class("dim-label")
        self.control = control
        entry = _entry_of(control)
        self.label.set_mnemonic_widget(entry or control)
        set_accessible_name(entry or control, label.replace("_", ""))
        control.set_hexpand(True)

        self.help = Gtk.Label(label=help_text)
        self.help.set_xalign(0.0)
        self.help.set_line_wrap(True)
        self.help.get_style_context().add_class("form-help")
        self.help.set_no_show_all(not help_text)

        self.error = Gtk.Label()
        self.error.set_xalign(0.0)
        self.error.set_line_wrap(True)
        self.error.get_style_context().add_class("form-error")
        self.error.set_no_show_all(True)

        self._validator = validator
        self._getter = value_getter or (lambda: _control_text(control))
        self._touched = False
        if entry is not None and validator is not None:
            entry.connect("changed", lambda *_: self._touched and self.validate())
            entry.connect("focus-out-event", self._on_focus_out)

    @property
    def value(self) -> str:
        return self._getter()

    def validate(self) -> bool:
        """Run the validator and show or clear the inline error."""

        self._touched = True
        message = self._validator(self.value) if self._validator else None
        self.set_error(message)
        return message is None

    def set_error(self, message: Optional[str]) -> None:
        entry = _entry_of(self.control)
        if entry is not None:
            ctx = entry.get_style_context()
            (ctx.add_class if message else ctx.remove_class)("invalid")
        self.error.set_text(message or "")
        self.error.set_visible(bool(message))

    def _on_focus_out(self, *_args) -> bool:
        if self.value:
            self.validate()
        return False


class FormSection(Gtk.Box):
    """Titled group of :class:`FormRow` objects laid out in a two-column grid."""

    def __init__(self, title: str = "", description: str = "") -> None:
        Gtk.Box.__init__(self, orientation=Gtk.Orientation.VERTICAL, spacing=Space.S)
        self.get_style_context().add_class("form-section")
        if title:
            heading = Gtk.Label(label=title)
            heading.set_xalign(0.0)
            heading.get_style_context().add_class("section-title")
            self.pack_start(heading, False, False, 0)
        if description:
            desc = Gtk.Label(label=description)
            desc.set_xalign(0.0)
            desc.set_line_wrap(True)
            desc.get_style_context().add_class("section-description")
            self.pack_start(desc, False, False, 0)
        self.grid = Gtk.Grid(column_spacing=Space.M, row_spacing=Space.XS)
        self.grid.set_margin_top(Space.XS)
        self.pack_start(self.grid, False, False, 0)
        self.rows: List[FormRow] = []
        self._next_row = 0

    def add_row(self, row: FormRow, label_group: Optional[Gtk.SizeGroup] = None) -> FormRow:
        r = self._next_row
        self.grid.attach(row.label, 0, r, 1, 1)
        self.grid.attach(row.control, 1, r, 1, 1)
        self.grid.attach(row.help, 1, r + 1, 1, 1)
        self.grid.attach(row.error, 1, r + 2, 1, 1)
        if label_group is not None:
            label_group.add_widget(row.label)
        self._next_row += 3
        self.rows.append(row)
        return row

    def add(self, label: str, control: Gtk.Widget, **kwargs) -> FormRow:  # type: ignore[override]
        label_group = kwargs.pop("label_group", None)
        return self.add_row(FormRow(label, control, **kwargs), label_group)

    def add_widget(self, widget: Gtk.Widget, *, full_width: bool = False) -> None:
        """Add a widget under the controls (check boxes, explanatory text, …); ``full_width``: under both columns."""

        if full_width:
            self.grid.attach(widget, 0, self._next_row, 2, 1)
        else:
            self.grid.attach(widget, 1, self._next_row, 1, 1)
        self._next_row += 1

    def validate(self) -> bool:
        return all([row.validate() for row in self.rows])


def wrapped_label(text: str = "", *, chars: int = 40, max_chars: int = 52, css_class: Optional[str] = None,
                  width: Optional[int] = None) -> Gtk.Label:
    """A label that wraps (left-aligned, not selectable) instead of widening its dialog.

    In GTK 3 a wrapping label without ``width_chars`` / ``max_width_chars`` asks for its *unwrapped*
    width, and a non-resizable dialog opens that wide. ``width`` (px) caps the natural width in pixels:
    the character count is recomputed from the label's own font, so a larger font wraps into more lines
    instead of a wider dialog. Long words (host names, addresses) break between characters.
    """

    label = Gtk.Label(label=text)
    label.set_line_wrap(True)
    label.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
    label.set_xalign(0.0)
    label.set_selectable(False)
    if css_class:
        label.get_style_context().add_class(css_class)
    label.set_width_chars(min(chars, max_chars))
    label.set_max_width_chars(max_chars)
    if width is not None:
        def fit(*_args) -> None:
            metrics = label.get_pango_context().get_metrics(None, None)
            char = max(metrics.get_approximate_char_width(), metrics.get_approximate_digit_width()) / Pango.SCALE
            if char > 0:
                fitting = max(8, int(width / char))
                label.set_width_chars(min(chars, fitting))
                label.set_max_width_chars(fitting)
        fit()
        label.connect("style-updated", fit)  # font or density changed
    return label


def entry(text: str = "", placeholder: str = "", *, activates_default: bool = True) -> Gtk.Entry:
    """Create a ``Gtk.Entry`` with common defaults."""

    widget = Gtk.Entry()
    widget.set_text(text or "")
    if placeholder:
        widget.set_placeholder_text(placeholder)
    widget.set_activates_default(activates_default)
    return widget
