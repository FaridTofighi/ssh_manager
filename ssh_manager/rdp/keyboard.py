"""Keyboard handling of an embedded remote desktop (pure Python state machine, no GTK).

While the remote screen has the focus and grabbing is on, every key — Alt+Tab,
the Windows key, Ctrl+Alt+End — goes to the remote computer. A **release
shortcut** always gives the keyboard back to this app, and the state is
visible in the toolbar. When the focus leaves (another window, another tab)
the grab ends and every pressed modifier is released, so no key is left
"stuck down" on the remote side.

Engines differ (``docs/rdp.md``): gtk-frdp leaves the grab to the app (this
machine drives it), FreeRDP inside the tab grabs the keyboard itself and
releases it with **Right Ctrl** (its own shortcut, not configurable), and an
external window is not ours to control.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import FrozenSet, List, Optional, Tuple

MODIFIERS = frozenset({"Control_L", "Control_R", "Alt_L", "Alt_R", "Shift_L", "Shift_R", "Super_L", "Super_R",
                       "Meta_L", "Meta_R", "ISO_Level3_Shift"})
DEFAULT_RELEASE = "<Control><Alt>Control_R"
FREERDP_RELEASE = "Control_R"  # xfreerdp's own release key ("<Right CTRL> releases keyboard and mouse grab")
# App shortcuts that keep working while the keyboard is NOT grabbed (they are never sent to the remote then).
RESERVED_WHEN_RELEASED = ("<Primary>Tab", "<Primary><Shift>Tab", "<Primary><Shift>t", "<Control><Alt>Return",
                          "<Primary>w")


class GrabState(str, Enum):
    RELEASED = "released"
    GRABBED = "grabbed"


class Effect(str, Enum):
    GRAB = "grab"  # take the keyboard (Gdk seat grab)
    UNGRAB = "ungrab"
    RELEASE_MODIFIERS = "release_modifiers"  # send key-up for every modifier still down
    FORWARD = "forward"  # this key goes to the remote
    HANDLE_LOCALLY = "handle_locally"  # this key is for the app


@dataclass
class KeyboardGrab:
    """``enabled`` is the user's choice (host setting / toolbar toggle); the grab is active only with focus."""

    enabled: bool = True
    release: Tuple[FrozenSet[str], str] = field(default_factory=lambda: parse_accelerator(DEFAULT_RELEASE))
    state: GrabState = GrabState.RELEASED
    focused: bool = False
    pressed: List[str] = field(default_factory=list)  # modifiers currently down (sent to the remote)

    def focus_in(self) -> List[Effect]:
        self.focused = True
        if self.enabled and self.state is GrabState.RELEASED:
            self.state = GrabState.GRABBED
            return [Effect.GRAB]
        return []

    def focus_out(self) -> List[Effect]:
        """Focus left the remote screen (other window, other tab): ungrab and release modifiers."""

        self.focused = False
        effects: List[Effect] = []
        if self.pressed:
            effects.append(Effect.RELEASE_MODIFIERS)
            self.pressed = []
        if self.state is GrabState.GRABBED:
            self.state = GrabState.RELEASED
            effects.append(Effect.UNGRAB)
        return effects

    tab_switch = focus_out

    def toggle(self) -> List[Effect]:
        """The toolbar toggle: grabbing on or off for this session."""

        self.enabled = not self.enabled
        if not self.enabled:
            return self.focus_out() if self.state is GrabState.GRABBED else []
        return self.focus_in() if self.focused else []

    def key(self, name: str, modifiers: FrozenSet[str] = frozenset(), *, pressed: bool = True) -> List[Effect]:
        """A key event while the remote screen has the focus."""

        mods, key = self.release
        if pressed and key == name and mods <= modifiers:
            effects = [Effect.HANDLE_LOCALLY]
            if self.pressed:
                effects.append(Effect.RELEASE_MODIFIERS)
                self.pressed = []
            if self.state is GrabState.GRABBED:
                self.state = GrabState.RELEASED
                effects.append(Effect.UNGRAB)
            return effects
        if self.state is not GrabState.GRABBED and is_reserved(name, modifiers):
            return [Effect.HANDLE_LOCALLY]
        if name in MODIFIERS:
            if pressed and name not in self.pressed:
                self.pressed.append(name)
            elif not pressed and name in self.pressed:
                self.pressed.remove(name)
        return [Effect.FORWARD]

    @property
    def grabbed(self) -> bool:
        return self.state is GrabState.GRABBED


_MOD_NAMES = {"control": "Control", "primary": "Control", "ctrl": "Control", "alt": "Alt", "shift": "Shift",
              "super": "Super", "meta": "Meta", "mod1": "Alt", "mod4": "Super"}


def parse_accelerator(text: str) -> Tuple[FrozenSet[str], str]:
    """``"<Control><Alt>Control_R"`` → ``({"Control", "Alt"}, "Control_R")`` (GTK accelerator syntax)."""

    mods = set()
    rest = (text or "").strip()
    while rest.startswith("<") and ">" in rest:
        name, rest = rest[1:].split(">", 1)
        mods.add(_MOD_NAMES.get(name.strip().lower(), name.strip().capitalize()))
    return frozenset(mods), rest.strip()


def accelerator_label(text: str) -> str:
    """``"<Control><Alt>Control_R"`` → ``"Ctrl+Alt+Right Ctrl"`` (for tooltips)."""

    mods, key = parse_accelerator(text)
    names = {"Control": "Ctrl", "Alt": "Alt", "Shift": "Shift", "Super": "Super", "Meta": "Meta"}
    keys = {"Control_R": "Right Ctrl", "Control_L": "Left Ctrl", "Alt_R": "Right Alt", "Return": "Enter"}
    order = ["Control", "Alt", "Shift", "Super", "Meta"]
    parts = [names.get(m, m) for m in sorted(mods, key=lambda m: order.index(m) if m in order else 9)]
    return "+".join(parts + [keys.get(key, key)])


def is_reserved(name: str, modifiers: FrozenSet[str]) -> bool:
    for accel in RESERVED_WHEN_RELEASED:
        mods, key = parse_accelerator(accel)
        if key.lower() == name.lower() and mods == frozenset(m for m in modifiers if m != "Shift" or "Shift" in mods):
            return True
    return False


def release_hint(engine_id: str, configured: Optional[str] = None) -> str:
    """How the user gets the keyboard back, per engine (English; the UI translates)."""

    if engine_id == "frdp":
        return f"{accelerator_label(configured or DEFAULT_RELEASE)} gives the keyboard back to SSH Manager."
    if engine_id == "xfreerdp-embed":
        return "Right Ctrl gives the keyboard back to SSH Manager (FreeRDP's own key)."
    return "The remote desktop runs in its own window; it handles the keyboard itself."


__all__ = ["KeyboardGrab", "GrabState", "Effect", "parse_accelerator", "accelerator_label", "release_hint",
           "DEFAULT_RELEASE", "FREERDP_RELEASE"]
