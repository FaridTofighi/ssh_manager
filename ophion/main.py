"""CLI entry point for the SSH manager application."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Optional

from .config import ConfigManager
from .launcher import ConnectionLauncher
from .ui import SSHManagerApp


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Browse grouped SSH configs and launch connections."
    )
    parser.add_argument(
        "--base-dir",
        default="~/.ssh/conf.d",
        help="Root directory containing grouped SSH configs (default: ~/.ssh/conf.d)",
    )
    parser.add_argument(
        "--terminal",
        default=None,
        help="Terminal emulator command to use for launching sessions.",
    )
    return parser.parse_args(argv)


def run_app(base_dir: Path, terminal_override: Optional[str]) -> int:
    manager = ConfigManager(base_dir)
    launcher = ConnectionLauncher(terminal_override=terminal_override)
    app = SSHManagerApp(manager=manager, launcher=launcher, base_dir=str(base_dir))
    app.run()
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    base_dir = Path(os.path.expanduser(args.base_dir)).resolve()
    return run_app(base_dir, args.terminal)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
