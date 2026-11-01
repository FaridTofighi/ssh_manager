#!/usr/bin/env python3
"""Compare a ``neutral-dark-match`` render with the approved neutral-gray dark mock.

Usage::

    python3 tools/ui_snapshots.py OUT neutral-dark-match
    python3 tools/compare_neutral_dark.py OUT/neutral-dark-match.png [--out OUT]

The mock (``docs/ui-target/neutral-dark-palette.png``) only defines colors —
the layout is the existing Comfortable one — so the comparison looks at the
flat color areas of each band (text-free boxes) rather than at glyphs:

* per region: mean absolute error between mock and render (percent of full
  scale) and the mean color of each;
* per region: whether the render's center pixel equals the expected palette
  token within ±1 per channel.

Writes ``neutral-dark-report.{md,json}``. Exit status 0 when every region's
error is ≤ 3 % and every token sample matches.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, Optional, Sequence, Tuple

from PIL import Image, ImageChops, ImageStat

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
MOCK = ROOT / "docs" / "ui-target" / "neutral-dark-palette.png"
ERROR_TARGET = 3.0

Box = Tuple[int, int, int, int]

# name: (mock box, render box or None = same, expected token or terminal color)
REGIONS: Dict[str, Tuple[Box, Optional[Box], str]] = {
    "top bar": ((760, 6, 1400, 70), None, "bg_surface"),
    "sidebar header": ((40, 82, 260, 104), None, "bg_surface"),
    "tree panel (padding)": ((0, 250, 9, 700), None, "bg_surface"),
    "selected row": ((330, 774, 400, 806), (330, 718, 400, 754), "bg_selected"),
    "sidebar footer": ((240, 985, 380, 1030), None, "bg_surface"),
    "page bar": ((900, 82, 1600, 145), None, "bg_page_bar"),
    "host header": ((1000, 158, 1450, 268), None, "bg_surface"),
    "tabs": ((900, 280, 1800, 326), None, "bg_surface"),
    "canvas margin": ((447, 420, 458, 940), None, "bg_canvas"),
    "terminal": ((600, 520, 1800, 900), None, "terminal"),
    "status bar": ((1800, 978, 1845, 1036), None, "bg_surface"),
    "primary button": ((283, 122, 297, 154), (288, 122, 297, 154), "accent"),
}


def _hex(rgb: Sequence[float]) -> str:
    return "#" + "".join(f"{int(round(c)):02X}" for c in rgb[:3])


def _parse(value: str) -> Tuple[int, int, int]:
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def expected_color(token: str) -> str:
    from ophion.gtkui.theme.terminal_schemes import DARK_COLORS
    from ophion.gtkui.theme.tokens import DARK

    return DARK_COLORS[0] if token == "terminal" else getattr(DARK, token)


def compare(render_path: Path, mock_path: Path, out_dir: Path) -> Dict[str, object]:
    mock = Image.open(mock_path).convert("RGB")
    render = Image.open(render_path).convert("RGB")
    if render.size != mock.size:
        raise SystemExit(f"size mismatch: render {render.size}, mock {mock.size}")
    regions = {}
    for name, (mock_box, render_box, token) in REGIONS.items():
        render_box = render_box or mock_box
        a, b = mock.crop(mock_box), render.crop(render_box)
        if a.size != b.size:
            b = b.resize(a.size)
        diff = ImageChops.difference(a, b)
        error = 100.0 * sum(ImageStat.Stat(diff).mean) / (3 * 255.0)
        want = _parse(expected_color(token))
        cx, cy = (render_box[0] + render_box[2]) // 2, (render_box[1] + render_box[3]) // 2
        got = render.getpixel((cx, cy))
        token_ok = all(abs(g - w) <= 1 for g, w in zip(got, want))
        regions[name] = {
            "mock_box": mock_box, "render_box": render_box, "token": token,
            "expected": _hex(want), "render_sample": _hex(got),
            "mock_mean": _hex(ImageStat.Stat(a).mean), "render_mean": _hex(ImageStat.Stat(b).mean),
            "mean_error": round(error, 2), "token_ok": token_ok,
            "ok": error <= ERROR_TARGET and token_ok,
        }
    report = {
        "render": str(render_path), "mock": str(mock_path), "target": ERROR_TARGET,
        "regions": regions, "ok": all(r["ok"] for r in regions.values()),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "neutral-dark-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (out_dir / "neutral-dark-report.md").write_text(markdown(report), encoding="utf-8")
    return report


def markdown(report: Dict[str, object]) -> str:
    lines = [
        "# Neutral-gray dark theme vs the approved mock",
        "",
        "Flat color areas of each band (no glyphs). Target: mean error ≤ 3 % and the",
        "render's sampled pixel equal to the palette token within ±1 per channel.",
        "",
        "| Region | Token | Expected | Render sample | Mock mean | Render mean | Error | OK |",
        "|---|---|---|---|---|---|---:|:---:|",
    ]
    for name, r in report["regions"].items():
        lines.append(
            f"| {name} | `{r['token']}` | `{r['expected']}` | `{r['render_sample']}` | `{r['mock_mean']}` "
            f"| `{r['render_mean']}` | {r['mean_error']} % | {'✓' if r['ok'] else '✗'} |"
        )
    return "\n".join(lines) + "\n"


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("render", type=Path)
    parser.add_argument("--mock", type=Path, default=MOCK)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)
    report = compare(args.render, args.mock, args.out or args.render.parent)
    print(markdown(report))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
