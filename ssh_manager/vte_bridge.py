from __future__ import annotations

import threading
from typing import Optional

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Vte", "2.91")
from gi.repository import GLib, Gtk, Vte  # type: ignore


class VTEBridge:
    """Bridge a Paramiko SSH channel to a VTE terminal."""

    def __init__(self, terminal: Vte.Terminal, client, logger, on_remote_closed=None) -> None:
        self.terminal = terminal
        self.client = client
        self.logger = logger
        self._on_remote_closed = on_remote_closed
        self.channel: Optional[object] = None
        self._running = False
        self._reader_thread: Optional[threading.Thread] = None
        self._commit_handler: Optional[int] = None
        self._size_handler: Optional[int] = None

    def start(self) -> None:
        if self._running:
            return
        transport = self.client.get_transport()
        if transport is None:
            raise RuntimeError("SSH transport unavailable")
        cols = self.terminal.get_column_count() or 80
        rows = self.terminal.get_row_count() or 24
        channel = transport.open_session()
        channel.get_pty(term="xterm-256color", width=cols, height=rows)
        channel.invoke_shell()
        self.channel = channel
        self._running = True
        self._commit_handler = self.terminal.connect("commit", self._on_commit)
        self._size_handler = self.terminal.connect("size-allocate", self._on_size_allocate)
        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()

    def stop(self) -> None:
        self._running = False
        if self._commit_handler is not None:
            self.terminal.disconnect(self._commit_handler)
            self._commit_handler = None
        if self._size_handler is not None:
            self.terminal.disconnect(self._size_handler)
            self._size_handler = None
        if self.channel is not None:
            try:
                self.channel.close()
            except Exception:
                pass
            self.channel = None
        if self._reader_thread and self._reader_thread.is_alive():
            self._reader_thread.join(timeout=1.0)
            self._reader_thread = None

    # ------------------------------------------------------------------ helpers
    def _reader_loop(self) -> None:
        assert self.channel is not None
        channel = self.channel
        try:
            while self._running and not channel.closed:
                try:
                    data = channel.recv(4096)
                except Exception as exc:
                    self.logger.warning("SSH channel recv failed: %s", exc)
                    break
                if not data:
                    break
                GLib.idle_add(self.terminal.feed, data)
        finally:
            GLib.idle_add(self._handle_remote_close)

    def _on_commit(self, terminal: Vte.Terminal, text: str, length: int) -> None:
        if not self.channel or self.channel.closed:
            return
        try:
            self.channel.sendall(text.encode("utf-8"))
        except Exception as exc:
            self.logger.warning("SSH channel send failed: %s", exc)

    def _on_size_allocate(self, terminal: Vte.Terminal, _allocation: Gtk.Allocation) -> None:
        if not self.channel or self.channel.closed:
            return
        cols = terminal.get_column_count() or 80
        rows = terminal.get_row_count() or 24
        try:
            self.channel.resize_pty(width=cols, height=rows)
        except Exception as exc:
            self.logger.warning("SSH channel resize failed: %s", exc)

    def _handle_remote_close(self) -> bool:
        self.stop()
        if callable(self._on_remote_closed):
            try:
                self._on_remote_closed()
            except Exception as exc:
                self.logger.warning("remote close callback failed: %s", exc)
        return False
