"""Short workers pause automatic cycle collection (so GTK objects are never finalized on them)."""

import gc
import threading
import unittest

from ophion import gc_guard


class GcGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.addCleanup(gc.enable if gc.isenabled() else gc.disable)
        gc.enable()

    def test_nested_and_overlapping_workers(self) -> None:
        inside = threading.Event()
        release = threading.Event()

        @gc_guard.worker
        def slow() -> str:
            inside.set()
            release.wait(5)
            return "done"

        thread = threading.Thread(target=slow)
        thread.start()
        self.assertTrue(inside.wait(5))
        self.assertFalse(gc.isenabled())
        with gc_guard.no_collection():
            release.set()
            thread.join(5)
            self.assertFalse(gc.isenabled())  # still one user left
        self.assertTrue(gc.isenabled())
        self.assertEqual(gc_guard.worker(lambda: 7)(), 7)

    def test_errors_restore_and_disabled_stays_disabled(self) -> None:
        with self.assertRaises(RuntimeError), gc_guard.no_collection():
            raise RuntimeError
        self.assertTrue(gc.isenabled())
        gc.disable()
        with gc_guard.no_collection():
            pass
        self.assertFalse(gc.isenabled())


if __name__ == "__main__":
    unittest.main()
