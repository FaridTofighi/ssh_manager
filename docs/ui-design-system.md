# SSH Manager UI design system ("Relay")

Everything visual in the GTK 3 front-end comes from one place:
`ssh_manager/gtkui/theme/`. The light theme reproduces the reference design
`docs/ui-target/relay-reference.png`; the dark theme is derived from it. This
document lists the tokens, the CSS classes built on them, the reusable
components and the rules for using them.

## Design tokens (`theme/tokens.py`)

Tokens are Python constants. `theme/stylesheet.py` turns them into CSS, where
every color role becomes `@define-color sm_<role>`.

### Color roles

| Role | Light | Dark | Used for |
|---|---|---|---|
| `bg_window` / `bg_surface` | `#FFFFFF` / `#FFFFFF` | `#131B18` / `#17201C` | Window, top bar, sidebar, host header, tabs, info bar |
| `bg_surface_raised` | `#FFFFFF` | `#1C2722` | Popovers, menus, toasts |
| `bg_canvas` | `#F5F7F6` | `#0F1613` | Content canvas (Terminal / Details / Activity) |
| `bg_page_bar` | `#FCFDFC` | `#18221E` | Page bar (breadcrumb row) |
| `bg_input` | `#FAFBFA` | `#1B2521` | Entries, search |
| `bg_hover` / `bg_selected` | `#F3F6F4` / `#EAF5EF` | `#202C27` / `#18382B` | Hovered / selected rows |
| `border_subtle` / `border_input` / `border_strong` | `#DFE5E1` / `#D9E0DC` / `#C9D2CD` | `#27332E` / `#33423B` / `#45564E` | Dividers / controls / checks |
| `text_primary` / `text_secondary` | `#18231F` / `#5B6B64` | `#E6EDE9` / `#A9B8B0` | Text |
| `text_muted` / `text_faint` / `tab_inactive` | `#8C9791` / `#9BA59F` / `#7C8882` | `#8D9C95` / `#6F7E77` / `#93A29B` | Caps labels / counts, captions / inactive tabs |
| `accent` / `accent_hover` / `accent_pressed` | `#176B4D` / `#12593F` / `#0E4A34` | `#1F7A57` / `#1A6E4E` / `#196A4B` | Primary buttons, selection bar, tab underline |
| `accent_text` / `accent_tint` / `accent_soft_border` | `#176B4D` / `#EAF5EF` / `#CFE6DB` | `#5CCB9C` / `#18382B` / `#2B5A47` | Accent text and icons, tinted surfaces |
| `on_accent` | `#FFFFFF` | `#FFFFFF` | Text on accent |
| `success` / `warning` / `danger` / `info` | `#2DA771` / `#B45309` / `#B42318` / `#2563A8` | `#48C48D` / `#E3A548` / `#F07A6E` / `#7DB1E8` | Status dots, latency, feedback levels |
| `danger_strong` / `on_danger` | `#B42318` / `#FFFFFF` | same | Destructive buttons |
| `focus_ring` | `#176B4D` | `#5CCB9C` | 2 px focus ring |
| `chip_bg` | `#F1F4F2` | `#202C27` | Chips |
| `avatar_bg` | `#233B32` | `#3E5A4E` | User avatar |
| `broadcast_bg` / `broadcast_fg` | `#FDECEA` / `#8F1D14` | `#4A1A16` / `#FFDAD5` | Broadcast warning strip |
| `icon_group` / `icon_subgroup` / `icon_host` | `#A57828` / `#5B6B64` / `#5B6B64` | `#D9A84E` / `#A9B8B0` / `#A9B8B0` | Default tree icon tints |
| `scrollbar` | `#C9D2CD` | `#45564E` | Overlay scrollbar thumb |
| `shadow` / `shadow_popover` | 6 % / 12 % of `#18231F` | 35 % / 45 % black | Soft elevation |

Environment badges (`ENV_BADGES`, `environment_badge_style(env, dark)`,
`environment_kind(env)`): production `#FFF1E5 / #8A4B1F`, staging
`#E8F1FB / #1D4F86`, development `#EEF1EF / #46534D`, anything else neutral
`#F1F3F2 / #4F5C56` (dark variants in `tokens.py`). Aliases such as
`prod`, `stage`, `uat`, `dev` and `qa` map to these kinds.

`Palette.status_color(state)` (connecting is amber) and
`Palette.level_color(level)` map states and feedback levels to roles. Never
hard-code a color in Python; ask `current_palette()`. The only exceptions are
colors the user picks (tree colors) and terminal color schemes
(`theme/terminal_schemes.py`).

### Scales

| Scale | Values |
|---|---|
| Spacing (`Space`) | `XS 4`, `S 8`, `M 12`, `L 16`, `XL 20`, `XXL 24`, `XXXL 32`, `GUTTER 36` px |
| Radius (`Radius`) | `SMALL 4` (checks), `KEYCAP 6`, `MEDIUM 8` (buttons, rows), `INPUT 10` (entries, logo tile), `LARGE 12` (cards, popovers, host tile), `CARD 18` (empty-state card), `PILL 999` |
| Sizes (`Size`) | top bar 76, sidebar 408 (300–520), page bar 73, host header 123, tabs 57, info bar 69 (band heights exclude their 1 px border), sidebar footer 66, tree row 41, tree inset 12, indent 25, logo tile 42, avatar 40, icon button 40, search 46, button 40 / 44 / 51 (hero), host tile 54, empty card 72, badge 22 |
| Layout (`Layout`) | Offsets measured on the reference: `Layout.Topbar`, `.Sidebar`, `.Tree`, `.Page`, `.Ready` (for example the 29 px logo inset, the 7 px gap above top-level groups, the 48 px info-bar field gap) |
| Type (`FontScale`, em of the 11 pt app font) | caps 12, badge 11, caption 13, mono 14, body 15, tree/subtitle 16, wordmark 20, hero title 22, host title 26, page title 28 px; caps tracking 0.12 em |
| Fonts | UI: Inter → Cantarell → Noto Sans → DejaVu Sans; mono: JetBrains Mono → Fira Code → DejaVu Sans Mono (`theme/fonts.py` picks the first installed family) |
| Motion (`Motion`) | `FAST 120`, `NORMAL 160`, `SLOW 200` ms, ease-out |
| Elevation | `sm_shadow` on primary buttons, the logo tile and the empty-state card; `sm_shadow_popover` on popovers, menus and toasts. Nothing else casts a shadow. |

`BASE_FONT_PX` is 11 pt at 96 dpi (14.67 px): `em(px)` converts a reference
pixel size to an `em` value, so the *Application font size* setting (default
11) scales the whole interface, and tree rows grow with it. GTK's
`min-height` excludes borders, so the stylesheet subtracts 2 px from the
outer token sizes of bordered controls.

### Contrast

`tokens.CONTRAST_PAIRS` lists every text/background and UI-component pair;
`tests/test_gtkui_theme.py` asserts WCAG AA (4.5:1 text, 3:1 UI) for both
themes, plus environment badges, the selected tree row and accent buttons.
`text_faint` is decorative (counts, captions next to stronger labels).

## Icons (`assets/icons/`, `widgets/icons.py`)

43 outline icons drawn on a 24 px grid with a 1.5 px round stroke, named
`sshm-<name>-symbolic.svg` (terminal, monitor, folder, chevrons, plus,
search, filter, settings, key, copy, more, activity, help, close, refresh,
split, grid, broadcast, download, clipboard, edit, trash, lock, user,
server, globe, check, warning, error, info, sidebar, stats, maximize,
restore, move, palette, tag, logout, folder-open, interrupt, eye). GTK 3's
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
| `.page-tabs`, `button.page-tab` | Tab bar | Terminal / Details / Activity, 2 px accent underline |
| `.page-canvas`, `.card`, `.detail-key`, `.detail-value`, `.activity-row`, `.activity-time` | Content | Canvas, Details and Activity cards |
| `.info-bar`, `.info-label`, `.info-value`, `.info-live`, `button.info-copy` | Info bar | Connection facts, docked live stats |
| `.empty-state`, `.ready-state`, `.empty-card`, `.empty-title`, `.empty-description`, `.empty-caption`, `button.hero` | Empty states | Ready to connect and other placeholders |
| `notebook.session-tabs`, `.session-strip-actions`, `.session-toolbar-row`, `.terminal-card` | Terminal tab | Session strip, toolbar, terminal card |
| `.statusbar`, `.stat-warn`, `.stat-critical`, `.stat-icon` | Live stats | Warn at 80 %, critical at 90 % |
| `.broadcast-strip`, `.broadcast-active`, `.tile-frame`, `.tile-header` | Workspace | Broadcast warning, tiles |
| `button.primary` / `.suggested-action`, `button.outlined`, `.destructive-action` | Buttons | Primary, secondary, destructive |
| `popover.sm-popover`, `.popover-menu-item`, `menuitem .menu-icon`, `.accel-hint`, `menuitem.destructive` | Menus | Popovers and menus |
| `.chip`, `.chip.accent`, `.badge`, `.status-dot.*`, `.toast*`, `.level-*`, `.caps`, `.mono` | Shared | Chips, counters, status, toasts, type helpers |
| `.section-title`, `.section-description`, `.form-help`, `.form-error`, `entry.invalid`, `.settings-sidebar` | Dialogs | Forms and settings |

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
- Never rely on `transparent` over stock GTK containers; always set an
  explicit token background and `background-image: none`. GTK themes paint
  nodes you don't see in the widget tree (TreeView draws rows and the empty
  area below them with `treeview.view.cell` nodes), so a transparent widget
  can still show the theme's base color. Transparent is only for overlays
  drawn on top of content, such as the scrolled window's undershoot and
  overshoot.
- Don't bind global accelerators to keys terminals need. Ctrl+K is passed
  through to a focused terminal (readline kill-line).
