#!/usr/bin/env python3
"""UI snapshots against the pre-refactor build (R11).

    tools/refactor_snapshots.py [check] [--tag TAG]    # exit 1 on a difference (TAG: pre-refactor)

``tools/ui_snapshots.py`` renders the windows and dialogs offscreen (118 images). It runs twice on the private
X server of ``tools/run_gtk_tests.py`` (96 dpi, fixed GTK settings): once on TAG, exported (``git archive``)
into a temporary folder, and once on the working tree. Rendering both on the same machine at the same time
keeps the comparison exact without committing images. An image passes when its size is the same and at most
``TOLERANCE`` of its bytes differ (the noise of two renders of the same code; see docs/refactor/baseline.md).
"""

from __future__ import annotations

import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import List, Tuple

ROOT = Path(__file__).resolve().parents[1]
TOLERANCE = 0.01  # fraction of differing bytes per image: two renders of unchanged code differ by up to 0.45 %


def render(tree: Path, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    subprocess.run([sys.executable, str(ROOT / "tools" / "run_gtk_tests.py"), "--", sys.executable,
                    str(tree / "tools" / "ui_snapshots.py"), str(out)], check=True, cwd=tree,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def export(tag: str, target: Path) -> None:
    archive = target.with_suffix(".tar")
    with archive.open("wb") as handle:
        subprocess.run(["git", "-C", str(ROOT), "archive", tag], check=True, stdout=handle)
    with tarfile.open(archive) as tar:
        tar.extractall(target, filter="data")
    archive.unlink()


def difference(a: Path, b: Path) -> Tuple[str, float]:
    """``(problem or "", fraction of differing bytes)``."""

    import gi

    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import GdkPixbuf

    if a.read_bytes() == b.read_bytes():
        return "", 0.0
    first, second = GdkPixbuf.Pixbuf.new_from_file(str(a)), GdkPixbuf.Pixbuf.new_from_file(str(b))
    if (first.get_width(), first.get_height()) != (second.get_width(), second.get_height()):
        return f"size {first.get_width()}x{first.get_height()} != {second.get_width()}x{second.get_height()}", 1.0
    pixels_a, pixels_b = first.get_pixels(), second.get_pixels()
    if len(pixels_a) != len(pixels_b):
        return "pixel formats differ", 1.0
    differing = sum(1 for x, y in zip(pixels_a, pixels_b) if x != y)
    return "", differing / max(1, len(pixels_a))


def compare(baseline: Path, current: Path) -> List[str]:
    problems: List[str] = []
    expected = sorted(p.name for p in baseline.glob("*.png"))
    found = sorted(p.name for p in current.glob("*.png"))
    if not expected:
        return ["the baseline rendered no images"]
    for name in sorted(set(expected) - set(found)):
        problems.append(f"{name}: not rendered any more")
    for name in sorted(set(found) - set(expected)):
        problems.append(f"{name}: new (not in the baseline)")
    for name in sorted(set(expected) & set(found)):
        problem, fraction = difference(baseline / name, current / name)
        if problem or fraction > TOLERANCE:
            problems.append(f"{name}: {problem or f'{fraction:.5%} of the bytes differ'}")
    return problems


def main(argv: List[str]) -> int:
    tag = argv[argv.index("--tag") + 1] if "--tag" in argv else "pre-refactor"
    with tempfile.TemporaryDirectory(prefix="ophion-snapshots-") as tmp:
        work = Path(tmp)
        export(tag, work / "tree")
        render(work / "tree", work / "baseline")
        render(ROOT, work / "current")
        problems = compare(work / "baseline", work / "current")
        count = len(list((work / "baseline").glob("*.png")))
    for problem in problems:
        print(f"snapshots: {problem}")
    print(f"snapshots: {'FAILED' if problems else 'ok'} ({count} images, {tag} vs working tree)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
