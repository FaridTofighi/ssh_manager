"""Chip widgets for tags and environment badges."""

from __future__ import annotations

from typing import Iterable

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Pango  # type: ignore  # noqa: E402

from ..theme.tokens import Space  # noqa: E402


class Chip(Gtk.Label):
    """A small rounded label using the ``.chip`` class."""

    def __init__(self, text: str, *, accent: bool = False, tooltip: str = "") -> None:
        Gtk.Label.__init__(self, label=text)
        ctx = self.get_style_context()
        ctx.add_class("chip")
        if accent:
            ctx.add_class("accent")
        self.set_ellipsize(Pango.EllipsizeMode.END)
        self.set_max_width_chars(24)
        if tooltip:
            self.set_tooltip_text(tooltip)


class ChipFlow(Gtk.FlowBox):
    """Wrapping row of chips."""

    def __init__(self) -> None:
        Gtk.FlowBox.__init__(self)
        self.set_selection_mode(Gtk.SelectionMode.NONE)
        self.set_column_spacing(Space.XS)
        self.set_row_spacing(Space.XS)
        self.set_homogeneous(False)
        self.set_max_children_per_line(30)
        self.set_valign(Gtk.Align.START)

    def set_chips(self, labels: Iterable[str], *, accent: Iterable[str] = ()) -> None:
        """Replace all chips; labels in ``accent`` get the accent style."""

        accented = set(accent)
        for child in list(self.get_children()):
            self.remove(child)
        for text in labels:
            chip = Chip(text, accent=text in accented)
            chip.set_halign(Gtk.Align.START)
            self.add(chip)
            # FlowBoxChild must not stretch the chip.
            parent = chip.get_parent()
            if parent is not None:
                parent.set_halign(Gtk.Align.START)
                parent.set_can_focus(False)
        self.show_all()

    def chip_texts(self) -> list:
        texts = []
        for child in self.get_children():
            inner = child.get_child() if hasattr(child, "get_child") else child
            if isinstance(inner, Gtk.Label):
                texts.append(inner.get_text())
        return texts
