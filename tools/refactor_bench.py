#!/usr/bin/env python3
"""Benchmarks against the pre-refactor build (R10).

    tools/refactor_bench.py check [--tag TAG]   # exit 1 on a regression (TAG: pre-refactor)
    tools/refactor_bench.py make                # write tests/refactor/bench_baseline.json (a record)

Measured on the private X server (tools/run_gtk_tests.py): every operation of
``tools/bench_large_inventory.py`` (median of ``REPEAT`` runs, on a 3,996-host store) and the startup time
(``tools/refactor_startup.py``: the main window on a 1,200-host store, best of 5).

``check`` measures TAG (exported with ``git archive`` into a temporary folder) and the working tree
alternately, ``ROUNDS`` times each, and compares the per-metric medians: stored numbers drift with the
machine's state by more than 10 % (the first gate run measured Preferences 11 % and a new window 14 % slower
on unchanged code), the same machine at the same time does not. A metric regresses when it is more than
10 % slower than TAG **and** more than ``FLOOR_MS`` slower.
"""

from __future__ import annotations

import json
import os
import re
import statistics
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Dict, List

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "tests" / "refactor" / "bench_baseline.json"
REPEAT = 5
ROUNDS = 3
LIMIT = 1.10
FLOOR_MS = 5.0


def _run(tree: Path, script: str, *args: str) -> str:
    done = subprocess.run([sys.executable, str(ROOT / "tools" / "run_gtk_tests.py"), "--", sys.executable,
                           str(tree / "tools" / script), *args], cwd=tree, capture_output=True, text=True)
    if done.returncode != 0:
        raise SystemExit(f"{script} failed:\n{done.stdout[-2000:]}\n{done.stderr[-2000:]}")
    return done.stdout


def measure(tree: Path, startup_script: Path) -> Dict[str, float]:
    metrics: Dict[str, float] = {}
    for line in _run(tree, "bench_large_inventory.py", "--repeat", str(REPEAT)).splitlines():
        match = re.match(r"\|\s*(.+?)\s*\|\s*([0-9.]+)\s*\|\s*$", line)
        if match:
            metrics[match.group(1)] = float(match.group(2))
    done = subprocess.run([sys.executable, str(ROOT / "tools" / "run_gtk_tests.py"), "--", sys.executable,
                           str(startup_script)], cwd=tree, capture_output=True, text=True,
                          env={**os.environ, "OPHION_TREE": str(tree)})
    match = re.search(r"startup_ms ([0-9.]+)", done.stdout)
    if match:
        metrics["startup (main window, 1,200 hosts, best of 5)"] = float(match.group(1))
    if not metrics:
        raise SystemExit("no measurements")
    return metrics


def export(tag: str, target: Path) -> None:
    archive = target.with_suffix(".tar")
    with archive.open("wb") as handle:
        subprocess.run(["git", "-C", str(ROOT), "archive", tag], check=True, stdout=handle)
    with tarfile.open(archive) as tar:
        tar.extractall(target, filter="data")
    archive.unlink()


def medians(runs: List[Dict[str, float]]) -> Dict[str, float]:
    return {name: statistics.median(run[name] for run in runs if name in run) for name in runs[0]}


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
    startup = ROOT / "tools" / "refactor_startup.py"  # the current script, run against either tree
    if command == "make":
        now = medians([measure(ROOT, startup) for _ in range(ROUNDS)])
        BASELINE.write_text(json.dumps(now, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        print(f"bench: {len(now)} metrics written to {BASELINE.relative_to(ROOT)}")
        return 0
    tag = argv[argv.index("--tag") + 1] if "--tag" in argv else "pre-refactor"
    with tempfile.TemporaryDirectory(prefix="ophion-bench-") as tmp:
        tree = Path(tmp) / "tree"
        export(tag, tree)
        old_runs, new_runs = [], []
        for _ in range(ROUNDS):  # alternating: the machine's drift affects both builds alike
            old_runs.append(measure(tree, startup))
            new_runs.append(measure(ROOT, startup))
    before, now = medians(old_runs), medians(new_runs)
    for name in sorted(before):
        print(f"bench: {name:55} {before[name]:9.1f} -> {now.get(name, float('nan')):9.1f} ms")
    problems = check(now, before)
    for problem in problems:
        print(f"bench: REGRESSION {problem}")
    print(f"bench: {'FAILED' if problems else 'ok'} ({tag} vs working tree, median of {ROUNDS} alternating rounds)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
