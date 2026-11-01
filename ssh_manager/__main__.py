"""``python3 -m ssh_manager`` (the command before the rename): starts Ophion (``python3 -m ophion``)."""

from __future__ import annotations

import sys

from ophion.gtk_main import main

if __name__ == "__main__":  # pragma: no cover
    print("ssh_manager is now ophion: start the app with  python3 -m ophion", file=sys.stderr)
    sys.exit(main())
