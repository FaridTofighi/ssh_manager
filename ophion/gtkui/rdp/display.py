"""The remote screen inside the tab: FreeRDP in a ``Gtk.Socket`` (X11) or the gtk-frdp widget.

:class:`SocketSurface` gives FreeRDP an X window (``/parent-window:<XID>``).
FreeRDP creates its window after the sign-in (``PostConnect``) and reparents it
into the socket, without XEmbed, so ``plug-added`` never fires: once the
session is connected the surface looks for the child with ``XQueryTree``
(:class:`EmbedWatch`) for :data:`EMBED_TIMEOUT_S` seconds. Size changes are
debounced (:data:`RESIZE_DEBOUNCE_MS`) and passed on with ``XResizeWindow``;
FreeRDP then asks the server for the new size (``/dynamic-resolution``).

:class:`FrdpSurface` wraps ``Frdp.Display`` (gtk-frdp 0.2): it connects
in-process, takes the password as a GObject property (never a command
line), reports the certificate through ``rdp-needs-certificate-verification``
and is answered from the app's own trust decision.

Native X windows always stack above widgets GTK draws, so the toolbar sits
*above* the remote screen (a collapsible bar), not floating over it.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, List, Optional

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk  # type: ignore  # noqa: E402

from ...rdp.x11 import EmbedWatch, X11, open_x11  # noqa: E402
from ...trust import normalize_fingerprint  # noqa: E402
from ..i18n import _  # noqa: E402
from ..widgets.icon_button import set_accessible_name  # noqa: E402

LOGGER = logging.getLogger(__name__)
EMBED_TIMEOUT_S = 10.0
POLL_MS = 200
RESIZE_DEBOUNCE_MS = 250


class Surface(Gtk.Box):
    """Base: the widget that shows the remote screen, its size and a screenshot."""

    engine_id = ""

    def __init__(self) -> None:
        Gtk.Box.__init__(self, orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.get_style_context().add_class("rdp-surface")
        self._sources: List[int] = []
        self._closed = False
        self.connect("destroy", lambda *_: self._on_destroy())

    def pixel_size(self) -> tuple:
        """The area in device pixels (HiDPI: the logical size times the scale factor)."""

        allocation = self.get_allocation()
        scale = max(1, self.get_scale_factor())
        return max(200, allocation.width * scale), max(200, allocation.height * scale)

    def screenshot(self) -> Optional[Any]:
        return None

    def _add_source(self, source: int) -> int:
        self._sources.append(source)
        return source

    def _drop_source(self, source: int) -> None:
        if source in self._sources:
            self._sources.remove(source)

    def _on_destroy(self) -> None:
        self._closed = True
        context = GLib.MainContext.default()
        for source in self._sources:
            if context.find_source_by_id(source) is not None:
                GLib.source_remove(source)
        self._sources.clear()


class SocketSurface(Surface):
    """FreeRDP (xfreerdp / xfreerdp3) drawing into an X window of the tab."""

    engine_id = "xfreerdp-embed"

    def __init__(self, x11_factory: Callable[[], Optional[X11]] = open_x11) -> None:
        Surface.__init__(self)
        self.socket = Gtk.Socket()
        self.socket.set_hexpand(True)
        self.socket.set_vexpand(True)
        self.socket.set_can_focus(True)
        set_accessible_name(self.socket, _("Remote screen"))
        self.pack_start(self.socket, True, True, 0)
        self._x11_factory = x11_factory
        self._x11: Optional[X11] = None
        self.watch: Optional[EmbedWatch] = None
        self.child_window: Optional[int] = None
        self._watch_started = 0.0
        self._resize_source = 0
        self.on_embedded: Optional[Callable[..., Any]] = None
        self.on_timeout: Optional[Callable[..., Any]] = None
        self.on_gone: Optional[Callable[..., Any]] = None
        self.resizes: List[tuple] = []  # sizes passed to FreeRDP (for the connection log / tests)
        self.socket.connect("size-allocate", self._on_size_allocate)
        self.socket.connect("plug-removed", lambda *_: True)  # never let GTK destroy the socket

    def xid(self) -> int:
        """The X window FreeRDP reparents into (realizes the socket if needed)."""

        if not self.socket.get_realized():
            self.socket.realize()
        return int(self.socket.get_id())

    def start_watch(self, timeout_s: float = EMBED_TIMEOUT_S) -> None:
        """Look for FreeRDP's window (call once the session is connected)."""

        if self.watch is not None or self._closed:
            return
        self._x11 = self._x11_factory()
        if self._x11 is None:
            if self.on_timeout is not None:
                self.on_timeout()
            return
        self.watch = EmbedWatch(timeout_s)
        self._watch_started = time.monotonic()
        self._add_source(GLib.timeout_add(POLL_MS, self._poll))

    def _poll(self) -> bool:
        if self._closed or self._x11 is None or self.watch is None:
            return GLib.SOURCE_REMOVE
        children = self._x11.children(self.xid())
        state = self.watch.poll(children, time.monotonic() - self._watch_started)
        if state == "found" and self.child_window is None:
            self.child_window = self.watch.child
            if self.on_embedded is not None and self.child_window is not None:
                self.on_embedded(self.child_window)  # the tab shows the display page first …
            self.resize_now()  # … so this uses the real size (or waits for the allocation)
            self.socket.grab_focus()
            return GLib.SOURCE_CONTINUE  # keep watching: FreeRDP's window may go away
        if state == "timeout":
            if self.on_timeout is not None:
                self.on_timeout()
            return GLib.SOURCE_REMOVE
        if state == "gone":
            self.child_window = None
            if self.on_gone is not None:
                self.on_gone()
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def _on_size_allocate(self, _widget, _allocation) -> None:
        if self.child_window is None or self._closed:
            return
        if self._resize_source:
            GLib.source_remove(self._resize_source)
            self._drop_source(self._resize_source)
        self._resize_source = self._add_source(GLib.timeout_add(RESIZE_DEBOUNCE_MS, self._resize_due))

    def _resize_due(self) -> bool:
        self._drop_source(self._resize_source)
        self._resize_source = 0
        self.resize_now()
        return GLib.SOURCE_REMOVE

    def resize_now(self) -> None:
        if self._x11 is None or self.child_window is None:
            return
        allocation = self.socket.get_allocation()
        if allocation.width <= 1 or allocation.height <= 1:
            return  # not laid out yet: size-allocate resizes it (never a transient tiny size)
        width, height = self.pixel_size()
        width, height = width - width % 2, height - height % 2  # FreeRDP aligns to even sizes anyway
        if self.resizes and self.resizes[-1] == (width, height):
            return
        self._x11.resize(self.child_window, width, height)
        self.resizes.append((width, height))

    def screenshot(self) -> Optional[Any]:
        window = self.socket.get_window()
        if window is None or self.child_window is None:
            return None
        allocation = self.socket.get_allocation()
        return Gdk.pixbuf_get_from_window(window, 0, 0, allocation.width, allocation.height)

    def _on_destroy(self) -> None:
        Surface._on_destroy(self)
        if self._x11 is not None:
            self._x11.close()
            self._x11 = None


class FrdpSurface(Surface):
    """The gtk-frdp ``Frdp.Display`` widget (members checked by ``engines.frdp_check`` beforehand)."""

    engine_id = "frdp"

    def __init__(self, display_class: Any) -> None:
        Surface.__init__(self)
        self.display = display_class()
        self.display.set_hexpand(True)
        self.display.set_vexpand(True)
        self.display.set_can_focus(True)
        set_accessible_name(self.display, _("Remote screen"))
        self.pack_start(self.display, True, True, 0)
        self.accepted: List[str] = []  # fingerprints the app approved before connecting
        self.on_connected: Optional[Callable[..., Any]] = None
        self.on_disconnected: Optional[Callable[..., Any]] = None
        self.on_error: Optional[Callable[..., Any]] = None
        self.on_auth_failure: Optional[Callable[..., Any]] = None
        self.on_certificate_refused: Optional[Callable[..., Any]] = None
        self.display.connect("rdp-connected", lambda *_: self.on_connected and self.on_connected())
        self.display.connect("rdp-disconnected", lambda *_: self.on_disconnected and self.on_disconnected())
        self.display.connect("rdp-error", lambda _d, message: self.on_error and self.on_error(str(message or "")))
        self.display.connect("rdp-auth-failure",
                             lambda _d, message: self.on_auth_failure and self.on_auth_failure(str(message or "")))
        self.display.connect("rdp-needs-certificate-verification", self._verify_certificate)
        try:
            self.display.connect("rdp-needs-certificate-change-verification", self._verify_changed_certificate)
        except TypeError:
            pass  # older gtk-frdp without the signal: unknown certificates still go through _verify_certificate

    def open(self, host: str, port: int, user: str, password: Optional[str], domain: str, scaling: bool) -> None:
        self.display.set_property("username", user or "")
        self.display.set_property("password", password or "")
        self.display.set_property("domain", domain or "")
        self.display.set_scaling(bool(scaling))
        self.display.open_host(host, int(port))
        self.display.set_property("password", "")  # the session has it; do not keep it on the widget

    def close(self) -> None:
        try:
            if self.display.is_open():
                self.display.close()
        except Exception as exc:
            LOGGER.warning("closing gtk-frdp failed: %s", type(exc).__name__)

    def _verify_certificate(self, display, _host, _port, _common_name, _subject, _issuer, fingerprint, _flags) -> None:
        """Accept for this session only (2) the certificate the app approved; refuse (0) anything else."""

        ok = normalize_fingerprint(str(fingerprint or "")) in {normalize_fingerprint(f) for f in self.accepted}
        display.certificate_verify_ex_finish(2 if ok else 0)
        if not ok and self.on_certificate_refused is not None:
            self.on_certificate_refused(str(fingerprint or ""))

    def _verify_changed_certificate(self, display, *args) -> None:
        fingerprint = args[5] if len(args) > 5 else ""
        ok = normalize_fingerprint(str(fingerprint or "")) in {normalize_fingerprint(f) for f in self.accepted}
        display.certificate_change_verify_ex_finish(2 if ok else 0)
        if not ok and self.on_certificate_refused is not None:
            self.on_certificate_refused(str(fingerprint or ""))

    def keyboard_grabbed(self) -> bool:
        try:
            return bool(self.display.is_keyboard_grabbed())
        except Exception:
            return False

    def screenshot(self) -> Optional[Any]:
        try:
            return self.display.get_pixbuf()
        except Exception:
            return None

    def _on_destroy(self) -> None:
        self.close()
        Surface._on_destroy(self)


__all__ = ["Surface", "SocketSurface", "FrdpSurface", "EMBED_TIMEOUT_S", "RESIZE_DEBOUNCE_MS"]
