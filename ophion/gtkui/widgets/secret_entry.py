"""Password entry with show/hide toggle, Caps Lock hint and optional generator."""

from __future__ import annotations

import secrets
import string
from typing import Optional

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gtk  # type: ignore  # noqa: E402

from .icons import make_icon  # noqa: E402
from ..i18n import _  # noqa: E402
from ..theme.tokens import Size, Space  # noqa: E402

_GENERATOR_ALPHABET = string.ascii_letters + string.digits + "-_.!@#%^*+="


def generate_password(length: int = 20) -> str:
    """Return a random password from a shell-safe alphabet."""

    return "".join(secrets.choice(_GENERATOR_ALPHABET) for _ in range(max(8, length)))


def password_strength(value: str) -> int:
    """Rough strength score 0–4 (length and character-class variety)."""

    if not value:
        return 0
    classes = sum(
        any(test(ch) for ch in value)
        for test in (str.islower, str.isupper, str.isdigit, lambda ch: not ch.isalnum())
    )
    score = 0
    if len(value) >= 8:
        score += 1
    if len(value) >= 12:
        score += 1
    if classes >= 3:
        score += 1
    if len(value) >= 16 and classes == 4:
        score += 1
    return min(4, score)


STRENGTH_LABELS = (_("Empty"), _("Weak"), _("Fair"), _("Good"), _("Strong"))


class SecretEntry(Gtk.Box):
    """A ``Gtk.Entry`` for secrets plus a reveal toggle and Caps Lock warning."""

    def __init__(
        self,
        placeholder: str = "",
        *,
        generate: bool = False,
        show_strength: bool = False,
        activates_default: bool = True,
    ) -> None:
        Gtk.Box.__init__(self, orientation=Gtk.Orientation.VERTICAL, spacing=Space.XS)
        self._build_entry_row(placeholder, generate, activates_default)
        self._build_hints(show_strength)

        self._keymap = Gdk.Keymap.get_for_display(Gdk.Display.get_default()) if Gdk.Display.get_default() else None
        self._keymap_handler = 0
        if self._keymap is not None:
            self._keymap_handler = self._keymap.connect("state-changed", lambda *_: self._update_caps())
        self.entry.connect("focus-in-event", lambda *_: self._update_caps() or False)
        self.entry.connect("focus-out-event", lambda *_: self._caps.hide() or False)
        self.connect("destroy", self._on_destroy)

    def _build_entry_row(self, placeholder: str, generate: bool, activates_default: bool) -> None:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        row.get_style_context().add_class("linked")

        self.entry = Gtk.Entry()
        self.entry.set_visibility(False)
        self.entry.set_invisible_char("•")
        self.entry.set_input_purpose(Gtk.InputPurpose.PASSWORD)
        self.entry.set_activates_default(activates_default)
        self.entry.set_hexpand(True)
        if placeholder:
            self.entry.set_placeholder_text(placeholder)
        row.pack_start(self.entry, True, True, 0)

        self._reveal = Gtk.ToggleButton()
        self._reveal.set_image(make_icon("sshm-eye", Size.ICON))
        self._reveal.set_tooltip_text(_("Show password"))
        self._reveal.get_accessible().set_name(_("Show password"))
        self._reveal.set_can_focus(True)
        self._reveal.connect("toggled", self._on_reveal_toggled)
        row.pack_start(self._reveal, False, False, 0)

        if generate:
            gen = Gtk.Button()
            gen.set_image(make_icon("sshm-refresh", Size.ICON))
            gen.set_tooltip_text(_("Generate a random password"))
            gen.get_accessible().set_name(_("Generate password"))
            gen.connect("clicked", lambda _b: self._generate())
            row.pack_start(gen, False, False, 0)
        self.pack_start(row, False, False, 0)

    def _build_hints(self, show_strength: bool) -> None:
        self._caps = Gtk.Label(label=_("Caps Lock is on"))
        self._caps.set_xalign(0.0)
        self._caps.get_style_context().add_class("form-help")
        self._caps.get_style_context().add_class("level-warning")
        self._caps.set_no_show_all(True)
        self.pack_start(self._caps, False, False, 0)

        self._strength: Optional[Gtk.LevelBar] = None
        self._strength_label: Optional[Gtk.Label] = None
        if show_strength:
            strength_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.S)
            self._strength = Gtk.LevelBar.new_for_interval(0, 4)
            self._strength.set_mode(Gtk.LevelBarMode.DISCRETE)
            self._strength.set_hexpand(True)
            self._strength_label = Gtk.Label()
            self._strength_label.get_style_context().add_class("form-help")
            strength_row.pack_start(self._strength, True, True, 0)
            strength_row.pack_start(self._strength_label, False, False, 0)
            self.pack_start(strength_row, False, False, 0)
            self.entry.connect("changed", lambda *_: self._update_strength())
            self._update_strength()

    # -------------------------------------------------------------- api
    def get_text(self) -> str:
        return self.entry.get_text()

    def set_text(self, text: str) -> None:
        self.entry.set_text(text)

    def grab_focus(self) -> None:  # type: ignore[override]
        self.entry.grab_focus()

    def connect_activate(self, callback) -> None:
        self.entry.connect("activate", lambda *_: callback())

    def set_invalid(self, invalid: bool) -> None:
        ctx = self.entry.get_style_context()
        (ctx.add_class if invalid else ctx.remove_class)("invalid")

    # --------------------------------------------------------- internal
    def _on_reveal_toggled(self, button: Gtk.ToggleButton) -> None:
        shown = button.get_active()
        self.entry.set_visibility(shown)
        icon = "view-conceal-symbolic" if shown else "sshm-eye"
        tip = _("Hide password") if shown else _("Show password")
        button.set_image(make_icon(icon, Size.ICON))
        button.set_tooltip_text(tip)
        button.get_accessible().set_name(tip)

    def _update_caps(self) -> None:
        on = bool(self._keymap and self._keymap.get_caps_lock_state() and self.entry.has_focus())
        self._caps.set_visible(on)

    def _update_strength(self) -> None:
        if self._strength is None or self._strength_label is None:
            return
        score = password_strength(self.entry.get_text())
        self._strength.set_value(score)
        self._strength_label.set_text(STRENGTH_LABELS[score])

    def _generate(self) -> None:
        self.entry.set_text(generate_password())
        self._reveal.set_active(True)

    def _on_destroy(self, *_args) -> None:
        if self._keymap is not None and self._keymap_handler:
            try:
                self._keymap.disconnect(self._keymap_handler)
            except Exception:
                pass
            self._keymap_handler = 0
