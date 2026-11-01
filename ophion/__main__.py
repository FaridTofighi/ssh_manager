"""Executable package entry point for the GTK SSH manager."""

from __future__ import annotations

import sys

from .gtk_main import main


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
