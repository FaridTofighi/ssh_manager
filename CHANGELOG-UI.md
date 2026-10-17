# UI changelog — GTK front-end redesign

Before/after screenshots: `docs/ui-before/` and `docs/ui-after/`. Design
reference: `docs/ui-design-system.md`. Code map: `docs/ui-architecture.md`.

## Highlights

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

## New features

- **Quick Connect** (Ctrl+Shift+T): `[user@]host[:port]` or the alias of a
  saved host.
- **Copy SSH Command** for a host (Copy SCP already existed).
- **Rename session tab** (F2 in the workspace), **Duplicate session**
  (Ctrl+Shift+D), **Close Others / Close Tabs to the Right**.
- **Open Config Folder** from the empty sidebar.
- Git update checks can be run on demand from the app menu.
- **About** dialog.

## Moved features (old location → new location)

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

## Behavior changes and fixes

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

## Keyboard shortcuts

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
