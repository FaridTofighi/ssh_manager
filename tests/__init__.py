"""The test suite. Guards that keep every test inside the app (pytest and unittest both import this)."""

import os

# A test that marks a fake session connected starts its remote statistics: they ran the real ssh to
# made-up hosts every 10 s, also from sessions of earlier tests. Sessions in tests fetch none.
os.environ.setdefault("OPHION_REMOTE_STATS", "off")
