"""Embedded terminal support for SSH sessions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pexpect
from textual.events import Key, Paste
from textual.message import Message
from textual.widgets import Log, Static

from .text_utils import strip_ansi


@dataclass
class TerminalConfig:
    """Configuration for launching a terminal session."""

    command: list[str]
    title: str


class TerminalSession:
    """Represents a running interactive terminal backed by pexpect."""

    def __init__(self, argv: list[str], env: Optional[dict[str, str]] = None) -> None:
        self.argv = argv
        self.env = env
        self.process = pexpect.spawn(
            argv[0],
            argv[1:],
            encoding="utf-8",
            codec_errors="ignore",
            echo=False,
            env=env,
        )
        self.closed = False

    def read_chunk(self, size: int = 1024, timeout: float = 0.1) -> Optional[str]:
        """Blocking read from the terminal."""

        if self.closed:
            return None
        try:
            return self.process.read_nonblocking(size=size, timeout=timeout)
        except pexpect.TIMEOUT:
            return ""
        except pexpect.EOF:
            self.closed = True
            return None

    def send(self, data: str) -> None:
        if self.closed:
            return
        self.process.send(data)

    def terminate(self) -> None:
        if self.closed:
            return
        try:
            self.process.terminate(force=True)
        finally:
            self.closed = True


class TerminalWidget(Static):
    """Widget that renders a terminal session with an output log and input prompt."""

    CAN_FOCUS = True

    class TerminalClosed(Message):
        """Posted when the terminal session ends."""

        def __init__(self, terminal: TerminalWidget, pane_id: str, title: str) -> None:
            self.terminal = terminal
            self.pane_id = pane_id
            self.title = title
            super().__init__()

    DEFAULT_CSS = """
    TerminalWidget {
        layout: vertical;
        border: round #1f2a40;
        background: #050b1a;
    }

    TerminalWidget TextLog {
        height: 1fr;
        background: #050b1a;
        color: #dce2f0;
    }

    TerminalWidget Input {
        border: round #2563eb;
        background: #0b1a33;
        color: #e2e8f0;
        height: 3;
        padding-left: 1;
        padding-right: 1;
    }
    """

    def __init__(
        self,
        session: TerminalSession,
        *,
        pane_id: str,
        title: str,
    ) -> None:
        super().__init__()
        self.session = session
        self.pane_id = pane_id
        self.title = title
        self._log: Optional[Log] = None
        self._worker = None
        self._closed_notified = False

    def compose(self):
        yield Log(
            id=f"log-{self.pane_id}",
            highlight=False,
            auto_scroll=True,
        )

    def on_mount(self) -> None:
        self._log = self.query_one(Log)
        if self._log:
            self._log.can_focus = False
        self._worker = self.app.run_worker(
            self._reader_loop,
            name=f"terminal-{self.pane_id}",
            thread=True,
            exit_on_error=False,
        )
        self.focus()

    def on_unmount(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            self._worker = None
        self.session.terminate()

    def on_click(self, event) -> None:
        event.stop()
        self.focus()

    def _append_output(self, chunk: str) -> None:
        if not chunk:
            return
        if self._log is None:
            return
        sanitized = strip_ansi(chunk.replace("\r\n", "\n").replace("\r", "\n"))
        if not sanitized:
            return
        self._log.write(sanitized, scroll_end=True, markup=False)

    def _notify_closed(self, force: bool = False) -> None:
        if self._closed_notified and not force:
            return
        self._closed_notified = True
        if self._log:
            self._log.write("\n[Session closed]", scroll_end=True, markup=False)
        self.post_message(self.TerminalClosed(self, self.pane_id, self.title))

    def _reader_loop(self) -> None:
        while True:
            chunk = self.session.read_chunk()
            if chunk is None:
                self.app.call_from_thread(self._notify_closed)
                break
            if chunk:
                self.app.call_from_thread(self._append_output, chunk)

    def _send(self, data: str) -> None:
        self.session.send(data)

    def on_focus(self, event) -> None:
        if self._log:
            self._log.set_class(True, "-focused")
        super().on_focus(event)

    def on_blur(self, event) -> None:
        if self._log:
            self._log.set_class(False, "-focused")
        super().on_blur(event)

    def on_key(self, event: Key) -> None:
        if self.session.closed:
            key = (event.character or event.key or "").lower()
            if key == "q":
                event.stop()
                self._notify_closed(force=True)
            return
        send: Optional[str] = None
        if event.key == "enter":
            send = "\r"
        elif event.key == "tab":
            send = "\t"
        elif event.key == "backspace":
            send = "\x7f"
        elif event.key == "escape":
            send = "\x1b"
        elif event.key in {"left", "right", "up", "down"}:
            directions = {
                "left": "\x1b[D",
                "right": "\x1b[C",
                "up": "\x1b[A",
                "down": "\x1b[B",
            }
            send = directions[event.key]
        elif event.key == "home":
            send = "\x1b[H"
        elif event.key == "end":
            send = "\x1b[F"
        elif event.key == "delete":
            send = "\x1b[3~"
        elif event.character:
            send = event.character

        if send is not None:
            event.stop()
            self._send(send)

    def on_paste(self, event: Paste) -> None:
        if self.session.closed:
            return
        event.stop()
        self._send(event.text)
