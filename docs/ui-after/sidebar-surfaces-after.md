# Sidebar surface check (after)

Pixels sampled in the tree area (padding, gaps, empty space below the rows)
must equal the palette's `bg_surface` within ±1 per channel.

| GTK theme | App mode | Expected | Worst sample | Max Δ | Image | OK |
|---|---|---|---|---:|---|:---:|
| Adwaita | light | `#FFFFFF` | `#FFFFFF` (top-right padding) | 0 | `sidebar-after-adwaita-light.png` | ✓ |
| Adwaita | dark | `#1A1B1E` | `#1A1B1E` (top-right padding) | 0 | `sidebar-after-adwaita-dark.png` | ✓ |
| Adwaita:dark | light | `#FFFFFF` | `#FFFFFF` (top-right padding) | 0 | `sidebar-after-adwaita-dark-light.png` | ✓ |
| Adwaita:dark | dark | `#1A1B1E` | `#1A1B1E` (top-right padding) | 0 | `sidebar-after-adwaita-dark-dark.png` | ✓ |
| HighContrast | light | `#FFFFFF` | `#FFFFFF` (top-right padding) | 0 | `sidebar-after-highcontrast-light.png` | ✓ |
| HighContrast | dark | `#1A1B1E` | `#1A1B1E` (top-right padding) | 0 | `sidebar-after-highcontrast-dark.png` | ✓ |
