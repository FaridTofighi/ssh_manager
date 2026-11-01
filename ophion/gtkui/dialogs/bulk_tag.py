"""Tag and/or color several hosts at once."""

from __future__ import annotations

from typing import List, Optional, Tuple

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gtk  # type: ignore  # noqa: E402

from ..i18n import _, ngettext  # noqa: E402
from ..widgets.forms import FormSection, entry  # noqa: E402
from .base import BaseDialog, DialogSize, is_accept  # noqa: E402


def _hex(rgba: Gdk.RGBA) -> str:
    return "#{:02x}{:02x}{:02x}".format(*(int(c * 255 + 0.5) for c in (rgba.red, rgba.green, rgba.blue)))


class BulkTagDialog(BaseDialog):
    """Add tags to and/or set a tree color for the selected hosts."""

    def __init__(self, parent: Gtk.Window, count: int) -> None:
        BaseDialog.__init__(self, parent, _("Tag or Color Hosts"), size=DialogSize.SMALL, resizable=False)
        intro = Gtk.Label(label=ngettext("Changes apply to {n} selected host.", "Changes apply to {n} selected hosts.", count).format(n=count))
        intro.set_xalign(0.0)
        intro.get_style_context().add_class("section-description")
        self.add_body(intro)
        section = FormSection()
        self.tags_entry = entry(placeholder=_("prod, web"))
        section.add(_("Add _tags"), self.tags_entry, help_text=_("Comma-separated; existing tags are kept."))
        self.set_color_check = Gtk.CheckButton.new_with_mnemonic(_("Set tree _color"))
        section.add_widget(self.set_color_check)
        self.color_button = Gtk.ColorButton()
        self.color_button.set_halign(Gtk.Align.START)
        self.color_button.set_sensitive(False)
        self.set_color_check.connect("toggled", lambda btn: self.color_button.set_sensitive(btn.get_active()))
        section.add(_("Co_lor"), self.color_button)
        self.add_body(section)
        self.add_cancel()
        self.add_primary(_("_Apply"))
        self.set_initial_focus(self.tags_entry)

    def ask(self) -> Optional[Tuple[List[str], Optional[str]]]:
        """``(tags_to_add, color_or_None)``, or ``None`` when cancelled."""

        self.show_all()
        try:
            if not is_accept(self.run()):
                return None
            tags = [part.strip() for part in self.tags_entry.get_text().split(",") if part.strip()]
            color = _hex(self.color_button.get_rgba()) if self.set_color_check.get_active() else None
            return tags, color
        finally:
            self.destroy()
