#!/usr/bin/env python3
"""Benchmarks against the refactor's baseline (R10).

    tools/refactor_bench.py make     # measure, write tests/refactor/bench_baseline.json (on the baseline)
    tools/refactor_bench.py check    # measure again: exit 1 on a regression

Measured on the private X server (tools/run_gtk_tests.py): every operation of
``tools/bench_large_inventory.py`` (median of ``REPEAT`` runs, on a 3,996-host store) and the startup time
(``tools/refactor_startup.py``: the main window on a 1,200-host store, best of 5). A metric regresses when it
is more than 10 % slower than the baseline **and** more than ``FLOOR_MS`` slower (operations of a few
milliseconds vary by more than 10 % between runs of unchanged code; see docs/refactor/baseline.md).
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "tests" / "refactor" / "bench_baseline.json"
REPEAT = 5
LIMIT = 1.10
FLOOR_MS = 5.0


def _run(script: str, *args: str) -> str:
    done = subprocess.run([sys.executable, str(ROOT / "tools" / "run_gtk_tests.py"), "--", sys.executable,
                           str(ROOT / "tools" / script), *args], cwd=ROOT, capture_output=True, text=True)
    if done.returncode != 0:
        raise SystemExit(f"{script} failed:\n{done.stdout[-2000:]}\n{done.stderr[-2000:]}")
    return done.stdout


def measure() -> Dict[str, float]:
    metrics: Dict[str, float] = {}
    for line in _run("bench_large_inventory.py", "--repeat", str(REPEAT)).splitlines():
        match = re.match(r"\|\s*(.+?)\s*\|\s*([0-9.]+)\s*\|\s*$", line)
        if match:
            metrics[match.group(1)] = float(match.group(2))
    match = re.search(r"startup_ms ([0-9.]+)", _run("refactor_startup.py"))
    if match:
        metrics["startup (main window, 1,200 hosts, best of 5)"] = float(match.group(1))
    if not metrics:
        raise SystemExit("no measurements")
    return metrics


def check(now: Dict[str, float], before: Dict[str, float]) -> List[str]:
    problems = []
    for name, baseline in before.items():
        value = now.get(name)
        if value is None:
            problems.append(f"{name}: not measured any more")
        elif value > baseline * LIMIT and value - baseline > FLOOR_MS:
            problems.append(f"{name}: {value:.1f} ms > {baseline:.1f} ms + 10 %")
    return problems


def main(argv: List[str]) -> int:
    command = argv[0] if argv else "check"
    now = measure()
    if command == "make":
        BASELINE.write_text(json.dumps(now, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        print(f"bench: {len(now)} metrics written to {BASELINE.relative_to(ROOT)}")
        return 0
    before = json.loads(BASELINE.read_text(encoding="utf-8"))
    for name in sorted(before):
        print(f"bench: {name:55} {before[name]:9.1f} -> {now.get(name, float('nan')):9.1f} ms")
    problems = check(now, before)
    for problem in problems:
        print(f"bench: REGRESSION {problem}")
    print(f"bench: {'FAILED' if problems else 'ok'}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
