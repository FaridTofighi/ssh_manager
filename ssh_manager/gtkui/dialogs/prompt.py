"""Single-value prompt dialog with inline validation."""

from __future__ import annotations

from typing import Optional

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Pango  # type: ignore  # noqa: E402

from ..i18n import _  # noqa: E402
from ..theme.tokens import Space  # noqa: E402
from ..validation import Validator  # noqa: E402
from ..widgets.forms import FormRow  # noqa: E402
from ..widgets.secret_entry import SecretEntry  # noqa: E402
from .base import BaseDialog, DialogSize, is_accept  # noqa: E402


MESSAGE_MAX_HEIGHT = 160  # px; longer messages scroll
MESSAGE_MAX_CHARS = 2000  # a safety net: callers should never put long lists into a message


def _bounded(message: str) -> str:
    if len(message) <= MESSAGE_MAX_CHARS:
        return message
    return message[:MESSAGE_MAX_CHARS].rstrip() + "… " + _("(+{n} more)").format(n=len(message) - MESSAGE_MAX_CHARS)


class PromptDialog(BaseDialog):
    """Ask for one line of text (or a secret)."""

    def __init__(
        self,
        parent: Optional[Gtk.Window],
        title: str,
        message: str = "",
        *,
        default: str = "",
        placeholder: str = "",
        accept_label: str = "",
        validator: Optional[Validator] = None,
        secret: bool = False,
        field_label: str = "",
    ) -> None:
        BaseDialog.__init__(self, parent, title, size=DialogSize.SMALL, resizable=False)
        if message:
            label = Gtk.Label(label=_bounded(message))
            label.set_xalign(0.0)
            label.set_line_wrap(True)
            label.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
            label.set_selectable(True)
            label.set_can_focus(False)
            label.set_max_width_chars(52)
            # A long message scrolls instead of growing the dialog past the screen; a short one
            # keeps its natural height (no scroll bar).
            scroller = Gtk.ScrolledWindow()
            scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            scroller.set_propagate_natural_height(True)
            scroller.set_max_content_height(MESSAGE_MAX_HEIGHT)
            scroller.add(label)
            self.message_scroller = scroller
            self.add_body(scroller)

        if secret:
            self._control: Gtk.Widget = SecretEntry(placeholder)
            entry = self._control.entry  # type: ignore[attr-defined]
        else:
            entry = Gtk.Entry()
            entry.set_activates_default(True)
            if placeholder:
                entry.set_placeholder_text(placeholder)
            self._control = entry
        entry.set_text(default or "")
        self._entry = entry

        self._row = FormRow(field_label or title, self._control, validator=self._wrap(validator))
        if field_label:
            grid = Gtk.Grid(column_spacing=Space.M, row_spacing=Space.XS)
            grid.attach(self._row.label, 0, 0, 1, 1)
            grid.attach(self._control, 1, 0, 1, 1)
            grid.attach(self._row.error, 1, 1, 1, 1)
            self.add_body(grid)
        else:
            self.add_body(self._control)
            self.add_body(self._row.error)

        self.add_cancel()
        self.add_primary(accept_label or _("_OK"))
        self.set_initial_focus(entry)
        self.set_response_validator(lambda response: not is_accept(response) or self._row.validate())
        if default:
            entry.connect("map", lambda e: e.select_region(0, -1))

    @staticmethod
    def _wrap(validator: Optional[Validator]) -> Validator:
        def check(value: str) -> Optional[str]:
            if not value.strip():
                return _("A value is required.")
            return validator(value) if validator else None

        return check

    @property
    def value(self) -> str:
        return self._entry.get_text()

    def ask(self, *, strip: bool = True) -> Optional[str]:
        self.show_all()
        try:
            response = int(self.run())
            text = self.value
        finally:
            self.destroy()
        if not is_accept(response):
            return None
        text = text.strip() if strip else text
        return text or None


def prompt_text(
    parent: Optional[Gtk.Window],
    title: str,
    message: str = "",
    default: str = "",
    *,
    placeholder: str = "",
    accept_label: str = "",
    validator: Optional[Validator] = None,
    secret: bool = False,
) -> Optional[str]:
    """Show a :class:`PromptDialog`; return the entered text or ``None``."""

    return PromptDialog(
        parent,
        title,
        message,
        default=default,
        placeholder=placeholder,
        accept_label=accept_label,
        validator=validator,
        secret=secret,
    ).ask(strip=not secret)
