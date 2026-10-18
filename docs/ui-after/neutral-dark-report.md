# Neutral-gray dark theme vs the approved mock

Flat color areas of each band (no glyphs). Target: mean error ≤ 3 % and the
render's sampled pixel equal to the palette token within ±1 per channel.

| Region | Token | Expected | Render sample | Mock mean | Render mean | Error | OK |
|---|---|---|---|---|---|---:|:---:|
| top bar | `bg_surface` | `#1A1B1E` | `#1A1B1E` | `#1A1B1E` | `#1A1B1E` | 0.0 % | ✓ |
| sidebar header | `bg_surface` | `#1A1B1E` | `#1A1B1E` | `#1A1B1E` | `#1A1B1E` | 0.0 % | ✓ |
| tree panel (padding) | `bg_surface` | `#1A1B1E` | `#1A1B1E` | `#1A1B1E` | `#1A1B1E` | 0.0 % | ✓ |
| selected row | `bg_selected` | `#202A25` | `#202A25` | `#202A25` | `#202A25` | 0.0 % | ✓ |
| sidebar footer | `bg_surface` | `#1A1B1E` | `#1A1B1E` | `#1A1B1E` | `#1A1B1E` | 0.0 % | ✓ |
| page bar | `bg_page_bar` | `#1A1B1E` | `#1A1B1E` | `#1A1B1E` | `#1A1B1E` | 0.0 % | ✓ |
| host header | `bg_surface` | `#1A1B1E` | `#1A1B1E` | `#1A1B1E` | `#1A1B1E` | 0.02 % | ✓ |
| tabs | `bg_surface` | `#1A1B1E` | `#1A1B1E` | `#1A1B1E` | `#1A1B1E` | 0.0 % | ✓ |
| canvas margin | `bg_canvas` | `#121315` | `#121315` | `#121315` | `#121315` | 0.0 % | ✓ |
| terminal | `terminal` | `#16171A` | `#16171A` | `#16171A` | `#16171A` | 0.0 % | ✓ |
| status bar | `bg_surface` | `#1A1B1E` | `#1A1B1E` | `#1A1B1E` | `#1A1B1E` | 0.0 % | ✓ |
| primary button | `accent` | `#1F7A57` | `#1F7A57` | `#1F7A57` | `#1F7A57` | 0.0 % | ✓ |
