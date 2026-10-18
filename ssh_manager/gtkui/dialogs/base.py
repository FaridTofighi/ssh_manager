"""Dialog framework shared by every dialog in the GTK front-end.

``BaseDialog`` standardises:

* sizes (:class:`DialogSize`, scaled by the density's ``dialog_scale``),
  margins (``Size.DIALOG_PADDING``) and content spacing (``Size.DIALOG_GAP``);
* action ordering – secondary/cancel first, the primary action last;
* ``suggested-action`` / ``destructive-action`` styling of the primary button;
* ``Esc`` = cancel and ``Enter`` = default response;
* initial focus;
* inline validation – a response validator can keep the dialog open;
* :meth:`run_and_destroy` so call sites never forget ``destroy()``.
"""

from __future__ import annotations

import warnings
from enum import IntEnum
from typing import Callable, Optional

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # type: ignore  # noqa: E402

from ..i18n import _  # noqa: E402
from ..theme import density as density_tokens  # noqa: E402
from ..theme.tokens import Size, Space  # noqa: E402


class DialogSize(IntEnum):
    """Standard dialog widths in pixels."""

    SMALL = 420
    MEDIUM = 560
    LARGE = 760


ResponseValidator = Callable[[int], bool]


def dialog_width(size: int) -> int:
    """Default width for ``size`` in the active density (Compact dialogs open smaller)."""

    return int(round(int(size) * density_tokens.active().dialog_scale))


def dialog_height(height: int) -> int:
    """Default height in the active density; ``-1`` (natural height) stays ``-1``."""

    return height if height <= 0 else int(round(height * density_tokens.active().dialog_scale))


class BaseDialog(Gtk.Dialog):
    """``Gtk.Dialog`` with the application's layout and button conventions."""

    def __init__(
        self,
        parent: Optional[Gtk.Window],
        title: str,
        *,
        size: DialogSize = DialogSize.MEDIUM,
        height: int = -1,
        resizable: bool = True,
        modal: bool = True,
    ) -> None:
        Gtk.Dialog.__init__(self, title=title, transient_for=parent, modal=modal)
        self.set_destroy_with_parent(True)
        self.set_default_size(dialog_width(size), dialog_height(height))
        self.set_resizable(resizable)
        self.get_style_context().add_class("sm-dialog")
        if parent is not None:
            self.set_position(Gtk.WindowPosition.CENTER_ON_PARENT)
        else:
            self.set_position(Gtk.WindowPosition.CENTER)

        content = self.get_content_area()
        pad = Size.DIALOG_PADDING
        content.set_spacing(Size.DIALOG_GAP)
        content.set_margin_start(pad)
        content.set_margin_end(pad)
        content.set_margin_top(pad)
        content.set_margin_bottom(Space.S)
        with warnings.catch_warnings():
            # Deprecated accessor, but GTK 3 offers no other way to style the action area.
            warnings.simplefilter("ignore", DeprecationWarning)
            action_area = self.get_action_area()
        action_area.set_margin_start(pad)
        action_area.set_margin_end(pad)
        action_area.set_margin_bottom(pad)
        action_area.set_spacing(Space.S)

        self._validator: Optional[ResponseValidator] = None
        self._initial_focus: Optional[Gtk.Widget] = None
        self.primary_button: Optional[Gtk.Button] = None
        self.connect("response", self._on_response)
        self.connect("map", self._on_map)

    # --------------------------------------------------------------- content
    @property
    def body(self) -> Gtk.Box:
        """The content area (vertical box with standard spacing)."""

        return self.get_content_area()

    def add_body(self, widget: Gtk.Widget, *, expand: bool = False) -> Gtk.Widget:
        self.get_content_area().pack_start(widget, expand, expand, 0)
        return widget

    # --------------------------------------------------------------- buttons
    def add_cancel(self, label: str = "") -> Gtk.Widget:
        return self.add_button(label or _("_Cancel"), Gtk.ResponseType.CANCEL)

    def add_secondary(self, label: str, response: int) -> Gtk.Widget:
        return self.add_button(label, response)

    def add_primary(
        self,
        label: str,
        response: int = Gtk.ResponseType.OK,
        *,
        destructive: bool = False,
        default: bool = True,
    ) -> Gtk.Widget:
        """Add the primary button (always last, i.e. on the trailing edge)."""

        button = self.add_button(label, response)
        button.get_style_context().add_class("destructive-action" if destructive else "suggested-action")
        if default:
            button.set_can_default(True)
            self.set_default_response(response)
        self.primary_button = button
        return button

    def set_initial_focus(self, widget: Gtk.Widget) -> None:
        self._initial_focus = widget

    def set_response_validator(self, validator: Optional[ResponseValidator]) -> None:
        """``validator(response) -> bool``; returning False keeps the dialog open."""

        self._validator = validator

    # ------------------------------------------------------------------ run
    def run_and_destroy(self) -> int:
        """Show the dialog modally, destroy it and return the response."""

        self.show_all()
        try:
            return int(self.run())
        finally:
            self.destroy()

    def run_keep(self) -> int:
        """Show the dialog modally and return the response, keeping the widget alive."""

        self.show_all()
        response = int(self.run())
        self.hide()
        return response

    # ------------------------------------------------------------- internal
    def _on_response(self, dialog: Gtk.Dialog, response: int) -> None:
        if response == Gtk.ResponseType.DELETE_EVENT:
            return  # Esc / window close always cancels
        if response in (Gtk.ResponseType.CANCEL, Gtk.ResponseType.CLOSE):
            return
        if self._validator is not None and not self._validator(response):
            dialog.stop_emission_by_name("response")

    def _on_map(self, *_args) -> None:
        if self._initial_focus is not None:
            self._initial_focus.grab_focus()


def is_accept(response: int) -> bool:
    """True for responses that mean "go ahead"."""

    return response in (Gtk.ResponseType.OK, Gtk.ResponseType.ACCEPT, Gtk.ResponseType.YES, Gtk.ResponseType.APPLY)
