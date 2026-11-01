"""GTK entry point for the SSH manager."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .gtk_app import SSHManagerApplication, show_legacy_app_running


def parse_args(argv: list[str] | None = None) -> tuple[argparse.Namespace, list[str]]:
    parser = argparse.ArgumentParser(description="GTK SSH manager with embedded terminals")
    parser.add_argument(
        "--base-dir",
        default=None,
        help="Open a folder of grouped SSH config files (the layout of older versions) instead of the "
             "SSH Manager inventory; changes are written to those files",
    )
    parser.add_argument(
        "--inventory",
        default=None,
        help="Inventory database to use (default: $XDG_DATA_HOME/ssh-manager/inventory.db)",
    )
    parser.add_argument("--rdp", metavar="HOST[:PORT]", default=None,
                        help="Open a Remote Desktop session to HOST without saving it (or pass an rdp:// address)")
    parser.add_argument("--user", default="", help="User for --rdp (DOMAIN\\user or user@domain)")
    parser.add_argument("--domain", default="", help="Windows domain for --rdp")
    args, extras = parser.parse_known_args(argv)
    return args, extras


def startup_target(args: argparse.Namespace, extras: list[str]):
    """The unsaved RDP host asked for on the command line, ``(host, error)``; ``(None, "")`` when none."""

    from .config import HostEntry
    from .protocols.urls import cli_rdp_target

    target = args.rdp or next((item for item in extras if item.lower().startswith("rdp://")), "")
    if not target:
        return None, ""
    url, reason = cli_rdp_target(target, args.user, args.domain)
    if url is None:
        return None, reason
    return HostEntry(alias=url.label(), options=url.options(), metadata={"rdp": {"v": 1}},
                     parent_group="Quick Connect", child_group="", protocol="rdp"), ""


def main(argv: list[str] | None = None) -> int:
    args, extras = parse_args(argv)
    base_dir = Path(args.base_dir).expanduser().resolve() if args.base_dir else None
    inventory = Path(args.inventory).expanduser().resolve() if args.inventory else None
    host, error = startup_target(args, extras)
    if error:
        print(f"ssh-manager: {error}", file=sys.stderr)
        return 2
    extras = [item for item in extras if not item.lower().startswith("rdp://")]
    from .legacy_paths import BLOCKED, migrate

    moved = migrate()  # SSH Manager's folders → Ophion's, before anything opens them
    if moved.status == BLOCKED:
        show_legacy_app_running()
        return 0  # nothing changed
    app = SSHManagerApplication(base_dir, inventory=inventory, startup_host=host, migration=moved)

    gtk_argv = (sys.argv[:1] if argv is None else ["ssh-manager"]) + extras
    return app.run(gtk_argv)
