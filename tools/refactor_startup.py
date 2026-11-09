"""Startup time for tools/refactor_bench.py: build the main window on a 1,200-host app store, best of 5.

Runs on the private X server (tools/run_gtk_tests.py) in a temporary HOME (the tests' WindowSandbox); prints
``startup_ms <milliseconds>``.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import gi  # noqa: E402

gi.require_version("Gtk", "3.0")

from tests.gtk_window_fixture import WindowSandbox  # noqa: E402


def main() -> int:
    from ophion.gtk_app import SSHManagerWindow
    from ophion.inventory.manager import StoreConfigManager
    from ophion.inventory.store import NewHost
    from ophion.settings import AppSettings

    sandbox = WindowSandbox(store=True)
    store = sandbox.store
    groups = [store.create_group(None, f"g{n}") for n in range(12)]
    store.add_hosts_bulk(NewHost(groups[n % 12].id, f"bulk-{n:04d}", f"10.{n // 256}.{n % 256}.1", 22, "u")
                         for n in range(1200))
    base = sandbox.window.manager.base_dir
    sandbox.window.destroy()
    times = []
    for _ in range(5):
        manager = StoreConfigManager(store)
        started = time.perf_counter()
        window = SSHManagerWindow(sandbox.app, base, AppSettings.load(), manager=manager)
        times.append(time.perf_counter() - started)
        window.destroy()
    store.close()
    sandbox.show_patch.stop()
    sandbox.env.stop()
    sandbox.tmp.cleanup()
    print(f"startup_ms {min(times) * 1000:.1f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
