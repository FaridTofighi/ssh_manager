#!/usr/bin/env python3
"""Compare a ``relay-match`` render with the Relay reference screenshot.

Usage::

    python3 tools/ui_snapshots.py OUT relay-match
    python3 tools/compare_to_reference.py OUT/relay-match.png \
        [--reference docs/ui-target/relay-reference.png] [--out OUT/compare]

Writes ``heatmap.png`` (per-pixel difference, amplified, over a dimmed copy of
the reference), ``side-by-side.png`` and ``report.json`` / ``report.md`` with:

* per-region mean absolute error (percent of full scale) and the share of
  pixels that differ visibly (any channel off by more than 32), and
* landmark positions (borders and accent-coloured shapes) located in both
  images, with their offsets in pixels.

Targets from the spec: ≤ 3 % mean error per region, ≤ 2 px per landmark.
Exit status is 0 when every target is met, 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from PIL import Image, ImageChops, ImageOps

ROOT = Path(__file__).resolve().parent.parent
REFERENCE = ROOT / "docs" / "ui-target" / "relay-reference.png"

ERROR_TARGET = 3.0  # percent
POSITION_TARGET = 2  # px
VISIBLE_DELTA = 32

Box = Tuple[int, int, int, int]  # left, top, right, bottom (exclusive)

REGIONS: Dict[str, Box] = {
    "top-bar": (0, 0, 1852, 77),
    "sidebar-header": (0, 77, 407, 240),
    "tree": (0, 240, 407, 868),
    "sidebar-footer": (0, 868, 407, 934),
    "page-bar": (408, 77, 1852, 151),
    "host-header": (408, 151, 1852, 275),
    "tabs": (408, 275, 1852, 333),
    "canvas": (408, 333, 1852, 864),
    "info-bar": (408, 864, 1852, 934),
}

ACCENT = (23, 107, 77)  # #176B4D
BORDER = (223, 229, 225)  # #DFE5E1


@dataclass(frozen=True)
class Landmark:
    name: str
    kind: str  # "hline" | "vline" | "blob"
    probe: Box  # search window
    color: Tuple[int, int, int]
    tolerance: int = 18


LANDMARKS: Sequence[Landmark] = (
    Landmark("top-bar bottom border", "hline", (600, 60, 601, 95), BORDER),
    Landmark("sidebar right border", "vline", (395, 500, 420, 501), BORDER),
    Landmark("top-bar brand divider", "vline", (395, 38, 420, 39), BORDER),
    Landmark("page-bar bottom border", "hline", (1000, 135, 1001, 165), BORDER),
    Landmark("host-header bottom border", "hline", (1000, 260, 1001, 290), BORDER),
    Landmark("tabs bottom border", "hline", (1000, 320, 1001, 345), BORDER),
    Landmark("info-bar top border", "hline", (1000, 850, 1001, 880), BORDER),
    Landmark("sidebar footer border", "hline", (200, 850, 201, 885), BORDER),
    Landmark("logo tile", "blob", (10, 5, 90, 72), ACCENT),
    Landmark("add host button", "blob", (240, 100, 400, 175), ACCENT),
    Landmark("selected-row bar", "blob", (0, 300, 20, 400), ACCENT),
    Landmark("connect button", "blob", (1650, 80, 1840, 145), ACCENT),
    Landmark("tab underline", "blob", (420, 325, 540, 334), ACCENT),
    Landmark("hero button", "blob", (980, 640, 1280, 730), ACCENT),
)


def _close(pixel: Tuple[int, ...], color: Tuple[int, int, int], tolerance: int) -> bool:
    return all(abs(int(a) - int(b)) <= tolerance for a, b in zip(pixel[:3], color))


def locate(image: Image.Image, landmark: Landmark) -> Optional[Box]:
    left, top, right, bottom = landmark.probe
    width, height = image.size
    right, bottom = min(right, width), min(bottom, height)
    pixels = image.load()
    hits = [
        (x, y)
        for y in range(top, bottom)
        for x in range(left, right)
        if _close(pixels[x, y], landmark.color, landmark.tolerance)
    ]
    if not hits:
        return None
    xs = [x for x, _ in hits]
    ys = [y for _, y in hits]
    if landmark.kind == "hline":
        y = min(ys)
        return (left, y, right, y + 1)
    if landmark.kind == "vline":
        x = min(xs)
        return (x, top, x + 1, bottom)
    return (min(xs), min(ys), max(xs) + 1, max(ys) + 1)


def region_error(reference: Image.Image, render: Image.Image, box: Box) -> Tuple[float, float]:
    ref = reference.crop(box)
    out = render.crop(box)
    diff = ImageChops.difference(ref, out)
    histogram = diff.histogram()  # 3 × 256 bins
    total = ref.size[0] * ref.size[1] * 3
    error = sum(value * count for channel in range(3) for value, count in enumerate(histogram[channel * 256:(channel + 1) * 256]))
    mean = 100.0 * error / (255.0 * total) if total else 0.0
    gray = diff.convert("L").point(lambda v: 255 if v > VISIBLE_DELTA else 0)
    visible = 100.0 * gray.histogram()[255] / (ref.size[0] * ref.size[1]) if total else 0.0
    return round(mean, 2), round(visible, 2)


def heatmap(reference: Image.Image, render: Image.Image) -> Image.Image:
    diff = ImageChops.difference(reference, render).convert("L")
    amplified = diff.point(lambda v: min(255, v * 4))
    base = ImageOps.grayscale(reference).point(lambda v: 160 + v // 3).convert("RGB")
    red = Image.new("RGB", reference.size, (220, 38, 38))
    return Image.composite(red, base, amplified)


def compare(render_path: Path, reference_path: Path, out_dir: Path) -> Dict[str, object]:
    reference = Image.open(reference_path).convert("RGB")
    render = Image.open(render_path).convert("RGB")
    if render.size != reference.size:
        render = render.resize(reference.size) if abs(render.size[0] - reference.size[0]) > 4 else render.crop((0, 0) + reference.size)
    out_dir.mkdir(parents=True, exist_ok=True)

    regions = {}
    for name, box in REGIONS.items():
        mean, visible = region_error(reference, render, box)
        regions[name] = {"box": box, "mean_error": mean, "visible_diff": visible, "ok": mean <= ERROR_TARGET}

    landmarks: List[Dict[str, object]] = []
    for landmark in LANDMARKS:
        expected = locate(reference, landmark)
        actual = locate(render, landmark)
        entry: Dict[str, object] = {"name": landmark.name, "reference": expected, "render": actual}
        if expected and actual:
            offset = max(abs(a - b) for a, b in zip(expected, actual))
            entry["offset"] = offset
            entry["ok"] = offset <= POSITION_TARGET
        else:
            entry["offset"] = None
            entry["ok"] = False
        landmarks.append(entry)

    overall_mean, overall_visible = region_error(reference, render, (0, 0) + reference.size)
    report = {
        "render": str(render_path),
        "reference": str(reference_path),
        "overall": {"mean_error": overall_mean, "visible_diff": overall_visible},
        "targets": {"region_mean_error": ERROR_TARGET, "position_px": POSITION_TARGET},
        "regions": regions,
        "landmarks": landmarks,
        "ok": all(r["ok"] for r in regions.values()) and all(entry["ok"] for entry in landmarks),
    }

    heatmap(reference, render).save(out_dir / "heatmap.png")
    side = Image.new("RGB", (reference.size[0] * 2, reference.size[1]), "white")
    side.paste(reference, (0, 0))
    side.paste(render, (reference.size[0], 0))
    side.save(out_dir / "side-by-side.png")
    (out_dir / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (out_dir / "report.md").write_text(markdown(report), encoding="utf-8")
    return report


def markdown(report: Dict[str, object]) -> str:
    lines = [
        "# Relay reference comparison",
        "",
        f"Overall mean error **{report['overall']['mean_error']} %**, "
        f"visibly different pixels {report['overall']['visible_diff']} %.",
        "",
        "| Region | Mean error | Visible diff | Target ≤ 3 % |",
        "|---|---:|---:|:---:|",
    ]
    for name, data in report["regions"].items():
        lines.append(f"| {name} | {data['mean_error']} % | {data['visible_diff']} % | {'✓' if data['ok'] else '✗'} |")
    lines += ["", "| Landmark | Reference box | Render box | Offset | ≤ 2 px |", "|---|---|---|---:|:---:|"]
    for entry in report["landmarks"]:
        offset = "—" if entry["offset"] is None else f"{entry['offset']} px"
        lines.append(f"| {entry['name']} | {entry['reference']} | {entry['render']} | {offset} | {'✓' if entry['ok'] else '✗'} |")
    return "\n".join(lines) + "\n"


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("render", type=Path)
    parser.add_argument("--reference", type=Path, default=REFERENCE)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)
    out_dir = args.out or args.render.parent / "compare"
    report = compare(args.render, args.reference, out_dir)
    print(markdown(report))
    print(f"wrote {out_dir}/heatmap.png, side-by-side.png, report.json, report.md")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
