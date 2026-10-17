"""Small diagnostic helper for listing loaded SSH config groups."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Optional

from ssh_manager.config import ConfigManager


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Print loaded SSH Manager config groups.")
    parser.add_argument(
        "--base-dir",
        default="~/.ssh/conf.d",
        help="Root directory or config file to inspect.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    manager = ConfigManager(Path(args.base_dir).expanduser())
    print(f"Base dir: {manager.base_dir}")
    for parent, children in manager.tree.items():
        print(f"[parent] {parent}")
        for child, group in children.items():
            aliases = ", ".join(host.alias for host in group.hosts) or "-"
            print(f"  [child] {child} -> hosts: {aliases}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
