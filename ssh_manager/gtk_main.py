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
        default="~/.ssh/conf.d",
        help="Directory containing grouped SSH configs (default: ~/.ssh/conf.d)",
    )
    args, extras = parser.parse_known_args(argv)
    return args, extras


def main(argv: list[str] | None = None) -> int:
    args, extras = parse_args(argv)
    base_dir = Path(args.base_dir).expanduser().resolve()
    app = SSHManagerApplication(base_dir)

    gtk_argv = (sys.argv[:1] if argv is None else ["ssh-manager"]) + extras
    return app.run(gtk_argv)
