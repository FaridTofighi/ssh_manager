# UI changelog — GTK front-end redesign

Screenshots: `docs/ui-before/` (original UI), `docs/ui-after/` (current
"Relay" UI, plus the reference comparison) and `docs/ui-target/` (the
reference). Phase-1 screenshots are in the history of `docs/ui-after/`
(commit b7597c5). Design reference: `docs/ui-design-system.md`. Code map:
`docs/ui-architecture.md`.

## Phase 8 — a press anywhere on a pane activates it

### Bug

With more than one terminal on screen, the active pane followed GTK keyboard
focus only.
- **Split pane:** a press on the header, footer, border or scrollbar of the
  other pane did nothing.
- **Tile mode:** clicks on a tile's footer, border or padding did nothing.
  Activating a tile from its terminal never updated the host bar or the
  status bar, because the tile's `focus-in-event` handler on the `TerminalTab`
  box never fired.
- **Cause, in code:** each path had its own bookkeeping
  (`_on_terminal_focus`, `_on_tab_focus`, `focus_pane`, `_focus_tab`).

### What changed

- **One activation path.**
  - The pure `ActivePane` (`gtkui/pane_activation.py`) holds the state.
  - `TerminalTab.activate_pane()` handles the panes of a tab.
  - `TerminalNotebook._on_tab_activated()` handles the current session. It
    updates tile frames, toolbar, host bar, status bar and the broadcast
    source.
  - Clicks, Alt+arrows, Ctrl+Tab, Alt+1…9, the session strip, new and closed
    splits, and focus-in all go through it. Re-activating the active pane is a
    no-op: no restyle, no toolbar or host-bar refresh.
- **`PaneFrame` is an input-only event box around the styled box.** A
  capture-phase gesture activates the pane on a left, middle or right press
  anywhere in it: terminal, scrollbar, header and its buttons, footer,
  padding, border.
  - The gesture never claims the press, so VTE still gets it: selection,
    double/triple click, Ctrl+click links, mouse reporting and the middle-click
    paste are unchanged.
  - The divider, the tile gutters and the scroll wheel do not activate.
- **A new split pane is the active pane.** Closing the active pane activates
  its neighbour.
- **Typing into a tile makes it the broadcast source** even if it was
  activated some other way.
- **API:** state CSS classes moved from the frame widget to `frame.style()`
  (the inner box). The CSS selectors are unchanged.

### Notes

- **Inactive window:** a press into a window that isn't focused activates
  the pane in the same press (keyboard focus is set on the window's focus
  widget). Raising the window is up to the window manager.
- **Offscreen test runs** log `gdk_window_move_resize_internal` criticals
  while the window is first laid out. The same count shows on the commit
  before this phase (Phase 7's pixel test logs 30 too), so it is a harness
  artifact, not part of this change.

### Verification

- **`tests/test_pane_activation.py`:** A→B→A, no-op re-activation, unknown
  panes, add → active, close active → neighbour, re-layout keeps the active
  pane, exactly one active through a sequence.
- **`tests/test_gtkui_pane_clicks.py`:** the window's content is rendered
  offscreen, and real `GdkEventButton` / `GdkEventScroll` events go through
  `Gtk.main_do_event`, hit-tested against the GdkWindow tree like GDK does.
  - Every region (terminal, header, footer, top/bottom/right border) of both
    split panes, and of 2, 4 and 6 tiles (6 = duplicated sessions), activates
    its pane. The check covers the `focused` class, keyboard focus, current
    tab and host bar.
  - A terminal press still reaches VTE. Middle and right presses activate and
    reach VTE. Drag selection still selects text.
  - The wheel, the divider and the tile gutter change nothing. A second press
    on the active pane changes nothing.
  - Header zoom and close activate first. After close, the neighbour is
    active.
  - Also checked: a new split is active, Alt+→, a split pane inside a tile,
    the tile ⇄ tabs toggle, and that a click sets the broadcast source.
  - Mutation check: with the frame gesture disconnected, 91 subtests fail
    across the split and tile region tests.
- **Full suite:** `python3 -m unittest discover` passes 375 tests.

## Phase 7 — visible pane boundaries (split pane, tile mode, duplicates)

Screenshots of the old layout: `docs/ui-before/pane-borders-tile-mode.png`
and `docs/ui-before/pane-borders-split-pane.png`. Tiles had a near-invisible
outline, and split panes had no border, gutter or label at all.

### What changed

- **`PaneFrame`** (`gtkui/widgets/pane_frame.py`) is shared by tiles and
  split panes.
  - It has a full border on all four sides and a canvas gutter between panes
    and around the group (6 px Compact, 8 px Comfortable).
  - **Header:** status dot, label, muted `user@host:port`, and on the right
    the Broadcast chip (tiles), zoom and close.
  - **Footer:** `● Connected · 59×35` per pane, inside the border.
- **Focused pane:** a 2 px accent border and an accent-tinted header; the
  other panes get a light veil.
  - Broadcast panes get a 2 px danger border and a `BROADCAST` badge.
  - A pane that is focused and broadcasting gets a danger border with an
    accent inner ring.
  - A failed or disconnected pane has a danger or warning border, with the
    status text in its footer.
- **Split pane:** both terminals sit in frames inside a styled `Gtk.Paned`.
  - The divider handle is gutter-wide with a centred grip that turns accent on
    hover or drag. Double-click resets it to 50/50, and no pane gets smaller
    than 120 px.
  - Panes are labelled `web-01 · pane 1` / `· pane 2`.
  - Closing the second pane gives its space and the focus back to the first.
    The first pane is the session itself, so it has no close button; close the
    tab instead.
- **Zoom:** maximize one pane or tile (button, or double-click its header)
  without leaving split or tile mode; again to restore.
- **Alt+←/→/↑/↓:** move focus between the panes of a split and between grid
  neighbours in tile mode. With nowhere to go, the keys reach the terminal.
  They are listed in the shortcuts window.
- **Broadcast chip:** the stock *Broadcast* check box in tile headers is now
  a compact chip. It is neutral when included and turns red only while
  broadcast is on.
- **New tokens:**
  - colors in both palettes: `pane_border`, `pane_border_focus`,
    `pane_border_broadcast`, `pane_header_bg`, `pane_header_bg_focus`,
    `pane_gutter`, `pane_dim`;
  - sizes in both densities: `PANE_GUTTER`, `PANE_RADIUS`, `PANE_HEADER`,
    `PANE_FOOTER`, `PANE_MIN`.

### Deviations from the brief

- **Border colors:** the brief's `#454951` (dark) and `#B9C2BD` (light) reach
  only 2.06:1 and 1.70:1 against the gutter, against its own ≥ 3:1 target. I
  used the nearest grays that meet it: dark `#5E6269` (3.03:1 on the gutter,
  2.93:1 on the terminal) and light `#868F8A` (3.09:1).
- **No three-pane split:** a tab can hold at most two panes. A third one
  would be a new feature, so there is no `split-three-panes` screenshot;
  `tile-four` shows four framed panes instead.
- **Footer contents:** the per-pane footer shows status and size. CPU,
  memory and disk belong to the session and stay in the session's status bar,
  which tile mode shows inside each tile.

### Verification

- **`tests/test_gtkui_panes.py`, tokens:** pane tokens in both palettes and
  densities; border, focus and broadcast contrast against the gutter;
  terminal contrast; pane CSS uses tokens.
- **`tests/test_gtkui_panes.py`, pixels:** offscreen renders of a split, three
  tiles and broadcast tiles in dark/light × Compact/Comfortable.
  - Every pane's border pixel on all four sides equals the expected token.
  - Focused and broadcast panes differ from the others.
  - The gutter between neighbours is ≥ 6 px, and every pixel in it equals
    `pane_gutter`.
  - The divider's minimum and its double-click reset are checked on real
    allocations.
- **`tests/test_gtkui_panes.py`, behaviour:** a single pane has no frame;
  labels; Alt+arrow focus in splits and tiles; zoom and restore; closing a
  pane gives back its space and focus; the terminal is kept across split and
  tile toggles; the Broadcast chip.
- **Label helper:** `tests/test_gtkui_tab_labels.py` covers the
  pane-label suffixes.
- **Screenshots:** `split-two-panes`, `tile-two`, `tile-four`,
  `tile-broadcast` and `duplicate-session`, each in dark and light, Compact
  and Comfortable.

## Phase 6 — session tabs only

Screenshot of the old layout: `docs/ui-before/session-tabs-current.png`. The
row under the host bar used to mix three content tabs (Terminal · Details ·
Activity) with the session tabs, and two sessions of one host had the same
label.

### What changed

- **Content tabs removed:** the Terminal · Details · Activity bar is gone,
  together with Ctrl+1/2/3. The workspace is the session area: the ready state
  when no session is open, otherwise the session strip and the terminal.
- **Session strip:** one row (33 px in Compact):
  - one tab per session, then `+` (Quick Connect) right after the last tab;
  - the all-sessions list and the session toolbar right-aligned;
  - overflow chevrons when the tabs do not fit;
  - drag a tab onto another to reorder.
- **Tab contents:**
  - a status dot; the status is also spelled out in the tooltip;
  - the label, ellipsized between 96 and 220 px;
  - a close button shown on hover and on the active tab, plus middle-click to
    close;
  - the activity marker on background tabs.
- **Tab states:** the active tab has accent text and a 2 px accent underline;
  hover is neutral, and keyboard focus shows a ring. Enter or Space activates
  a tab, Delete closes it, and the Menu key opens its menu, which is
  unchanged.
- **Tab labels:** *Settings → Appearance → Tab label* offers *Name* (default;
  the IP or host name when there is no alias), *IP address* (`ip:port` when
  the port is not 22, `[v6]:port`) and *Name and IP*
  (`alias · 10.60.110.98`).
  - Duplicates get stable counters: `web-01`, `web-01 (2)`, `(3)`. Closing a
    tab never renames another, and freed counters are reused.
  - A name given with Rename (F2) always wins.
  - The setting is saved as `tab_label_mode`; an old `page_tab` key is
    ignored.
  - The pure `gtkui/tab_labels.session_tab_labels()` implements all of this.
- **Tab tooltip:** the alias, `user@host:port`, the status and size
  (`Connected · 121×38`), and "Duplicate session" when it applies.
- **Tile mode:** tile headers use the same labels.
- **Host bar follows the active session:** name, badge, address, last
  connected, latency, Reconnect and `···` describe the session in front, or
  the tree selection when no session is open. Selecting another host in the
  tree does not switch tabs; double-click, Enter or Connect still open a
  session.

### Where Details and Activity went

- **Details:** the host bar's new ⓘ button (Ctrl+I, F9, `···` → *Host
  details…*, or the ready state's *Host details* link) opens a 360 px panel
  over the right edge of the session area, below the strip.
  - It holds the fact rows, the tag chips and Edit… / Duplicate / Copy SSH
    command.
  - Esc, a click elsewhere in the workspace, its close button or ⓘ close it.
  - It is an overlay, so the terminal keeps its size; a test measures this.
  - In a focused terminal, Ctrl+I stays Tab (0x09) and Esc stays Esc; use F9
    or the button there.
- **Activity:** `···` → *Host activity…* opens the panel's second page. The
  top bar's activity popover gains *All* / *This host* filters, and its empty
  state reads "No activity yet".

### Fixes found on the way

- The `···` menu's *New Session* entry never appeared: the section was built
  with its item list in the name argument.

### Effect on space

- **Comfortable at 1920 × 1041 with a session open:** the terminal grows from
  539 to 610 px (51.8 % → 58.6 %), as the 58 px tab band is gone.
- **Compact, one row of sessions:** unchanged at 84.9 %; the strip already
  shared its row with the content tabs.
- **Compact without a session:** the canvas is taller (85.6 % → 88.9 %).
- **At 960 px wide:** 73.8 % → 78.2 %.

### Verification

- **Unit tests** (`tests/test_gtkui_tab_labels.py`): the three modes, a
  missing alias, ports other than 22, IPv6, very long names, duplicates
  (2)/(3), stable counters after closing, counter reuse, the rename override,
  and the setting's round-trip including old keys.
- **GTK tests:**
  - no Terminal/Details/Activity widget in the strip or the host bar;
  - two sessions of one host get distinct labels, and the second keeps its
    label after the first closes;
  - tooltips contain `user@host:port`;
  - the panel opens from ⓘ, Ctrl+I and `···`, and Esc closes it;
  - the panel does not change the terminal's allocation (real offscreen
    render);
  - 16 tabs show the overflow chevrons and the all-sessions list;
  - reordering works;
  - the host bar follows the active session and ignores tree clicks;
  - the activity popover's *This host* filter works.
- **Screenshots:** `compact-session-{dark,light}` show
  `apzkrfpspst0103.fns` and `apzkrfpspst0103.fns (2)`, and `main-two-tabs-*`
  show a duplicate pair. New: `compact-host-details` and
  `compact-host-activity`.
- **Relay reference comparison:** the reference still shows the removed band.
  `tools/compare_to_reference.py` now lists it as removed. It compares the
  ready state, which moved 50 px up, on its horizontal position and size, and
  the empty-state region at that shift. Everything passes.

## Phase 5 — neutral-gray dark theme

Approved design: `docs/ui-target/neutral-dark-palette.png` (full-window mock)
and `docs/ui-target/neutral-dark-mock.png` (token table). Only colors change;
the layout, densities and behavior are the same.

- **Dark palette:** the surfaces move from green-tinted to neutral cool gray.

  | Token | Before | After |
  |---|---|---|
  | `bg_canvas` | `#0F1613` | `#121315` |
  | `bg_window` | `#131B18` | `#151619` |
  | `bg_surface` | `#17201C` | `#1A1B1E` |
  | `bg_surface_raised` | `#1C2722` | `#212226` |
  | `bg_input` | `#1B2521` | `#16171A` |
  | `bg_hover` | `#202C27` | `#25272B` |
  | `bg_selected` / `accent_tint` | `#18382B` | `#202A25` |

  Borders, text, chips, avatar, scrollbar and tree icons follow the approved
  table. The accent family, the semantic colors, folder amber, broadcast and
  shadows are unchanged.
- **Selected tree row:** a neutral fill with a faint green hint plus the
  accent bar, instead of a green panel.
- **Environment badges (dark):** re-derived from the neutral surface:
  production `#2E2620 / #F0B27A`, staging `#1F2733 / #8DB7E8`, development
  and neutral `#25272B / #A3A8B0`.
- **Built-in dark terminal scheme:** `#16171A` / `#D6D9DE` (was the bluish
  `#1F2329` / `#F5F6F8`), with a neutral cursor `#B8BCC2` and an accent-tinted
  selection `#28443A`. The ANSI palette, the Light and Solarized schemes and
  user colors are unchanged.
- **Popovers:** the filter and message-history popovers carry `sm-popover`,
  so they are styled wherever they are attached.
- **No literal colors:** the tree's fallback color comes from the palette.
  A test now fails on any `#rrggbb` outside `tokens.py` and
  `terminal_schemes.py`.

### Verification

- **Mock comparison:** `tools/ui_snapshots.py docs/ui-after neutral-dark-match`
  then `tools/compare_neutral_dark.py`. All 12 regions match: top bar, sidebar
  header, tree panel, selected row, footer, page bar, host header, tabs,
  canvas, terminal, status bar and primary button. The error is 0.0–0.02 %,
  and every sampled pixel equals its token exactly
  (`docs/ui-after/neutral-dark-report.md`).
- **Tests** (`tests/test_gtkui_theme.py`):
  - the exact palette;
  - neutrality (surfaces and borders spread ≤ 12, no green cast);
  - elevation steps ≥ 1.05:1;
  - text 4.5:1 on surface, raised, canvas and input;
  - accent, semantic and badge contrast;
  - focus ring 3:1 on every layer;
  - terminal 12.7:1, with selected text 7.5:1;
  - user schemes untouched;
  - no old green literals in the dark CSS;
  - `text_faint` used only decoratively;
  - no literal colors outside the token modules.
- **Surfaces:** `tests/test_gtkui_surfaces.py` and `tools/debug_surfaces.py`
  are clean (Adwaita, Adwaita:dark, HighContrast; System/Light/Dark; focused
  and backdrop).
- **Light theme:** every light screenshot is pixel-identical to before,
  except the terminal area in the two light scenes with a live session (the
  built-in Default terminal scheme is dark in both themes).
- **New screenshots:** `toasts-dark` (one toast per level) and
  `popovers-dark` (avatar, help, workspace and message history).
  Context menus are `Gtk.Menu` popups, which only draw in a mapped window; the
  snapshots never map windows, so their colors are checked through the
  stylesheet tests and the surface audit.

### Deviations from the brief, kept on purpose

- **Elevation:** the approved values put the window base only 1.03:1 above the
  canvas, against the ≥ 1.05 rule. The other steps meet it, and the window
  base is almost never visible.
- **Neutrality:** the approved text grays (`#A3A8B0`, `#8A9098`, `#6B7079`,
  `#8F959E`) are cool grays with a 13–15 channel spread, so the test allows
  ≤ 16 for text and ≤ 12 for surfaces and borders. It also asserts that no
  neutral has a green cast.
- **`border_input`:** 1.5:1 on the surface, below the 3:1 UI target, as in the
  approved table. Inputs are still identified by their fill, label and focus
  ring.

## Phase 4 — compact density

Screenshot of the old layout: `docs/ui-before/density-current.png`.
The Relay look is unchanged (palette, icons, language), but the interface is
now **compact by default** so the terminal gets the window. The previous
geometry is kept as the *Comfortable* density, which still passes the Relay
reference comparison.

### Chrome budget at 1920 × 1041, sidebar open, one live session

Measured from real widget allocations
(`tools/ui_snapshots.py docs/ui-after compact-session-dark comfortable-session-dark`).

| Band | Before (Comfortable) | After (Compact) |
|---|---:|---:|
| Top bar | 77 | 44 |
| Page bar | 74 | — (merged) |
| Host header / host bar (row A) | 124 | 44 |
| Tabs (row B) | 58 | shared with the session chips |
| Session strip, borders, terminal padding | 74 | 37 |
| Status bar | 70 | 28 |
| **Chrome total** | **502** | **157** |
| **Terminal** | 1462 × 539 = **51.8 % of the height, 76.1 % of the width** | 1624 × 884 = **84.9 %, 84.6 %** |
| Sidebar width | 407 | 283 |
| Tree rows fully visible (42-host inventory) | 17 (41 px rows) | **31** (28 px rows, 30 px groups) |

Focus mode reaches 96.1 % of the height and 99.4 % of the width. At 960 ×
720 (narrow variant, sessions on their own strip) the terminal gets 73.8 % ×
75.7 %.

### What changed

- **Density presets** (`theme/density.py`): Compact (default) and
  Comfortable share one set of keys. Views lay themselves out in
  `apply_density()`.
- **Interface density setting:** Settings → Appearance → *Interface density*,
  saved in the UI state. Switching it at runtime regenerates the CSS and lays
  the window out again, and sessions and scrollback are kept.
- **Type:** body, tabs and tree text 13 px; captions 11.5; mono 12; caps 11
  (tracking 0.08 em); wordmark 15; "Hosts" 15 semibold; host title 16
  semibold; empty-state titles 17; dialog titles 15.
  - Everything is `em`-relative to the app font × the density's base scale,
    and floored at 11 px at any app font size.
  - The terminal keeps the app font size.
- **Top bar:** 44 px with a 28 px logo, avatar and icon buttons. The
  workspace switcher shows the name and chevron only. The health indicator
  keeps its dot and short text.
- **Sidebar:**
  - 284 px wide (220–480).
  - One 40 px header row: "Hosts", a host count and a 28 px *Add host*. The
    "CONNECTION MANAGER" eyebrow is hidden.
  - 32 px search and filter.
  - Tree rows 28 px (groups 30), indent 16, group gap 3, selection inset 6
    with a 3 px bar and radius 6.
  - 32 px footer.
- **Host bar (row A):** the page bar and host header merge into one line:
  tile, title, environment badge, `user@host`, then *Last connected …* and the
  latency as inline text (captions in tooltips), *More*, and the primary
  button. The breadcrumb only shows as a muted prefix for groups.
- **Tabs (row B, 34 px):** while the Terminal tab shows sessions, the
  session chips and toolbar share the row. Narrow windows and tile mode keep a
  separate 32 px strip.
- **Terminal:** edge to edge, with no card, margin or radius, and 4–6 px of
  inner padding.
- **Status bar:** one 28 px line. Session stats sit on the left (with the
  same amber and red thresholds); `user@host:22 · Credential: … · Proxy: …`
  sits on the right in muted text, with captions in tooltips.
- **Dialogs:** they open at 0.85 × their old default size (Host 760 × 560)
  and stay resizable, with 30 px controls, 16 px padding and an 8 px gap.
  Popover and menu rows are 28 px; toasts, cards and the broadcast strip use
  the same tokens.
- **Focus mode** (F11, **Ctrl+Shift+M**, avatar menu, session overflow
  menu) replaces "Maximize workspace". It hides the top bar, sidebar, host bar
  and status bar and keeps row B, the broadcast strip and the terminal. Esc
  outside a terminal leaves it. The state is saved per window.
- **Narrow windows** (< 1100 px): secondary labels hide (health text,
  address, search keycap) and sessions get their own strip.

### Fixes found on the way

- **Shortcuts-window crash:** GTK 3.24 leaves dangling CSS nodes behind a
  destroyed `Gtk.ShortcutsWindow`, and the next theme or font change
  segfaulted. This also happened before this phase and made the test suite
  crash now and then. The window is now created once, hidden on close and
  reused.
- **Empty toolbar row:** the session toolbar's own row was re-shown by
  `show_all()`, leaving an empty band (8 px in Comfortable) above the session
  strip.
- **Search keycap:** "Ctrl K" no longer covers the search placeholder in
  narrow sidebars.

### Verification

- **Tests:** `tests/test_gtkui_density.py` checks:
  - preset key parity, the accessibility floor and the setting's round-trip;
  - CSS for every theme × density, and the 11 px floor at 10, 11, 14 and 24 pt;
  - runtime density and theme switches with a live session (same terminal,
    same cursor);
  - the real 1920 × 1041 layout: terminal ≥ 78 % / 80 %, top bar ≤ 44,
    status bar ≤ 28, ≥ 28 tree rows;
  - that Comfortable keeps the reference geometry.
- **Comfortable regression:** Comfortable renders are pixel-identical to
  Phase 3, apart from the intended changes (the new setting row, the removed
  empty toolbar band, the hidden keycap at 960 px). `relay-match` still passes
  `tools/compare_to_reference.py`.
- **Screenshots** in `docs/ui-after/`:
  - `compact-session-{dark,light}`, `compact-empty-{dark,light}`,
    `compact-narrow`, `compact-focus` and `comfortable-session-dark`, with
    `*-budget.md` files;
  - `main-*` and `dialog-*` are now Compact; `relay-*` stays Comfortable.
  - HiDPI (`GDK_SCALE=2`) was checked.

## Phase 3 — fix: stock theme backgrounds showing through

Bug: `docs/bugs/sidebar-background.png`. In the dark theme the sidebar's tree
panel was gray `#2D2D2D` (Adwaita-dark's `theme_base_color`) instead of
`bg_surface` `#17201C`. Under `GTK_THEME=Adwaita` with the app in dark mode it
was white, and the light theme showed `#FCFCFC` in unfocused windows.

- **Cause** (found with `tools/debug_surfaces.py`): TreeView paints every row,
  and the empty area below the last row, with a transient
  `treeview.view.cell` style node (`style_context.save()`). The stylesheet
  only made the widget's own node transparent
  (`scrolledwindow > treeview.view`). The `.cell` node kept the theme's
  `treeview.view` background in every state.
- **Same class elsewhere:**
  - The config viewer and host dialog text views (`textview text`).
  - The smart-group and message-history popover lists.
  - The keyboard-shortcuts window, which did not carry the `sm-dialog` class
    and so showed the stock header bar, search bar, popover and scrollbars.
- **Fix:**
  - Every node from `.sidebar-pane` down to the TreeView cells, and the
    sidebar empty states, now gets `@sm_bg_surface` with
    `background-image: none` in every state, including `:backdrop`.
  - A "stock surfaces" block gives lists, icon views, text views, notebook
    header and stack, search bar, action bar and popover lists explicit tokens.
  - The shortcuts window gets `sm-dialog`.
  - Selected and hovered rows are unchanged: the row renderer still paints
    the inset highlight and accent bar.
- **Verification:**
  - `tools/debug_surfaces.py` reports zero opaque non-token backgrounds for
    the main window (hosts, no results, first run) and 9 dialogs. This covers
    Adwaita, Adwaita:dark and HighContrast, each in modes System (light and
    dark desktop), Light and Dark, focused and backdrop. Yaru and Arc are
    checked when installed.
  - `tests/test_gtkui_surfaces.py` covers the pixels, including the runtime
    mode switch, plus a CSS lint.
  - `docs/ui-after/sidebar-{before,after}-*.png` and
    `sidebar-surfaces-{before,after}.md` show the per-theme comparison.
  - `relay-match.png` is pixel-identical to before. Only dark-theme tree areas
    and the config viewer's text view changed in the other snapshots.

## Phase 2 — "Relay" redesign

The GTK UI now reproduces `docs/ui-target/relay-reference.png` (light theme)
with a derived dark theme. Business logic, the settings model and the CLI UI
are unchanged; every feature has a new place (see the move map).

### Highlights

- **Tokens**: a green accent palette sampled from the reference (accent
  `#176B4D`, canvas `#F5F7F6`, borders `#DFE5E1`, text `#18231F` / `#5B6B64`),
  wider spacing and radius scales, reference component sizes (`Size`) and
  measured offsets (`Layout`), an em type scale on an 11 pt base, and
  Inter → Cantarell font resolution. Every stock control is restyled; no
  GTK default blue remains.
- **Icons**: 43 bundled outline SVG icons (`sshm-*`), rendered with Rsvg in
  the widget's CSS color and at the device scale.
- **Top bar** (76 px): logo tile and "SSH Manager" wordmark aligned with the
  sidebar edge, a **workspace switcher** (current, recent and shared
  inventories, Open Shared Inventory, Open Config Folder), a **health
  indicator** ("All systems operational", "N sessions active", "N warnings",
  "N sessions failed"), Activity (message history), Help and an **avatar
  menu** with the former app-menu items.
- **Sidebar**: CONNECTION MANAGER / Hosts header with **Add host**, search
  with a **Ctrl K** keycap, outlined filter button with an accent dot, a
  footer with **Settings**, **Keys** and a collapse button.
- **Tree**: custom-painted 41 px rows — chevrons, amber folders on top-level
  groups, monitor icons, counts, status dots, an inset rounded selection with
  a 4 px accent bar, hover and focus ring. Chevron clicks and Left/Right
  expand/collapse; collapsed groups survive rebuilds. Top-level groups named
  after deployment stages are listed in pipeline order (production, staging,
  development, test), then the rest alphabetically.
- **Host page**: page bar (breadcrumb, ··· menu, Connect / Reconnect /
  Connect all), host header (icon tile, title, environment badge, mono
  `user@host`, LAST CONNECTED, LATENCY), **Terminal / Details / Activity**
  tabs and an info bar (CONNECTION / AUTH / PROXY, copy SSH command) that
  docks the live session stats.
- **Ready to connect** empty state with host, group, selection, nothing
  selected and empty-inventory variants; the caption names the
  authentication ("Using ed25519 authentication").
- **Terminal tab**: session tabs and the session toolbar share one strip;
  terminals sit in a rounded card in the terminal's background color.
- **Data**: last-connected times and a per-host activity log
  (`.host_history.json` next to the UI state), a non-blocking cached TCP
  latency probe (off for proxied hosts, switchable in Settings →
  Connection), auth summaries from vault / credential / key type / password
  / agent.

### Move map

| Existing feature | New place |
|---|---|
| Header: New Host split button / templates / Quick Connect | Sidebar **Add host** (right-click, Shift+click or long-press for templates, Quick Connect, New Group, New Child Group), Ctrl+N, Ctrl+Shift+T, "+" in the session strip, empty states |
| Header: app menu (Settings, Credentials, Vault, SSH Config File, Open Shared, Git updates, Reload, About, Shortcuts) | Avatar menu (plus Maximize Workspace, Toggle Sidebar, Quit), Help popover (Shortcuts, Documentation, About), sidebar footer (Settings, Keys) |
| Header: search / maximize workspace / message history | Ctrl+K (or Ctrl+F) search in the sidebar; maximize in the avatar menu and F11; history behind the Activity button and the health indicator |
| Header: sidebar toggle | Ctrl+B, the footer collapse button, and an expand button in the top bar while the sidebar is hidden |
| Header: inventory path | Workspace switcher (tooltip and menu show the path) |
| Details drawer (facts, tags, actions, F9) | **Details** tab, page-bar primary button and ··· menu; F9 switches Terminal ⇄ Details |
| Details drawer bulk actions | Slim accent-tint **bulk bar** under the tree when ≥ 2 hosts are selected |
| Terminal status bar | Docked into the info bar while a session is live (each tile keeps its own in tile mode) |
| Session toolbar, tabs, tile mode, broadcast, split pane | Session strip on the Terminal tab — behavior unchanged |
| Tree context menus, DnD, smart groups, filters | Same behavior, restyled |
| Toasts, broadcast strip | Same behavior, restyled |

### Shortcut changes

| Action | Shortcut |
|---|---|
| Search hosts | **Ctrl+K** (new; passed through to a focused terminal as readline kill-line) or Ctrl+F |
| Terminal / Details / Activity tab | **Ctrl+1 / Ctrl+2 / Ctrl+3** |
| Switch Terminal ⇄ Details | F9 (was: toggle details drawer) |
| Quit | **Ctrl+Q** |
| Expand / collapse a tree group | Right / Left (also clicking the chevron) |

### Behavior notes

- The default application font size is 11 pt (was 14); the whole UI scales
  from it, including tree row heights.
- The Terminal tab shows the ready state until a session exists; with
  sessions open it keeps showing them while you browse other hosts, and
  selecting a host with a session offers *Reconnect* (New session in ···).
- On narrow windows the info bar hides AUTH and PROXY while a session is
  live so the live stats stay readable (both remain on the Details tab).
- Window title and wordmark read "SSH Manager"; native window controls stay.

### Verification

`tools/ui_snapshots.py docs/ui-after relay-match` renders the reference scene
at 1852 × 934 and `tools/compare_to_reference.py docs/ui-after/relay-match.png
--out docs/ui-after` compares it (`docs/ui-after/relay-report.md`,
`relay-diff.png`, `relay-side-by-side.png`):

| Region | Mean error | Masked error (text edges excluded) |
|---|---:|---:|
| top bar | 2.38 % | 0.81 % |
| sidebar header | 2.74 % | 0.21 % |
| search row | 1.26 % | 0.04 % |
| tree | 2.36 % | 0.06 % |
| sidebar footer | 1.45 % | 0.13 % |
| page bar | 1.01 % | 0.15 % |
| host header | 1.35 % | 0.13 % |
| tabs | 0.62 % | 0.07 % |
| empty state | 1.74 % | 0.15 % |
| canvas | 0.46 % | 0.04 % |
| info bar | 1.39 % | 0.00 % |

Overall 1.17 %. All 21 landmarks (borders, dividers, buttons, logo, selection
bar, tab underline and text cap/baselines) are within 2 px. Remaining
differences: Inter is not installed here, so Cantarell is used (glyph
shapes and widths differ — the masked error isolates this); the wordmark
reads "SSH Manager" instead of "Relay"; the tree lists children
alphabetically ("Data services" before "Web cluster", "kafka-stage-01"
before "zookeeper-01") where the mock uses authoring order; the sidebar
footer has the collapse button the spec asks for; window controls are hidden
in the snapshot only, as the reference is a borderless mock.

## Phase 1 — first redesign

### Highlights

- **Complete light and dark themes**, generated from one set of design
  tokens. A *System / Light / Dark* setting switches without a restart and
  follows the desktop color scheme. Light mode was previously unstyled.
- **New header bar**: an app menu (Settings, Credentials, Vault, SSH Config
  File, Open Shared Inventory, Git updates, Reload, Keyboard Shortcuts,
  About), a primary **New Host** split button (templates, Quick Connect,
  groups), a sidebar toggle, search, message history, settings and maximize.
  Credentials and Vault now have different icons.
- **Always-visible search** in the sidebar. An advanced-filter popover holds
  syntax help, smart groups and *Clear filter*. An active filter shows as a
  chip.
- **Session tree**: themed icons (tinted by your colors), host counts on
  groups, status glyphs (● connected, ◐ connecting, ✖ failed) with tooltips,
  a "No matching hosts" state with *Clear*, a first-run state (*Add Host /
  Open Config Folder*), and context menus grouped into Connect / Edit /
  Organize / Delete with icons and shortcut hints.
- **Details drawer**: stable fact rows ("—" when empty), tag chips, and
  actions (Connect, Edit, Duplicate, Copy SSH/SCP command, Move, Forget
  Password, Delete). It collapses with F9.
- **Workspace**: one session toolbar instead of a button row inside every
  tab; tabs with status dot, activity marker, middle-click close and a
  context menu (Reconnect, Duplicate, Split, Rename, Save Transcript, Close,
  Close Others, Close to the Right); a **+** button for Quick Connect; a thin
  status bar with connection state, terminal size and CPU/memory/disk
  (amber at 80 %, red at 90 %).
- **Broadcast safety**: a persistent red strip ("Broadcast is ON — typing is
  mirrored to N sessions") with a *Stop Broadcasting* button, and red borders
  on the sessions that receive input.
- **Notifications**: toasts at four levels. Info and success dismiss
  themselves; warnings and errors stay until dismissed and can carry an
  action. All messages are kept in a history popover. Warnings no longer look
  like errors.
- **Dialogs** share one framework: consistent spacing, Cancel then primary
  button, destructive styling, inline validation (alias, host name, port,
  `key=value` options) and password fields with show/hide, Caps Lock hint,
  strength and match feedback.
- **Keyboard**: every action has an entry in *Keyboard Shortcuts*
  (Ctrl+? / F1); the tree supports Enter, Delete, Shift+F10 and type-ahead.
- **Accessibility**: tooltips and accessible names on all icon buttons, a
  visible 2 px focus ring, WCAG AA contrast in both themes, and status never
  shown by color alone.

### New features

- **Quick Connect** (Ctrl+Shift+T): `[user@]host[:port]` or the alias of a
  saved host.
- **Copy SSH Command** for a host (Copy SCP already existed).
- **Rename session tab** (F2 in the workspace), **Duplicate session**
  (Ctrl+Shift+D), **Close Others / Close Tabs to the Right**.
- **Open Config Folder** from the empty sidebar.
- Git update checks can be run on demand from the app menu.
- **About** dialog.

### Moved features (old location → new location)

| Feature | Before | Now |
|---|---|---|
| Credentials, Settings, Config, Vault, Open Shared | Header bar text buttons | App menu (Settings also has a header button) |
| Git updates | Permanent header button when updates exist | Badge on the app menu, a one-time toast with *Details*, menu item |
| Maximize terminal area | Header button with a fullscreen icon | Header toggle (F11) with "Maximize workspace" tooltip |
| Sidebar collapse | Detached arrow column left of the sidebar | Header toggle (Ctrl+B) |
| Search & filter | Hidden panel behind an icon (Ctrl+F) | Always-visible search; filter in a popover next to it |
| Smart groups | Card inside the search panel | Filter popover |
| Bulk Connect / Move / Tag-Color | Bar at the bottom of the content area | Details drawer when several hosts are selected |
| Show All Sessions / Broadcast Input | Text toggles above the tabs | Session toolbar icons (Ctrl+Shift+A / Ctrl+Shift+B) |
| Duplicate Pane / Close Duplicate | Text buttons in every tab | Session toolbar *Split pane* toggle, tab menu (Ctrl+Shift+E) |
| Save Transcript, Strip ANSI on Copy | Text buttons in every tab | Session toolbar (Ctrl+Shift+S), tab menu |
| Show Stats | Button in every tab | Session toolbar stats toggle |
| Reconnect banner | Custom box above the terminal | `Gtk.InfoBar` with *Reconnect* and *Close Session* |
| Status messages | 5-second banner at the bottom | Toasts and message history |
| Prefer dark theme | Switch | Theme: Follow system / Light / Dark |
| Application font size | Combo with 13 values | Spin button 10–48 |
| Vault entry add/edit | Three consecutive text prompts | One form |
| Config viewer import/export | Four buttons in a row | Toolbar menu |

### Behavior changes and fixes

- Delete confirmations name what they delete ("Delete Host", "Delete
  Group", "Delete Child Group", …) and default to Cancel. Non-delete
  questions use the real action label.
- Editing a host that uses **ProxyCommand** no longer rewrites it into
  ProxyJump; ProxyCommand stays in *SSH Options*.
- **Reconnect** in the host menu appears only when a session for that host
  is open, and reconnects it. It used to just open another session.
- Hosts found by search keep their child-group path in the tree. Groups
  without matches are hidden while a search or filter is active.
- The tree keeps the selection when it is rebuilt (search, theme change,
  edits).
- Remote stats are only polled while the stats view is enabled.
- Broadcast still needs two sessions and switches to tile view; leaving tile
  view turns it off. `Esc` stops broadcasting only when focus is outside a
  terminal (the tree or toolbar), because terminal programs need `Esc`. The
  strip button and Ctrl+Shift+B always work.
- Closing the config viewer with unsaved edits asks for confirmation.
- Session tiles shrink to the workspace width instead of scrolling
  sideways.

### Keyboard shortcuts

| Action | Shortcut |
|---|---|
| New host | Ctrl+N |
| Quick connect | Ctrl+Shift+T |
| Connect to selected host | Ctrl+T, Enter (in the tree) |
| Search hosts | Ctrl+F |
| Reload configuration | F5 |
| Settings | Ctrl+, |
| Application menu | F10 |
| Keyboard shortcuts | Ctrl+? or F1 |
| Rename selected item (tree) / session tab | F2 |
| Delete selected item (tree) | Delete |
| Context menu (tree) | Shift+F10 or Menu |
| Close session | Ctrl+W |
| Next / previous session | Ctrl+Tab / Ctrl+Shift+Tab (also Ctrl+PgDn / Ctrl+PgUp) |
| Go to session 1–9 | Alt+1 … Alt+9 |
| Duplicate session | Ctrl+Shift+D |
| Reconnect session | Ctrl+Shift+R |
| Split pane | Ctrl+Shift+E |
| Save transcript | Ctrl+Shift+S |
| Show all sessions (tiles) | Ctrl+Shift+A |
| Toggle broadcast input | Ctrl+Shift+B |
| Copy / paste in the terminal | Ctrl+Shift+C / Ctrl+Shift+V |
| Send interrupt | Ctrl+Shift+K |
| Zoom in / out / reset | Ctrl++ / Ctrl+- / Ctrl+0 |
| Toggle sidebar | Ctrl+B |
| Toggle details | F9 |
| Maximize workspace | F11 |
| Clear search / leave a field | Esc |
