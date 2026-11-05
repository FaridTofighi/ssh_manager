# Ophion icon: source and master

Final artwork supplied by the product owner (raster, RGBA). Prepared by `tools/prepare_ophion_icon.py`:
crop + resample only. No vector source exists; a vector redraw is a future task.

| File | What |
|---|---|
| `source/SOURCE_ORIGINAL_UNMODIFIED.png` | The approved artwork, byte for byte (1536×1024 RGBA, transparent; from `Ophion_Exact_Source_Icon_Kit`). The only source. |
| `ophion-master-crop.png` | The mark cut out as a square (969×969), pixels unchanged. |
| `ophion-master-1024.png` | The same square, one Lanczos resample to 1024×1024. |

The application's sizes (16 … 512 px) are in `ophion/gtkui/assets/app-icon/hicolor/`. To make them again:

```bash
python tools/prepare_ophion_icon.py            # Pillow + numpy (development only)
```

**What the script guarantees:**
- **Crop:** it finds the mark from its opaque core (alpha ≥ 128, specks ignored) and cuts a square centred on
  it, with 6 % padding.
- **Resample:** each size is a downscale of that square (premultiplied alpha, box halvings, then Lanczos).
- **Nothing else:** no pixel inside the square is recoloured, smoothed, sharpened or retouched.
- **Haze:** the source's faint background haze is kept; inside the square its alpha is at most 2/255.
  `--clear-haze` would clear it outside the mark only. It is not used.

**Not made:** a scalable SVG (an SVG wrapping this PNG would not be a vector), Windows `.ico` (no Windows
packaging), macOS / iOS / Android icons.
