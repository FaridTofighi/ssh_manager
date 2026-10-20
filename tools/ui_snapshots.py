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
gi.require_version("Gdk", "3.0")
gi.require_version("Vte", "2.91")
from gi.repository import Gdk, Gio, GLib, Gtk  # type: ignore  # noqa: E402

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

# The inventory shown in docs/ui-target/relay-reference.png.
RELAY_INVENTORY = {
    "Production/Core services": """Host api-prod-01
# meta environment=production
    hostname 10.60.110.98
    user deploy
    port 22
    identityfile ~/.ssh/id_ed25519

Host api-prod-02
# meta environment=production
    hostname 10.60.110.99
    user deploy

Host worker-prod-01
# meta environment=production
    hostname 10.60.111.20
    user deploy
""",
    "Production/Databases": """Host db-prod-01
    hostname 10.60.120.10
    user postgres

Host db-prod-02
    hostname 10.60.120.11
    user postgres
""",
    "Staging/Web cluster": """Host web-stage-01
# meta environment=staging
    hostname 10.70.10.11
    user deploy

Host web-stage-02
# meta environment=staging
    hostname 10.70.10.12
    user deploy
""",
    "Staging/Data services": """Host zookeeper-01
    hostname 10.70.20.5
    user ops

Host kafka-stage-01
    hostname 10.70.20.9
    user ops
""",
    "Development/Sandboxes": """Host dev-box-01
    hostname 10.80.0.21
    user dev

Host dev-box-02
    hostname 10.80.0.22
    user dev
""",
}
RELAY_SIZE = (1852, 934)

# Phase 9 tree icons: the mini tree of docs/ui-target/tree-icons-sheet.png plus
# every host state, a read-only host, a nested folder and a collapsed colored group.
TREE_ICONS_INVENTORY = {
    "pardis/zookeeper.conf": "".join(f"Host {alias}\n    hostname {alias}\n\n" for alias in (
        "10.60.110.74", "apzkrfpspst0103.fns", "apzkrfpspst0107.fns", "zk-connecting", "zk-shared")),
    "pardis/kafka/brokers.conf": "Host broker-01\n    hostname 10.60.112.4\n",
    "staging/hosts.conf": "Host web-stage-01\n    hostname 10.70.10.11\n\nHost web-stage-02\n    hostname 10.70.10.12\n",
    "legacy/old.conf": "Host old-box\n    hostname 10.90.0.2\n",
}
TREE_ICONS_STATES = {
    "apzkrfpspst0103.fns": "connected", "apzkrfpspst0107.fns": "failed", "zk-connecting": "connecting",
    "web-stage-01": "connected",
}
TREE_ICONS_USER_COLOR = "#7DB1E8"  # a user-picked group color (legacy)

# The state of docs/ui-before/density-current.png (Phase 4 brief).
DENSITY_INVENTORY = {
    "da/.keep": "",
    "farid/.keep": "",
    "lk/.keep": "",
    "pardis/new-group-farid/asdasdasd.host": """Host asdasdasd
    hostname 10.60.110.40
    user ftofighi
""",
    "pardis/zookeeper": """Host 10.60.110.74
    hostname 10.60.110.74
    user ftofighi

Host apzkrfpspst0103.fns
# meta environment=sandbox
    hostname 10.60.110.98
    user ftofighi

Host apzkrfpspst0107.fns
    hostname 10.60.110.99
    user ftofighi

Host apzkrfpspst0109.fns
    hostname 10.60.110.100
    user ftofighi

Host zk-sandbox
    hostname 10.60.110.120
    user ftofighi
""",
}
DENSITY_SIZE = (1920, 1041)
DENSITY_PROMPT = "\x1b[1;32mftofighi@apzkrfpspst0103\x1b[0m:\x1b[1;34m~\x1b[0m$ "

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
    # Split panes: never spawn ssh for the second terminal.
    gtk_terminal.TerminalTab._spawn_secondary = (  # type: ignore[assignment]
        lambda tab, term: term.feed(DENSITY_PROMPT.encode("utf-8"))
    )
    gtk_terminal.TerminalTab._update_stats = (  # type: ignore[assignment]
        lambda tab: (tab._apply_stats_result(dict(FAKE_STATS)), True)[1]
    )


def _write_inventory(base: Path, populated: bool, inventory: dict = SAMPLE_INVENTORY) -> None:
    base.mkdir(parents=True, exist_ok=True)
    if not populated:
        return
    for rel, text in inventory.items():
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
def _render(widget: Gtk.Widget, path: Path, width: int, height: int, on_rendered=None) -> None:
    offscreen = Gtk.OffscreenWindow()
    offscreen.set_default_size(width, height)
    parent = widget.get_parent()
    if parent is not None:
        parent.remove(widget)
    offscreen.add(widget)
    widget.set_size_request(width, height)
    offscreen.show_all()
    _pump()
    # Lay out once and run what that queued (idle callbacks reacting to the final
    # allocation, e.g. the search keycap), then capture.
    offscreen.get_surface()
    _pump()
    surface = offscreen.get_surface() if offscreen.get_scale_factor() > 1 else None
    pixbuf = None if surface is not None else offscreen.get_pixbuf()
    if surface is not None:
        surface.write_to_png(str(path))  # device pixels (HiDPI: GDK_SCALE=2)
        print(f"saved {path}")
    elif pixbuf is not None:
        pixbuf.savev(str(path), "png", [], [])
        print(f"saved {path}")
    else:
        print(f"FAILED {path}")
    if on_rendered is not None:
        on_rendered(widget)
    offscreen.remove(widget)
    widget.set_size_request(-1, -1)
    if parent is not None:
        parent.add(widget)
    offscreen.destroy()


def capture_window(window: Gtk.Window, path: Path, width: int = 1280, height: int = 800, on_rendered=None) -> None:
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
    _render(holder, path, width, height, on_rendered)
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


# --------------------------------------------------------------- popovers
def render_widget_alone(widget: Gtk.Widget, background: str):
    """Draw ``widget`` at its preferred size onto ``background`` (for popovers)."""

    import cairo
    from gi.repository import Gdk

    _min, natural = widget.get_preferred_size()
    rect = Gdk.Rectangle()
    rect.x = rect.y = 0
    rect.width, rect.height = natural.width, natural.height
    widget.size_allocate(rect)
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, max(1, natural.width), max(1, natural.height))
    cr = cairo.Context(surface)
    rgba = Gdk.RGBA()
    rgba.parse(background)
    cr.set_source_rgb(rgba.red, rgba.green, rgba.blue)
    cr.paint()
    widget.draw(cr)
    return surface


def capture_popovers(window, path: Path) -> None:
    """Avatar, help, workspace and message-history popovers side by side.

    Popovers draw in a child window of the toplevel, so each one is popped up
    on an offscreen anchor and drawn with ``Gtk.Widget.draw``. ``Gtk.Menu``
    context menus only draw inside their own mapped popup window, which the
    snapshots never map; their colors are covered by the stylesheet tests and
    ``tools/debug_surfaces.py``.
    """

    import cairo

    from ssh_manager.gtkui.theme.loader import current_palette

    header = window._header
    offscreen = Gtk.OffscreenWindow()
    offscreen.get_style_context().add_class("sm-window")  # popovers take their CSS parent from the toplevel
    holder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    for css in ("sm-window", "background"):
        holder.get_style_context().add_class(css)
    holder.insert_action_group("win", window)
    anchor = Gtk.Button(label="anchor")
    holder.pack_start(anchor, False, False, 0)
    offscreen.add(holder)
    holder.set_size_request(400, 600)
    offscreen.show_all()
    _pump()
    popovers = [
        header._avatar_popover,
        header.help_button.get_popover(),
        header._workspace_popover,
        header.history_button.get_popover(),
    ]
    surfaces = []
    for popover in popovers:
        if popover is None:
            continue
        popover.set_relative_to(anchor)
        popover.set_transitions_enabled(False)
        popover.popup()
        _pump()
        surfaces.append(render_widget_alone(popover, current_palette().bg_canvas))
        popover.popdown()
    gap = 24
    width = sum(s.get_width() for s in surfaces) + gap * (len(surfaces) + 1)
    height = max(s.get_height() for s in surfaces) + 2 * gap
    sheet = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
    cr = cairo.Context(sheet)
    rgba = Gdk.RGBA()
    rgba.parse(current_palette().bg_canvas)
    cr.set_source_rgb(rgba.red, rgba.green, rgba.blue)
    cr.paint()
    x = gap
    for surface in surfaces:
        cr.set_source_surface(surface, x, gap)
        cr.paint()
        x += surface.get_width() + gap
    sheet.write_to_png(str(path))
    print(f"saved {path}")
    offscreen.destroy()


# ------------------------------------------------------------ chrome budget
def chrome_budget(window, holder) -> dict:
    """Band heights and the terminal's share of the window, from real allocations."""

    def box(widget):
        if widget is None or not widget.get_visible() or not widget.get_mapped():
            return None
        x, y = widget.translate_coordinates(holder, 0, 0)
        alloc = widget.get_allocation()
        return x, y, alloc.width, alloc.height

    total_w, total_h = holder.get_allocated_width(), holder.get_allocated_height()
    view = window.host_page.view
    current = window.terminal_tabs.current_tab
    tab = current() if callable(current) else current
    terminal = box(tab._terminal) if tab is not None else None
    if terminal is None:  # no session: the ready state's canvas is the "terminal area"
        terminal = box(window.host_page.terminal_page)
    header = box(window._header)
    sidebar = box(window._sidebar) if window.layout.sidebar_visible else None
    strip = box(getattr(window.terminal_tabs, "_strip", None))
    page_bar, row_a, info = (box(w) for w in (view.page_bar, view.header, view.info_bar))
    above = max((b[1] + b[3] for b in (header, page_bar, row_a, strip) if b), default=0)
    rows = {
        "top bar": header[3] if header else 0,
        "page bar": page_bar[3] if page_bar else 0,
        "host header / host bar": row_a[3] if row_a else 0,
        "session strip": strip[3] if strip else 0,
        "terminal padding / margins": max(0, terminal[1] - above) if terminal else 0,
        "status bar": info[3] if info else 0,
    }
    tree = window.tree_view
    visible_rows = 0
    rng = tree.get_visible_range() if tree.get_realized() else None
    if rng:
        start, end = rng
        scroller_h = tree.get_parent().get_allocated_height()
        path = start.copy()
        while True:
            rect = tree.get_background_area(path, None)
            if rect.y >= 0 and rect.y + rect.height <= scroller_h:
                visible_rows += 1
            if path.compare(end) == 0:
                break
            if not _next_visible_path(tree, path):
                break
    return {
        "window": (total_w, total_h),
        "rows": rows,
        "chrome": total_h - (terminal[3] if terminal else 0),
        "terminal": terminal[2:] if terminal else (0, 0),
        "height_share": (terminal[3] / total_h) if terminal else 0.0,
        "width_share": (terminal[2] / total_w) if terminal else 0.0,
        "sidebar": sidebar[2] if sidebar else 0,
        "tree_rows": visible_rows,
    }


def _next_visible_path(tree, path) -> bool:
    """Advance ``path`` in place to the next row shown in ``tree`` (depth first)."""

    model = tree.get_model()
    it = model.get_iter(path)
    if model.iter_has_child(it) and tree.row_expanded(path):
        path.down()
        return True
    while True:
        nxt = model.iter_next(model.get_iter(path))
        if nxt is not None:
            path.next()
            return True
        if path.get_depth() <= 1:
            return False
        path.up()


def format_budget(name: str, budget: dict) -> str:
    width, height = budget["window"]
    lines = [f"Chrome budget — {name} ({width} × {height})", "", "| Band | Height |", "|---|---:|"]
    for band, value in budget["rows"].items():
        lines.append(f"| {band} | {value} |")
    tw, th = budget["terminal"]
    lines += [
        f"| **chrome total** | **{budget['chrome']}** |",
        "",
        f"Terminal {tw} × {th}: **{budget['height_share']:.1%}** of the height, "
        f"**{budget['width_share']:.1%}** of the width; sidebar {budget['sidebar']} px, "
        f"{budget['tree_rows']} tree rows fully visible.",
    ]
    return "\n".join(lines)


def report_budget(window, out_dir: Path, name: str):
    """``on_rendered`` hook for capture_window(): print and save the chrome budget."""

    def hook(holder) -> None:
        text = format_budget(name, chrome_budget(window, holder))
        print(text)
        (out_dir / f"{name}-budget.md").write_text(text + "\n", encoding="utf-8")

    return hook


# ------------------------------------------------------------ sidebar probe
def render_window_pixbuf(window: Gtk.Window, width: int, height: int, *, backdrop: bool = False):
    """Render ``window`` offscreen like :func:`capture_window`, returning ``(pixbuf, holder)``.

    The caller must call ``restore()`` on the returned holder's ``restore``
    attribute once done, which moves the widgets back into ``window``.
    """

    titlebar = window.get_titlebar()
    child = window.get_child()
    holder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    holder.get_style_context().add_class("background")
    for css_class in window.get_style_context().list_classes():
        holder.get_style_context().add_class(css_class)
    if isinstance(window, Gtk.ApplicationWindow):
        holder.insert_action_group("win", window)
    if titlebar is not None:
        window.set_titlebar(None)
        holder.pack_start(titlebar, False, False, 0)
    if child is not None:
        window.remove(child)
        holder.pack_start(child, True, True, 0)
    offscreen = Gtk.OffscreenWindow()
    offscreen.set_default_size(width, height)
    offscreen.add(holder)
    holder.set_size_request(width, height)
    offscreen.show_all()
    if backdrop:
        offscreen.set_state_flags(Gtk.StateFlags.BACKDROP, False)
    else:
        offscreen.unset_state_flags(Gtk.StateFlags.BACKDROP)
    _pump()
    offscreen.queue_draw()
    _pump()
    pixbuf = offscreen.get_pixbuf()

    def restore() -> None:
        offscreen.remove(holder)
        if child is not None:
            holder.remove(child)
            window.add(child)
        if titlebar is not None:
            holder.remove(titlebar)
            window.set_titlebar(titlebar)
        offscreen.destroy()

    holder.restore = restore  # type: ignore[attr-defined]
    return pixbuf, holder


def tree_area(sidebar, holder) -> tuple[int, int, int, int]:
    """``(x, y, width, height)`` of the sidebar's tree/empty-state stack in holder coordinates."""

    stack = sidebar.stack
    x, y = stack.translate_coordinates(holder, 0, 0)
    alloc = stack.get_allocation()
    return x, y, alloc.width, alloc.height


def tree_sample_points(sidebar, holder) -> list[tuple[str, int, int]]:
    """Background-only points of the tree area: padding, gaps and the empty space."""

    x, y, width, height = tree_area(sidebar, holder)
    points = [
        ("top-left padding", x + 2, y + 2),
        ("top-right padding", x + width - 3, y + 2),
        ("middle-left padding", x + 2, y + height // 2),
        ("middle-right padding", x + width - 3, y + height // 2),
        ("bottom-left", x + 2, y + height - 3),
        ("bottom-right", x + width - 3, y + height - 3),
    ]
    page = sidebar.stack.get_visible_child_name()
    if page == "tree":
        tree = sidebar.tree_view
        rows_bottom = 0
        visible = tree.get_visible_range()
        if visible:
            rect = tree.get_background_area(visible[1], None)
            _wx, rows_bottom = tree.convert_bin_window_to_widget_coords(0, rect.y + rect.height)
        _tx, ty = tree.translate_coordinates(holder, 0, 0)
        empty_top = ty + rows_bottom
        if empty_top < y + height - 12:
            middle = (empty_top + y + height) // 2
            points += [
                ("empty space below rows (centre)", x + width // 2, middle),
                ("empty space below rows (left)", x + width // 4, middle),
            ]
    else:
        points += [("empty-state margin", x + width // 2, y + 4)]
    return points


def pixel(pixbuf, x: int, y: int) -> tuple[int, int, int]:
    channels = pixbuf.get_n_channels()
    data = pixbuf.get_pixels()
    offset = y * pixbuf.get_rowstride() + x * channels
    return data[offset], data[offset + 1], data[offset + 2]


def hex_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def sidebar_samples(window, *, backdrop: bool = False, size: tuple[int, int] = (1100, 1000)):
    """Render ``window`` and return ``(pixbuf, sidebar crop box, [(label, (x, y), rgb)])``."""

    sidebar = window._sidebar
    pixbuf, holder = render_window_pixbuf(window, *size, backdrop=backdrop)
    try:
        samples = [(label, (px, py), pixel(pixbuf, px, py)) for label, px, py in tree_sample_points(sidebar, holder)]
        sx, sy = sidebar.translate_coordinates(holder, 0, 0)
        alloc = sidebar.get_allocation()
        box = (sx, sy, alloc.width, alloc.height)
    finally:
        holder.restore()
    return pixbuf, box, samples


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


def prepare_relay_match(window) -> None:
    """Put a window in the exact state shown by the reference screenshot.

    Window decorations are hidden: the reference is a borderless mock, the real
    app keeps the native minimize/maximize/close buttons.
    """

    import time

    from ssh_manager.gtkui.constants import TreeColumn

    window._header.set_show_close_button(False)
    window._header.set_user_initials("AF")
    view = window.tree_view
    view.set_row_collapsed("child", "Production", "Databases")
    view.set_row_collapsed("parent", "Development", "")
    host = window.tree.find_host_by_alias("api-prod-01")
    key_iter = window.tree.host_iters.get(next((k for k in window.tree.host_iters if k.endswith("api-prod-01")), ""))
    if key_iter is not None:
        window.tree_store.set_value(key_iter, TreeColumn.STATUS, "connected")
    page = getattr(window, "host_page", None)
    if page is not None and host is not None:
        page.history.record_connected(window.creds.host_key(host), time.time() - 2 * 3600 - 60)
        page.inject_latency(host, 24.0)
        page.latency.enabled = True  # the injected result is fresh, so no probe runs
    if host is not None:
        window.tree.select_host(host.parent_group, host.child_group, host.alias)


def render_tree_icon_sheet(path: Path, tree_crops: dict) -> None:
    """The icon sheet (docs/ui-target/tree-icons-sheet.png) drawn with the real renderer.

    Same layout as the approved Phase 9 sheet: per theme, every row icon at 16 /
    20 / 24 px, the status badges, and the tree itself (``tree_crops``: theme →
    pixbuf of the compact tree-icons scene).
    """

    import cairo
    from gi.repository import Gdk, Pango, PangoCairo

    from ssh_manager.gtkui.constants import Icons
    from ssh_manager.gtkui.theme.tokens import DARK, LIGHT
    from ssh_manager.gtkui.tree_icons import status_badge
    from ssh_manager.gtkui.widgets.icons import render_tree_icon

    width, height = 1960, 1240
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
    cr = cairo.Context(surface)

    def rgb(value):
        rgba = Gdk.RGBA()
        rgba.parse(value)
        return rgba.red, rgba.green, rgba.blue

    def text(x, y, value, color, size=15, weight=Pango.Weight.NORMAL, spacing=0.0):
        layout = PangoCairo.create_layout(cr)
        font = Pango.FontDescription.from_string(f"Inter, Cantarell, Sans {size}px")
        font.set_weight(weight)
        layout.set_font_description(font)
        if spacing:
            attrs = Pango.AttrList()
            attrs.insert(Pango.attr_letter_spacing_new(int(spacing * Pango.SCALE)))
            layout.set_attributes(attrs)
        layout.set_text(value, -1)
        cr.set_source_rgb(*rgb(color))
        cr.move_to(x, y)
        PangoCairo.show_layout(cr, layout)

    def rounded(x, y, w, h, r):
        import math

        cr.new_sub_path()
        cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
        cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
        cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
        cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
        cr.close_path()

    cr.set_source_rgb(*rgb(DARK.bg_canvas))
    cr.paint()
    text(40, 34, "Tree icons — duotone outline, 24-grid, drawn 20 px (Phase 9b: one icon color)",
         DARK.text_primary, 30, Pango.Weight.BOLD)
    text(40, 80, "Columns: 16 px (menus) · 20 px (Compact) · 24 px (Comfortable) · icon_default: "
         "white on dark, #18231F on light", DARK.text_secondary, 17)
    rows = [("Group · collapsed", Icons.FOLDER, False), ("Group · expanded", Icons.FOLDER_OPEN, False),
            ("Config-file group", Icons.FILE_CONF, False), ("Host", Icons.HOST, False),
            ("Host · read-only", Icons.HOST, True)]
    for index, palette in enumerate((DARK, LIGHT)):
        x0, y0, w, h = 40 + index * 960, 130, 920, 1070
        cr.set_source_rgb(*rgb(palette.bg_surface))
        rounded(x0, y0, w, h, 16)
        cr.fill()
        text(x0 + 32, y0 + 30, f"{palette.name.upper()} THEME", palette.text_secondary, 18, Pango.Weight.BOLD, 1.5)
        for r, (label, name, lock) in enumerate(rows):
            y = y0 + 84 + r * 58
            text(x0 + 32, y, label, palette.text_primary, 17)
            for c, size in enumerate((16, 20, 24)):
                icon = render_tree_icon(name, palette.icon_default, size, lock=lock, cache={})
                cr.set_source_surface(icon, x0 + 250 + c * 56, y + 12 - size / 2.0)
                cr.paint()
        y = y0 + 84 + len(rows) * 58 + 20
        text(x0 + 32, y, "STATUS BADGE (RING CUT-OUT IN THE ROW BACKGROUND)", palette.text_secondary, 15,
             spacing=1.2)
        for c, (status, label) in enumerate((("connected", "connected"), ("connecting", "connecting"),
                                             ("failed", "failed (20 / 24 px)"), ("unknown", "unknown"))):
            bx = x0 + 32 + c * 210
            for k, size in enumerate((20, 24) if status == "failed" else (20,)):
                badge = status_badge(status, size)
                icon = render_tree_icon(Icons.HOST, palette.icon_default, size, badge=badge.name,
                                        badge_color=getattr(palette, badge.color_role), cache={})
                cr.set_source_surface(icon, bx + k * 34, y + 40)
                cr.paint()
            text(bx, y + 76, label, palette.text_secondary, 15)
        y += 130
        text(x0 + 32, y, "IN THE TREE (COMPACT: 20 PX ICONS, 30 PX ROWS)", palette.text_secondary, 15, spacing=1.2)
        crop = tree_crops.get(palette.name)
        if crop is not None:
            Gdk.cairo_set_source_pixbuf(cr, crop, x0 + 32, y + 34)
            cr.paint()
    surface.write_to_png(str(path))


def prepare_tree_icons(window) -> None:
    """Statuses, a read-only host, a user color, a collapsed group and a selected host."""

    creds = window.creds
    original = creds.permissions_for_host

    def permissions(host, original=original):
        perms = dict(original(host))
        if host.alias == "zk-shared":
            perms["can_edit"] = False
        return perms

    creds.permissions_for_host = permissions
    window.colors.set_group_color("legacy", "", TREE_ICONS_USER_COLOR)
    window.tree.rebuild()
    for alias, state in TREE_ICONS_STATES.items():
        host = window.tree.find_host_by_alias(alias)
        if host is not None:
            window.tree.set_host_status(host, state)
    window.tree_view.set_row_collapsed("parent", "legacy", "", True)
    host = window.tree.find_host_by_alias("apzkrfpspst0103.fns")
    window.tree.select_host(host.parent_group, host.child_group, host.alias)
    _pump()


def snapshot_density(default: str = "compact") -> str:
    """Density for a scenario; ``SSHM_DENSITY`` overrides it (regression runs)."""

    return os.environ.get("SSHM_DENSITY") or default


def _make_window(app, base: Path, prefer_dark: bool, density: str = ""):
    import json

    from ssh_manager.gtk_app import SSHManagerWindow
    from ssh_manager.settings import AppSettings

    settings = AppSettings.load()
    settings.master_password_disabled = True
    ui_state = base / ".ui_state.json"
    ui_state.write_text(
        json.dumps({"prefer_dark": bool(prefer_dark), "density": density or snapshot_density()}), encoding="utf-8"
    )
    window = SSHManagerWindow(app, base, settings)
    window.host_page.latency.enabled = False  # snapshots never open sockets
    _pump()
    return window


def make_window_with_mode(app, base: Path, theme_mode: str, density: str = ""):
    """Main window for ``base`` with the app theme mode ``system`` / ``light`` / ``dark``."""

    import json

    from ssh_manager.gtk_app import SSHManagerWindow
    from ssh_manager.settings import AppSettings

    (base / ".ui_state.json").write_text(
        json.dumps({"theme_mode": theme_mode, "density": density or snapshot_density()}), encoding="utf-8"
    )
    settings = AppSettings.load()
    settings.master_password_disabled = True
    window = SSHManagerWindow(app, base, settings)
    window.host_page.latency.enabled = False
    _pump()
    return window


def set_desktop_color_scheme(dark: bool) -> None:
    """Change ``org.gnome.desktop.interface color-scheme`` (use with GSETTINGS_BACKEND=memory)."""

    from ssh_manager.gtkui.theme.loader import _interface_settings

    settings = _interface_settings()
    if settings is not None:
        settings.set_string("color-scheme", "prefer-dark" if dark else "default")
        Gio.Settings.sync()
    _pump()


def _pin_resolution(dpi: float = 96.0) -> None:
    """Render at 96 dpi regardless of the desktop's text scaling (reproducible PNGs)."""

    from gi.repository import Gdk

    screen = Gdk.Screen.get_default()
    if screen is not None:
        screen.set_resolution(dpi)
    settings = Gtk.Settings.get_default()
    if settings is not None:
        settings.set_property("gtk-xft-dpi", int(dpi * 1024))


SIDEBAR_THEMES = ("Adwaita", "Adwaita:dark", "HighContrast", "Yaru", "Arc-Dark")


def _theme_slug(theme: str) -> str:
    return theme.lower().replace(":", "-")


def run_sidebar_surfaces(out_dir: Path) -> bool:
    """Render the sidebar per GTK theme (one child process each) and write sidebar-surfaces.md.

    Files are named ``sidebar-<label>-<theme>-<mode>.png``; the label comes
    from ``SSHM_SNAPSHOT_LABEL`` (default ``after``) so a run against an older
    checkout can produce the matching ``before`` set.
    """

    import json
    import subprocess

    sys.path.insert(0, str(ROOT / "tools"))
    from debug_surfaces import theme_installed

    label = os.environ.get("SSHM_SNAPSHOT_LABEL", "after")
    rows = []
    for theme in SIDEBAR_THEMES:
        if not theme_installed(theme):
            print(f"sidebar-surfaces: {theme} not installed, skipped")
            continue
        env = dict(os.environ, GTK_THEME=theme, GSETTINGS_BACKEND="memory", SSHM_SNAPSHOT_LABEL=label)
        env.pop("GDK_SCALE", None)
        proc = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), str(out_dir), "--sidebar-child", theme],
            env=env, capture_output=True, text=True, timeout=600,
        )
        found = [json.loads(line[8:]) for line in proc.stdout.splitlines() if line.startswith("SAMPLES ")]
        if not found:
            print(f"sidebar-surfaces: {theme} failed:\n{proc.stderr[-2000:]}")
        rows += found
    ok = bool(rows) and all(row["ok"] for row in rows)
    lines = [
        f"# Sidebar surface check ({label})",
        "",
        "Pixels sampled in the tree area (padding, gaps, empty space below the rows)",
        "must equal the palette's `bg_surface` within ±1 per channel.",
        "",
        "| GTK theme | App mode | Expected | Worst sample | Max Δ | Image | OK |",
        "|---|---|---|---|---:|---|:---:|",
    ]
    for row in rows:
        lines.append(
            f"| {row['theme']} | {row['mode']} | `{row['expected']}` | `{row['worst']}` ({row['worst_label']}) "
            f"| {row['max_delta']} | `{row['image']}` | {'✓' if row['ok'] else '✗'} |"
        )
    report = out_dir / f"sidebar-surfaces-{label}.md"
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"saved {report}: {'all surfaces match the token' if ok else 'MISMATCH'}")
    return ok


def sidebar_surface_child(out_dir: Path, theme: str) -> None:
    import json

    label = os.environ.get("SSHM_SNAPSHOT_LABEL", "after")
    _pin_resolution()
    _patch_toplevel_mapping()
    _stub_terminal_io()
    from ssh_manager import gtk_app
    from ssh_manager.gtkui.theme.loader import get_theme_manager

    app = gtk_app.SSHManagerApplication(_SANDBOX / "inventory")
    app.set_flags(Gio.ApplicationFlags.NON_UNIQUE)
    app.register(None)
    base = _SANDBOX / "Infrastructure"
    _write_inventory(base, populated=True, inventory=RELAY_INVENTORY)
    for mode in ("light", "dark"):
        # Comfortable, so the crops stay comparable with the Phase 3 "before" set.
        window = make_window_with_mode(app, base, mode, snapshot_density("comfortable"))
        prepare_relay_match(window)
        _pump()
        pixbuf, (x, y, width, height), samples = sidebar_samples(window, size=RELAY_SIZE)
        expected = get_theme_manager().palette.bg_surface
        want = hex_rgb(expected)
        deltas = [(max(abs(a - b) for a, b in zip(rgb, want)), name, rgb) for name, _pt, rgb in samples]
        worst = max(deltas)
        name = f"sidebar-{label}-{_theme_slug(theme)}-{mode}.png"
        pixbuf.new_subpixbuf(x, y, width, height).savev(str(out_dir / name), "png", [], [])
        print("SAMPLES " + json.dumps({
            "theme": theme, "mode": mode, "expected": expected, "image": name,
            "max_delta": worst[0], "worst_label": worst[1],
            "worst": "#%02X%02X%02X" % tuple(worst[2]), "ok": worst[0] <= 1,
        }))
        window.destroy()
        _pump()


def run(out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    _pin_resolution()
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
            for host in (hosts[0], hosts[0], hosts[1]):  # a duplicate gets "(2)"
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

    relay_base = _SANDBOX / "Infrastructure"
    _write_inventory(relay_base, populated=True, inventory=RELAY_INVENTORY)

    @scenario("relay-match")
    def _relay_match() -> None:
        """Reproduce docs/ui-target/relay-reference.png (light theme, 1852×934)."""

        window = _make_window(app, relay_base, False, snapshot_density("comfortable"))
        prepare_relay_match(window)
        _pump()
        capture_window(window, out_dir / "relay-match.png", *RELAY_SIZE)
        window.destroy()

    @scenario("relay-dark")
    def _relay_dark() -> None:
        """The reference scene in the derived dark theme."""

        window = _make_window(app, relay_base, True, snapshot_density("comfortable"))
        prepare_relay_match(window)
        _pump()
        capture_window(window, out_dir / "relay-dark.png", *RELAY_SIZE)
        window.destroy()

    @scenario("relay-compact")
    def _relay_compact() -> None:
        """The reference scene at the 960 px minimum width (compact top bar)."""

        window = _make_window(app, relay_base, False, snapshot_density("comfortable"))
        prepare_relay_match(window)
        window._header.set_compact(True)
        window._paned.set_position(300)
        _pump()
        capture_window(window, out_dir / "relay-compact.png", 960, 720)
        window.destroy()

    tree_icons_base = _SANDBOX / "tree-icons"
    _write_inventory(tree_icons_base, populated=True, inventory=TREE_ICONS_INVENTORY)

    for theme, dark in (("dark", True), ("light", False)):
        for density_name in ("compact", "comfortable"):

            @scenario(f"tree-icons-{theme}-{density_name}")
            def _tree_icons(dark=dark, theme=theme, density_name=density_name) -> None:
                """Sidebar crop: every tree icon, badge and overlay (compare with tree-icons-sheet.png)."""

                window = _make_window(app, tree_icons_base, dark, density_name)
                prepare_tree_icons(window)
                pixbuf, holder = render_window_pixbuf(window, 1280, 1000)
                x, y, width, height = tree_area(window._sidebar, holder)
                tree = window.tree_view
                visible = tree.get_visible_range()
                rows = height
                if visible:
                    last = tree.get_background_area(visible[1], tree.get_column(0))
                    _bx, last_y = tree.convert_bin_window_to_widget_coords(last.x, last.y)
                    _tx, ty = tree.translate_coordinates(window._sidebar.stack, 0, 0)
                    rows = ty + last_y + last.height + 8
                pixbuf.new_subpixbuf(x, y, width, min(height, rows)).savev(
                    str(out_dir / f"tree-icons-{theme}-{density_name}.png"), "png", [], [])
                holder.restore()
                window.destroy()

    for theme, dark in (("dark", True), ("light", False)):

        @scenario(f"inventory-store-{theme}")
        def _inventory_store(dark=dark, theme=theme) -> None:
            """The app-owned inventory: readable names, any depth, a group with hosts and subgroups."""

            import json

            from ssh_manager.config import HostEntry
            from ssh_manager.gtk_app import SSHManagerWindow
            from ssh_manager.inventory.manager import StoreConfigManager
            from ssh_manager.inventory.store import InventoryStore
            from ssh_manager.settings import AppSettings

            folder = _SANDBOX / f"store-{theme}"
            folder.mkdir(parents=True, exist_ok=True)
            (folder / ".ui_state.json").write_text(json.dumps({"prefer_dark": dark, "density": snapshot_density()}),
                                                   encoding="utf-8")
            store = InventoryStore(folder / "inventory.db")
            manager = StoreConfigManager(store)
            top = "Prod DB (10.0.0.5) \u2014 \u0645\u0631\u06a9\u0632"
            manager.create_parent_group(top)
            manager.create_child_group(top, "Team A \u2014 EU/Servers")
            manager.create_child_group(top, "Empty group")
            manager.add_host(top, "", HostEntry("Bastion (jump)", {"hostname": "10.0.0.1", "user": "ops"}))
            manager.add_host(top, "Team A \u2014 EU", HostEntry("Load balancer", {"hostname": "10.0.0.2"}))
            for index, name in enumerate(("Web 01 (primary)", "Web 02", "\u0633\u0631\u0648\u0631 \u0627\u0635\u0644\u06cc")):
                manager.add_host(top, "Team A \u2014 EU/Servers", HostEntry(name, {"hostname": f"10.0.1.{index + 1}"}))
            manager.create_parent_group("Staging")
            manager.add_host("Staging", "", HostEntry("stage app", {"hostname": "10.70.0.5"}))
            settings = AppSettings.load()
            settings.master_password_disabled = True
            window = SSHManagerWindow(app, manager.base_dir, settings, manager=manager)
            window.host_page.latency.enabled = False
            window.tree.select_host(top, "Team A \u2014 EU/Servers", "Web 01 (primary)")
            _pump()
            capture_window(window, out_dir / f"inventory-store-{theme}.png")
            window.destroy()
            store.close()

    @scenario("tree-icons-sheet")
    def _tree_icons_sheet() -> None:
        """docs/ui-target/tree-icons-sheet.png, regenerated with the real renderer.

        Only when named: ``tools/ui_snapshots.py docs/ui-target tree-icons-sheet``.
        """

        if "tree-icons-sheet" not in sys.argv[2:]:
            return
        crops = {}
        for theme, dark in (("dark", True), ("light", False)):
            window = _make_window(app, tree_icons_base, dark, "compact")
            prepare_tree_icons(window)
            pixbuf, holder = render_window_pixbuf(window, 1280, 1000)
            x, y, width, height = tree_area(window._sidebar, holder)
            crops[theme] = pixbuf.new_subpixbuf(x, y, width, min(height, 470)).copy()
            holder.restore()
            window.destroy()
        render_tree_icon_sheet(out_dir / "tree-icons-sheet.png", crops)

    for theme, dark in (("dark", True), ("light", False)):

        @scenario(f"import-wizard-{theme}")
        def _import_wizard(dark=dark, theme=theme) -> None:
            """The MobaXterm import wizard, one image per step (synthetic export, no real data)."""

            from ssh_manager.gtkui.dialogs.mobaxterm_import import RESPONSE_NEXT, MobaXtermImportDialog
            from ssh_manager.gtkui.theme import density
            from tests.mxt_synthetic import ACME

            export = _SANDBOX / "acme.mxtsessions"
            export.write_bytes(ACME.encode("utf-8"))
            import json

            from ssh_manager.gtk_app import SSHManagerWindow
            from ssh_manager.inventory.manager import StoreConfigManager
            from ssh_manager.inventory.openssh import commit_openssh_import, plan_openssh_import
            from ssh_manager.inventory.store import InventoryStore
            from ssh_manager.settings import AppSettings

            # A fresh app inventory per theme (the scene imports into it), seeded with the sample hosts.
            folder = _SANDBOX / f"import-store-{theme}"
            folder.mkdir(parents=True, exist_ok=True)
            (folder / ".ui_state.json").write_text(json.dumps({"prefer_dark": dark, "density": snapshot_density()}),
                                                   encoding="utf-8")
            seed = _SANDBOX / f"import-seed-{theme}"
            _write_inventory(seed, populated=True)
            manager = StoreConfigManager(InventoryStore(folder / "inventory.db"))
            commit_openssh_import(plan_openssh_import([seed]), manager)
            settings = AppSettings.load()
            settings.master_password_disabled = True
            window = SSHManagerWindow(app, manager.base_dir, settings, manager=manager)
            window.host_page.latency.enabled = False
            store = window._credential_store
            ops = next((c for c in store.list() if c.username == "ops"), None) or store.add("Ops account", "ops", "pw")
            dialog = MobaXtermImportDialog(window, window.manager, store, report_dir=_SANDBOX / "imports",
                                           choose_file=lambda _parent: str(export))
            scale = density.active().dialog_scale
            size = (int(round(820 * scale)), int(round(660 * scale)))

            def shot(step: str) -> None:
                _pump()
                capture_window(dialog, out_dir / f"import-wizard-{step}-{theme}.png", *size)

            dialog.choose_file()
            dialog.wait()
            shot("1-file")
            dialog.response(RESPONSE_NEXT)
            dialog.set_folder_active(("Acme", "+ Empty container (old)"), False)
            shot("2-options")
            dialog.response(RESPONSE_NEXT)
            dialog.wait()
            dialog.set_row_credential("alice", ops.id)
            dialog.default_combo.set_active_id("__none__")
            shot("3-credentials")
            dialog.response(RESPONSE_NEXT)
            shot("4-review")
            dialog.start_import()
            dialog.wait()
            shot("5-result")
            dialog.destroy()
            window.destroy()

    @scenario("sidebar-surfaces")
    def _sidebar_surfaces() -> None:
        """Sidebar crops under several GTK themes, checked against the surface token."""

        run_sidebar_surfaces(out_dir)

    density_base = _SANDBOX / "conf"
    _write_inventory(density_base, populated=True, inventory=DENSITY_INVENTORY)

    def _density_scene(window, session: bool) -> None:
        import time

        window._header.set_user_initials("F")
        host = window.tree.find_host_by_alias("apzkrfpspst0103.fns")
        if host is None:
            return
        window.tree.select_host(host.parent_group, host.child_group, host.alias)
        page = window.host_page
        page.history.record_connected(window.creds.host_key(host), time.time() - 60)
        page.inject_latency(host, 2.0)
        page.latency.enabled = True
        if session:
            # Two sessions of the same host, as in docs/ui-before/session-tabs-current.png:
            # the tabs read "apzkrfpspst0103.fns" and "apzkrfpspst0103.fns (2)".
            for _number in range(2):
                tab = window.terminal_tabs.new_terminal(host, autostart=False)
                tab._terminal.feed(DENSITY_PROMPT.encode("utf-8"))
                tab._set_status("Connected", True)
                tab._apply_stats_result({"cpu": 0.0, "mem_used": 1600.0, "mem_total": 3800.0,
                                         "disk_used": 10.4, "disk_total": 15.2})
            window.terminal_tabs.focus_tab(window.terminal_tabs.tabs()[0])
        page.refresh()
        _pump()

    for theme, dark in (("dark", True), ("light", False)):
        for kind, session in (("session", True), ("empty", False)):

            @scenario(f"compact-{kind}-{theme}")
            def _compact_scene(dark=dark, theme=theme, kind=kind, session=session) -> None:
                name = f"compact-{kind}-{theme}"
                window = _make_window(app, density_base, dark, snapshot_density("compact"))
                _density_scene(window, session)
                capture_window(window, out_dir / f"{name}.png", *DENSITY_SIZE,
                               on_rendered=report_budget(window, out_dir, name))
                window.destroy()

    for page in ("details", "activity"):

        @scenario(f"compact-host-{page}")
        def _host_panel(page=page) -> None:
            """The host panel (slide-over) over a live session."""

            from ssh_manager.gtkui.views.host_page import PanelPage

            window = _make_window(app, density_base, True, snapshot_density("compact"))
            _density_scene(window, True)
            host = window.tree.find_host_by_alias("apzkrfpspst0103.fns")
            key = window.creds.host_key(host)
            window.host_page.history.record_event(key, "error", "Connection failed")
            window.host_page.history.record_connected(key, text="Connected")
            window.host_page.view.panel.set_transition_duration(0)
            window.host_page.view.open_panel(PanelPage(page))
            _pump()
            capture_window(window, out_dir / f"compact-host-{page}.png", *DENSITY_SIZE,
                           on_rendered=report_budget(window, out_dir, f"compact-host-{page}"))
            window.destroy()

    def _pane_scene(window, kind: str) -> None:
        """Sessions shown together: split panes, tiles, broadcast, a duplicated split."""

        notebook = window.terminal_tabs
        host = window.tree.find_host_by_alias("apzkrfpspst0103.fns")
        other = window.tree.find_host_by_alias("10.60.110.74")
        window.tree.select_host(host.parent_group, host.child_group, host.alias)
        hosts = {
            "split-two-panes": [host],
            "tile-two": [host, host],
            "tile-four": [host, host, other, other],
            "tile-broadcast": [host, host, other],
            "duplicate-session": [host, host],
        }[kind]
        tabs = []
        for target in hosts:
            tab = notebook.new_terminal(target, autostart=False)
            tab._terminal.feed(DENSITY_PROMPT.encode("utf-8"))
            tab._set_status("Connected", True)
            tab._apply_stats_result(dict(FAKE_STATS))
            tabs.append(tab)
        if kind in ("split-two-panes", "duplicate-session"):
            tabs[-1].toggle_split()
        if kind.startswith("tile") or kind == "duplicate-session":
            notebook.set_tile_mode(True)
        if kind == "tile-broadcast":
            notebook.set_broadcast_mode(True)
            notebook._tile_wrappers[tabs[2]].include.set_active(False)  # one session left out
        notebook.focus_tab(tabs[0])
        _pump()

    for kind in ("split-two-panes", "tile-two", "tile-four", "tile-broadcast", "duplicate-session"):
        for theme, dark in (("dark", True), ("light", False)):
            for dens in ("compact", "comfortable"):

                @scenario(f"{kind}-{theme}-{dens}")
                def _panes(kind=kind, theme=theme, dark=dark, dens=dens) -> None:
                    window = _make_window(app, density_base, dark, dens)
                    _pane_scene(window, kind)
                    capture_window(window, out_dir / f"{kind}-{theme}-{dens}.png", 1280, 800)
                    window.destroy()

    @scenario("compact-narrow")
    def _compact_narrow() -> None:
        """960 px wide: narrow variant (session chips get their own strip, short labels)."""

        window = _make_window(app, density_base, True, snapshot_density("compact"))
        _density_scene(window, True)
        window.layout.set_narrow(True)
        _pump()
        capture_window(window, out_dir / "compact-narrow.png", 960, 720,
                       on_rendered=report_budget(window, out_dir, "compact-narrow"))
        window.destroy()

    @scenario("compact-focus")
    def _compact_focus() -> None:
        """Focus mode: only the tabs / session row and the terminal."""

        window = _make_window(app, density_base, True, snapshot_density("compact"))
        _density_scene(window, True)
        window.layout.set_maximized(True)
        _pump()
        capture_window(window, out_dir / "compact-focus.png", *DENSITY_SIZE,
                       on_rendered=report_budget(window, out_dir, "compact-focus"))
        window.destroy()

    @scenario("popovers-dark")
    def _popovers_dark() -> None:
        window = _make_window(app, base, True)
        window._notify("Host key changed for web-02.", Level.WARNING)
        capture_popovers(window, out_dir / "popovers-dark.png")
        window.destroy()

    @scenario("toasts-dark")
    def _toasts_dark() -> None:
        """One toast of each level."""

        window = _make_window(app, base, True)
        window._toasts.MAX_VISIBLE = 4
        window._notify("Reconnecting to web-01…", Level.INFO)
        window._notify("Copied “ssh deploy@web-01”.", Level.SUCCESS)
        window._notify("Host key for web-02 changed.", Level.WARNING)
        window._notify("Connection to db-primary failed.", Level.ERROR, action_label="_Retry", action=lambda: None)
        _pump()
        capture_window(window, out_dir / "toasts-dark.png")
        window.destroy()

    @scenario("neutral-dark-match")
    def _neutral_dark_match() -> None:
        """Reproduce docs/ui-target/neutral-dark-palette.png (the full-window mock).

        The mock is the Comfortable layout captured on a 115 dpi desktop with a
        444 px sidebar, so this scene renders the same way; compare it with
        tools/compare_neutral_dark.py.
        """

        _pin_resolution(115.0)
        try:
            window = _make_window(app, density_base, True, "comfortable")
            window._paned.set_position(444)
            _density_scene(window, True)
            capture_window(window, out_dir / "neutral-dark-match.png", *DENSITY_SIZE)
            window.destroy()
        finally:
            _pin_resolution()

    @scenario("comfortable-session-dark")
    def _comfortable_scene() -> None:
        """The same scene in the Comfortable preset: the "before" chrome budget."""

        window = _make_window(app, density_base, True, "comfortable")
        _density_scene(window, True)
        capture_window(window, out_dir / "comfortable-session-dark.png", *DENSITY_SIZE,
                       on_rendered=report_budget(window, out_dir, "comfortable-session-dark"))
        window.destroy()

    def _dialog_scenario(name: str, factory: Callable[[object], Gtk.Dialog], size: tuple[int, int] = (0, 0)) -> None:
        @scenario(name)
        def _run() -> None:
            from ssh_manager.gtkui.theme import density

            window = _make_window(app, base, True)
            dialog = factory(window)
            _pump()
            scale = density.active().dialog_scale  # Compact dialogs open smaller
            capture_dialog(dialog, out_dir / f"{name}.png", *(int(round(v * scale)) for v in size))
            window.destroy()

    from ssh_manager.gtkui.theme.tokens import Level  # noqa: E402
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
        return PreferencesDialog(window, True, 1.0, 11, window._settings)

    def config_dialog(window) -> Gtk.Dialog:
        path = base / "production" / "web.conf"
        return ConfigViewerDialog(window, path)

    _dialog_scenario("dialog-host", host_dialog, (900, 650))

    def host_password_dialog(window, saved: bool = True) -> Gtk.Dialog:
        """Credentials page with "a password specific to this host" (synthetic credentials, no secret)."""

        from ssh_manager.credentials import Credential

        host = _first_host(window)
        shared = Credential("snap-shared", "Ops team", "ops")
        dialog = HostDialog(
            window,
            host,
            credentials=[shared],
            selected_credential_id=shared.id,
            private_credential=(Credential("snap-private", "host:web", "ops", private=True, based_on=shared.id)
                                if saved else None),
            private_has_password=saved,
            metadata=host.metadata if host else {},
            profiles=window._profiles,
        )
        if not saved:
            dialog._pw_host_radio.set_active(True)
        dialog.layout.show_page("credentials")
        return dialog

    for dark in (False, True):
        for saved in (True, False):
            name = f"dialog-host-password-{'saved' if saved else 'notset'}-{'dark' if dark else 'light'}"

            @scenario(name)
            def _host_password(dark=dark, saved=saved, name=name) -> None:
                from ssh_manager.gtkui.theme import density

                window = _make_window(app, base, dark)
                dialog = host_password_dialog(window, saved)
                _pump()
                scale = density.active().dialog_scale
                capture_dialog(dialog, out_dir / f"{name}.png", *(int(round(v * scale)) for v in (900, 650)))
                window.destroy()
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
    if len(sys.argv) == 4 and sys.argv[2] == "--sidebar-child":
        sidebar_surface_child(target, sys.argv[3])
    else:
        run(target)
