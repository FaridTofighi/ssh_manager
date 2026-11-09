"""R5: the API surface only grows. Every name of the snapshot is still there, with the same signature."""

import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

import api_surface  # noqa: E402

BASELINE = Path(__file__).with_name("api_surface.json")
ALLOWLIST = Path(__file__).with_name("api_allowlist.txt")


class ApiSurfaceTests(unittest.TestCase):
    def test_the_surface_contains_the_baseline(self) -> None:
        problems = api_surface.compare(json.loads(BASELINE.read_text(encoding="utf-8")), api_surface.surface(),
                                       api_surface.load_allowlist(ALLOWLIST))
        self.assertEqual(problems, [], "\n".join(problems))

    def test_the_comparison_sees_a_removed_or_changed_name(self) -> None:
        """Not vacuous: a missing method, a changed signature and a missing module are reported."""

        before = {"m": {"names": {"f": "function(a, b=)", "C": "class"},
                        "classes": {"C": {"run": "method(self, x)", "_state": "instance"}}},
                  "gone": {"names": {}, "classes": {}}}
        after = copy.deepcopy(before)
        del after["gone"]
        after["m"]["names"]["f"] = "function(a)"
        del after["m"]["classes"]["C"]["run"]
        problems = api_surface.compare(before, after, set())
        self.assertEqual(sorted(problems), ["m.f changed: function(a, b=) -> function(a)", "m:C.run is gone",
                                            "module gone is gone"])
        self.assertEqual(api_surface.compare(before, after, {"gone", "m.f", "m:C.run"}), [])  # the allowlist


if __name__ == "__main__":
    unittest.main()
