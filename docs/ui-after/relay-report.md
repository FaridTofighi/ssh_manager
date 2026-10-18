# Relay reference comparison

Overall mean error **2.09 %**, visibly different pixels 3.44 %.

Masked error ignores pixels on the reference's glyph edges (antialiased text),
which differ wherever a substitute font is used.

Removed since Phase 6 (still in the reference): tabs band (408, 275)-(1852, 333), tabs bottom border, tab underline.
The ready state moved down with the taller canvas: its landmarks are compared on x
position and size, the empty-state region at the measured shift (shown in the tables).

| Region | Mean error | Masked error | Visible diff | Target ≤ 3 % |
|---|---:|---:|---:|:---:|
| top-bar | 2.38 % | 0.81 % | 4.13 % | ✓ |
| sidebar-header | 2.74 % | 0.21 % | 5.45 % | ✓ |
| search-row | 1.26 % | 0.04 % | 3.32 % | ✓ |
| tree | 2.36 % | 0.06 % | 4.8 % | ✓ |
| sidebar-footer | 1.45 % | 0.13 % | 3.11 % | ✓ |
| page-bar | 1.02 % | 0.17 % | 1.69 % | ✓ |
| host-header | 1.35 % | 0.13 % | 2.77 % | ✓ |
| empty-state (shifted -50 px) | 1.74 % | 0.15 % | 3.76 % | ✓ |
| canvas | 2.16 % | 1.42 % | 3.5 % | ✓ |
| info-bar | 1.39 % | 0.0 % | 2.79 % | ✓ |

| Landmark | Reference box | Render box | Offset | ≤ 2 px |
|---|---|---|---:|:---:|
| top-bar bottom border | (600, 76, 601, 77) | (600, 76, 601, 77) | 0 px | ✓ |
| sidebar right border | (407, 500, 408, 501) | (407, 500, 408, 501) | 0 px | ✓ |
| top-bar brand divider | (408, 38, 409, 39) | (408, 38, 409, 39) | 0 px | ✓ |
| page-bar bottom border | (1000, 150, 1001, 151) | (1000, 150, 1001, 151) | 0 px | ✓ |
| host-header bottom border | (1000, 274, 1001, 275) | (1000, 274, 1001, 275) | 0 px | ✓ |
| info-bar top border | (1000, 864, 1001, 865) | (1000, 864, 1001, 865) | 0 px | ✓ |
| sidebar footer border | (200, 868, 201, 869) | (200, 867, 201, 868) | 1 px | ✓ |
| logo tile | (29, 18, 70, 58) | (29, 17, 71, 59) | 1 px | ✓ |
| add host button | (257, 118, 383, 158) | (259, 118, 383, 158) | 2 px | ✓ |
| selected-row bar | (12, 335, 16, 359) | (12, 337, 16, 361) | 2 px | ✓ |
| connect button | (1674, 91, 1818, 136) | (1672, 91, 1816, 135) | 2 px | ✓ |
| hero button (x/size; shifted -50 px) | (1032, 662, 1228, 713) | (1033, 612, 1226, 663) | 2 px | ✓ |
| sidebar title text | (24, 134, 24, 155) | (24, 134, 24, 154) | 1 px | ✓ |
| tree row text (Production) | (79, 259, 79, 271) | (79, 261, 79, 272) | 2 px | ✓ |
| host title text | (515, 191, 515, 209) | (515, 191, 515, 211) | 2 px | ✓ |
| breadcrumb current text | (515, 108, 515, 119) | (515, 106, 515, 118) | 2 px | ✓ |
| empty-state title text (x/size; shifted -50 px) | (1000, 555, 1000, 571) | (1000, 505, 1000, 521) | 0 px | ✓ |
| stat caption text | (1560, 194, 1560, 203) | (1560, 195, 1560, 204) | 1 px | ✓ |
| info caption text | (440, 884, 440, 892) | (440, 883, 440, 891) | 1 px | ✓ |
