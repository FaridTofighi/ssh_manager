"""What a mouse click in a terminal does (gnome-terminal conventions; no GTK).

:func:`decide_mouse_action` is the whole rule set; ``gtk_terminal`` only
translates GTK events into its arguments and carries out the result:

* **Shift + right-click** always opens the terminal menu (xterm / VTE
  convention: Shift bypasses mouse reporting, so it works inside vim, tmux,
  mc, htop …).
* **Right-click** without Shift: if the program in the terminal has mouse
  reporting on, the click is passed to it; otherwise the ``right_click``
  setting applies (menu, paste or nothing).
* **Middle-click**: the same, with ``middle_click`` (default: paste the
  PRIMARY selection, as every X11 terminal does); Shift + middle-click always
  uses the setting.
* Everything else (left button, wheel, extra buttons) goes to the terminal.

Mouse reporting is detected by the GTK glue (see ``gtk_terminal``), not here.
"""

from __future__ import annotations

from enum import Enum

MIDDLE_CLICK_CHOICES = ("paste_primary", "paste_clipboard", "none")
RIGHT_CLICK_CHOICES = ("menu", "paste_clipboard", "paste_primary", "none")


class MouseAction(str, Enum):
    NONE = "none"  # swallow the click
    PASS_TO_TERMINAL = "pass"  # VTE handles it (selection, mouse reporting, its own paste …)
    SHOW_MENU = "menu"
    PASTE_CLIPBOARD = "paste_clipboard"
    PASTE_PRIMARY = "paste_primary"


_CONFIGURED = {
    "menu": MouseAction.SHOW_MENU,
    "paste_clipboard": MouseAction.PASTE_CLIPBOARD,
    "paste_primary": MouseAction.PASTE_PRIMARY,
    "none": MouseAction.NONE,
}


def decide_mouse_action(button: int, shift: bool, ctrl: bool, mouse_tracking: bool, settings) -> MouseAction:
    """The action for a press of ``button`` (1 left, 2 middle, 3 right) on a terminal.

    ``settings`` provides ``right_click`` and ``middle_click`` (:class:`TerminalInputSettings`);
    ``ctrl`` is accepted for future bindings and does not change the result today.
    """

    del ctrl
    if button == 3:
        if shift:
            return MouseAction.SHOW_MENU
        if mouse_tracking:
            return MouseAction.PASS_TO_TERMINAL
        return _CONFIGURED.get(getattr(settings, "right_click", "menu"), MouseAction.SHOW_MENU)
    if button == 2:
        if mouse_tracking and not shift:
            return MouseAction.PASS_TO_TERMINAL
        return _CONFIGURED.get(getattr(settings, "middle_click", "paste_primary"), MouseAction.PASTE_PRIMARY)
    return MouseAction.PASS_TO_TERMINAL


__all__ = ["MouseAction", "decide_mouse_action", "MIDDLE_CLICK_CHOICES", "RIGHT_CLICK_CHOICES"]
