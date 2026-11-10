"""Layer boundaries (ratchets) from the import graph of ``tools/arch_metrics.py`` (source only, nothing imported).

(a) Modules outside ``ophion/gtkui`` import ``gi`` / ``ophion.gtkui`` at module level only when
``tests/architecture_allowlist.txt`` lists them; (b) an entry whose violation is gone fails too, until it is
removed; (c) runtime import cycles stay within the baseline's; (d) no class grows beyond its baseline size.
"""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import arch_metrics  # noqa: E402

ALLOWLIST = Path(__file__).with_name("architecture_allowlist.txt")
BASELINE = Path(__file__).parent / "refactor" / "metrics_baseline.json"


def load_allowlist(path: Path = ALLOWLIST) -> set:
    entries = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        entry = line.split("#", 1)[0].strip()
        if entry:
            entries.add(entry)
    return entries


def boundary_problems(core_gtk, allowed) -> list:
    problems = [f"{module} imports {', '.join(sorted(core_gtk[module]))} at module level (not allowlisted)"
                for module in sorted(set(core_gtk) - allowed)]
    problems += [f"{module} no longer imports GTK at module level: remove it from {ALLOWLIST.name}"
                 for module in sorted(allowed - set(core_gtk))]
    return problems


def cycle_problems(current, baseline) -> list:
    known = [set(cycle) for cycle in baseline]
    return [f"runtime import cycle not in the baseline: {cycle}"
            for cycle in current if not any(set(cycle) <= old for old in known)]


def class_problems(current, baseline) -> list:
    problems = []
    for name, now in sorted(current.items()):
        before = baseline.get(name)
        if before is not None and now["lines"] > before["lines"]:
            problems.append(f"class {name} grew: {now['lines']} > {before['lines']} lines")
        elif before is None and now["lines"] > arch_metrics.BIG_CLASS:
            problems.append(f"new class {name} has {now['lines']} lines (> {arch_metrics.BIG_CLASS})")
    return problems


class ArchitectureBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.current = arch_metrics.measure()
        cls.baseline = json.loads(BASELINE.read_text(encoding="utf-8"))

    def test_core_modules_do_not_import_gtk(self) -> None:
        problems = boundary_problems(self.current["core_gtk"], load_allowlist())
        self.assertEqual(problems, [], "\n".join(problems))

    def test_runtime_cycles_stay_within_the_baseline(self) -> None:
        problems = cycle_problems(self.current["runtime_sccs"], self.baseline["runtime_sccs"])
        self.assertEqual(problems, [], "\n".join(problems))

    def test_classes_do_not_grow(self) -> None:
        problems = class_problems(self.current["classes"], self.baseline["classes"])
        self.assertEqual(problems, [], "\n".join(problems))

    def test_the_ratchets_see_violations(self) -> None:
        """Not vacuous: a new violation, a stale entry, a new cycle and a grown class are reported."""

        self.assertEqual(len(boundary_problems({"ophion.a": ["gi"], "ophion.b": ["gi"]}, {"ophion.b", "ophion.c"})), 2)
        self.assertEqual(cycle_problems([["ophion.a", "ophion.b"]], [["ophion.a", "ophion.c"]]),
                         ["runtime import cycle not in the baseline: ['ophion.a', 'ophion.b']"])
        self.assertEqual(cycle_problems([["ophion.a"]], [["ophion.a", "ophion.c"]]), [])
        self.assertEqual(len(class_problems({"m.C": {"lines": 11}, "m.New": {"lines": arch_metrics.BIG_CLASS + 1}},
                                            {"m.C": {"lines": 10}})), 2)
        self.assertTrue(load_allowlist() <= set(self.baseline["core_gtk"]))


if __name__ == "__main__":
    unittest.main()
