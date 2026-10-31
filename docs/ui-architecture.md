# SSH Manager GTK UI architecture

```
ssh_manager/
  gtk_main.py           CLI entry → SSHManagerApplication
  gtk_app.py            SSHManagerApplication + SSHManagerWindow (composition root) + re-exports
  gtk_terminal.py       TerminalTab (one session) and TerminalNotebook (workspace)
  password_dialog.py    re-export of gtkui.dialogs.password
  config_viewer.py      GTK-free config lint/save helpers (+ lazy dialog re-export)
  gtkui/
    constants.py        RowKind, TreeColumn, Icons
    i18n.py             _() / ngettext()
    validation.py       alias, port, host name and group-name validators
    menu_spec.py        MenuItemSpec / MenuSection / menu_layout()      (GTK-free)
    view_models.py      DetailContent / DetailAction / status_text()      (GTK-free)
    host_facts.py       relative time, latency levels, auth summary, breadcrumbs, connection text (GTK-free)
    health.py           compute_health() for the top-bar indicator     (GTK-free)
    workspaces.py       workspace (inventory) list, recent inventories   (GTK-free)
    identity.py         avatar initials of the local user              (GTK-free)
    latency.py          LatencyProbe: threaded, cached TCP connect timing
    host_history.py     HostHistory: last connected + activity (.host_history.json)
    unlock_flow.py      start-up: set / ask for the master password, recovery key, key file missing
    migration_flow.py   the dialogs of the move to the key file (ssh_manager/migration.py does it)
    assets/icons/       bundled sshm-*-symbolic.svg outline icons
    assets/app-icon/    the application icon "ophion" (hicolor PNGs, see "Icons" below)
    theme/              density (Compact/Comfortable presets), tokens (palette; live Space/Size/
                        Layout/FontScale views), stylesheet generator, fonts, css_dark/css_light,
                        loader, terminal_schemes
    widgets/            chips, status_dot, dot, toast, empty_state, ready_state, secret_entry,
                        icon_button, menus, forms, icons (SvgIcon), searchable_list
    dialogs/            base, confirm, prompt, files, secrets, settings_layout, list_manager,
                        host_dialog, preferences, credentials, credentials_transfer, group_picker,
                        child_group_chooser (New Host / drop on a top-level group: search +
                        list of its child groups, "Create new group"), config_viewer, password,
                        bulk_tag, native_export / native_import (the .sshm export dialog and
                        import wizard), security_page (Settings → Security)
    views/              header (top bar), sidebar, session_tree, host_page, workspace, statusbar
    controllers/
      ui_state.py           UiState + load/save (tolerant)                 (GTK-free)
      notifications.py      ToastController + MessageHistory               (GTK-free)
      tree_controller.py    search/filter parsing, row building, empty states (GTK-free)
      details_presenter.py  selection → DetailContent, quick-connect parser (GTK-free)
      context_menus.py      MenuSpec builders for the tree                  (GTK-free)
      shortcuts.py          action table, accelerators, Gtk.ShortcutsWindow
      actions.py            WindowActions facade for menus/presenter
      tree.py               TreeController: rebuild, selection, smart groups, context menus
      drag_drop.py          DragDropController
      session_controller.py SessionController: open hosts, Paramiko, status, audit log, quick connect
      credentials_controller.py  credential resolution, permissions, password persistence
      host_actions.py       HostActions: create/edit/duplicate/move/tag/delete hosts
      group_actions.py      GroupActions: parent/child group CRUD
      inventory.py          reloads, shared inventory polling, Git watch
      app_dialogs.py        settings, credentials, config file, about, shortcuts
      security.py           SecurityActions: Settings → Security (unlock mode, recovery key, saving off)
      layout.py             sidebar visibility and width, focus mode, narrow variant, brand alignment
      colors.py             per-item tree colors
      native_transfer.py    NativeTransferController: .sshm export (dialog, worker write) and import
                            (wizard, then colours / smart groups saved after the commit)
      host_page.py          HostPageController: page content, ready state, tabs, latency, history
```

## Responsibilities

- **Views** build widgets and expose callbacks or attributes. They hold no
  inventory logic. Examples: `SidebarView.on_search_changed`,
  `SessionTreeView.populate(rows)`, `HostPageView.set_content(content)`.
- **Controllers** contain the UI logic and never construct widgets, apart
  from opening dialogs. Each one gets the window (`self.win`) to reach shared
  services (`manager`, stores, settings) and its sibling controllers
  (`win.tree`, `win.sessions`, …).
- **Tree expansion state** lives in `SessionTreeView.expanded_keys`: the
  `(kind, parent, child)` keys of the groups opened in this session.
  - **Startup:** a new view starts empty, so **every launch shows all groups
    collapsed**. The set is never persisted (not in `.ui_state.json`,
    settings or workspace files), and old keys found there are ignored.
  - **Rebuilds:** `populate(rows)` fills the store with the model detached,
    then expands exactly those rows, parents first. Keys of deleted groups are
    dropped. `TreeController.remap_group_keys` carries an open group across
    a rename or move.
  - **Search and filters:** `TreeController.rebuild` passes
    `populate(rows, temporary=search_reveal_keys(...))`. The ancestors of
    the matches open without touching `expanded_keys`, so clearing the
    search restores the user's view.
  - **Selection:** `restore_selection` never expands anything; a previous
    selection inside a closed group is simply not re-selected. User actions
    (`select_host` / `select_*_group`, `reveal=True` by default) open the
    ancestors via `reveal_path`.
  - **Imports:** `reveal_imported` opens only the first level of the
    imported top-level groups.
  - **Temporary opens:** rows opened by drag hover or GTK's type-ahead search
    are closed again afterwards.
  - **Helpers:** `set_row_expanded` / `set_row_collapsed`, `expand_to_row`,
    `expand_all_rows` and `collapse_all_rows`.
- **GTK-free modules** hold logic worth testing without a display: tree
  rows, filters, menus, toast rules, UI state, validation and presenters.
- **`SSHManagerWindow`** creates services and controllers, builds the views,
  connects view callbacks to controller methods, registers actions, and
  loads and saves `UiState`.

## Window layout

```
HeaderView (top bar: 44 px Compact, 76 px Comfortable)
Paned ─┬─ SidebarView ─ header · search · SessionTreeView · bulk bar · footer
       └─ HostPageView ─ page bar · host header ·
                         Overlay[ TerminalPage(ReadyState | TerminalNotebook) + HostPanel ] ·
                         info bar (docks the current session's status bar)
TerminalNotebook ─ SessionStrip (tabs · + · all-sessions ▾ · toolbar) · broadcast strip ·
                   Stack[ placeholder | Notebook (pages, tabs hidden) | tile grid ]
```

There are no content tabs. The host bar (page bar + host header) shows the
host of the session in front, or the tree selection when no session is open
(`HostPageController.selection()`); selecting another host in the tree never
switches tabs. Host details and activity live in `HostPanel`, a `Gtk.Revealer`
over the right edge of the session area (below the session strip), opened by
the ⓘ button, Ctrl+I/F9, the `···` menu or the ready state's *Host details*
link, and closed by Esc, a click elsewhere in the workspace or its close
button. The header's activity popover has *All* / *This host* filters.

`SessionStrip` replaces the notebook's own tab row so `+` can follow the last
tab: one focusable `SessionTab` per session (status dot, ellipsized label,
close button, tooltip from `TerminalNotebook.tab_tooltip`), overflow chevrons,
drag-to-reorder (`notebook.reorder_child`). Labels come from the pure
`gtkui/tab_labels.session_tab_labels()`: name / IP / both
(`TerminalNotebook.set_label_mode`, Settings → Appearance → Tab label, saved as
`tab_label_mode`), stable duplicate counters, and Rename overrides.

In Compact, `HostPageView.apply_density()` re-parents the same widgets:
- **Host bar:** the page bar is hidden; its ⓘ, *More* and primary buttons
  move into the host header, which becomes the one-line host bar.
- **Session strip:** 33 px, directly under the host bar (tile mode gives the
  toolbar its own row instead).
- **Status bar:** the info bar becomes one line, with the session stats on
  the left and the fields on the right.

### Active pane

Split panes and tiles share one activation path. The pure
`gtkui/pane_activation.ActivePane` keeps the ordered panes and the one active
pane: there is exactly one, a new split is active, closing the active pane
activates its neighbour, a re-layout keeps it, and re-activating it is a no-op.
- **Tab level:** `TerminalTab.activate_pane(term)` updates the `ActivePane`,
  restyles the frames once, reports to the notebook (`on_activated`) and moves
  keyboard focus.
- **Workspace level:** `TerminalNotebook._on_tab_activated(tab)` makes the
  tab current. Tile mode syncs the tile frames, toolbar and host/status bar;
  tabs mode switches the page. `TerminalNotebook.activate_pane(tab, term)` is
  the public entry point.
- **Callers:** clicks, Alt+arrows, Ctrl+Tab, Alt+1…9, the session strip, new
  and closed splits, and the terminals' own focus-in all go through it.

`PaneFrame` is an input-only `Gtk.EventBox` around the styled box
(`frame.style()` carries `pane-frame` and the state classes). Every GdkWindow
inside the pane is then below it, including the terminal, scrollbar, header
buttons, footer, padding and border. Its capture-phase `GestureMultiPress`
(all buttons) activates the pane and never claims the sequence, so VTE still
gets the press. A press inside a split pane of a tile is left to that pane's
own frame. The divider, the tile gutters and the scroll wheel never activate.

### Density

`theme/density.py` holds the presets and the active one. Views implement
`apply_density()`, which sets every token-driven size, margin and icon size
and runs at construction. `SSHManagerWindow.set_density()` activates a
preset; `_on_density_changed()` then does the following in order:
1. reloads the CSS and re-applies the base font;
2. calls `apply_density()` on the top bar, sidebar, tree, notebook and host page;
3. moves a default-width sidebar to the new default
   (`LayoutController.on_density_changed`).

Dialogs, menus and popovers read the tokens when they are built.

### Focus mode

The `toggle-maximize` action (F11, Ctrl+Shift+M, the avatar menu and the
session toolbar's overflow menu) hides the top bar, sidebar, host bar and
status bar. The tabs or session row, the broadcast strip and the terminal
stay. Esc leaves focus mode unless a terminal has focus; if broadcast is on,
the first Esc stops broadcasting. The state is saved as `terminal_maximized`.

`HostPageController.refresh()` turns the tree selection into a
`HostPageContent` (host, group, multi-selection or overview) and is called on
selection, status, session, history and theme changes. `TerminalNotebook`
composes the workspace: its session toolbar sits at the end of the tab strip
in tabs mode and on its own row in tile mode, and the current tab's
`SessionStatusBar` is reparented into the info bar (`set_status_dock`). It
reports output through the `log(message, level)` callback and session changes
through `on_sessions_changed`.

The pane separator is the sidebar border; `LayoutController` keeps the top-bar
brand block exactly as wide as the sidebar so the two dividers line up.

## Adding things

### A new dialog

1. Subclass `BaseDialog` in `gtkui/dialogs/`. Pick a `DialogSize`, add
   content with `add_body()`, then `add_cancel()` and
   `add_primary(_("_Action"), destructive=…)`.
2. Use `FormSection` and `FormRow` with validators from `validation.py`, and
   call `set_response_validator(lambda r: not is_accept(r) or section.validate())`.
3. Provide an `ask()` that runs, reads the values, destroys the dialog and
   returns a result. Call it from a controller.
4. Never put a list that grows with the inventory (groups, hosts) into a
   message: use `widgets/searchable_list.py` (one `TreeView`, filtered) or
   "first N … (+M more)". `BaseDialog` caps any dialog at 85 % of the
   monitor's work area (the body scrolls) and `PromptDialog` scrolls long
   messages, but that is a safety net.

### A new action, shortcut or menu entry

1. Add an `ActionSpec` to `controllers/shortcuts.py`, with accelerators if it
   needs them. It then appears in the shortcuts window automatically.
2. Map its name to a controller method in `SSHManagerWindow._register_actions`.
3. For a context-menu entry, add a `MenuItemSpec` to the right section in
   `context_menus.py`. If it needs a new callback, add a one-line method to
   `WindowActions`.

### A toast

```python
self.win._notify(_("Copied."), Level.SUCCESS)           # auto-dismisses
self.win._notify(_("Upload failed."), Level.ERROR,      # stays 30 s (warnings 20 s), always in history
                 action_label=_("_Retry"), action=retry)
```

Use `Level.INFO` for hints. Every toast is also recorded in the message
history popover in the header bar.

## Icons

- **Application icon:** the icon name is `ophion`. Its files are in
  `gtkui/assets/app-icon/hicolor/<size>x<size>/apps/ophion.png` (16, 22, 24,
  32, 48, 64, 96, 128, 256, 512 px), copied unchanged from the approved Ophion
  icon package (dark-multicolor).
- **Startup:** `install_app_icon()` (`widgets/icons.py`) adds that folder to
  the icon theme search path and makes `ophion` the default icon of every
  window and dialog, before the first window opens. If the theme cannot find
  it, the 256 px file is used; a missing icon never stops the app. No window
  sets an icon of its own.
- **Where it shows:** the About logo, and the "Add your first host" welcome,
  which shows the mark at 128 px instead of the card
  (`ReadyState.set_content(logo=True)`). The header bar has no Ophion logo.
- **Packaging:** the AppImage installs the same files under
  `usr/share/icons/hicolor` (`packaging/appimage/build.sh`). Its desktop
  entry says `Icon=ophion`, and `StartupWMClass` is the application id, which
  is also the X11 `WM_CLASS` (`GLib.set_prgname` / `Gdk.set_program_class`
  in `SSHManagerApplication`).
- **Adding a size:** put the PNG at `hicolor/<n>x<n>/apps/ophion.png`, add `n`
  to `APP_ICON_SIZES`, and make sure the size is in the hicolor theme's
  `index.theme`. The build script copies every size folder.
- **Raster only:** there is no SVG. Never resample the PNGs (no enlarging, no
  "fixing"); sizes above 512 px or sharper small sizes need a vector redraw.
- **Wordmark:** the package's wordmark (opaque white background) is not used
  yet. It spells the new name and belongs with the rename. When it is added,
  prepare a transparent light-theme copy at build time and show only the mark
  on dark themes.
- **Symbolic icons:** the UI's own `sshm-*-symbolic.svg` icons are separate
  (`assets/icons`, `make_icon`).

## Lifetimes and cleanup

- `ToastController.shutdown()` (overlay destroy and window destroy) cancels
  the auto-dismiss timers.
- `InventoryController.shutdown()` removes the shared-inventory poll and
  stops the Git watcher thread.
- `HostPageController.shutdown()` removes the latency and clock timers and
  invalidates pending latency probes (late results are dropped).
- `TerminalTab.destroy()` stops stats polling, password-change polling and
  the Paramiko bridge.
- The window removes its `ThemeManager` listener on destroy, and
  `SecretEntry` disconnects its keymap handler.
- `SSHManagerWindow.closing` suppresses UI refreshes that destroy-time
  signals would otherwise trigger.

## Tests

| Area | Tests |
|---|---|
| Tokens, CSS generation, contrast, terminal schemes | `test_gtkui_theme.py` |
| Host facts, latency probe, host history | `test_gtkui_host_facts.py` |
| Health, identity, workspaces | `test_gtkui_relay_helpers.py` |
| Validators, menu specs, toast rules, dialogs and widgets | `test_gtkui_components.py`, `test_gtkui_dialogs.py` |
| Tree rows, filtering, empty states | `test_gtkui_tree.py` |
| Workspace, broadcast, spawn path | `test_gtkui_workspace.py` |
| Accessibility, accelerators, shortcuts window | `test_gtkui_accessibility.py` |
| Surface colors: sidebar pixels per GTK theme, mode and focus state; sidebar CSS lint | `test_gtkui_surfaces.py` |
| Density: preset keys and floor, setting round-trip, CSS per theme × density, 11 px floor at 10/11/14/24 pt, runtime switch with a live session, real 1920 × 1041 layout (terminal share, bands, tree rows), host panel never resizes the terminal, tab overflow | `test_gtkui_density.py` |
| Session tab labels (modes, ports, IPv6, duplicates, stable counters, rename), tab-label setting | `test_gtkui_tab_labels.py` |
| Tree icons: icon choice, sizes per density, token contrast, status → badge (pure), SVG fill-only lint; rows rendered to cairo (ink size and centring, not a blob / not empty, colors, user tint, badge and cut-out ring on normal / hover / selected, lock, counts-only right edge, HiDPI, pulse, drag icon), GTK symbolic loader, 2,040-row cache and timing | `test_tree_icons.py`, `test_gtkui_tree_icons.py` |
| Pane activation: state machine (pure); real button presses on every region of split panes, 2/4/6 tiles and duplicated sessions (offscreen, hit-tested like GDK), press still reaches VTE, drag selection, divider/gutter/wheel, no-op re-click, broadcast source | `test_pane_activation.py`, `test_gtkui_pane_clicks.py` |
| End-to-end flows on the real window (top bar, tree, host page, session strip, bulk bar) | `test_gtkui_window_flows.py` (fixture: `tests/gtk_window_fixture.py`) |

GTK tests skip when `Gtk.init_check()` fails. Windows are never mapped and
latency probing is disabled, so tests never open sockets.

Snapshots use the Compact density unless a scenario pins one: the `relay-*`
scenarios use Comfortable, and `SSHM_DENSITY` overrides it for a whole run.
The `compact-*` and `comfortable-session-dark` scenarios print and save a
chrome budget (`<name>-budget.md`): band heights, plus the terminal's share of
the window measured from real allocations.

`tools/ui_snapshots.py <dir> [scenario …]` renders the screenshots in
`docs/ui-after/` offscreen at 96 dpi (device pixels under `GDK_SCALE=2`),
including `relay-match` (the reference scene at 1852 × 934), `relay-dark`
and `relay-compact`. `tools/compare_to_reference.py
docs/ui-after/relay-match.png --out docs/ui-after` writes `relay-diff.png`,
`relay-side-by-side.png` and `relay-report.md` and exits non-zero when a
region exceeds 3 % mean error or a landmark is more than 2 px off.

`tools/debug_surfaces.py` walks every widget (internal children, popovers and
TreeView's `.cell` nodes included) under each installed GTK theme and app
mode, and flags opaque backgrounds that are not palette surface tokens.
`--verbose` prints every node and `--interactive` opens the app with
`GTK_DEBUG=interactive`. `tools/ui_snapshots.py docs/ui-after
sidebar-surfaces` writes per-theme sidebar crops and
`sidebar-surfaces-after.md`.

`tools/ui_snapshots.py docs/ui-after neutral-dark-match` renders the
neutral-gray dark mock's scene (Comfortable layout, 115 dpi, 444 px sidebar).
`tools/compare_neutral_dark.py docs/ui-after/neutral-dark-match.png` compares
its flat color areas with `docs/ui-target/neutral-dark-palette.png` and checks
the sampled pixels against the palette tokens. It writes
`neutral-dark-report.md` and exits non-zero on a mismatch. The `popovers-dark`
and `toasts-dark` scenarios render the header popovers and one toast of each
level.
