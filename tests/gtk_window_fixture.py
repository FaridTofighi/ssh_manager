"""Shared fixture: a real SSHManagerWindow in a throw-away sandbox, never mapped."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest import mock

SAMPLE = {
    "prod/web.conf": "Host web-01\n    hostname web-01.internal\n    user deploy\n    tags prod,web\n\n"
    "Host web-02\n    hostname web-02.internal\n",
    "prod/db/primary.conf": "Host db-primary\n    hostname 10.0.4.12\n    port 2222\n",
    "staging/hosts.conf": "Host stage-app\n    hostname stage.internal\n",
}


_APP = None


def shared_application():
    """One registered, non-unique Gtk.Application for the whole test process."""

    global _APP
    if _APP is None:
        from gi.repository import Gio, Gtk  # type: ignore

        _APP = Gtk.Application(application_id="org.codex.SSHManager.Tests", flags=Gio.ApplicationFlags.NON_UNIQUE)
        _APP.register(None)
    return _APP


def display_available() -> bool:
    try:
        import gi

        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk  # type: ignore

        return bool(Gtk.init_check(None)[0])
    except Exception:
        return False


class WindowSandbox:
    """Create the main window against a temporary HOME/XDG and sample inventory."""

    def __init__(self) -> None:
        from gi.repository import Gtk  # type: ignore

        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = mock.patch.dict(
            os.environ,
            {
                "HOME": str(self.root / "home"),
                "XDG_CONFIG_HOME": str(self.root / "config"),
                "XDG_DATA_HOME": str(self.root / "data"),
            },
        )
        self.env.start()
        self.base = self.root / "inventory"
        for rel, text in SAMPLE.items():
            path = self.base / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

        original_show_all = Gtk.Widget.show_all

        def show_children_only(widget):
            if isinstance(widget, Gtk.Window):
                for part in (widget.get_child(), widget.get_titlebar()):
                    if part is not None:
                        original_show_all(part)
                return
            original_show_all(widget)

        self.show_patch = mock.patch.object(Gtk.Widget, "show_all", show_children_only)
        self.show_patch.start()

        from ssh_manager.gtk_app import SSHManagerWindow
        from ssh_manager.settings import AppSettings

        self.app = shared_application()
        settings = AppSettings.load()
        settings.master_password_disabled = True
        self.window = SSHManagerWindow(self.app, self.base, settings)
        self.window.host_page.latency.enabled = False  # tests never open sockets

    def close(self) -> None:
        self.window.destroy()
        self.show_patch.stop()
        self.env.stop()
        self.tmp.cleanup()
