"""GTK entry point for the SSH manager."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .gtk_app import SSHManagerApplication


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
    args, extras = parser.parse_known_args(argv)
    return args, extras


def main(argv: list[str] | None = None) -> int:
    args, extras = parse_args(argv)
    base_dir = Path(args.base_dir).expanduser().resolve() if args.base_dir else None
    inventory = Path(args.inventory).expanduser().resolve() if args.inventory else None
    app = SSHManagerApplication(base_dir, inventory=inventory)

    gtk_argv = (sys.argv[:1] if argv is None else ["ssh-manager"]) + extras
    return app.run(gtk_argv)
