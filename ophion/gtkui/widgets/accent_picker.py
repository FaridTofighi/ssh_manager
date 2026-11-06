"""Preferences → Appearance → Accent colour: round swatches, a custom colour, a live preview.

One ``Gtk.RadioButton`` group (arrow keys move between swatches, Space / Enter selects; ATK sees the
checked one) drawn as round swatches with a check mark. "Custom…" opens the standard colour chooser.
``on_change(value)`` runs on every choice (a preset id or ``#RRGGBB``); the preview below uses the app's
own CSS classes, so it shows exactly what the stylesheet does with the new palette.
"""

from __future__ import annotations

import math
from typing import Callable, Dict, Optional

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk  # type: ignore  # noqa: E402

from ..i18n import _  # noqa: E402
from ..theme import accent as A  # noqa: E402
from ..theme.tokens import Space  # noqa: E402
from .icon_button import set_accessible_name  # noqa: E402

CUSTOM = "custom"
SWATCH = 26
SWATCHES_PER_LINE = 8  # swatches wrap onto a second line: one long row widened Preferences


def _cell(index: int) -> tuple:
    """``(column, line)`` of the ``index``-th swatch, in reading order (= the radio group's focus order)."""

    return index % SWATCHES_PER_LINE, index // SWATCHES_PER_LINE


def _rgb(color: str):
    rgba = Gdk.RGBA()
    rgba.parse(color)
    return rgba.red, rgba.green, rgba.blue


class _Swatch(Gtk.RadioButton):
    """A round colour swatch; the selected one has a check mark (custom: a ring with a plus when unset)."""

    def __init__(self, group: Optional[Gtk.RadioButton], key: str, color: Optional[str], name: str) -> None:
        Gtk.RadioButton.__init__(self, group=group)
        self.key, self.color = key, color
        self.set_mode(False)  # a button, not a radio indicator
        self.set_relief(Gtk.ReliefStyle.NONE)
        self.get_style_context().add_class("accent-swatch")
        area = Gtk.DrawingArea()
        area.set_size_request(SWATCH, SWATCH)
        area.connect("draw", self._draw)
        self.add(area)
        self.set_tooltip_text(name)
        set_accessible_name(self, name)

    def _draw(self, area: Gtk.DrawingArea, cr) -> bool:
        size = min(area.get_allocated_width(), area.get_allocated_height())
        cx = area.get_allocated_width() / 2
        cy = area.get_allocated_height() / 2
        radius = size / 2 - 1
        ink = area.get_style_context().get_color(area.get_style_context().get_state())
        if self.color:
            cr.set_source_rgb(*_rgb(self.color))
            cr.arc(cx, cy, radius, 0, 2 * math.pi)
            cr.fill()
            cr.set_source_rgba(ink.red, ink.green, ink.blue, 0.25)  # a faint outline: pale colours on pale dialogs
            cr.set_line_width(1.0)
            cr.arc(cx, cy, radius - 0.5, 0, 2 * math.pi)
            cr.stroke()
        else:  # custom, not chosen yet: an outlined circle with a plus
            cr.set_source_rgba(ink.red, ink.green, ink.blue, 0.7)
            cr.set_line_width(1.5)
            cr.arc(cx, cy, radius - 1, 0, 2 * math.pi)
            cr.stroke()
            cr.move_to(cx - 5, cy)
            cr.line_to(cx + 5, cy)
            cr.move_to(cx, cy - 5)
            cr.line_to(cx, cy + 5)
            cr.stroke()
        if self.get_active():  # the check mark, in the colour that reads on the swatch
            r, g, b = (1.0, 1.0, 1.0)
            if self.color and sum(c * w for c, w in zip(_rgb(self.color), (0.2126, 0.7152, 0.0722))) > 0.55:
                r, g, b = (0.05, 0.07, 0.06)
            elif not self.color:
                r, g, b = ink.red, ink.green, ink.blue
            cr.set_source_rgb(r, g, b)
            cr.set_line_width(2.2)
            cr.move_to(cx - 5, cy)
            cr.line_to(cx - 1.5, cy + 3.5)
            cr.line_to(cx + 5.5, cy - 4)
            cr.stroke()
        return False


class AccentPicker(Gtk.Box):
    """``value`` is a preset id or ``#RRGGBB``; ``on_change(value)`` runs on every choice."""

    def __init__(self, value: str = A.DEFAULT, on_change: Callable[[str], None] = lambda _v: None,
                 dark: bool = False) -> None:
        Gtk.Box.__init__(self, orientation=Gtk.Orientation.VERTICAL, spacing=Space.S)
        self._on_change = on_change
        self._dark = dark
        self._quiet = False
        self.value = A.normalize_accent(value)
        self._custom_color = self.value if self.value.startswith("#") else ""

        row = Gtk.Grid(column_spacing=Space.XS, row_spacing=Space.XS)
        self.swatches: Dict[str, _Swatch] = {}
        group: Optional[Gtk.RadioButton] = None
        for key, name, _color in A.PRESETS:
            swatch = _Swatch(group, key, A.display_color(key), _("{name} accent").format(name=_(name)))
            group = group or swatch
            swatch.connect("toggled", self._on_toggled)
            row.attach(swatch, *_cell(len(self.swatches)), 1, 1)
            self.swatches[key] = swatch
        self.custom = _Swatch(group, CUSTOM, self._custom_color or None, _("Custom accent colour"))
        self.custom.set_tooltip_text(_("Custom…"))
        self.custom.connect("toggled", self._on_toggled)
        # The chooser opens on a click, Enter or Space, not when the arrow keys move onto the swatch.
        self.custom.connect("button-release-event",
                            lambda _w, event: event.button == 1 and GLib.idle_add(self.pick_custom) and False)
        self.custom.connect("key-press-event", lambda _w, event: event.keyval in (
            Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_space) and GLib.idle_add(self.pick_custom) and False)
        row.attach(self.custom, *_cell(len(self.swatches)), 1, 1)
        set_accessible_name(row, _("Accent colour"))
        self.pack_start(row, False, False, 0)

        self.hint = Gtk.Label(xalign=0.0)
        self.hint.set_line_wrap(True)
        self.hint.get_style_context().add_class("form-help")
        self.hint.set_no_show_all(True)
        self.pack_start(self.hint, False, False, 0)

        self.pack_start(self._build_preview(), False, False, 0)
        self.reset_button = Gtk.Button.new_with_mnemonic(_("Reset to _default"))
        self.reset_button.set_halign(Gtk.Align.START)
        self.reset_button.connect("clicked", lambda *_a: self.set_value(A.DEFAULT, notify=True))
        self.pack_start(self.reset_button, False, False, 0)
        self.set_value(self.value)

    def _build_preview(self) -> Gtk.Widget:
        """A primary button, a selected row and a focus ring, styled by the app's stylesheet."""

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.S)
        box.get_style_context().add_class("accent-preview")
        set_accessible_name(box, _("Preview"))
        button = Gtk.Button(label=_("Connect"))
        button.get_style_context().add_class("primary")
        button.set_can_focus(False)
        button.set_valign(Gtk.Align.CENTER)
        selected = Gtk.Label(label=_("Selected host"))
        selected.get_style_context().add_class("accent-preview-selected")
        selected.set_valign(Gtk.Align.CENTER)
        focus = Gtk.Label(label=_("Focus"))
        focus.get_style_context().add_class("accent-preview-focus")
        focus.set_valign(Gtk.Align.CENTER)
        for widget in (button, selected, focus):
            box.pack_start(widget, False, False, 0)
        self.preview_button = button
        return box

    # ------------------------------------------------------------- choices
    def choose_color(self) -> Optional[str]:
        """The standard colour chooser (no alpha, editor shown); ``#RRGGBB`` or ``None`` (cancelled)."""

        top = self.get_toplevel()
        dialog = Gtk.ColorChooserDialog(title=_("Custom Accent Colour"),
                                        transient_for=top if isinstance(top, Gtk.Window) else None, modal=True)
        dialog.set_use_alpha(False)
        dialog.set_property("show-editor", True)
        rgba = Gdk.RGBA()
        if rgba.parse(self._custom_color or A.display_color(self.value)):
            dialog.set_rgba(rgba)
        try:
            if dialog.run() != Gtk.ResponseType.OK:
                return None
            chosen = dialog.get_rgba()
            return "#{:02X}{:02X}{:02X}".format(*(int(round(c * 255)) for c in (chosen.red, chosen.green, chosen.blue)))
        finally:
            dialog.destroy()

    def pick_custom(self) -> bool:
        """Ask for a custom colour; cancelled or invalid keeps the previous choice."""

        color = A.normalize_color(self.choose_color() or "")
        if color is None:
            self.set_value(self.value)
        else:
            self._custom_color = color
            self.set_value(color, notify=True)
        return GLib.SOURCE_REMOVE

    def _on_toggled(self, swatch: _Swatch) -> None:
        swatch.queue_draw()
        if self._quiet or not swatch.get_active():
            return
        if swatch is self.custom:
            if self._custom_color:  # the last custom colour again (arrow keys); else the chooser decides
                self.set_value(self._custom_color, notify=True)
            return
        self.set_value(swatch.key, notify=True)

    def set_value(self, value: object, *, notify: bool = False) -> None:
        self.value = A.normalize_accent(value)
        self._quiet = True
        try:
            if self.value.startswith("#"):
                self._custom_color = self.value
                self.custom.color = self.value
                self.custom.set_active(True)
            else:
                self.swatches[self.value].set_active(True)
        finally:
            self._quiet = False
        for swatch in list(self.swatches.values()) + [self.custom]:
            swatch.queue_draw()
        self._update_hint()
        if notify:
            self._on_change(self.value)

    def set_dark(self, dark: bool) -> None:
        self._dark = dark
        self._update_hint()

    def _update_hint(self) -> None:
        lines = []
        color = A.base_color(self.value)
        if color is not None and A.derive_accent(color, self._dark).adjusted:
            lines.append(_("Adjusted for readability."))
        if A.near_broadcast(self.value):
            lines.append(_("This colour is close to the Broadcast indicator; broadcasting sessions may be harder "
                           "to tell apart."))
        self.hint.set_text(" ".join(lines))
        self.hint.set_visible(bool(lines))


AUTOMATIC = ""


def icon_presets():
    """(English name, colour) of the default icon colours: the accent presets' colours plus a few lighter ones."""

    from ..theme.tokens import ICON_COLOR_EXTRA_PRESETS

    extra = dict(ICON_COLOR_EXTRA_PRESETS)
    presets = [(name, A.display_color(key)) for key, name, _color in A.PRESETS if name not in extra]
    return presets + list(ICON_COLOR_EXTRA_PRESETS)


class IconColorPicker(Gtk.Box):
    """Default colour of group or host icons: "Automatic" (the theme's), presets, "Custom…" and a preview.

    ``value`` is ``""`` (automatic) or ``#RRGGBB``; ``on_change(value)`` runs on every choice. The preview
    and the hint show the colour as the tree draws it on this theme (lightness adjusted to stay readable).
    """

    def __init__(self, kind: str, value: str = AUTOMATIC, on_change: Callable[[str], None] = lambda _v: None) -> None:
        Gtk.Box.__init__(self, orientation=Gtk.Orientation.VERTICAL, spacing=Space.S)
        from ..constants import Icons
        from ..theme.loader import current_palette
        from .icons import make_icon

        self.kind = kind  # "group" | "host"
        self._on_change = on_change
        self._quiet = False
        self.value = A.normalize_color(value or "") or AUTOMATIC
        self._custom_color = self.value if self.value and self.value not in dict(
            (color, name) for name, color in icon_presets()) else ""
        title = _("Default group colour") if kind == "group" else _("Default host colour")

        row = Gtk.Grid(column_spacing=Space.XS, row_spacing=Space.XS)
        self.swatches: Dict[str, _Swatch] = {}
        self.automatic = _Swatch(None, AUTOMATIC, current_palette().icon_default,
                                 _("{title}: Automatic").format(title=title))
        self.automatic.set_tooltip_text(_("Automatic (follows the theme)"))
        self.automatic.connect("toggled", self._on_toggled)
        row.attach(self.automatic, 0, 0, 1, 1)
        for name, color in icon_presets():
            swatch = _Swatch(self.automatic, color, color, _("{title}: {name}").format(title=title, name=_(name)))
            swatch.set_tooltip_text(_(name))
            swatch.connect("toggled", self._on_toggled)
            self.swatches[color] = swatch
            row.attach(swatch, *_cell(len(self.swatches)), 1, 1)
        self.custom = _Swatch(self.automatic, CUSTOM, self._custom_color or None,
                              _("{title}: Custom colour").format(title=title))
        self.custom.set_tooltip_text(_("Custom…"))
        self.custom.connect("toggled", self._on_toggled)
        self.custom.connect("button-release-event",
                            lambda _w, event: event.button == 1 and GLib.idle_add(self.pick_custom) and False)
        self.custom.connect("key-press-event", lambda _w, event: event.keyval in (
            Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_space) and GLib.idle_add(self.pick_custom) and False)
        row.attach(self.custom, *_cell(len(self.swatches) + 1), 1, 1)
        set_accessible_name(row, title)
        self.pack_start(row, False, False, 0)

        preview = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.S)
        preview.get_style_context().add_class("icon-color-preview")
        self.preview_icon = make_icon(Icons.FOLDER if kind == "group" else Icons.SERVER, 16)
        preview.pack_start(self.preview_icon, False, False, 0)
        preview.pack_start(Gtk.Label(label=_("Production") if kind == "group" else _("web-01")), False, False, 0)
        set_accessible_name(preview, _("Preview"))
        self.pack_start(preview, False, False, 0)

        self.hint = Gtk.Label(xalign=0.0)
        self.hint.set_line_wrap(True)
        self.hint.get_style_context().add_class("form-help")
        self.hint.set_no_show_all(True)
        self.pack_start(self.hint, False, False, 0)
        self.reset_button = Gtk.Button.new_with_mnemonic(_("Reset to automatic"))
        self.reset_button.set_halign(Gtk.Align.START)
        self.reset_button.connect("clicked", lambda *_a: self.set_value(AUTOMATIC, notify=True))
        self.pack_start(self.reset_button, False, False, 0)
        self.set_value(self.value)

    choose_color = AccentPicker.choose_color

    def pick_custom(self) -> bool:
        color = A.normalize_color(self.choose_color() or "")
        if color is None:
            self.set_value(self.value)
        else:
            self._custom_color = color
            self.set_value(color, notify=True)
        return GLib.SOURCE_REMOVE

    def _on_toggled(self, swatch: _Swatch) -> None:
        swatch.queue_draw()
        if self._quiet or not swatch.get_active():
            return
        if swatch is self.custom:
            if self._custom_color:
                self.set_value(self._custom_color, notify=True)
            return
        self.set_value(swatch.key, notify=True)

    def set_value(self, value: str, *, notify: bool = False) -> None:
        self.value = A.normalize_color(value or "") or AUTOMATIC
        self._quiet = True
        try:
            if not self.value:
                self.automatic.set_active(True)
            elif self.value in self.swatches:
                self.swatches[self.value].set_active(True)
            else:
                self._custom_color = self.value
                self.custom.color = self.value
                self.custom.set_active(True)
        finally:
            self._quiet = False
        for swatch in [self.automatic, *self.swatches.values(), self.custom]:
            swatch.queue_draw()
        self.refresh()
        if notify:
            self._on_change(self.value)

    def drawn(self):
        """``(colour as the tree draws it on this theme, adjusted)``; automatic: the theme's icon colour."""

        from ..theme.loader import current_palette

        palette = current_palette()
        if not self.value:
            return palette.icon_default, False
        return A.readable_color(self.value, (palette.bg_surface, palette.bg_selected))

    def refresh(self) -> None:
        """Preview and hint for the current theme (also after a theme switch)."""

        from ..theme.loader import current_palette, get_theme_manager

        self.automatic.color = current_palette().icon_default
        self.automatic.queue_draw()
        color, adjusted = self.drawn()
        if hasattr(self.preview_icon, "set_color"):
            self.preview_icon.set_color(color)
        theme = _("dark") if get_theme_manager().is_dark else _("light")
        self.hint.set_text(_("Adjusted for readability in the {theme} theme.").format(theme=theme) if adjusted else "")
        self.hint.set_visible(adjusted)


__all__ = ["AccentPicker", "IconColorPicker", "CUSTOM", "AUTOMATIC", "icon_presets"]
