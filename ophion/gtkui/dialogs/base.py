"""Dialog framework shared by every dialog in the GTK front-end.

``BaseDialog`` standardises:

* sizes (:class:`DialogSize`, scaled by the density's ``dialog_scale``),
  margins (``Size.DIALOG_PADDING``) and content spacing (``Size.DIALOG_GAP``);
* action ordering – secondary/cancel first, the primary action last;
* ``suggested-action`` / ``destructive-action`` styling of the primary button;
* ``Esc`` = cancel and ``Enter`` = default response;
* initial focus;
* inline validation – a response validator can keep the dialog open;
* :meth:`run_and_destroy` so call sites never forget ``destroy()``;
* the screen: never taller than 85 % of the work area (the body scrolls) and never wider than its
  standard width, 85 % of the work area, or a narrower parent window (at least 320 px). Every wrapping
  label should be a ``widgets.forms.wrapped_label``: a wrapping label without ``width_chars`` /
  ``max_width_chars`` asks for its unwrapped width, and a non-resizable dialog would open that wide.
"""

from __future__ import annotations

import logging
import warnings
from enum import IntEnum
from typing import Callable, Optional

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # type: ignore  # noqa: E402

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


MAX_SCREEN_FRACTION = 0.85  # of the monitor's work area
MIN_DIALOG_WIDTH = 320  # a dialog over a narrower parent is never squeezed below this
LOGGER = logging.getLogger(__name__)


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
        self.size_class = size  # the declared DialogSize (the width clamp and the dialog sweep read it)
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

    # ------------------------------------------------------- fit the screen
    def do_show(self) -> None:  # type: ignore[override]
        self.fit_wrapping_labels()
        self._fit_width_to_screen()
        self._fit_to_screen()
        Gtk.Dialog.do_show(self)

    def _work_area(self):
        display = self.get_display()
        parent = self.get_transient_for()
        monitor = None
        if parent is not None and parent.get_window() is not None:
            monitor = display.get_monitor_at_window(parent.get_window())
        monitor = monitor or display.get_primary_monitor() or (display.get_monitor(0) if display.get_n_monitors() else None)
        return monitor.get_workarea() if monitor is not None else None

    def _work_area_height(self) -> int:
        area = self._work_area()
        return area.height if area is not None else 0

    def _work_area_width(self) -> int:
        area = self._work_area()
        return area.width if area is not None else 0

    def width_limit(self) -> int:
        """The widest this dialog may be: its standard width, 85 % of the work area, a narrower parent."""

        limit = dialog_width(getattr(self, "size_class", DialogSize.MEDIUM))
        work = self._work_area_width()
        if work:
            limit = min(limit, int(work * MAX_SCREEN_FRACTION))
        parent = self.get_transient_for()
        if parent is not None and parent.get_mapped():
            parent_width = parent.get_allocated_width()
            if 1 < parent_width < limit:
                limit = parent_width
        return max(MIN_DIALOG_WIDTH, limit)

    def fit_wrapping_labels(self) -> int:
        """Keep every wrapping label of the body within the dialog's text width (in the label's own font).

        A wrapping label without ``max_width_chars`` asks for its unwrapped width, so a long message made
        the dialog as wide as the text; a limit in characters can still be wider than the dialog in a
        wide font. Labels without a limit, or with a wider one, get the dialog's text width (never a
        wider one; ``widgets.forms.wrapped_label`` does the same for the labels that use it). Returns how
        many labels changed.
        """

        from gi.repository import Pango

        limit = self.width_limit()
        budget = limit - 2 * Size.DIALOG_PADDING
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            action_area = self.get_action_area()
        labels = []

        def collect(widget) -> None:
            if isinstance(widget, Gtk.Label) and widget.get_line_wrap():
                labels.append(widget)
            if isinstance(widget, Gtk.Container):
                widget.forall(collect)

        for child in self.get_content_area().get_children():
            if child is not action_area:
                collect(child)
        changed = set()
        for _round in range(3):  # an icon or a second column beside a label: shrink by what is still over
            for label in labels:
                metrics = label.get_pango_context().get_metrics(None, None)
                char = max(metrics.get_approximate_char_width(), metrics.get_approximate_digit_width()) / Pango.SCALE
                fitting = max(8, int(budget / char)) if char > 0 else 0
                current = label.get_max_width_chars()
                if fitting and (current < 0 or current > fitting):  # none, or wider than the dialog's text
                    label.set_max_width_chars(fitting)
                    if label.get_width_chars() > fitting:
                        label.set_width_chars(fitting)
                    changed.add(label)
            overflow = self.get_preferred_width()[1] - limit
            if overflow <= 0 or not labels:
                break
            budget -= overflow
        if changed:
            LOGGER.debug("%s: %d wrapping label(s) fitted to the dialog's width", type(self).__name__, len(changed))
        return len(changed)

    def _fit_width_to_screen(self) -> None:
        """Never wider than :meth:`width_limit`; large screens keep the standard width (no stretching)."""

        limit = self.width_limit()
        width, height = self.get_default_size()
        if width > limit:
            self.set_default_size(limit, height)
        _minimum, natural = self.get_preferred_width()
        if natural <= limit:
            return
        # A wrapping label without width_chars / max_width_chars (or a very large font): the content asks
        # for more than the standard width. A resizable window takes its default width and wraps.
        LOGGER.debug("%s asks for %d px, more than its %d px: clamped", type(self).__name__, natural, limit)
        if not self.get_resizable():
            self.set_resizable(True)
        self.set_default_size(limit, height)

    def _fit_to_screen(self) -> None:
        """Never taller than 85 % of the work area: the body scrolls, the buttons stay on screen."""

        if getattr(self, "_fitted", False):
            return
        limit = int(self._work_area_height() * MAX_SCREEN_FRACTION)
        width = self.get_default_size()[0]
        if width <= 0 or not self.get_resizable():
            width = self.get_preferred_width()[1]
        # At the width the dialog will have: wrapped text is taller there than at its natural width.
        _minimum, natural = self.get_preferred_height_for_width(max(width, self.get_preferred_width()[0]))
        if not limit or natural <= limit:
            return
        self._scroll_body(limit)

    def _check_height_after_map(self) -> bool:
        """Wrapped text measured at the real width can still be taller than estimated: fit it then."""

        if not self.get_mapped():  # closed meanwhile
            return False
        limit = int(self._work_area_height() * MAX_SCREEN_FRACTION)
        if limit and not getattr(self, "_fitted", False) and self.get_allocated_height() > limit:
            self._scroll_body(limit)
            if not self.get_resizable():
                self.set_resizable(True)
            self.resize(self.get_allocated_width(), limit)
        return False

    def _scroll_body(self, limit: int) -> None:
        """Move the body into a scrolled window and open ``limit`` px tall (the buttons stay visible)."""

        self._fitted = True
        content = self.get_content_area()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            action_area = self.get_action_area()
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=content.get_spacing())
        for child in [c for c in content.get_children() if c is not action_area]:
            expand, fill, padding, _pack = content.query_child_packing(child)
            content.remove(child)
            body.pack_start(child, expand, fill, padding)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)
        scroller.add(body)
        scroller.show_all()
        content.pack_start(scroller, True, True, 0)
        content.reorder_child(scroller, 0)
        width, _height = self.get_default_size()
        self.set_default_size(width, limit)

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
        GLib.idle_add(self._check_height_after_map)


def is_accept(response: int) -> bool:
    """True for responses that mean "go ahead"."""

    return response in (Gtk.ResponseType.OK, Gtk.ResponseType.ACCEPT, Gtk.ResponseType.YES, Gtk.ResponseType.APPLY)
