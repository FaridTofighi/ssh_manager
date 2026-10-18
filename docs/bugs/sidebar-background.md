# Fix: the host-tree panel ignores the theme's surface color

Screenshot: `sidebar-background.png` (dark theme, tree panel `#2D2D2D`).

## Findings (`tools/debug_surfaces.py`)

The audit before the fix flagged 330 nodes across Adwaita, Adwaita:dark and
HighContrast × modes system-light, system-dark, light and dark × focused and
backdrop. Unique culprits:

| Node | Computed background | Why |
|---|---|---|
| `box.sidebar-pane > stack > scrolledwindow > treeview.view.relay-tree > treeview.view.relay-tree.cell` (transient) | `#2D2D2D` / `#303030` (backdrop) in Adwaita-dark; `#FFFFFF` with the app in dark under `GTK_THEME=Adwaita`; `#FCFCFC` backdrop in light | TreeView paints each row and the empty area below the last row with a `style_context.save()` + `.cell` node. The stylesheet only made the widget's own node (`scrolledwindow > treeview.view`) transparent, so the `.cell` node kept the theme's `treeview.view { background-color: theme_base_color }`. **This is the bug in the screenshot.** |
| `popover > box > frame > list` (smart groups), `popover > … > viewport.frame > list` (message history) | `theme_base_color` | No rule for `list` in the main window. |
| `dialog.sm-dialog … textview.view.monospace` (config viewer, host dialog extras) | `#2D2D2D`, `#FCFCFC` backdrop | No rule for `textview text`. |
| `window.background.csd` (keyboard-shortcuts window): headerbar, searchbar box, search entry, popover, scrollbars | stock colors and gradients | The window did not carry the `sm-dialog` class. |
| `label.env-badge.*` | badge colors | False positive: badge tokens now count as surfaces (`Palette.surface_colors()`). |

The scrollbars, viewport, stack and empty-state pages were not culprits in
the sidebar, but each now gets an explicit token too.

## Fix

- **Sidebar chain:** `stylesheet.py` gives every node from `.sidebar-pane`
  down to `treeview.view.cell` `@sm_bg_surface` and `background-image: none`.
  That covers the pane, `stack`, `scrolledwindow`, `viewport`, `.empty-state`,
  `treeview.view` and its `:backdrop`, `:disabled`, `:focus`, `:hover`,
  `:selected`, `:selected:backdrop`, `.even` and `.odd` states. Box shadows
  are removed. Undershoot and overshoot are drawn over the rows, so they stay
  transparent and lose the theme gradient. Selected and hovered rows still get
  their rounded highlight and accent bar from the renderer.
- **Stock surfaces block:** lists, icon views, text views, notebook header and
  stack, search bar and action bar get `@sm_bg_surface`. Popover lists and
  viewports get `@sm_bg_surface_raised`. All of these cover `:backdrop`.
- **Shortcuts window:** it gets the `sm-dialog` class.
- **Priority:** the provider stays at `STYLE_PROVIDER_PRIORITY_APPLICATION`.
  Once the transient node was covered, no theme rule won.
- **Renderers:** none paint `cell-background`. `RelayRowRenderer.do_render`
  only paints the selected and hover rect.

## Verification

- **Audit:** `python3 tools/debug_surfaces.py` reports "clean: no opaque
  non-token backgrounds". It covers the main window (hosts, no results, first
  run) and 9 dialogs, in every theme, mode and focus state above.
- **Pixel test:** `tests/test_gtkui_surfaces.py` covers Adwaita and
  Adwaita:dark, each with modes System (light and dark desktop), Light and
  Dark. Each combination is checked for hosts, no results, first run and a
  runtime mode switch, both focused and backdrop. The test fails against the
  old stylesheet, with Δ 25 (`#303030`) in dark and Δ 3 (`#FCFCFC`) in light.
  It also includes a CSS lint test.
- **Sidebar crops:** `docs/ui-after/sidebar-before-*.png` and
  `sidebar-after-*.png`, with `sidebar-surfaces-{before,after}.md`. Before:
  4 of 6 theme × mode combinations failed, the worst with Δ 232. After: all
  have Δ 0.
- **Other snapshots:** `relay-match.png` and `relay-compact.png` are
  pixel-identical to before, and the reference comparison still passes. Only
  dark tree areas and the dark config viewer changed.
