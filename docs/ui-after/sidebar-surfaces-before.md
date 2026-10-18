# Sidebar surface check (before)

Pixels sampled in the tree area (padding, gaps, empty space below the rows)
must equal the palette's `bg_surface` within ±1 per channel.

| GTK theme | App mode | Expected | Worst sample | Max Δ | Image | OK |
|---|---|---|---|---:|---|:---:|
| Adwaita | light | `#FFFFFF` | `#FFFFFF` (top-right padding) | 0 | `sidebar-before-adwaita-light.png` | ✓ |
| Adwaita | dark | `#17201C` | `#FFFFFF` (middle-right padding) | 232 | `sidebar-before-adwaita-dark.png` | ✗ |
| Adwaita:dark | light | `#FFFFFF` | `#2D2D2D` (middle-right padding) | 210 | `sidebar-before-adwaita-dark-light.png` | ✗ |
| Adwaita:dark | dark | `#17201C` | `#2D2D2D` (middle-right padding) | 22 | `sidebar-before-adwaita-dark-dark.png` | ✗ |
| HighContrast | light | `#FFFFFF` | `#FFFFFF` (top-right padding) | 0 | `sidebar-before-highcontrast-light.png` | ✓ |
| HighContrast | dark | `#17201C` | `#FFFFFF` (middle-right padding) | 232 | `sidebar-before-highcontrast-dark.png` | ✗ |
