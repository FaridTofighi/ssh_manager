"""Centered "Ready to connect" empty state for the Terminal tab.

Layout (reference)::

          ┌──────┐
          │  >_  │        72×72 card, radius 18 (Compact 48)
          └──────┘
      Ready to connect     22/400 (Compact 17)
   Start a secure SSH session with **api-prod-01**. Your
        session will open here in the workspace.        16/400, ≤ 520px
       [ >_  Connect to host ]   h 51, accent (Compact 34)
     ⚷ Using ed25519 authentication                     13/400 faint

Sizes come from the active density preset (:meth:`ReadyState.apply_density`).
"""

from __future__ import annotations

from typing import Callable, Optional

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # type: ignore  # noqa: E402

from ..constants import Icons  # noqa: E402
from ..i18n import _  # noqa: E402
from ..theme.tokens import Layout, Size, Space  # noqa: E402
from ..controllers.shortcuts import set_accel_tooltip  # noqa: E402
from .icons import make_icon  # noqa: E402


class ReadyState(Gtk.Box):
    """Icon card, title, body, primary (+ optional secondary) button and caption."""

    def __init__(self) -> None:
        Gtk.Box.__init__(self, orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.get_style_context().add_class("empty-state")
        self.get_style_context().add_class("ready-state")
        self.set_halign(Gtk.Align.CENTER)
        self.set_valign(Gtk.Align.CENTER)
        self._primary_cb: Optional[Callable[[], None]] = None
        self._secondary_cb: Optional[Callable[[], None]] = None

        self._card = card = Gtk.Box()
        card.get_style_context().add_class("empty-card")
        card.set_halign(Gtk.Align.CENTER)
        self._card_icon = make_icon(Icons.TERMINAL, Size.EMPTY_CARD_ICON, css_class="empty-icon")
        self._card_icon.set_valign(Gtk.Align.CENTER)
        card.set_center_widget(self._card_icon)
        self.pack_start(card, False, False, 0)

        self.title = Gtk.Label()
        self.title.get_style_context().add_class("empty-title")
        self.title.set_justify(Gtk.Justification.CENTER)
        self.pack_start(self.title, False, False, 0)

        self.body = Gtk.Label()
        self.body.get_style_context().add_class("empty-description")
        self.body.set_line_wrap(True)
        self.body.set_justify(Gtk.Justification.CENTER)
        self.body.set_max_width_chars(1)
        self.pack_start(self.body, False, False, 0)

        self._buttons = buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        buttons.set_halign(Gtk.Align.CENTER)
        self.primary = Gtk.Button()
        self.primary.get_style_context().add_class("suggested-action")
        self.primary.get_style_context().add_class("hero")
        self.primary.set_can_default(True)
        self._primary_content = content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self._primary_icon = make_icon(Icons.TERMINAL, Size.ICON_BUTTON_GLYPH - 2)
        content.pack_start(self._primary_icon, False, False, 0)
        self._primary_label = Gtk.Label()
        self._primary_label.set_use_underline(True)
        content.pack_start(self._primary_label, False, False, 0)
        self.primary.add(content)
        self.primary.connect("clicked", lambda *_: self._primary_cb and self._primary_cb())
        buttons.pack_start(self.primary, False, False, 0)
        self.secondary = Gtk.Button()
        self.secondary.get_style_context().add_class("outlined")
        self.secondary.get_style_context().add_class("hero")
        self.secondary.set_no_show_all(True)
        self.secondary.connect("clicked", lambda *_: self._secondary_cb and self._secondary_cb())
        buttons.pack_start(self.secondary, False, False, 0)
        self.pack_start(buttons, False, False, 0)

        self.caption = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self.caption.set_halign(Gtk.Align.CENTER)
        self.caption.get_style_context().add_class("empty-caption")
        self.caption.pack_start(make_icon(Icons.KEY, Size.ICON_SMALL, css_class="empty-caption"), False, False, 0)
        self._caption_label = Gtk.Label()
        self._caption_label.get_style_context().add_class("empty-caption")
        self.caption.pack_start(self._caption_label, False, False, 0)
        self.caption.set_no_show_all(True)
        self.pack_start(self.caption, False, False, 0)

        self.on_details: Callable[[], None] = lambda: None
        self.details_link = Gtk.Button.new_with_mnemonic(_("Host _details"))
        self.details_link.get_style_context().add_class("flat")
        self.details_link.get_style_context().add_class("link-button")
        self.details_link.set_halign(Gtk.Align.CENTER)
        set_accel_tooltip(self.details_link, _("Show the host's details and activity"), "toggle-details")
        self.details_link.connect("clicked", lambda *_: self.on_details())
        self.details_link.set_no_show_all(True)
        self.pack_start(self.details_link, False, False, 0)
        self.apply_density()

    def apply_density(self) -> None:
        ready = Layout.Ready
        self._card.set_size_request(Size.EMPTY_CARD, Size.EMPTY_CARD)
        self._card_icon.set_pixel_size(Size.EMPTY_CARD_ICON)
        self.title.set_margin_top(ready.TITLE_TOP)
        # Wrap at exactly BODY_WIDTH: the size request sets the width, and a
        # one-character natural width keeps the label from growing past it.
        self.body.set_size_request(ready.BODY_WIDTH, -1)
        self.body.set_margin_top(ready.BODY_TOP)
        self._buttons.set_spacing(Space.M)
        self._buttons.set_margin_top(ready.BUTTON_TOP)
        self._primary_content.set_spacing(Space.M)
        self._primary_icon.set_pixel_size(Size.ICON_BUTTON_GLYPH - 2)
        self.caption.set_spacing(Space.S + 1)
        self.caption.set_margin_top(ready.CAPTION_TOP)
        self.details_link.set_margin_top(Space.S)

    def set_content(
        self,
        title: str,
        body_markup: str,
        primary_label: str,
        on_primary: Callable[[], None],
        *,
        primary_icon: str = Icons.TERMINAL,
        secondary_label: str = "",
        on_secondary: Optional[Callable[[], None]] = None,
        caption: str = "",
        details_link: bool = False,
    ) -> None:
        self.title.set_text(title)
        self.body.set_markup(body_markup)
        self._primary_label.set_text_with_mnemonic(primary_label)
        self._primary_icon.set_icon(primary_icon)
        self._primary_cb = on_primary
        self._secondary_cb = on_secondary
        self.secondary.set_label(secondary_label)
        self.secondary.set_use_underline(True)
        self.secondary.set_visible(bool(secondary_label))
        self._caption_label.set_text(caption)
        # no_show_all: a parent's show_all() skips the caption, so show its parts here.
        for child in self.caption.get_children():
            child.show()
        self.caption.set_visible(bool(caption))
        self.details_link.set_visible(details_link)

    @property
    def body_text(self) -> str:
        return self.body.get_text()

