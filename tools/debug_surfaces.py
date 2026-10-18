#!/usr/bin/env python3
"""Find widgets whose background comes from the GTK theme instead of our tokens.

Usage::

    python3 tools/debug_surfaces.py                      # every installed theme × mode
    python3 tools/debug_surfaces.py --themes Adwaita:dark --modes dark --verbose
    python3 tools/debug_surfaces.py --interactive        # open GTK_DEBUG=interactive

For every GTK theme (``GTK_THEME``) the script starts a child process (GTK reads
the theme once, at start-up) and, for every app theme mode (System with a light
and a dark desktop preference, Light, Dark), builds the main window, its
empty-state variants and every dialog, focused and unfocused (``:backdrop``).
It walks each widget tree, including internal children and popovers, and
prints the CSS node path, state flags and computed ``background-color`` /
``background-image`` of every widget. The nodes TreeView pushes with
``style_context.save()`` while it paints rows and the empty area below them
(``treeview.view.cell``) are probed too, in the states rows can be in.

A widget is *flagged* when its computed background is opaque and not one of
the palette's tokens, or when it has a ``background-image``. The exit status
is the number of flagged nodes (0 = clean). Nothing is mapped on screen; set
``--xvfb`` (or run without ``DISPLAY``) to wrap each child in ``xvfb-run``.

The run uses a throw-away HOME and an in-memory GSettings backend, so it never
touches the user's settings or desktop colour scheme.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterable, Iterator, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parent.parent

CANDIDATE_THEMES = ("Adwaita", "Adwaita:dark", "Yaru", "Yaru-dark", "Arc-Dark", "HighContrast")
MODES = ("system-light", "system-dark", "light", "dark")
THEME_DIRS = (
    Path("/usr/share/themes"),
    Path("/usr/local/share/themes"),
    Path.home() / ".themes",
    Path.home() / ".local/share/themes",
)


def theme_installed(spec: str) -> bool:
    """``Adwaita`` and ``HighContrast`` are built into GTK; others need a gtk-3.0 directory."""

    name = spec.split(":", 1)[0]
    if name in ("Adwaita", "HighContrast"):
        return True
    return any((base / name / "gtk-3.0").is_dir() for base in THEME_DIRS)


# ====================================================================== child
def _child(theme: str, modes: Sequence[str], verbose: bool, as_json: bool) -> int:
    os.environ["GTK_THEME"] = theme
    os.environ["GSETTINGS_BACKEND"] = "memory"
    sys.path.insert(0, str(ROOT / "tools"))
    sys.path.insert(0, str(ROOT))
    import ui_snapshots as snap  # sandboxes HOME/XDG on import

    from gi.repository import Gio  # type: ignore

    snap._pin_resolution()
    snap._patch_toplevel_mapping()
    snap._stub_terminal_io()

    from ssh_manager import gtk_app

    app = gtk_app.SSHManagerApplication(snap._SANDBOX / "inventory")
    app.set_flags(Gio.ApplicationFlags.NON_UNIQUE)
    app.register(None)

    populated = snap._SANDBOX / "surfaces-inventory"
    snap._write_inventory(populated, populated=True)
    empty = snap._SANDBOX / "surfaces-empty"
    snap._write_inventory(empty, populated=False)

    results: List[dict] = []
    for mode in modes:
        for scene, window, dark in scenes(app, snap, mode, populated, empty):
            for backdrop in (False, True):
                set_backdrop(window, backdrop)
                snap._pump()
                findings = list(audit(window, dark))
                flagged = [f for f in findings if f["flagged"]]
                results.append(
                    {"theme": theme, "mode": mode, "scene": scene, "backdrop": backdrop,
                     "nodes": len(findings), "flagged": flagged}
                )
                label = f"{theme:<14} {mode:<13} {scene:<24} {'backdrop' if backdrop else 'focused ':<8}"
                if not as_json:
                    print(f"{label} {len(findings):4d} nodes, {len(flagged)} flagged")
                    for f in (findings if verbose else flagged):
                        mark = "!!" if f["flagged"] else "  "
                        print(f"   {mark} {f['path']}  [{f['state']}]  {f['background']}")
            window.destroy()
            snap._pump()
    if as_json:
        print(json.dumps(results, indent=1))
    return min(sum(len(r["flagged"]) for r in results), 255)


def scenes(app, snap, mode: str, populated: Path, empty: Path) -> Iterator[Tuple[str, object, bool]]:
    """Yield (name, toplevel, is_dark) for the main window states and each dialog."""

    from ssh_manager.gtkui.theme.loader import get_theme_manager

    theme_mode = mode.split("-", 1)[0]
    snap.set_desktop_color_scheme(mode == "system-dark")

    def window_for(base: Path):
        return snap.make_window_with_mode(app, base, theme_mode)

    def dark() -> bool:
        return get_theme_manager().is_dark

    window = window_for(populated)
    snap._select_first_host(window)
    snap._pump()
    yield "main", window, dark()

    window = window_for(populated)
    window._sidebar.search_entry.set_text("zz-no-such-host")
    window.tree.rebuild(refresh=False)  # search-changed is debounced
    snap._pump()
    yield "main-no-results", window, dark()

    window = window_for(empty)
    yield "main-first-run", window, dark()

    for name, factory in dialog_factories(populated):
        owner = window_for(populated)
        try:
            dialog = factory(owner)
        except Exception as exc:  # pragma: no cover - diagnostic tool
            print(f"   (could not build {name}: {exc!r})")
            owner.destroy()
            continue
        snap._pump()
        yield name, dialog, dark()
        owner.destroy()


def dialog_factories(base: Path):
    from ssh_manager.config_viewer import ConfigViewerDialog
    from ssh_manager.gtk_app import CredentialManagerDialog, HostDialog, PreferencesDialog, VaultDialog
    from ssh_manager.gtkui.controllers.shortcuts import build_shortcuts_window
    from ssh_manager.gtkui.dialogs.confirm import ConfirmDialog
    from ssh_manager.gtkui.dialogs.password import PasswordDialog
    from ssh_manager.gtkui.dialogs.prompt import PromptDialog

    def first_host(window):
        for groups in window.manager.tree.values():
            for group in groups.values():
                if group.hosts:
                    return group.hosts[0]
        return None

    def host(window):
        h = first_host(window)
        return HostDialog(window, h, credentials=window._credential_store.list(),
                          metadata=h.metadata if h else {}, profiles=window._profiles)

    def config(window):
        path = next(p for p in sorted(base.rglob("*.conf")))
        return ConfigViewerDialog(window, path)

    return (
        ("dialog-host", host),
        ("dialog-settings", lambda w: PreferencesDialog(w, True, 1.0, 11, w._settings)),
        ("dialog-credentials", lambda w: CredentialManagerDialog(w, w._credential_store)),
        ("dialog-vault", lambda w: VaultDialog(w, w._vault)),
        ("dialog-config-viewer", config),
        ("dialog-password", lambda _w: PasswordDialog()),
        ("dialog-prompt", lambda w: PromptDialog(w, "Rename", "Name", default="value")),
        ("dialog-confirm", lambda w: ConfirmDialog(w, "Delete host?", "This cannot be undone.")),
        ("shortcuts-window", build_shortcuts_window),
    )


def set_backdrop(window, backdrop: bool) -> None:
    from gi.repository import Gtk  # type: ignore

    if backdrop:
        window.set_state_flags(Gtk.StateFlags.BACKDROP, False)
    else:
        window.unset_state_flags(Gtk.StateFlags.BACKDROP)


# ====================================================================== audit
def token_colors(dark: bool) -> List[Tuple[float, float, float]]:
    """Opaque surface tokens of the palette (foreground-only roles excluded)."""

    from gi.repository import Gdk  # type: ignore

    from ssh_manager.gtkui.theme.tokens import palette_for

    colors = []
    for value in palette_for(dark).surface_colors():
        rgba = Gdk.RGBA()
        if rgba.parse(value) and rgba.alpha >= 0.999:
            colors.append((rgba.red, rgba.green, rgba.blue))
    return colors


def is_token(rgba, tokens: Iterable[Tuple[float, float, float]]) -> bool:
    channels = (rgba.red, rgba.green, rgba.blue)
    return any(all(abs(a - b) <= 1.5 / 255 for a, b in zip(channels, token)) for token in tokens)


def hex_of(rgba) -> str:
    r, g, b = (round(c * 255) for c in (rgba.red, rgba.green, rgba.blue))
    alpha = "" if rgba.alpha >= 0.999 else f" α{rgba.alpha:.2f}"
    return f"#{r:02X}{g:02X}{b:02X}{alpha}"


def node_name(context) -> str:
    """``treeview.view.relay-tree`` from the style context's own node."""

    text = context.to_string(0).splitlines()[0].strip()
    text = text.strip("[]")
    return text.split(":", 1)[0]


def state_names(flags) -> str:
    from gi.repository import Gtk  # type: ignore

    names = [flag.first_value_nick for flag in Gtk.StateFlags.__flags_values__.values()
             if flag and flags & flag and flag.first_value_nick != "dir-ltr"]
    return ",".join(sorted(set(names) - {"dir-rtl"})) or "normal"


def walk(widget) -> Iterator[Tuple[object, List[str]]]:
    """Depth-first over ``widget`` (internal children and popovers included)."""

    from gi.repository import Gtk  # type: ignore

    seen = set()

    def visit(node, ancestry: List[str]):
        if id(node) in seen:
            return
        seen.add(id(node))
        path = ancestry + [node_name(node.get_style_context())]
        yield node, path
        children: List = []
        if isinstance(node, Gtk.Container):
            node.forall(children.append)
        if isinstance(node, Gtk.Window) and node.get_titlebar() is not None:
            children.append(node.get_titlebar())
        if isinstance(node, Gtk.MenuButton):
            children += [p for p in (node.get_popover(), node.get_popup()) if p is not None]
        for value in list(getattr(node, "__dict__", {}).values()):
            if isinstance(value, (Gtk.Popover, Gtk.Menu)):
                children.append(value)
        for child in children:
            yield from visit(child, path)

    yield from visit(widget, [])


def describe(context, state, tokens) -> Tuple[str, bool]:
    color = context.get_property("background-color", state)
    image = context.get_property("background-image", state)
    text = hex_of(color) + (" + background-image" if image is not None else "")
    flagged = image is not None or (color.alpha >= 0.999 and not is_token(color, tokens))
    return text, flagged


def audit(window, dark: bool) -> Iterator[dict]:
    from gi.repository import Gtk  # type: ignore

    tokens = token_colors(dark)
    for widget, path in walk(window):
        if not widget.get_visible() and not isinstance(widget, (Gtk.Popover, Gtk.Menu)):
            continue
        context = widget.get_style_context()
        if node_name(context).startswith("vte-terminal"):
            continue  # VTE paints the user's terminal color scheme, not a UI surface
        state = context.get_state()
        text, flagged = describe(context, state, tokens)
        yield {"path": " > ".join(path), "state": state_names(state), "background": text, "flagged": flagged}
        if isinstance(widget, Gtk.TreeView):
            yield from _transient_cells(context, path, state, tokens)


def _transient_cells(context, path: List[str], base_state, tokens) -> Iterator[dict]:
    """Probe the nodes TreeView paints rows and the empty area with."""

    from gi.repository import Gtk  # type: ignore

    S = Gtk.StateFlags
    for extra in (S.NORMAL, S.PRELIGHT, S.SELECTED, S.SELECTED | S.FOCUSED, S.SELECTED | S.PRELIGHT, S.DROP_ACTIVE):
        state = base_state | extra
        context.save()
        context.add_class("cell")
        context.set_state(state)
        try:
            text, flagged = describe(context, state, tokens)
            name = node_name(context)
        finally:
            context.restore()
        yield {"path": " > ".join(path + [name + " (transient)"]), "state": state_names(state),
               "background": text, "flagged": flagged}


# ===================================================================== parent
def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--themes", default=",".join(CANDIDATE_THEMES),
                        help="comma-separated GTK_THEME values (uninstalled ones are skipped)")
    parser.add_argument("--modes", default=",".join(MODES), help="comma-separated: " + ", ".join(MODES))
    parser.add_argument("--verbose", action="store_true", help="print every node, not only flagged ones")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--xvfb", action="store_true", help="run each theme under xvfb-run")
    parser.add_argument("--interactive", action="store_true",
                        help="open the app with GTK_DEBUG=interactive for manual inspection")
    parser.add_argument("--child", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    modes = [m for m in args.modes.split(",") if m]

    if args.child:
        return _child(args.child, modes, args.verbose, args.json)

    if args.interactive:
        env = dict(os.environ, GTK_DEBUG="interactive")
        theme = args.themes.split(",")[0] if args.themes != ",".join(CANDIDATE_THEMES) else None
        if theme:
            env["GTK_THEME"] = theme
        return subprocess.call([sys.executable, "-m", "ssh_manager.gtk_main"], cwd=ROOT, env=env)

    use_xvfb = args.xvfb or not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY")
    if use_xvfb and not shutil.which("xvfb-run"):
        print("xvfb-run is not installed and there is no display.", file=sys.stderr)
        return 2
    total = 0
    for theme in (t for t in args.themes.split(",") if t):
        if not theme_installed(theme):
            print(f"{theme:<14} not installed, skipped")
            continue
        command = [sys.executable, str(Path(__file__).resolve()), "--child", theme, "--modes", ",".join(modes)]
        command += ["--verbose"] if args.verbose else []
        command += ["--json"] if args.json else []
        if use_xvfb:
            command = ["xvfb-run", "-a", "-s", "-screen 0 1920x1080x24"] + command
        status = subprocess.call(command, cwd=ROOT)
        total += status
    if not args.json:
        print("clean: no opaque non-token backgrounds" if total == 0 else f"{total} flagged node(s)")
    return 0 if total == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
