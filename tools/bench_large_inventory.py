#!/usr/bin/env python3
"""Time the GTK app's common operations on a large inventory (dev tool; needs a display).

Usage::

    python3 tools/bench_large_inventory.py [--repeat N] [--profile NAME] [--small]

It builds a throw-away app-store inventory (``tests/large_inventory.py``: 3 top
groups × 333 deep child groups × 4 hosts = 3,996 hosts, about 1,000 groups) in a
temporary HOME (``tests/gtk_window_fixture.WindowSandbox``; nothing real is
read or written) and prints the median time of each operation. Dialogs are
built but never run: ``run()`` returns Cancel at once. ``--profile NAME`` prints
the cProfile top 25 of one operation; ``--small`` uses the 10-host sample
inventory instead (the budget tests' reference).
"""

from __future__ import annotations

import argparse
import cProfile
import pstats
import statistics
import sys
import time
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gi  # noqa: E402

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gtk  # type: ignore  # noqa: E402


def pump() -> None:
    while Gtk.events_pending():
        Gtk.main_iteration_do(False)


def operations(sandbox):
    """``(name, callable)`` in display order; each callable undoes what it did (or is harmless to repeat)."""

    from ophion.gtkui.dialogs.group_picker import GroupPickerDialog
    from ophion.inventory.manager import StoreConfigManager

    win = sandbox.window
    hosts = win.hosts
    store = sandbox.store
    rows = store.list_hosts()
    ssh_row = next(row for row in rows if row.protocol == "ssh" and row.alias.startswith("node-"))
    rdp_row = next((row for row in rows if row.protocol == "rdp"), None)

    def entry(row):
        return next(host for groups in win.manager.tree.values() for group in groups.values()
                    for host in group.hosts if host.id == row.id)

    def build(host=None, **kwargs):
        hosts.host_dialog(host, **kwargs).destroy()

    def prompt_new_host():
        ssh = entry(ssh_row)
        win.tree.select_host(ssh.parent_group, ssh.child_group, ssh.alias)  # the new host's group: no picker
        shown = []
        with mock.patch.object(type(hosts), "run_host_dialog",
                               lambda _self, dialog, *_a: (shown.append(dialog), dialog.destroy(), (None, None, None))[-1]):
            hosts.prompt_new_host()
        assert shown, "the New Host dialog was not built"

    def add_host_and_select():
        from ophion.config import HostEntry

        ssh = entry(ssh_row)
        host = HostEntry(alias=f"bench-{time.monotonic_ns()}", options={"hostname": "10.9.9.9"},
                         parent_group=ssh.parent_group, child_group=ssh.child_group)
        ok, message = win.manager.add_host(ssh.parent_group, ssh.child_group, host)
        assert ok, message
        win.tree.rebuild(refresh=True)
        win.tree.select_host(ssh.parent_group, ssh.child_group, host.alias)
        win.manager.delete_host(ssh.parent_group, ssh.child_group, host.alias)

    def search_typing():
        for text in ("n", "no", "node-01", "node-012", ""):
            win._sidebar.search_entry.set_text(text)
            win.tree.rebuild(refresh=False)

    def move_hosts():
        ssh = entry(ssh_row)
        targets = [(parent, child) for parent, groups in win.manager.tree.items() for child in groups
                   if (parent, child) != (ssh.parent_group, ssh.child_group)]
        parent, child = targets[len(targets) // 2]
        ok, message = win.manager.move_hosts([(ssh.parent_group, ssh.child_group, ssh.alias)], parent, child)
        assert ok, message
        win.tree.rebuild(refresh=True)
        ok, message = win.manager.move_hosts([(parent, child, ssh.alias)], ssh.parent_group, ssh.child_group)
        win.tree.rebuild(refresh=True)

    def startup():
        from ophion.gtk_app import SSHManagerWindow
        from ophion.settings import AppSettings

        manager = StoreConfigManager(store)
        window = SSHManagerWindow(sandbox.app, manager.base_dir, AppSettings.load(), manager=manager)
        window.host_page.latency.enabled = False
        pump()
        window.destroy()

    ops = [
        ("manager.refresh", lambda: win.manager.refresh()),
        ("tree.rebuild(refresh=True)", lambda: win.tree.rebuild(refresh=True)),
        ("tree.rebuild()", lambda: win.tree.rebuild()),
        ("tunnel_hosts", lambda: hosts.tunnel_hosts()),
        ("host_dialog() new host", lambda: build()),
        ("GroupPickerDialog", lambda: GroupPickerDialog(win, win.manager, "Bench").destroy()),
        ("prompt_new_host (until shown)", prompt_new_host),
        ("edit SSH host dialog", lambda: build(entry(ssh_row))),
        ("duplicate host dialog", lambda: build(entry(ssh_row), for_copy=True)),
        ("add host → rebuild → select", add_host_and_select),
        ("move host (there and back)", move_hosts),
        ("search typing (5 keystrokes)", search_typing),
        ("Preferences dialog", lambda: win.app_dialogs.open_settings()),
        ("Credentials dialog", lambda: win.app_dialogs.open_credentials()),
        ("_check_external_changes (no change)", lambda: win._check_external_changes()),
        ("startup (new window)", startup),
    ]
    if rdp_row is not None:
        ops.insert(8, ("edit RDP host dialog", lambda: build(entry(rdp_row))))
    return ops


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--profile", default="", help="print the cProfile top 25 of the operation with this name")
    parser.add_argument("--small", action="store_true", help="the 10-host sample inventory only")
    args = parser.parse_args(argv)

    from tests.gtk_window_fixture import WindowSandbox
    from tests.large_inventory import populate

    with mock.patch.object(Gtk.Dialog, "run", lambda _dialog: Gtk.ResponseType.CANCEL):
        sandbox = WindowSandbox(store=True)
        try:
            total = len(sandbox.store.list_hosts())
            if not args.small:
                populate(sandbox.store)
                total = len(sandbox.store.list_hosts())
            sandbox.window.manager.refresh()
            sandbox.window.tree.rebuild()
            pump()
            groups = sum(len(children) for children in sandbox.window.manager.tree.values())
            print(f"inventory: {total} hosts, {groups} groups; median of {args.repeat} runs\n")
            print(f"| {'operation':40} | {'ms':>8} |")
            print(f"|{'-' * 42}|{'-' * 10}|")
            for name, run in operations(sandbox):
                if args.profile and args.profile != name:
                    continue
                times = []
                for _ in range(max(1, args.repeat)):
                    started = time.perf_counter()
                    run()
                    times.append((time.perf_counter() - started) * 1000)
                    pump()
                print(f"| {name:40} | {statistics.median(times):8.1f} |", flush=True)
                if args.profile:
                    profile = cProfile.Profile()
                    profile.runcall(run)
                    pstats.Stats(profile).sort_stats("cumulative").print_stats(25)
        finally:
            sandbox.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
