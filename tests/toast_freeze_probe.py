"""Diagnostic harness (not a test): does anything block input after a failed connection's error toast?

Run from the repository root on an X11 display (the real desktop or a nested one)::

    python -m tests.toast_freeze_probe [--map] [--scenario NAME] [--motion0] [--real-ssh]

It opens a sandboxed main window, connects to a host whose ``ssh`` / ``sshpass``
are fake scripts that print "Host key verification failed." and exit 255
(``--real-ssh`` keeps the system ones and points the host at a local port that
refuses), waits for the error toast and then reports, as ``key: value`` lines:

* main-loop stalls (a 100 ms heartbeat; any gap over 500 ms);
* GTK grabs, pointer / keyboard grabs, visible toplevels (modal, transient),
  open popovers and menus, nested main loops;
* hit testing: for a grid of points, the widget whose window receives a press
  (GDK's picking: viewable child windows, topmost first, pass-through skipped);
* synthesized clicks (in-process events, the real pointer is never moved) on a
  sidebar host and the header menu button, and on the toast's dismiss button.

Scenarios: ``plain``, ``three`` (three sticky error toasts), ``panel`` (host
panel open), ``menu`` (a context menu popped up), ``history`` (the message
history popover open), ``unfocused`` (window iconified before the failure).
"""

from __future__ import annotations

import argparse
import os
import stat
import sys
import tempfile
import time
from pathlib import Path
from typing import List, Optional, Tuple

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk  # type: ignore  # noqa: E402

FAKE_SSH = """#!/bin/sh
echo "Host key verification failed." >&2
exit 255
"""
FAKE_SSHPASS = """#!/bin/sh
# sshpass -e ssh ...: drop the sshpass options and run the (fake) ssh
while [ $# -gt 0 ]; do case "$1" in -e|-d*|-f*|-p*) shift ;; *) break ;; esac; done
exec "$@"
"""


def write_fakebin(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for name, text in (("ssh", FAKE_SSH), ("sshpass", FAKE_SSHPASS)):
        path = directory / name
        path.write_text(text)
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return directory


def pump(seconds: float, until=None) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        if until is not None and until():
            return True
        time.sleep(0.01)
    return until() if until is not None else True


class Heartbeat:
    def __init__(self) -> None:
        self.last = time.monotonic()
        self.worst = 0.0
        self.stalls: List[float] = []
        GLib.timeout_add(100, self._tick)

    def _tick(self) -> bool:
        now = time.monotonic()
        gap = now - self.last
        self.worst = max(self.worst, gap)
        if gap > 0.5:
            self.stalls.append(round(gap, 3))
        self.last = now
        return True


# --------------------------------------------------------------------- picking
def pick(window: Gdk.Window, x: float, y: float) -> Gdk.Window:
    """The deepest viewable, non-pass-through child window at (x, y) (window coordinates), like GDK."""

    while True:
        for child in window.get_children():  # topmost first
            if not child.is_viewable() or child.is_input_only() is None:
                continue
            cx, cy = child.get_position()
            if not (cx <= x < cx + child.get_width() and cy <= y < cy + child.get_height()):
                continue
            if child.get_pass_through():
                inner = pick(child, x - cx, y - cy)
                if inner is child:
                    continue
                return inner
            window, x, y = child, x - cx, y - cy
            break
        else:
            return window


def _address(obj) -> int:
    import ctypes

    getter = ctypes.pythonapi.PyCapsule_GetPointer
    getter.restype, getter.argtypes = ctypes.c_void_p, [ctypes.py_object, ctypes.c_char_p]
    return getter(obj.__gpointer__, None) or 0


def owner(window: Gdk.Window) -> Optional[Gtk.Widget]:
    """The widget whose window this is (``user_data`` is a raw pointer in Python)."""

    data = window.get_user_data()
    if not isinstance(data, int):
        return data
    found = []

    def walk(widget):
        if found:
            return
        if _address(widget) == data:
            found.append(widget)
            return
        if isinstance(widget, Gtk.Container):
            widget.forall(walk)
        if isinstance(widget, Gtk.MenuButton) and widget.get_popover() is not None:
            walk(widget.get_popover())

    for top in Gtk.Window.list_toplevels():
        walk(top)
    return found[0] if found else None


def describe(widget: Optional[Gtk.Widget]) -> str:
    if widget is None:
        return "None"
    chain = []
    w = widget
    while w is not None and len(chain) < 4:
        chain.append(type(w).__name__)
        w = w.get_parent()
    return "<".join(chain)


def _all(widget):
    yield widget
    if isinstance(widget, Gtk.Container):
        for child in widget.get_children():
            yield from _all(child)


def widget_rect(widget: Gtk.Widget, toplevel: Gtk.Widget) -> Optional[Tuple[int, int, int, int]]:
    if not widget.get_mapped():
        return None
    coords = widget.translate_coordinates(toplevel, 0, 0)
    if coords is None:
        return None
    alloc = widget.get_allocation()
    return coords[0], coords[1], alloc.width, alloc.height


def click(widget: Gtk.Widget, toplevel: Gtk.Window, x: int, y: int) -> str:
    """Synthesize press + release at toplevel coordinates (x, y) on the window GDK would pick."""

    target = pick(toplevel.get_window(), x, y)
    wx, wy = x, y
    w = target
    while w is not None and w != toplevel.get_window():
        px, py = w.get_position()
        wx, wy = wx - px, wy - py
        w = w.get_parent()
    seat = Gdk.Display.get_default().get_default_seat()
    for kind in (Gdk.EventType.BUTTON_PRESS, Gdk.EventType.BUTTON_RELEASE):
        event = Gdk.Event.new(kind)
        event.button.window = target
        event.button.send_event = True
        event.button.time = Gtk.get_current_event_time() or Gdk.CURRENT_TIME
        event.button.x, event.button.y = float(wx), float(wy)
        event.button.button = 1
        event.set_device(seat.get_pointer())
        Gtk.main_do_event(event)
        pump(0.05)
    return describe(owner(target))


# ------------------------------------------------------------- real X input
PRESSES: List[str] = []


def install_press_log() -> None:
    """Log every button press GDK dispatches: the widget it reaches and the current GTK grab."""

    def handler(event, _data=None):
        if event.type in (Gdk.EventType.BUTTON_PRESS, Gdk.EventType.BUTTON_RELEASE):
            target = Gtk.get_event_widget(event)
            grab = Gtk.grab_get_current()
            PRESSES.append(f"{event.type.value_nick}->{describe(target)} grab={describe(grab) if grab else None}")
        Gtk.main_do_event(event)

    Gdk.event_handler_set(handler, None)


def xtest_click(window: Gtk.Window, x: int, y: int) -> List[str]:
    """A real click through the X server (XTest, like xdotool) at window coordinates; returns the press log."""

    import ctypes

    xlib = ctypes.CDLL("libX11.so.6")
    xtst = ctypes.CDLL("libXtst.so.6")
    xlib.XOpenDisplay.restype = ctypes.c_void_p
    xlib.XOpenDisplay.argtypes = [ctypes.c_char_p]
    for fn in (xtst.XTestFakeMotionEvent, xtst.XTestFakeButtonEvent):
        fn.restype = ctypes.c_int
    xtst.XTestFakeMotionEvent.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_ulong]
    xtst.XTestFakeButtonEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong]
    xlib.XFlush.argtypes = [ctypes.c_void_p]
    xlib.XCloseDisplay.argtypes = [ctypes.c_void_p]
    display = xlib.XOpenDisplay(Gdk.Display.get_default().get_name().encode())
    _ok, ox, oy = window.get_window().get_origin()
    del PRESSES[:]
    xtst.XTestFakeMotionEvent(display, -1, ox + x, oy + y, 0)
    xlib.XFlush(display)
    pump(0.15)
    for down in (1, 0):
        xtst.XTestFakeButtonEvent(display, 1, down, 0)
        xlib.XFlush(display)
        pump(0.15)
    xlib.XCloseDisplay(display)
    pump(2.0, until=lambda: any(entry.startswith("button-release") for entry in PRESSES))  # slow under load
    pump(0.1)
    return list(PRESSES)


# ------------------------------------------------------------------- audits
def audit(win: Gtk.Window) -> dict:
    display = Gdk.Display.get_default()
    seat = display.get_default_seat()
    grab = Gtk.grab_get_current()
    tops = []
    for top in Gtk.Window.list_toplevels():
        if top.get_visible():
            tops.append(f"{type(top).__name__}(modal={top.get_modal()},transient={top.get_transient_for() is not None},"
                        f"type={top.get_window_type().value_nick})")
    popovers = []

    def walk(widget):
        if isinstance(widget, (Gtk.Popover, Gtk.Menu)) and widget.get_visible():
            popovers.append(type(widget).__name__)
        if isinstance(widget, Gtk.MenuButton) and widget.get_popover() is not None:
            if widget.get_popover().get_visible():
                popovers.append(f"MenuButton.popover({type(widget).__name__})")
        if isinstance(widget, Gtk.Container):
            for child in widget.get_children():
                walk(child)

    for top in Gtk.Window.list_toplevels():
        walk(top)
    return {
        "gtk_grab": describe(grab) if grab is not None else "None",
        "pointer_grabbed": display.device_is_grabbed(seat.get_pointer()),
        "keyboard_grabbed": display.device_is_grabbed(seat.get_keyboard()) if seat.get_keyboard() else False,
        "visible_toplevels": tops,
        "open_popovers_or_menus": popovers,
        "main_level": Gtk.main_level(),
        "window_sensitive": win.is_sensitive(),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--map", action="store_true", help="map the window (needed for a real window manager)")
    parser.add_argument("--scenario", default="plain",
                        choices=("plain", "three", "panel", "menu", "history", "unfocused"))
    parser.add_argument("--motion0", action="store_true", help="theme motion 0 ms")
    parser.add_argument("--real-ssh", action="store_true", help="use the system ssh against a refusing port")
    parser.add_argument("--xtest", action="store_true", help="real X clicks (XTest) on the toast's x and a header button")
    args = parser.parse_args(argv)

    if not Gtk.init_check(None)[0]:
        print("result: no display")
        return 2
    tmp = tempfile.TemporaryDirectory()
    if not args.real_ssh:
        fakebin = write_fakebin(Path(tmp.name) / "fakebin")
        os.environ["PATH"] = f"{fakebin}{os.pathsep}{os.environ.get('PATH', '')}"

    from tests.gtk_window_fixture import WindowSandbox

    sandbox = WindowSandbox(store=True)
    win = sandbox.window
    if args.motion0:
        from ssh_manager.gtkui.theme.loader import get_theme_manager

        manager = get_theme_manager()
        manager.motion_ms = lambda *_a, **_k: 0  # type: ignore[assignment]
    win.set_default_size(1200, 760)
    print(f"display: {Gdk.Display.get_default().get_name()} backend={type(Gdk.Display.get_default()).__name__}")
    if args.map:
        sandbox.show_patch.stop()  # dialogs really appear (the sandbox hides every toplevel)
        sandbox.show_patch.start = lambda: None  # type: ignore[assignment]
        sandbox.show_patch.stop = lambda: None  # type: ignore[assignment]
        win.show()
        pump(2.0, until=lambda: win.get_mapped() and win.get_window() is not None)
    else:
        win.realize()
        rect = Gdk.Rectangle()
        rect.width, rect.height = 1200, 760
        win.size_allocate(rect)  # never mapped: same as the user's headless check
    pump(1.0)
    heartbeat = Heartbeat()

    host = win.tree.find_host_by_alias("web-01")
    if args.real_ssh:
        host.options["hostname"] = "127.0.0.1"
        host.options["port"] = "1"
    stored = win.tree.find_host_by_alias("web-01")
    win.tree.select_host(stored.parent_group, stored.child_group, stored.alias)

    if args.scenario == "panel":
        view = win.host_page.view
        if view is None:
            print("scenario_panel: no host page view")
        else:
            view.open_panel()
            pump(0.5)
            print(f"scenario_panel_open: {view.panel_open if hasattr(view, 'panel_open') else '?'}")
    if args.scenario == "menu":
        menu = Gtk.Menu()
        menu.append(Gtk.MenuItem(label="probe"))
        menu.show_all()
        menu.popup_at_widget(win.tree_view, Gdk.Gravity.CENTER,
                             Gdk.Gravity.NORTH_WEST, None)
        pump(0.3)
    if args.scenario == "history":
        button = win._header.history_button
        if button is not None:
            button.set_active(True)
            pump(0.3)
        else:
            print("scenario_history: button not found")
    if args.scenario == "unfocused" and args.map:
        win.iconify()
        pump(1.0)
    if args.scenario == "three":
        for n in range(2):
            win._log_event(f"Earlier failure {n}", False)

    started = time.monotonic()
    win.sessions.open_hosts([stored])
    from ssh_manager.gtkui.dialogs.password import PasswordRequestDialog

    def prompt():
        return next((t for t in Gtk.Window.list_toplevels() if isinstance(t, PasswordRequestDialog)), None)

    if pump(5.0, until=lambda: prompt() is not None):
        dialog = prompt()
        print(f"password_prompt: visible={dialog.get_visible()} modal={dialog.get_modal()} "
              f"transient={dialog.get_transient_for() is win}")
        pump(1.5)  # let the window manager place and focus it
        import subprocess as _sp

        stacking = _sp.run(["xprop", "-root", "_NET_CLIENT_LIST_STACKING"], capture_output=True, text=True).stdout
        order = [int(w, 16) for w in stacking.split("#")[-1].replace(",", " ").split()] if "#" in stacking else []
        main_xid, dialog_xid = win.get_window().get_xid(), dialog.get_window().get_xid()
        if main_xid in order and dialog_xid in order:
            print(f"stacking: dialog_above_main={order.index(dialog_xid) > order.index(main_xid)}")
        print(f"focus: dialog_active={dialog.is_active()} main_active={win.is_active()}")
        win.get_window().raise_()  # what a click on the main window's frame / taskbar does
        pump(1.0)
        stacking = _sp.run(["xprop", "-root", "_NET_CLIENT_LIST_STACKING"], capture_output=True, text=True).stdout
        order = [int(w, 16) for w in stacking.split("#")[-1].replace(",", " ").split()] if "#" in stacking else []
        if main_xid in order and dialog_xid in order:
            print(f"stacking_after_raising_main: dialog_above_main={order.index(dialog_xid) > order.index(main_xid)}")
        print(f"while_prompt_open: {audit(win)}")
        before = win.tree.selected_host()
        other = win.tree.find_host_by_alias("db-primary")
        model = win.tree_view.get_model()
        target_path = []
        win.tree_view.expand_all()
        pump(0.2)
        model.foreach(lambda m, path, it, _d=None: (not target_path and other.alias in
                                                    " ".join(str(m.get_value(it, c)) for c in range(min(2, m.get_n_columns())))
                                                    and target_path.append(path.copy())) or False)
        if target_path:
            cell = win.tree_view.get_cell_area(target_path[0], win.tree_view.get_column(0))
            bx, by = win.tree_view.convert_bin_window_to_widget_coords(cell.x + 10, cell.y + cell.height // 2)
            tx, ty = win.tree_view.translate_coordinates(win, bx, by)
            got = click(win.tree_view, win, tx, ty)
            after = win.tree.selected_host()
            print(f"click_tree_while_prompt_open: target={got} selection_changed="
                  f"{(after.alias if after else None) != (before.alias if before else None)}")
        dialog.secret.entry.set_text("not-a-real-password")
        dialog.retry_button.clicked()  # the user's Connect
        pump(0.2)
        print(f"password_prompt_after_connect: {[type(t).__name__ for t in Gtk.Window.list_toplevels() if t.get_visible()]}")
    else:
        print("password_prompt: none")
    ok = pump(15.0, until=lambda: any("Host key" in m.text or "Could not reach" in m.text
                                        for m in win._toasts.visible))
    print(f"toast_shown: {ok} after {time.monotonic() - started:.2f}s")
    print(f"toasts_visible: {[m.text for m in win._toasts.visible]}")
    print(f"history: {[m.text for m in win._toasts.history.items()][:6]}")
    from ssh_manager.gtk_terminal import TerminalTab as _TT
    _tabs = []

    def _find(widget):
        if isinstance(widget, _TT):
            _tabs.append(widget)
        elif isinstance(widget, Gtk.Container):
            for c in widget.get_children():
                _find(c)

    _find(win)
    for _t in _tabs:
        print(f"tab: status={getattr(_t, 'status', None)} pid={_t._child_pid} auth={_t._authenticated} "
              f"source={_t._password_source} mapped={_t.get_mapped()} "
              f"text={_t._terminal_tail(_t._terminal)[-120:]!r}")
    print(f"which_ssh: {__import__('shutil').which('ssh')}")
    pump(1.5)  # reveal animation, session-closed idle handlers
    if args.scenario == "unfocused" and args.map:
        win.deiconify()
        win.present()
        pump(1.0)

    for key, value in audit(win).items():
        print(f"{key}: {value}")
    print(f"heartbeat_worst_gap_s: {heartbeat.worst:.3f}")
    print(f"heartbeat_stalls_over_0.5s: {heartbeat.stalls}")

    overlay = win._toast_overlay
    stack = overlay._stack
    rect = widget_rect(stack, win)
    print(f"toast_stack_rect: {rect}")
    toast_rects = [widget_rect(t, win) for t in overlay._toasts.values()]
    print(f"toast_rects: {toast_rects}")
    stack_window = None
    for child in win.get_window().get_children():
        if owner(child) is overlay:
            stack_window = stack_window or child
    overlay_windows = []

    def collect(window, depth=0):
        for child in window.get_children():
            if owner(child) is overlay:
                overlay_windows.append((child.get_position(), child.get_width(), child.get_height(),
                                        child.is_viewable(), child.get_pass_through()))
            collect(child, depth + 1)

    collect(win.get_window())
    print(f"overlay_child_windows(pos,w,h,viewable,pass_through): {overlay_windows}")
    natives = []

    def find_native(window):
        for child in window.get_children():
            if child.has_native():
                natives.append((describe(owner(child)), child.get_position(), child.get_width(), child.get_height(),
                                child.is_viewable()))
            find_native(child)

    find_native(win.get_window())
    print(f"native_child_windows: {natives}")

    width, height = win.get_allocated_width(), win.get_allocated_height()
    wrong = []
    counted = 0
    for gx in range(10):
        for gy in range(10):
            x, y = int((gx + 0.5) * width / 10), int((gy + 0.5) * height / 10)
            if any(r and r[0] <= x < r[0] + r[2] and r[1] <= y < r[1] + r[3] for r in toast_rects):
                continue
            counted += 1
            target = owner(pick(win.get_window(), x, y))
            chain = describe(target)
            if chain.split("<")[0] in ("ToastOverlay", "Toast", "Revealer", "Box") and "Toast" in chain:
                wrong.append((x, y, chain))
    print(f"hit_test_points: {counted}")
    print(f"hit_test_wrong_count: {len(wrong)}")
    print(f"hit_test_wrong_sample: {wrong[:3]}")
    stack_hits = []
    if rect:
        x0, y0, w0, h0 = rect
        for x in range(x0, x0 + w0, max(1, w0 // 8)):
            for y in range(y0, y0 + h0, max(1, h0 // 4)):
                if any(r and r[0] <= x < r[0] + r[2] and r[1] <= y < r[1] + r[3] for r in toast_rects):
                    continue
                stack_hits.append((x, y, describe(owner(pick(win.get_window(), x, y)))))
    print(f"stack_area_outside_toasts: {stack_hits[:6]} (of {len(stack_hits)})")

    if args.xtest:
        install_press_log()
        overlay_toasts = list(overlay._toasts.values())
        if overlay_toasts:
            toast = overlay_toasts[-1]
            close = [w for w in _all(toast) if isinstance(w, Gtk.Button)][-1]
            r = widget_rect(close, win)
            before = len(win._toasts.visible)
            tr = widget_rect(toast, win)
            body = xtest_click(win, tr[0] + tr[2] // 3, tr[1] + tr[3] // 2)  # on the text
            print(f"xtest_toast_body_press_log: {body}")
            log = xtest_click(win, r[0] + r[2] // 2, r[1] + r[3] // 2)
            print(f"xtest_dismiss_press_log: {log}")
            print(f"xtest_dismiss_removed_one: {len(win._toasts.visible) == before - 1}")
        header_buttons = [w for w in _all(win._header) if isinstance(w, Gtk.Button) and w.get_mapped()]
        rightmost = max(header_buttons, key=lambda b: widget_rect(b, win)[0]) if header_buttons else None
        if rightmost is not None:
            r = widget_rect(rightmost, win)
            hits = []
            rightmost.connect("clicked", lambda b: hits.append(b))
            if isinstance(rightmost, Gtk.ToggleButton):
                rightmost.connect("toggled", lambda b: hits.append(b))
            log = xtest_click(win, r[0] + r[2] // 2, r[1] + r[3] // 2)
            print(f"xtest_header_button: {type(rightmost).__name__} at {r} reacted={bool(hits)} log={log}")
            if isinstance(rightmost, Gtk.ToggleButton) and rightmost.get_active():
                rightmost.set_active(False)
                pump(0.3)
        width, height = win.get_allocated_width(), win.get_allocated_height()
        toast_rects = [widget_rect(t, win) for t in overlay._toasts.values()]
        grid = []
        for gx in range(6, 10):  # the right part of the window, where the toast column is
            for gy in range(1, 10):
                x, y = int((gx + 0.5) * width / 10), int((gy + 0.5) * height / 10)
                if any(r and r[0] - 4 <= x < r[0] + r[2] + 4 and r[1] - 4 <= y < r[1] + r[3] + 4 for r in toast_rects):
                    continue
                log = xtest_click(win, x, y)
                press = next((entry for entry in log if entry.startswith("button-press")), "no press")
                grid.append((x, y, press.split("->", 1)[-1].split(" grab=")[0]))
        swallowed = [g for g in grid if g[2].startswith(("ToastOverlay", "Toast", "Revealer", "Box<Toast"))]
        print(f"xtest_grid_points: {len(grid)}")
        print(f"xtest_grid_swallowed_by_toasts: {len(swallowed)} {swallowed[:4]}")
        print(f"xtest_grid_targets: {sorted(set(g[2] for g in grid))[:8]}")
        for g in grid:
            print(f"xtest_point: {g[0]},{g[1]} {g[2]}")
        print(f"xtest_heartbeat_worst_gap_s: {heartbeat.worst:.3f}")

    # Clicks: another host in the sidebar, then the header's main menu button.
    other = win.tree.find_host_by_alias("db-primary")
    view = win.tree_view
    if view is not None and other is not None:
        path = None
        model = view.get_model()

        def find(model, p, it, _data=None):
            nonlocal path
            if path is None and other.alias in str(model.get_value(it, 0)) + str(model.get_value(it, 1)):
                path = p.copy()
            return False

        try:
            view.expand_all()
            pump(0.2)
            model.foreach(find)
        except Exception as exc:
            print(f"tree_find_error: {type(exc).__name__}")
        if path is not None:
            cell = view.get_cell_area(path, view.get_column(0))
            bx, by = view.convert_bin_window_to_widget_coords(cell.x + 10, cell.y + cell.height // 2)
            tx, ty = view.translate_coordinates(win, bx, by)
            got = click(view, win, tx, ty)
            selected = win.tree.selected_host()
            print(f"click_tree_target: {got}")
            print(f"click_tree_selected: {selected.alias if selected else None}")
        else:
            print("click_tree: db-primary row not found")
    print(f"after_clicks: {audit(win)}")

    # The toast's own dismiss button.
    toasts = list(overlay._toasts.values())
    if toasts:
        toast = toasts[-1]
        buttons = []

        def walk(widget):
            if isinstance(widget, Gtk.Button):
                buttons.append(widget)
            if isinstance(widget, Gtk.Container):
                for c in widget.get_children():
                    walk(c)

        walk(toast)
        close = buttons[-1]
        r = widget_rect(close, win)
        before = len(win._toasts.visible)
        got = click(close, win, r[0] + r[2] // 2, r[1] + r[3] // 2)
        pump(1.0)
        print(f"click_dismiss_target: {got}")
        print(f"dismiss_removed_one: {len(win._toasts.visible) == before - 1}")
    print(f"final: {audit(win)}")
    sandbox.close()
    tmp.cleanup()
    return 0


if __name__ == "__main__":
    sys.exit(main())
