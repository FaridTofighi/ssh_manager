# UI changelog — GTK front-end redesign

Screenshots: `docs/ui-before/` (original UI), `docs/ui-after/` (current
"Relay" UI, plus the reference comparison) and `docs/ui-target/` (the
reference). Phase-1 screenshots are in the history of `docs/ui-after/`
(commit b7597c5). Design reference: `docs/ui-design-system.md`. Code map:
`docs/ui-architecture.md`.

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
