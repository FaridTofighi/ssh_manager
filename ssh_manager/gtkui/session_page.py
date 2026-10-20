"""``SessionPage``: what every session tab is to the notebook (terminal, file browser, later RDP …).

The notebook (tabs, strip, tile grid, status-bar dock, toolbar) talks to its
pages only through this class. A terminal (``TerminalTab``) overrides the
terminal features; other kinds keep the defaults here, so SSH-only features —
broadcast, split panes, transcripts, remote stats, copy-as-plain-text, zoom —
are simply not offered for them (``is_terminal`` is ``False``).
"""

from __future__ import annotations

from typing import Any, Callable, Optional, Tuple

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GObject, Gtk  # type: ignore  # noqa: E402

from ..host_utils import host_connection_params  # noqa: E402
from .theme.tokens import ConnectionStatus  # noqa: E402


class SessionPage(Gtk.Box):
    """Base class of the notebook's pages; the signals every page emits."""

    __gtype_name__ = "SessionPage"
    __gsignals__ = {
        "session-closed": (GObject.SignalFlags.RUN_LAST, None, (str, bool)),
        "status-changed": (GObject.SignalFlags.RUN_LAST, None, (str, bool)),
        "close-requested": (GObject.SignalFlags.RUN_LAST, None, ()),
    }

    is_terminal = False  # broadcast, split, transcript, stats, zoom, copy/paste apply only to terminals
    kind = "session"

    # The notebook sets these (current tab, Alt+arrows between tiles).
    on_activated: Optional[Callable[["SessionPage"], None]] = None
    on_focus_neighbor: Optional[Callable[[str], bool]] = None

    # ------------------------------------------------------------- identity
    @property
    def host(self) -> Any:
        return self._host

    @property
    def title(self) -> str:
        return getattr(self, "_custom_title", "") or self._host.alias

    @title.setter
    def title(self, value: str) -> None:
        text = (value or "").strip()
        self._custom_title = "" if text == self._host.alias else text

    @property
    def custom_title(self) -> str:
        return getattr(self, "_custom_title", "")

    @property
    def status(self) -> ConnectionStatus:
        return getattr(self, "_status", ConnectionStatus.UNKNOWN)

    @property
    def profile(self) -> Any:
        return None

    @property
    def terminal_size(self) -> Tuple[int, int]:
        return 0, 0

    def _pane_address(self) -> str:
        try:
            hostname, port, user = host_connection_params(self._host)
        except Exception:
            return ""
        return f"{user}@{hostname}:{port}"

    def set_pane_label(self, label: str) -> None:
        self._pane_label = label

    # --------------------------------------------------------- status bar
    @property
    def statusbar(self) -> Any:
        return self._statusbar

    def release_statusbar(self) -> Any:
        if self._statusbar.get_parent() is self:
            self.remove(self._statusbar)
        return self._statusbar

    def reclaim_statusbar(self) -> None:
        parent = self._statusbar.get_parent()
        if parent is self:
            return
        if parent is not None:
            parent.remove(self._statusbar)
        self.pack_start(self._statusbar, False, False, 0)

    # ---------------------------------------------------------- activation
    def activate_pane(self, _term: Any = None, *, grab: bool = True) -> bool:
        if self.on_activated is not None:
            self.on_activated(self)
        if grab:
            self.focus_terminal()
        return False

    def focus_terminal(self) -> None:
        """Give the keyboard to the page's main widget."""

    def connect_activity(self, callback: Callable[["SessionPage"], None]) -> None:
        self._activity_callback = callback

    # ------------------------------------------ terminal features (no-ops)
    @property
    def is_split(self) -> bool:
        return False

    def toggle_split(self) -> None:
        pass

    @property
    def stats_visible(self) -> bool:
        return False

    def set_stats_visible(self, _visible: bool) -> None:
        pass

    @property
    def strip_ansi_on_copy(self) -> bool:
        return False

    def _set_strip_ansi(self, _active: bool) -> None:
        pass

    def toggle_strip_ansi_copy(self) -> None:
        pass

    def save_transcript(self) -> None:
        pass

    def copy_clipboard(self) -> None:
        pass

    def paste_clipboard(self) -> None:
        pass

    def send_interrupt(self) -> None:
        pass

    def set_input_observer(self, _observer: Any) -> None:
        pass

    def feed_input(self, _data: str) -> None:
        pass

    def set_broadcast_highlight(self, _active: bool) -> None:
        pass

    def set_appearance(self, _appearance: Any) -> None:
        pass

    def increase_font(self) -> None:
        pass

    def decrease_font(self) -> None:
        pass

    def reset_font(self, _scale: Optional[float] = None) -> None:
        pass

    def set_font_scale(self, _scale: float) -> None:
        pass

    def reconnect(self) -> None:
        pass
