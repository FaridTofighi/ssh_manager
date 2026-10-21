"""The few Xlib calls an embedded FreeRDP window needs (``ctypes``; no new dependency).

FreeRDP's ``/parent-window:<XID>`` does not speak XEmbed: it creates its own
window and ``XReparentWindow``s it into the given one (``xf_window.c``). So
the tab finds that child with ``XQueryTree`` and resizes it with
``XResizeWindow``; FreeRDP sees the ``ConfigureNotify`` and, with
``/dynamic-resolution``, asks the server for the new size.

The connection is separate from GTK's and used only on the GTK thread.
:func:`open_x11` returns ``None`` where there is no X server or no libX11.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
from typing import List, Optional

_Window = ctypes.c_ulong


class X11:
    def __init__(self, lib: ctypes.CDLL, display: int) -> None:
        self._lib = lib
        self._display = ctypes.c_void_p(display)

    def _trapped(self, call):
        """Run ``call`` with an ignore-all X error handler, then restore the previous one (GDK's).

        libX11 has one error handler per process; replacing GDK's for good breaks its error traps (and can
        crash). Errors of these requests arrive during the round trip / ``XSync``, while ours is installed.
        """

        previous = self._lib.XSetErrorHandler(_ignore_errors())
        try:
            result = call()
            self._lib.XSync(self._display, 0)
            return result
        finally:
            self._lib.XSetErrorHandler(previous)

    def children(self, window: int) -> List[int]:
        """The direct child windows of ``window`` (``[]`` when it no longer exists)."""

        root, parent = _Window(), _Window()
        children = ctypes.POINTER(_Window)()
        count = ctypes.c_uint()
        ok = self._trapped(lambda: self._lib.XQueryTree(self._display, _Window(window), ctypes.byref(root),
                                                        ctypes.byref(parent), ctypes.byref(children),
                                                        ctypes.byref(count)))
        if not ok:
            return []
        try:
            return [int(children[i]) for i in range(count.value)]
        finally:
            if children:
                self._lib.XFree(children)

    def resize(self, window: int, width: int, height: int) -> None:
        self._trapped(lambda: self._lib.XResizeWindow(self._display, _Window(window), ctypes.c_uint(max(1, width)),
                                                      ctypes.c_uint(max(1, height))))

    def close(self) -> None:
        if self._display:
            self._lib.XCloseDisplay(self._display)
            self._display = ctypes.c_void_p(0)


_HANDLER_TYPE = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
_error_handler = None  # kept alive while installed (ctypes callbacks must not be collected)


def _ignore_errors():
    global _error_handler
    if _error_handler is None:
        _error_handler = _HANDLER_TYPE(lambda _display, _event: 0)
    return ctypes.cast(_error_handler, ctypes.c_void_p)


def open_x11(display_name: Optional[str] = None) -> Optional[X11]:
    name = display_name or os.environ.get("DISPLAY")
    if not name:
        return None
    path = ctypes.util.find_library("X11")
    if not path:
        return None
    try:
        lib = ctypes.CDLL(path)
    except OSError:
        return None
    lib.XOpenDisplay.restype = ctypes.c_void_p
    lib.XOpenDisplay.argtypes = [ctypes.c_char_p]
    lib.XQueryTree.argtypes = [ctypes.c_void_p, _Window, ctypes.POINTER(_Window), ctypes.POINTER(_Window),
                               ctypes.POINTER(ctypes.POINTER(_Window)), ctypes.POINTER(ctypes.c_uint)]
    lib.XResizeWindow.argtypes = [ctypes.c_void_p, _Window, ctypes.c_uint, ctypes.c_uint]
    lib.XFree.argtypes = [ctypes.c_void_p]
    lib.XFlush.argtypes = [ctypes.c_void_p]
    lib.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.XCloseDisplay.argtypes = [ctypes.c_void_p]
    lib.XSetErrorHandler.argtypes = [ctypes.c_void_p]  # handlers pass through as plain pointers (GDK's back)
    lib.XSetErrorHandler.restype = ctypes.c_void_p
    display = lib.XOpenDisplay(name.encode())
    if not display:
        return None
    return X11(lib, display)


class EmbedWatch:
    """Waits for FreeRDP's window to appear inside the socket (pure logic; the tab polls)."""

    def __init__(self, timeout_s: float = 10.0) -> None:
        self.timeout_s = timeout_s
        self.child: Optional[int] = None

    def poll(self, children: List[int], elapsed_s: float) -> str:
        """``"found"`` (``child`` is set), ``"waiting"`` or ``"timeout"``."""

        if self.child is None and children:
            self.child = children[-1]
        if self.child is not None:
            return "found" if self.child in children else "gone"
        return "timeout" if elapsed_s >= self.timeout_s else "waiting"


__all__ = ["X11", "open_x11", "EmbedWatch"]
