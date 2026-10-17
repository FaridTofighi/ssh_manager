#!/usr/bin/env python3
"""Render SSH Manager windows and dialogs to PNG files without showing them.

Usage::

    python3 tools/ui_snapshots.py docs/ui-after

The script runs against a throw-away HOME / XDG directory and a generated
sample inventory, so it never touches the user's real settings, passwords or
SSH configuration. Toplevel windows are never mapped: their contents are moved
into a ``Gtk.OffscreenWindow`` and rendered from there.

Terminal sessions are simulated (no network access): tabs are created without
spawning ``ssh`` and sample output is fed into the VTE widget.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_SANDBOX = Path(tempfile.mkdtemp(prefix="ssh-manager-snapshots-"))
os.environ["HOME"] = str(_SANDBOX / "home")
os.environ["XDG_CONFIG_HOME"] = str(_SANDBOX / "config")
os.environ["XDG_DATA_HOME"] = str(_SANDBOX / "data")
for _sub in ("home", "config", "data"):
    (_SANDBOX / _sub).mkdir(parents=True, exist_ok=True)

import gi  # noqa: E402

gi.require_version("Gtk", "3.0")
gi.require_version("Vte", "2.91")
from gi.repository import Gio, GLib, Gtk  # type: ignore  # noqa: E402

SAMPLE_INVENTORY = {
    "production/web.conf": """Host web-01
# meta environment=prod
# meta owner=platform
# meta service=frontend
    hostname web-01.example.internal
    user deploy
    port 22
    tags prod,web,eu

Host web-02
# meta environment=prod
    hostname web-02.example.internal
    user deploy
    tags prod,web
""",
    "production/db/primary.conf": """Host db-primary
# meta environment=prod
# meta owner=dba
    hostname 10.0.4.12
    user postgres
    port 2222
    proxyjump bastion.example.internal
    tags prod,db
""",
    "staging/hosts.conf": """Host stage-app
    hostname stage-app.example.internal
    user ubuntu
    identityfile ~/.ssh/id_ed25519
    tags staging

Host stage-worker
    hostname stage-worker.example.internal
    user ubuntu
""",
}

SAMPLE_OUTPUT = (
    "Welcome to Ubuntu 24.04.1 LTS (GNU/Linux 6.8.0-45-generic x86_64)\r\n\r\n"
    "Last login: Fri Oct 17 09:12:44 2026 from 10.0.0.15\r\n"
    "\x1b[1;32mdeploy@web-01\x1b[0m:\x1b[1;34m~\x1b[0m$ uptime\r\n"
    " 09:13:02 up 41 days,  3:07,  1 user,  load average: 0.21, 0.18, 0.12\r\n"
    "\x1b[1;32mdeploy@web-01\x1b[0m:\x1b[1;34m~\x1b[0m$ "
)
FAKE_STATS = {"cpu": 37.5, "mem_used": 6100.0, "mem_total": 7900.0, "disk_used": 71.2, "disk_total": 78.0}


# --------------------------------------------------------------------- setup
def _patch_toplevel_mapping() -> None:
    """Keep toplevel windows unmapped so nothing appears on screen."""

    original_show_all = Gtk.Widget.show_all

    def show_all(widget: Gtk.Widget) -> None:
        if isinstance(widget, Gtk.Window) and not isinstance(widget, Gtk.OffscreenWindow):
            child = widget.get_child()
            if child is not None:
                original_show_all(child)
            titlebar = widget.get_titlebar()
            if titlebar is not None:
                original_show_all(titlebar)
            return
        original_show_all(widget)

    def present(_window: Gtk.Window, *_args) -> None:
        return None

    Gtk.Widget.show_all = show_all  # type: ignore[assignment]
    Gtk.Window.present = present  # type: ignore[assignment]
    Gtk.Window.show = lambda *_a: None  # type: ignore[assignment]


def _stub_terminal_io() -> None:
    """Replace network-bound terminal behaviour with deterministic fakes."""

    from ssh_manager import gtk_terminal

    def fake_fetch(tab) -> None:
        GLib.idle_add(tab._apply_stats_result, dict(FAKE_STATS))

    gtk_terminal.TerminalTab._fetch_stats_thread = fake_fetch  # type: ignore[assignment]
    gtk_terminal.TerminalTab._update_stats = (  # type: ignore[assignment]
        lambda tab: (tab._apply_stats_result(dict(FAKE_STATS)), True)[1]
    )


def _write_inventory(base: Path, populated: bool) -> None:
    base.mkdir(parents=True, exist_ok=True)
    if not populated:
        return
    for rel, text in SAMPLE_INVENTORY.items():
        path = base / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _pump(iterations: int = 400) -> None:
    """Run a bounded number of main-loop iterations (idle sources may re-arm forever)."""

    for _ in range(iterations):
        if not Gtk.events_pending():
            break
        Gtk.main_iteration_do(False)


# ------------------------------------------------------------------- capture
def _render(widget: Gtk.Widget, path: Path, width: int, height: int) -> None:
    offscreen = Gtk.OffscreenWindow()
    offscreen.set_default_size(width, height)
    parent = widget.get_parent()
    if parent is not None:
        parent.remove(widget)
    offscreen.add(widget)
    widget.set_size_request(width, height)
    offscreen.show_all()
    _pump()
    pixbuf = offscreen.get_pixbuf()
    if pixbuf is not None:
        pixbuf.savev(str(path), "png", [], [])
        print(f"saved {path}")
    else:
        print(f"FAILED {path}")
    offscreen.remove(widget)
    widget.set_size_request(-1, -1)
    if parent is not None:
        parent.add(widget)
    offscreen.destroy()


def capture_window(window: Gtk.Window, path: Path, width: int = 1280, height: int = 800) -> None:
    """Render a toplevel window (titlebar + content) into ``path``."""

    titlebar = window.get_titlebar()
    child = window.get_child()
    holder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    holder.get_style_context().add_class("background")
    for css_class in window.get_style_context().list_classes():
        holder.get_style_context().add_class(css_class)
    if isinstance(window, Gtk.ApplicationWindow):
        # Moved widgets must still resolve win.* actions, or actionable buttons render insensitive.
        holder.insert_action_group("win", window)
    if titlebar is not None:
        window.set_titlebar(None)
        holder.pack_start(titlebar, False, False, 0)
        titlebar.get_style_context().add_class("titlebar")
    if child is not None:
        window.remove(child)
        holder.pack_start(child, True, True, 0)
    _render(holder, path, width, height)
    if child is not None:
        holder.remove(child)
        window.add(child)
    if titlebar is not None:
        holder.remove(titlebar)
        window.set_titlebar(titlebar)


def capture_dialog(dialog: Gtk.Dialog, path: Path, width: int = 0, height: int = 0) -> None:
    default_w, default_h = dialog.get_default_size()
    capture_window(
        dialog,
        path,
        width or (default_w if default_w > 0 else 560),
        height or (default_h if default_h > 0 else 420),
    )
    dialog.destroy()


# ------------------------------------------------------------------ scenarios
def _first_host(window):
    for groups in window.manager.tree.values():
        for group in groups.values():
            if group.hosts:
                return group.hosts[0]
    return None


def _all_hosts(window):
    return [h for groups in window.manager.tree.values() for g in groups.values() for h in g.hosts]


def _fake_session(window, host) -> None:
    notebook = window.terminal_tabs
    tab = notebook.new_terminal(host, autostart=False)
    tab._terminal.feed(SAMPLE_OUTPUT.replace("web-01", host.alias).encode("utf-8"))
    tab._set_status("Connected", True)
    tab._apply_stats_result(dict(FAKE_STATS))


def _set_toggle(window, new_name: str, old_attr: str, active: bool) -> None:
    """Toggle a workspace mode using the new API when present, else the old widget."""

    notebook = window.terminal_tabs
    setter = getattr(notebook, new_name, None)
    if callable(setter):
        setter(active)
        return
    getattr(notebook, old_attr).set_active(active)


def _select_first_host(window) -> None:
    host = _first_host(window)
    if host is None:
        return
    tree = getattr(window, "tree", None)
    selector = getattr(tree, "select_host", None) or getattr(window, "_select_host", None)
    if callable(selector):
        selector(host.parent_group, host.child_group, host.alias)


def _make_window(app, base: Path, prefer_dark: bool):
    from ssh_manager.gtk_app import SSHManagerWindow
    from ssh_manager.settings import AppSettings

    settings = AppSettings.load()
    settings.master_password_disabled = True
    ui_state = base / ".ui_state.json"
    ui_state.write_text('{"prefer_dark": %s}' % ("true" if prefer_dark else "false"), encoding="utf-8")
    window = SSHManagerWindow(app, base, settings)
    _pump()
    return window


def run(out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    _patch_toplevel_mapping()
    _stub_terminal_io()

    from ssh_manager import gtk_app

    app = gtk_app.SSHManagerApplication(_SANDBOX / "inventory")
    # Never talk to (or become) a running SSH Manager instance.
    app.set_flags(Gio.ApplicationFlags.NON_UNIQUE)
    app.register(None)

    empty_base = _SANDBOX / "empty-inventory"
    _write_inventory(empty_base, populated=False)
    base = _SANDBOX / "inventory"
    _write_inventory(base, populated=True)

    scenarios: list[tuple[str, Callable[[], None]]] = []

    def scenario(name: str):
        def wrap(fn: Callable[[], None]) -> Callable[[], None]:
            scenarios.append((name, fn))
            return fn

        return wrap

    for theme, dark in (("dark", True), ("light", False)):

        @scenario(f"main-empty-{theme}")
        def _empty(dark=dark, theme=theme) -> None:
            window = _make_window(app, empty_base, dark)
            capture_window(window, out_dir / f"main-empty-{theme}.png")
            window.destroy()

        @scenario(f"main-hosts-{theme}")
        def _hosts(dark=dark, theme=theme) -> None:
            window = _make_window(app, base, dark)
            _select_first_host(window)
            _pump()
            capture_window(window, out_dir / f"main-hosts-{theme}.png")
            window.destroy()

        @scenario(f"main-two-tabs-{theme}")
        def _tabs(dark=dark, theme=theme) -> None:
            window = _make_window(app, base, dark)
            hosts = _all_hosts(window)
            _select_first_host(window)
            for host in hosts[:2]:
                _fake_session(window, host)
            _pump()
            capture_window(window, out_dir / f"main-two-tabs-{theme}.png")
            window.destroy()

    @scenario("main-tile-mode")
    def _tile() -> None:
        window = _make_window(app, base, True)
        for host in _all_hosts(window)[:3]:
            _fake_session(window, host)
        _set_toggle(window, "set_tile_mode", "_show_all_button", True)
        _pump()
        capture_window(window, out_dir / "main-tile-mode.png")
        window.destroy()

    @scenario("main-broadcast")
    def _broadcast() -> None:
        window = _make_window(app, base, True)
        for host in _all_hosts(window)[:3]:
            _fake_session(window, host)
        _set_toggle(window, "set_broadcast_mode", "_broadcast_all_button", True)
        _pump()
        capture_window(window, out_dir / "main-broadcast.png")
        window.destroy()

    def _dialog_scenario(name: str, factory: Callable[[object], Gtk.Dialog], size: tuple[int, int] = (0, 0)) -> None:
        @scenario(name)
        def _run() -> None:
            window = _make_window(app, base, True)
            dialog = factory(window)
            _pump()
            capture_dialog(dialog, out_dir / f"{name}.png", *size)
            window.destroy()

    from ssh_manager.gtk_app import (  # noqa: E402
        CredentialManagerDialog,
        HostDialog,
        PreferencesDialog,
        VaultDialog,
    )
    from ssh_manager.config_viewer import ConfigViewerDialog

    def host_dialog(window) -> Gtk.Dialog:
        host = _first_host(window)
        return HostDialog(
            window,
            host,
            credentials=window._credential_store.list(),
            metadata=host.metadata if host else {},
            profiles=window._profiles,
        )

    def prefs_dialog(window) -> Gtk.Dialog:
        return PreferencesDialog(window, True, 1.0, 14, window._settings)

    def config_dialog(window) -> Gtk.Dialog:
        path = base / "production" / "web.conf"
        return ConfigViewerDialog(window, path)

    _dialog_scenario("dialog-host", host_dialog, (900, 650))
    _dialog_scenario("dialog-settings", prefs_dialog, (820, 620))
    _dialog_scenario("dialog-credentials", lambda w: CredentialManagerDialog(w, w._credential_store), (560, 400))
    _dialog_scenario("dialog-vault", lambda w: VaultDialog(w, w._vault), (560, 460))
    _dialog_scenario("dialog-config-viewer", config_dialog, (800, 600))

    menu_capture = getattr(gtk_app, "render_context_menu_preview", None)
    if callable(menu_capture):

        @scenario("context-menu-host")
        def _menu() -> None:
            window = _make_window(app, base, True)
            _select_first_host(window)
            widget = menu_capture(window)
            _render(widget, out_dir / "context-menu-host.png", 300, 420)
            window.destroy()

    only = set(sys.argv[2:])
    for name, fn in scenarios:
        if only and name not in only:
            continue
        try:
            fn()
        except Exception as exc:  # pragma: no cover - diagnostic tool
            print(f"scenario {name} failed: {exc!r}")
        _pump()


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "docs" / "ui-snapshots"
    run(target)
