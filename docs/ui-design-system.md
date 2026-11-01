# Ophion UI design system ("Relay")

Everything visual in the GTK 3 front-end comes from one place:
`ophion/gtkui/theme/`. The light theme reproduces the reference design
`docs/ui-target/relay-reference.png`; the dark theme is derived from it. Two
interface densities share that look: **Compact** (the default, built for
terminal space) and **Comfortable** (the reference geometry). This document
lists the tokens, the CSS classes built on them, the reusable components and
the rules for using them.

## Design tokens (`theme/tokens.py`)

Tokens are Python constants. `theme/stylesheet.py` turns them into CSS, where
every color role becomes `@define-color sm_<role>`.

### Color roles

| Role | Light | Dark | Used for |
|---|---|---|---|
| `bg_window` / `bg_surface` | `#FFFFFF` / `#FFFFFF` | `#151619` / `#1A1B1E` | Window, top bar, sidebar, host header, tabs, info bar |
| `bg_surface_raised` | `#FFFFFF` | `#212226` | Popovers, menus, toasts |
| `bg_canvas` | `#F5F7F6` | `#121315` | Content canvas (session area, ready state) |
| `bg_page_bar` | `#FCFDFC` | `#1A1B1E` (same as surface) | Page bar (breadcrumb row) |
| `bg_input` | `#FAFBFA` | `#16171A` | Entries, search |
| `bg_hover` / `bg_selected` | `#F3F6F4` / `#EAF5EF` | `#25272B` / `#202A25` (faint green hint) | Hovered / selected rows |
| `border_subtle` / `border_input` / `border_strong` | `#DFE5E1` / `#D9E0DC` / `#C9D2CD` | `#2A2C31` / `#35383E` / `#454951` | Dividers / controls / checks |
| `text_primary` / `text_secondary` | `#18231F` / `#5B6B64` | `#E6E8EB` / `#A3A8B0` | Text |
| `text_muted` / `text_faint` / `tab_inactive` | `#8C9791` / `#9BA59F` / `#7C8882` | `#8A9098` / `#6B7079` / `#8F959E` | Caps labels / counts, captions / inactive tabs |
| `accent` / `accent_hover` / `accent_pressed` | `#176B4D` / `#12593F` / `#0E4A34` | `#1F7A57` / `#1A6E4E` / `#196A4B` | Primary buttons, selection bar, tab underline |
| `accent_text` / `accent_tint` / `accent_soft_border` | `#176B4D` / `#EAF5EF` / `#CFE6DB` | `#5CCB9C` / `#202A25` / `#2F4A3E` | Accent text and icons, tinted surfaces |
| `on_accent` | `#FFFFFF` | `#FFFFFF` | Text on accent |
| `success` / `warning` / `danger` / `info` | `#2DA771` / `#B45309` / `#B42318` / `#2563A8` | `#48C48D` / `#E3A548` / `#F07A6E` / `#7DB1E8` | Status dots, latency, feedback levels |
| `danger_strong` / `on_danger` | `#B42318` / `#FFFFFF` | same | Destructive buttons |
| `focus_ring` | `#176B4D` | `#5CCB9C` | 2 px focus ring |
| `chip_bg` | `#F1F4F2` | `#25272B` | Chips |
| `avatar_bg` | `#233B32` | `#3F4A45` | User avatar |
| `broadcast` | `#5B47B8` | `#B3A6F5` | Broadcast's identity colour (violet): the strip's line, the toolbar toggle, the strip's Stop button, the checked Broadcast chip, the `BROADCAST` badge. **Violet = broadcast; red = errors only** |
| `broadcast_bg` / `broadcast_fg` | `#F0EDFB` / `#3B2D86` | `#2A2550` / `#E4DFFC` | Broadcast strip (9.6:1 / 11.0:1) |
| `icon_default` (aliases: `icon_group`, `icon_file`, `icon_host`, `icon_host_selected`) | `#18231F` | `#FFFFFF` | Every group and host icon, in every row state (Phase 9b); a user-picked item color overrides it |
| `badge_connected` / `badge_unknown` | `#2A9D6A` / `#848E88` | `#48C48D` / `#8A9098` | Host status badge (connecting and failed use `warning` / `danger`); light values are the nearest shades of `success` / `text_muted` that reach 3:1 on the selected row |
| `scrollbar` | `#C9D2CD` | `#454951` | Overlay scrollbar thumb |
| `pane_border` / `pane_border_focus` / `pane_border_broadcast` | `#868F8A` / `#176B4D` / `#6A55C9` | `#5E6269` / `#1F7A57` / `#9C8CF0` | Pane outlines in split and tile mode (all ≥ 3:1 against the gutter) |
| `pane_header_bg` / `pane_header_bg_focus` | `#FFFFFF` / `#EAF5EF` | `#212226` / `#202A25` | Pane header and footer; focused pane header |
| `pane_gutter` / `pane_dim` | `#F5F7F6` / 12 % black | `#121315` / 12 % black | Gutter between panes (= `bg_canvas`); veil over unfocused panes |
| `shadow` / `shadow_popover` | 6 % / 12 % of `#18231F` | 35 % / 45 % black | Soft elevation |

Environment badges (`ENV_BADGES`, `environment_badge_style(env, dark)`,
`environment_kind(env)`): production `#FFF1E5 / #8A4B1F`, staging
`#E8F1FB / #1D4F86`, development `#EEF1EF / #46534D`, anything else neutral
`#F1F3F2 / #4F5C56`. Dark variants sit on the neutral surface: production
`#2E2620 / #F0B27A`, staging `#1F2733 / #8DB7E8`, development and neutral
`#25272B / #A3A8B0` (all ≥ 6:1). Aliases such as
`prod`, `stage`, `uat`, `dev` and `qa` map to these kinds.

`Palette.status_color(state)` (connecting is amber) and
`Palette.level_color(level)` map states and feedback levels to roles. Never
hard-code a color in Python; ask `current_palette()`. The only exceptions are
colors the user picks (tree colors) and terminal color schemes
(`theme/terminal_schemes.py`).

### Density (`theme/density.py`)

The geometry and type scale live in two `DensityTokens` presets with the same
keys: `COMPACT` (default) and `COMFORTABLE` (the reference; renders made with
it pass `tools/compare_to_reference.py`). Each preset has these groups:
`space`, `radius`, `size`, `topbar`, `sidebar`, `tree`, `page`, `ready`,
`font` and `variant`, plus `caps_tracking`, `dialog_scale` and
`base_font_scale`.

`Space`, `Radius`, `Size`, `Layout.*` and `FontScale` in `tokens.py` are
**live views** of the active preset (`density.active()`). `Size.TOPBAR`
returns 43 in Compact and 76 in Comfortable at the moment you read it. Views
read them in their `apply_density()` method, which runs at construction and
again when the density changes at runtime (`density.add_listener`). That is
why no view keeps its own copy of a size.

Settings → Appearance → *Interface density* (Compact / Comfortable) is stored
in the UI state as `density`. Missing or invalid values fall back to Compact.
Switching it regenerates the CSS and lays every view out again, with no
restart, and sessions and scrollback are kept.

**Base font.** The window's base font is the app font size ×
`base_font_scale` (Compact 13/14.67, so body text is 13 px at the default
11 pt). Unstyled text (session chips, dialog labels) therefore follows the
density, and `em` sizes are relative to that base. The terminal keeps the
app font size: the Compact stylesheet sets `vte-terminal` back to it in `pt`.

**Layout variants** (`variant`):
- *Single-row sidebar header:* "Hosts", a host count chip and *Add host* on
  one 40 px row, with no eyebrow.
- *Merged page header:* the page bar and host header become one 44 px host
  bar (row A). The tabs (row B) share their row with the session chips and
  toolbar while the Terminal tab shows sessions.
- *Single-line status bar.*
- *Terminal card* (Comfortable) or edge-to-edge terminal (Compact).

### Scales

| Scale | Compact (default) | Comfortable (reference) |
|---|---|---|
| Spacing (`Space`: XS S M L XL XXL XXXL GUTTER) | 2 4 8 12 16 16 24 12 | 4 8 12 16 20 24 32 36 |
| Radius (SMALL KEYCAP MEDIUM INPUT LARGE CARD PILL) | 3 4 6 8 8 12 999 | 4 6 8 10 12 18 999 |
| Bands, total height incl. 1 px border (top bar · page bar · host header · tabs · status bar) | 44 · — · 44 (host bar) · 34 · 28 | 77 · 74 · 124 · 58 · 70 |
| Sidebar (default / min / max) | 284 / 220 / 480 | 408 / 300 / 520 |
| Sidebar header · search · footer | 40 · 32 · 32 | 52 · 46 · 66 |
| Tree row (host / top-level group) · indent · inset · bar | 30 / 30 · 18 · 6 · 3 px | 41 · 25 · 12 · 4 px |
| Tree icon · chevron · chevron → icon · icon → label | 20 · 14 · 4 · 8 px | 24 · 16 · 6 · 10 px |
| Logo tile · avatar · icon button · toolbar button | 28 · 28 · 28 · 28 | 42 · 40 · 40 · 38 |
| Button · large · hero · entry | 28 · 28 · 34 · 30 | 40 · 44 · 51 · 32 |
| Empty-state card · host tile (icon) · badge | 48 · 28 (24) · 18 | 72 · 54 (28) · 22 |
| Dialogs | 0.85 × default size, padding 16, gap 8 | default size, padding 16, gap 12 |
| Type in px at 11 pt (caps · badge · caption · mono · body · tree · wordmark · hero · host title · page title · dialog title) | 11 · 11 · 11.5 · 12 · 13 · 13 · 15 · 17 · 16/600 · 15/600 · 15 | 12 · 11 · 13 · 14 · 15 · 16 · 20 · 22 · 26 · 28 · 18 |
| Caps tracking | 0.08 em | 0.12 em |
| Layout offsets (`Layout.Topbar`, `.Sidebar`, `.Tree`, `.Page`, `.Ready`) | per preset | measured on the reference (29 px logo inset, 7 px group gap, 48 px info-bar field gap, …) |

The brief asked for 10.5 px caps and 10 px badges in Compact. The 11 px floor
wins: no preset value and no rendered size goes below it.
| Fonts | UI: Inter → Cantarell → Noto Sans → DejaVu Sans; mono: JetBrains Mono → Fira Code → DejaVu Sans Mono (`theme/fonts.py` picks the first installed family) |
| Motion (`Motion`) | `FAST 120`, `NORMAL 160`, `SLOW 200` ms, ease-out |
| Elevation | `sm_shadow` on primary buttons, the logo tile and the empty-state card; `sm_shadow_popover` on popovers, menus, toasts and the host panel. Nothing else casts a shadow. |

`BASE_FONT_PX` is 11 pt at 96 dpi (14.67 px). `em(px)` converts a pixel size
to an `em` value of the density's base font, so the *Application font size*
setting (default 11) scales the whole interface, and tree rows grow with it.
The stylesheet floors every size at 11 px for the current app font size
(`stylesheet.font_floor_em`). GTK's
`min-height` excludes borders, so the stylesheet subtracts 2 px from the
outer token sizes of bordered controls.

### Dark theme rules (neutral gray, Phase 5)

The dark theme is neutral cool gray (hue ≈ 220°); green is only the accent.

1. **Elevation by lightness, not hue:** canvas `#121315` < window
   `#151619` < surface `#1A1B1E` < raised `#212226` < hover `#25272B`. Each
   visible step is at least 1.05:1. The terminal background (`#16171A`) is
   darker than the surface, so it sinks into the window. The window base is
   only 1.03:1 above the canvas, but it is almost never on screen.
2. **Single accent:** green (`accent`, `accent_text`, `focus_ring`) is the
   only chromatic brand color. Hover is neutral; selection is neutral with a
   faint green hint (`bg_selected`). The accent bar, dot and text carry the
   selection.
3. **No green-tinted neutrals:** borders, chips, scrollbars, popovers,
   dialogs, toasts, tile frames and disabled states are gray. In every neutral
   token green ≤ blue. The exceptions are the deliberate hints `bg_selected`,
   `accent_tint` and `avatar_bg`.
4. **Buttons:**
   - Primary: `accent` with white text (5.3:1).
   - Outlined: `border_input` with a `bg_hover` hover.
   - Destructive: red.
   - Icon buttons: neutral until hovered.
5. **Semantic colors** (success, warning, danger, info) are unchanged and at
   least 5.8:1 on the surface and on raised layers.
6. **Explicit surfaces:** every surface sets an explicit token background,
   never `transparent` over a stock container (see Phase 3).
7. **No literal colors:** colors live only in `tokens.py` and
   `terminal_schemes.py`, and a test fails on any `#rrggbb` elsewhere in
   `ophion/gtkui`.

`text_faint` (3.5:1) is decorative: counts, separators, keyboard hints,
timestamps and captions. A test fails if any other style uses it.
`border_input` (1.5:1 on the surface) is below the 3:1 non-text target. That
is accepted as in the approved table: inputs are also identified by their
`bg_input` fill, their label and the 2 px focus ring (≥ 7:1 on every layer).

The built-in dark terminal scheme is `#16171A` / `#D6D9DE` (12.7:1), with the
cursor `#B8BCC2` and the selection `#28443A` (25 % `accent_text` over the
background; selected text 7.5:1). Other schemes and user colors are
unchanged.

### Panes (split pane, tile mode)

When several terminals are on screen together, each one sits in a
`PaneFrame` (`widgets/pane_frame.py`). Tiles (`TileFrame`) and split panes
share it.
- **Frame:** a full border on all four sides, radius 6 (Compact) or 8
  (Comfortable).
- **Gutter:** `pane_gutter` (the canvas), 6 or 8 px between panes and around
  the group. In a split it is the `Gtk.Paned` handle, which has a centred
  2 × 24 grip that turns accent on hover or drag.
- **Header** (28 or 34 px): a status dot, the label (the tab label policy,
  plus `· pane 1` / `· pane 2` in a split), the muted `user@host:port`, and on
  the right the Broadcast chip (tiles), zoom (maximize / restore one pane) and
  close.
- **Footer:** `● Connected · 59×35` inside the border.
- **States:**
  - plain: 1 px `pane_border` with 1 px padding;
  - focused: 2 px `pane_border_focus` with an accent-tinted header;
  - broadcast (while on): 2 px `pane_border_broadcast` and a `BROADCAST` badge;
  - focused and broadcast: 1 px broadcast (violet) border with a 1 px accent inner ring;
  - failed / disconnected: 1 px danger / warning border, with the status text
    in the footer.

  Focused and broadcast frames use 2 px of border where a plain frame uses
  1 px of border plus 1 px of padding, so the terminal never moves.
- **Unfocused panes** get a `pane_dim` veil through a pass-through overlay,
  never widget opacity on the terminal.
- **Single session:** a lone session stays edge to edge with no frame.

### Contrast

`tokens.CONTRAST_PAIRS` lists every text/background and UI-component pair;
`tests/test_gtkui_theme.py` asserts WCAG AA (4.5:1 text, 3:1 UI) for both
themes, plus environment badges, the selected tree row and accent buttons.
`text_faint` is decorative (counts, captions next to stronger labels).

## Icons (`assets/icons/`, `widgets/icons.py`)

### Session-tree family (`gtkui/tree_icons.py`, Phase 9)

Drawn on the 24-grid with a 1.75 outline **converted to filled paths**, plus
a second path at `opacity="0.2"` (the duotone fill), so the icons read as
shapes, not hairlines. They contain no strokes, so GTK's own symbolic
recoloring (which rewrites `fill` and ignores `stroke`) cannot turn them into
blobs. Reference: `docs/ui-target/tree-icons-sheet.png`.

| Row | Icon |
|---|---|
| Group (top-level or nested directory), collapsed / empty | `sshm-folder` |
| Group, expanded | `sshm-folder-open` |
| Config-file group (`*.conf`, `RowKind.CHILD`) | `sshm-file-conf` |
| Host | `sshm-server` |
| Read-only / shared / no edit permission | `sshm-server` + `sshm-badge-lock` (50 %) |

**Icon color (Phase 9b):** every group and host icon is drawn in one token,
`icon_default`, in every row state (normal, hover, selected, focused,
drag-over, backdrop): **`#FFFFFF` in the dark palette**, **`#18231F` (the
light `text_primary`) in the light palette**. Groups and hosts differ by
shape, badge and label weight, never by icon color. The duotone fill is the
same color at 20 %. A user-picked item color overrides it, on selected rows
too; *Clear Color* returns to `icon_default`. Decision: white icons would be
invisible (1:1) on the light theme's white surfaces. A white icon on light
would need a dark rounded tile behind it, which is deliberately not done. The
host-bar tile, the group picker and empty states use `icon_default` too; menu
icons stay in the menu text color.

Status badge on the host icon (≈ 42 % of it, bottom-right):

| Status | Badge | Color | Shape |
|---|---|---|---|
| Connected | `sshm-badge-dot` | `badge_connected` | dot |
| Connecting | `sshm-badge-ring` | `warning` | ring with a half fill; pulses at 0.8 Hz unless `gtk-enable-animations` is off |
| Failed | `sshm-badge-failed` (24 px) / `sshm-badge-dot` (20 px) | `danger` | dot with a cut-out × where it fits |
| Unknown | `sshm-badge-dot` | `badge_unknown` | dot |

- **Lock with a status:** a read-only host with an unknown status shows only
  the lock, in the badge's corner. With a known status, the badge takes the
  bottom-right and the lock moves to the top-right.
- **Tooltip:** the row tooltip spells the status out ("Connected",
  "Connection failed") and is refreshed on every status change. The right
  edge of a row shows only group counts.
- **Compositing:** `render_tree_icon()` renders the icon, erases a circle 2 px
  wider than each overlay (`sshm-badge-cutout`, `DEST_OUT`) and draws the
  overlay SVG. The ring is therefore the row's real background on normal,
  hover and selected rows, with no halo.
- **Cache:** every layer is rendered at device pixels and the composite is
  cached by (icon, color, size, scale, badge, badge color, pulse step, lock).
  Nothing parses SVG while the tree draws.
- **Other places:** the same icons are used in the group picker's parent
  (folder) and child (file-conf) lists, the host-bar tile (24/28 px), the drag
  icon (it is the rendered row) and context menus (16 px).

### Outline set

40 outline icons drawn on a 24 px grid with a 1.5 px round stroke, named
`sshm-<name>-symbolic.svg` (terminal, monitor, chevrons, plus, search,
filter, settings, key, copy, more, activity, help, close, refresh, split,
grid, broadcast, download, clipboard, edit, trash, lock, user, globe, check,
warning, error, info, sidebar, stats, maximize, restore, move, palette, tag,
logout, interrupt, eye); folder, folder-open and server belong to the tree
family above. GTK 3's
symbolic recoloring only replaces fills, so stroke icons are rendered with
Rsvg: `SvgIcon` draws in its CSS `color` (states and theme follow
automatically), `render_svg_surface()` / `tinted_icon_surface()` give
cached cairo surfaces at the device scale (HiDPI). Missing names fall back
to the icon theme.

## Theme loading (`theme/loader.py`)

`ThemeManager` owns the single `Gtk.CssProvider`. Modes are `system`,
`light` and `dark`; `system` follows `org.gnome.desktop.interface
color-scheme`. Switching reloads the stylesheet and notifies listeners (the
tree clears its icon cache, the host page re-renders palette-colored markup),
so no restart is needed.

The stylesheet restyles every stock control the app shows — buttons,
entries, spin buttons, combos, checks, radios, switches, level bars,
progress bars, scales, links, text selection, popovers, menus, scrollbars,
info bars — so no default theme accent appears anywhere.

Stock containers that GTK themes fill with `theme_base_color` or
`theme_bg_color` (`list`, `iconview`, `textview`/`text`, `notebook` header and
stack, `searchbar`, `actionbar`, popover lists, the sidebar's `stack` →
`scrolledwindow` → `viewport` → `treeview.view` chain and TreeView's
`.cell` nodes) get an explicit surface token and `background-image: none` in
every state, including `:backdrop`. The stylesheet never references the GTK
theme's own colors, so the app looks the same under Adwaita, Adwaita-dark,
HighContrast or any other `GTK_THEME`, whatever the app's theme mode.
`Palette.surface_colors()` lists the roles a widget background may use.

`tools/debug_surfaces.py` audits this. It builds the main window, its empty
states and every dialog under each installed GTK theme and app mode, focused
and unfocused, and flags any widget whose computed background is opaque and
not a surface token. `tests/test_gtkui_surfaces.py` samples the sidebar's
pixels, and the `sidebar-surfaces` snapshot scenario writes per-theme crops
with a token report.

## CSS classes

| Class | Where | Meaning |
|---|---|---|
| `.sm-window`, `.sm-dialog` | Main window, every `BaseDialog` | Scope for app styles |
| `headerbar.sm-topbar`, `.topbar-brand`, `.logo-tile`, `.wordmark`, `.topbar-divider` | Top bar | Bar, brand block, logo, divider |
| `.workspace-switcher`, `.workspace-label`, `.workspace-name` | Top bar | Workspace menu button |
| `button.health`, `.health-dot`(`.warning`/`.error`), `button.topbar-icon`, `button.avatar`(`.attention`) | Top bar | Health, activity/help, user menu |
| `.sidebar-pane`, `paned.main-paned > separator` | Sidebar | Pane; the 1 px separator is the sidebar border |
| `.sidebar-eyebrow`, `.sidebar-title`, `button.add-host` | Sidebar header | Caps label, "Hosts", primary button |
| `.sidebar-search-entry`, `.search-icon`, `.keycap`, `button.filter-button`, `.filter-dot` | Search row | Search, Ctrl K keycap, filter |
| `.relay-tree` | Tree | Custom-painted rows (see `RelayRowRenderer`) |
| `.bulk-bar` | Sidebar | Multi-selection actions |
| `.sidebar-footer`, `button.footer-link`, `button.footer-icon` | Sidebar footer | Settings, Keys, collapse |
| `.page-bar`, `.breadcrumb-*`, `button.page-more`, `button.page-primary` | Page bar | Breadcrumb, ··· menu, Connect |
| `.host-header`, `.host-tile`, `.host-title`, `.host-address`, `.env-badge.env-<kind>` | Host header | Tile, title, environment badge, mono address |
| `.stat-label`, `.stat-value`, `.latency-dot`/`.latency-{good,warn,bad}` | Host header | LAST CONNECTED / LATENCY |
| `.session-strip`, `.session-tab`, `.session-tab-box` (`.current`, `.hover`) | Session strip | One tab per session: status dot, label (96–220 px, ellipsized), close on hover/active; the active tab has accent text and a 2 px accent underline |
| `.host-panel`, `button.panel-tab`, `.host-panel-actions` | Host panel | Slide-over (360 px) with Details / Activity, raised surface and a left shadow |
| `.page-canvas`, `.card`, `.detail-key`, `.detail-value`, `.activity-row`, `.activity-time` | Content | Canvas, Details and Activity cards |
| `.info-bar`, `.info-label`, `.info-value`, `.info-live`, `button.info-copy` | Info bar | Connection facts, docked live stats |
| `.empty-state`, `.ready-state`, `.empty-card`, `.empty-title`, `.empty-description`, `.empty-caption`, `button.hero` | Empty states | Ready to connect and other placeholders |
| `notebook.session-tabs`, `.session-strip-actions`, `.session-toolbar-row`, `.terminal-card` | Terminal tab | Session strip, toolbar, terminal card |
| `.statusbar`, `.stat-warn`, `.stat-critical`, `.stat-icon` | Live stats | Warn at 80 %, critical at 90 %; one height in every state (`min-height`, a fixed 12 px spinner slot) |
| `.broadcast-strip`, `.broadcast-active`, `button.broadcast-toggle` | Workspace | Broadcast strip (violet, the broadcast icon); a single session's outline while broadcasting in tabs mode; the toolbar toggle |
| `.pane-frame` (`.focused`, `.broadcast`, `.broadcasting`, `.failed`, `.disconnected`), `.pane-header`, `.pane-footer`, `.pane-dim`, `.pane-gutter`, `paned.pane-split`, `button.broadcast-chip` | Panes | Split panes and tiles (see "Panes" below) |
| `button.primary` / `.suggested-action`, `button.outlined`, `.destructive-action` | Buttons | Primary, secondary, destructive |
| `popover.sm-popover`, `.popover-menu-item`, `menuitem .menu-icon`, `.accel-hint`, `menuitem.destructive` | Menus | Popovers and menus |
| `.chip`, `.chip.accent`, `.badge`, `.status-dot.*`, `.toast*`, `.level-*`, `.caps`, `.mono` | Shared | Chips, counters, status, toasts, type helpers |
| `.section-title`, `.section-description`, `.form-help`, `.form-error`, `entry.invalid`, `.settings-sidebar` | Dialogs | Forms and settings |

Widgets next to a terminal must never change size at runtime; reserve space instead of show/hide (a
terminal that gains or loses a row reflows and its content jumps).

## Components

| Component | Module | Purpose |
|---|---|---|
| `HeaderView` | `views/header.py` | Top bar: brand (aligned to the sidebar edge), workspace switcher, health, activity, help, avatar menu |
| `SidebarView` | `views/sidebar.py` | Header + Add host (secondary menu on right-click / Shift+click / long-press), search with keycap, filter popover, tree stack, bulk bar, footer |
| `SessionTreeView`, `RelayRowRenderer` | `views/session_tree.py` | Tree with inset rounded selection, accent bar, chevrons, counts and status dots |
| `HostPageView` (`PageBar`, `HostHeader`, `PageTabs`, `DetailsCard`, `ActivityCard`, `InfoBar`) | `views/host_page.py` | The right-hand page |
| `ReadyState` | `widgets/ready_state.py` | Card, title, body, hero button, caption |
| `Dot` | `widgets/dot.py` | Filled dot in its CSS color (health, latency, filter) |
| `SvgIcon`, `make_icon` | `widgets/icons.py` | Bundled icons |
| `BaseDialog`, `ConfirmDialog`, `MessageDialog`, `PromptDialog`, `UnlockDialog`, `NewSecretDialog`, `SettingsPageLayout`, `ListManagerDialog` | `dialogs/` | Dialog framework (unchanged layout, restyled) |
| `FormRow`/`FormSection`, `SecretEntry`, `Chip`/`ChipFlow`, `StatusDot`, `IconButton` family, `EmptyState`, `Toast`/`ToastOverlay`/`MessageHistoryButton`, `build_menu` | `widgets/` | Shared widgets |

## Usage rules

**Do**

- Take sizes from `Size`/`Layout`, spacing from `Space` and colors from
  tokens or classes.
- Use sentence case for buttons ("Add host", "Connect to host"); caps only
  for the small labels (`.caps`, `.stat-label`, `.info-label`).
- Build icons with `make_icon()`; give every icon-only control a tooltip
  (`IconButton` also sets the accessible name).
- Show status with shape or text, not color alone (tooltips, labels).
- Wrap user-visible strings in `_()`; keyboard hints say "Ctrl".

**Don't**

- Don't write `#hex` in views or ad-hoc CSS, or pixel font sizes (the theme
  test rejects `font-size: Npx`).
- Don't add shadows beyond the elevation list above.
- Don't use `Gtk.Image` for bundled icons: GTK fills strokes solid.
- Don't put `em` font sizes on selectors that can match TreeView's saved
  style nodes (`treeview.view` without a parent combinator) — the scale is
  applied twice.
- Don't write literal pixel sizes in views, widgets or dialogs. Read
  `Size` / `Space` / `Layout` inside `apply_density()`, or at construction for
  widgets that are rebuilt anyway (popovers, dialogs, menus).
- Don't let a parent's `show_all()` decide the visibility of something a
  controller owns. Show its contents once and set `no_show_all`; the host-page
  bands, sidebar, top bar and toolbar row do this.
- Never rely on `transparent` over stock GTK containers; always set an
  explicit token background and `background-image: none`. GTK themes paint
  nodes you don't see in the widget tree (TreeView draws rows and the empty
  area below them with `treeview.view.cell` nodes), so a transparent widget
  can still show the theme's base color. Transparent is only for overlays
  drawn on top of content, such as the scrolled window's undershoot and
  overshoot.
- Don't bind global accelerators to keys terminals need. Ctrl+K is passed
  through to a focused terminal (readline kill-line).
