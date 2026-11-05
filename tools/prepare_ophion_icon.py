#!/usr/bin/env python3
"""Prepare the Ophion application icon from the approved artwork (dev-only: Pillow + numpy).

Usage::

    python tools/prepare_ophion_icon.py [--source PNG] [--clear-haze] [--contact-sheet PNG] [--previews DIR]

Crop + resample only. The artwork (a transparent RGBA raster, ``data/icons/source/SOURCE_ORIGINAL_UNMODIFIED.png``)
is never recoloured, smoothed, sharpened or retouched:

1. The mark's bounding box comes from its opaque core (alpha >= 128): the largest connected component plus
   components of at least 3 % of its size (isolated specks do not count).
2. A square centred on that box, 6 % of its longer side as padding on every side, is cut out unchanged
   (transparent where it passes the canvas edge) → ``data/icons/ophion-master-crop.png`` (native size) and
   ``data/icons/ophion-master-1024.png`` (one Lanczos resample).
3. The hicolor sizes are made from the native crop: halvings by box averaging (``Image.reduce``) while the
   image is more than twice the target, then one Lanczos step, in premultiplied alpha (no dark fringes)
   → ``ophion/gtkui/assets/app-icon/hicolor/<n>x<n>/apps/ophion.png``.

The faint background haze of the source is kept and measured. ``--clear-haze`` (opt-in) sets alpha to 0
for pixels outside the mark's mask dilated by 4 % of its size: empty background only, never the artwork.
"""

from __future__ import annotations

import argparse
import sys
from collections import deque
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

REPO = Path(__file__).resolve().parent.parent
SOURCE = REPO / "data" / "icons" / "source" / "SOURCE_ORIGINAL_UNMODIFIED.png"
DATA = REPO / "data" / "icons"
HICOLOR = REPO / "ophion" / "gtkui" / "assets" / "app-icon" / "hicolor"
SIZES = (16, 22, 24, 32, 48, 64, 96, 128, 256, 512)
CORE_ALPHA = 128
SPECK_SHARE = 0.03
PADDING = 0.06
HAZE_DILATE = 0.04
PREVIEW_BACKGROUNDS = ("#0F1115", "#1E2127", "#808080", "#FFFFFF")


def components(mask: np.ndarray) -> list:
    """4-connected components of ``mask`` as (size, (x0, y0, x1, y1)), largest first."""

    height, width = mask.shape
    seen = np.zeros_like(mask, dtype=bool)
    found = []
    for y0, x0 in zip(*np.nonzero(mask)):
        if seen[y0, x0]:
            continue
        seen[y0, x0] = True
        queue, count = deque([(y0, x0)]), 0
        left, top, right, bottom = x0, y0, x0, y0
        while queue:
            y, x = queue.popleft()
            count += 1
            left, right, top, bottom = min(left, x), max(right, x), min(top, y), max(bottom, y)
            for ny, nx in ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)):
                if 0 <= ny < height and 0 <= nx < width and mask[ny, nx] and not seen[ny, nx]:
                    seen[ny, nx] = True
                    queue.append((ny, nx))
        found.append((count, (int(left), int(top), int(right), int(bottom))))
    return sorted(found, reverse=True)


def mark_bbox(alpha: np.ndarray) -> tuple:
    """Inclusive (x0, y0, x1, y1) of the opaque core, specks ignored."""

    parts = components(alpha >= CORE_ALPHA)
    largest = parts[0][0]
    kept = [box for size, box in parts if size >= SPECK_SHARE * largest]
    return (min(b[0] for b in kept), min(b[1] for b in kept), max(b[2] for b in kept), max(b[3] for b in kept))


def square_around(bbox: tuple) -> tuple:
    """(left, top, side): the padded square centred on ``bbox`` (integer, centre kept within half a pixel)."""

    x0, y0, x1, y1 = bbox
    longer = max(x1 - x0 + 1, y1 - y0 + 1)
    side = int(round(longer * (1 + 2 * PADDING)))
    centre_x, centre_y = (x0 + x1 + 1) / 2, (y0 + y1 + 1) / 2
    return int(round(centre_x - side / 2)), int(round(centre_y - side / 2)), side


def crop(image: Image.Image, left: int, top: int, side: int) -> Image.Image:
    """The square cut out with its pixels unchanged; transparent where it passes the canvas."""

    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(image.crop((max(left, 0), max(top, 0), min(left + side, image.width), min(top + side, image.height))),
                 (max(-left, 0), max(-top, 0)))
    return canvas


def resample(image: Image.Image, size: int) -> Image.Image:
    work = image.convert("RGBa")  # premultiplied: transparent pixels' colour does not bleed into the edge
    while work.width >= 4 * size:
        work = work.reduce(2)
    if work.size != (size, size):
        work = work.resize((size, size), Image.LANCZOS)
    return work.convert("RGBA")


def dilated(mask: np.ndarray, radius: int) -> np.ndarray:
    image = Image.fromarray((mask * 255).astype(np.uint8))
    for _step in range(max(1, radius // 4)):
        image = image.filter(ImageFilter.MaxFilter(9))
    return np.array(image) > 0


def save(image: Image.Image, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, optimize=True)  # no metadata: the same input gives the same bytes


def preview(images: dict, path: Path) -> None:
    """A contact sheet: every size on every preview background."""

    sizes = [s for s in (16, 22, 24, 32, 48, 64, 128) if s in images]
    cell = 144
    sheet = Image.new("RGBA", (cell * len(sizes), cell * len(PREVIEW_BACKGROUNDS)), "#000000")
    for row, colour in enumerate(PREVIEW_BACKGROUNDS):
        for column, size in enumerate(sizes):
            tile = Image.new("RGBA", (cell, cell), colour)
            tile.alpha_composite(images[size], ((cell - size) // 2, (cell - size) // 2))
            sheet.paste(tile, (column * cell, row * cell))
    save(sheet, path)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--clear-haze", action="store_true",
                        help="alpha 0 outside the mark's dilated mask (empty background only)")
    parser.add_argument("--contact-sheet", type=Path, help="write a preview of the small sizes here")
    parser.add_argument("--previews", type=Path, help="write the 48 px icon on a dark and a light background here")
    args = parser.parse_args(argv)

    source = Image.open(args.source)
    source.load()
    if source.mode != "RGBA":
        print(f"error: {args.source} is {source.mode}, not RGBA", file=sys.stderr)
        return 1
    alpha = np.array(source)[..., 3]
    bbox = mark_bbox(alpha)
    print(f"source {source.width}x{source.height} {source.mode}; mark bbox x {bbox[0]}-{bbox[2]} "
          f"y {bbox[1]}-{bbox[3]} ({bbox[2] - bbox[0] + 1}x{bbox[3] - bbox[1] + 1})")

    left, top, side = square_around(bbox)
    print(f"square: left {left} top {top} side {side} (padding {PADDING:.0%} of the longer side)")
    master = crop(source, left, top, side)
    pixels = np.array(master)
    core = pixels[..., 3] >= CORE_ALPHA
    keep = dilated(core, int(round(HAZE_DILATE * max(bbox[2] - bbox[0], bbox[3] - bbox[1]))))
    haze = (pixels[..., 3] > 0) & ~keep
    print(f"haze outside the dilated mask, inside the square: {int(haze.sum())} px, "
          f"max alpha {int(pixels[..., 3][haze].max()) if haze.any() else 0}")
    if args.clear_haze:
        pixels[..., 3][haze] = 0
        master = Image.fromarray(pixels, "RGBA")
        print("--clear-haze: those pixels are now fully transparent (the artwork is untouched)")

    save(master, DATA / "ophion-master-crop.png")
    save(master if side == 1024 else resample(master, 1024), DATA / "ophion-master-1024.png")
    images = {}
    for size in SIZES:
        images[size] = resample(master, size)
        save(images[size], HICOLOR / f"{size}x{size}" / "apps" / "ophion.png")
    print(f"wrote {DATA.relative_to(REPO)}/ophion-master-*.png and {len(SIZES)} hicolor sizes")
    if args.contact_sheet:
        preview(images, args.contact_sheet)
    if args.previews:
        for name, colour in (("dark", "#0F1115"), ("light", "#FFFFFF")):
            tile = Image.new("RGBA", (96, 96), colour)
            tile.alpha_composite(images[48], (24, 24))
            save(tile, args.previews / f"ophion-48-{name}.png")
    return 0


if __name__ == "__main__":
    sys.exit(main())
