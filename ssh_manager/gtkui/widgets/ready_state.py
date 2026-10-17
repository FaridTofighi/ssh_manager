"""Centered "Ready to connect" empty state for the Terminal tab.

Layout (reference)::

          ┌──────┐
          │  >_  │        72×72 card, radius 18
          └──────┘
      Ready to connect     22/400
   Start a secure SSH session with **api-prod-01**. Your
        session will open here in the workspace.        16/400, ≤ 520px
       [ >_  Connect to host ]   h 51, accent
     ⚷ Using ed25519 authentication                     13/400 faint
"""

from __future__ import annotations

from typing import Callable, Optional

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # type: ignore  # noqa: E402

from ..constants import Icons  # noqa: E402
from ..theme.tokens import Size, Space  # noqa: E402
from .icons import make_icon  # noqa: E402

BODY_WIDTH = 520
TITLE_TOP = 24
BODY_TOP = 14
BUTTON_TOP = 32
CAPTION_TOP = 16


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

        card = Gtk.Box()
        card.get_style_context().add_class("empty-card")
        card.set_size_request(Size.EMPTY_CARD, Size.EMPTY_CARD)
        card.set_halign(Gtk.Align.CENTER)
        self._card_icon = make_icon(Icons.TERMINAL, 32, css_class="empty-icon")
        self._card_icon.set_valign(Gtk.Align.CENTER)
        card.set_center_widget(self._card_icon)
        self.pack_start(card, False, False, 0)

        self.title = Gtk.Label()
        self.title.get_style_context().add_class("empty-title")
        self.title.set_margin_top(TITLE_TOP)
        self.title.set_justify(Gtk.Justification.CENTER)
        self.pack_start(self.title, False, False, 0)

        self.body = Gtk.Label()
        self.body.get_style_context().add_class("empty-description")
        self.body.set_line_wrap(True)
        self.body.set_justify(Gtk.Justification.CENTER)
        self.body.set_max_width_chars(60)
        self.body.set_size_request(-1, -1)
        self.body.set_margin_top(BODY_TOP)
        self.pack_start(self.body, False, False, 0)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.M)
        buttons.set_halign(Gtk.Align.CENTER)
        buttons.set_margin_top(BUTTON_TOP)
        self.primary = Gtk.Button()
        self.primary.get_style_context().add_class("suggested-action")
        self.primary.get_style_context().add_class("hero")
        self.primary.set_can_default(True)
        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.M)
        self._primary_icon = make_icon(Icons.TERMINAL, 18)
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

        self.caption = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.S + 1)
        self.caption.set_halign(Gtk.Align.CENTER)
        self.caption.set_margin_top(CAPTION_TOP)
        self.caption.get_style_context().add_class("empty-caption")
        self.caption.pack_start(make_icon(Icons.KEY, 14, css_class="empty-caption"), False, False, 0)
        self._caption_label = Gtk.Label()
        self._caption_label.get_style_context().add_class("empty-caption")
        self.caption.pack_start(self._caption_label, False, False, 0)
        self.caption.set_no_show_all(True)
        self.pack_start(self.caption, False, False, 0)

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

    @property
    def body_text(self) -> str:
        return self.body.get_text()

