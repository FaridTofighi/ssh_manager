# SSH Manager UI design system

Everything visual in the GTK 3 front-end comes from one place:
`ssh_manager/gtkui/theme/`. This document lists the tokens, the CSS classes
built on them, the reusable components, and the rules for using them.

## Design tokens (`theme/tokens.py`)

Tokens are Python constants. `theme/stylesheet.py` turns them into CSS, where
every color role becomes `@define-color sm_<role>`.

### Color roles

| Role | Dark | Light | Used for |
|---|---|---|---|
| `bg_window` | `#1b1e23` | `#f6f7f9` | Window background, notebook header |
| `bg_surface` | `#23272e` | `#ffffff` | Sidebar, toolbar, details drawer, status bar |
| `bg_surface_raised` | `#2b3038` | `#ffffff` | Toasts, raised cards |
| `bg_hover` | `#323843` | `#eef0f3` | Hovered rows and tabs |
| `bg_selected` | `#24456e` | `#d6e4fa` | Selected rows, checked toolbar toggles |
| `border_subtle` / `border_strong` | `#363c46` / `#4d5563` | `#dde1e6` / `#b3bac4` | Dividers / toast borders |
| `text_primary` / `text_secondary` / `text_disabled` | `#e8eaed` / `#aeb4bf` / `#7a828e` | `#1f2328` / `#57606a` / `#7d858f` | Text |
| `accent` / `accent_hover` / `on_accent` | `#1c71d8` / `#2a7de6` / `#fff` | `#1c71d8` / `#1a64c2` / `#fff` | Suggested actions, active tab line |
| `success` / `warning` / `danger` / `info` | `#57e389` / `#f6d32d` / `#ff7b72` / `#78aeed` | `#1a7f37` / `#9a6700` / `#c01c28` / `#1c71d8` | Status and feedback levels |
| `danger_strong` / `on_danger` | `#c01c28` / `#fff` | same | Destructive buttons |
| `focus_ring` | `#78aeed` | `#1c71d8` | 2 px focus outline |
| `chip_bg` | `#323843` | `#eef0f3` | Chips, subtle badges |
| `broadcast_bg` / `broadcast_fg` | `#5c1a1f` / `#ffe1df` | `#ffe3e5` / `#82071e` | Broadcast warning strip |
| `icon_group` / `icon_subgroup` / `icon_host` | amber / blue / grey | darker variants | Default tree icon tints |

`Palette.status_color(state)` and `Palette.level_color(level)` map
connection states and feedback levels to roles. Never hard-code a color in
Python; ask `current_palette()`. The only exceptions are colors the user
picks (tree colors) and terminal color schemes (`theme/terminal_schemes.py`,
which belong to the terminal, not the app chrome).

### Scales

| Scale | Values |
|---|---|
| Spacing (`Space`) | `XS 4`, `S 8`, `M 12`, `L 16`, `XL 24` px — nothing else |
| Radius (`Radius`) | `SMALL 4` (inputs, small chips), `MEDIUM 8` (buttons, toasts, tiles), `LARGE 12` (panels), `PILL 999` |
| Type (`FontScale`, em) | caption `0.85`, body `1.0`, subtitle `1.1`, title `1.35` |
| Motion (`Motion`) | `FAST 120`, `NORMAL 160`, `SLOW 200` ms, ease-out curve |
| Elevation | One shadow (`sm_shadow`), used only on toasts. Inputs, buttons and inline panels have none. |

Font sizes are relative (`em`), so the *Application font size* setting
scales the whole interface. Transitions are only emitted when
`gtk-enable-animations` is on, and revealers use `ThemeManager.motion_ms()`,
which returns 0 when animations are disabled.

### Contrast

`tokens.CONTRAST_PAIRS` lists every text/background and UI-component pair.
`tests/test_gtkui_theme.py` asserts WCAG AA for both themes: 4.5:1 for text
and 3:1 for UI components.

## Theme loading (`theme/loader.py`)

`ThemeManager` owns the single `Gtk.CssProvider`:

- Modes are `system`, `light` and `dark`. `system` follows
  `org.gnome.desktop.interface color-scheme` when it is available, otherwise
  a theme name ending in `-dark`.
- The manager mirrors the result to `gtk-application-prefer-dark-theme`, so
  Adwaita widgets follow too.
- It reloads when the desktop color scheme, `gtk-theme-name` or
  `gtk-enable-animations` change, and notifies listeners (the tree, for
  example) so the switch happens without a restart.

The stylesheet is layered over the system theme. It styles app-owned classes
and adds distinct hover, selected, checked, focus and disabled states, but
does not restyle every stock widget.

## CSS classes

| Class | Where | Meaning |
|---|---|---|
| `.sm-window`, `.sm-dialog` | Main window, every `BaseDialog` | Scope for app styles |
| `.surface`, `.surface-raised` | Panels | Surface backgrounds |
| `.sidebar-pane`, `.sidebar-search` | Sidebar | Pane and search row |
| `.title-1`, `.subtitle`, `.caption` | Labels | Type scale |
| `.section-title`, `.section-description` | Forms, settings pages | Section header and help text |
| `.form-help`, `.form-error`, `entry.invalid` | `FormRow` | Inline help and validation |
| `.chip`, `.chip.accent` | `Chip` | Tags, environment badge |
| `.badge`, `.badge.subtle` | Header buttons | Counters, update dot |
| `.status-dot` + `.connected` `.connecting` `.failed` `.unknown` | `StatusDot` | Color of the status shape |
| `.toolbar-icon`, `.danger-toggle` | `IconButton`s | Compact icon buttons; broadcast toggle turns red when checked |
| `.session-toolbar`, `notebook.session-tabs`, `.tab-activity` | Workspace | Toolbar, tabs, background-output marker |
| `.statusbar`, `.stat-warn`, `.stat-critical` | Session status bar | Warn at 80 %, critical at 90 % |
| `.broadcast-strip`, `.broadcast-active` | Workspace | Broadcast warning and included-session border |
| `.tile-frame`, `.tile-header` | Tile mode | Session tiles |
| `.details-drawer`, `.detail-key` | Details drawer | Drawer surface, fact labels |
| `.empty-state` (`.empty-icon`, `.empty-title`, `.empty-description`) | `EmptyState` | Placeholders |
| `.toast`, `.toast-info` / `-success` / `-warning` / `-error` | `Toast` | Notifications |
| `.level-info` … `.level-error` | Icons | Level-colored icons |
| `menuitem.destructive`, `.accel-hint` | Menus | Destructive items, shortcut hints |
| `.settings-sidebar`, `.form-section` | `SettingsPageLayout` | Navigation and sections |

GTK built-ins in use: `suggested-action`, `destructive-action`, `flat`,
`linked`, `dim-label` (recolored to `text_secondary` for contrast),
`titlebar`.

## Components

| Component | Module | Purpose |
|---|---|---|
| `BaseDialog` | `dialogs/base.py` | Sizes (`SMALL 420`, `MEDIUM 560`, `LARGE 760`), 16 px margins, 12 px spacing, Cancel then primary, `add_primary(destructive=…)`, Esc cancels, Enter triggers the default, initial focus, response validators, `run_and_destroy()` |
| `ConfirmDialog` / `confirm()` | `dialogs/confirm.py` | Explicit action label and destructive flag; destructive dialogs default to Cancel |
| `MessageDialog` / `show_message()` | `dialogs/confirm.py` | Information, warning and error messages |
| `PromptDialog` / `prompt_text()` | `dialogs/prompt.py` | One value, wrapping message, validator, inline error |
| `UnlockDialog`, `NewSecretDialog` | `dialogs/secrets.py` | Password unlock, and new/confirm with strength and match feedback |
| `SettingsPageLayout` | `dialogs/settings_layout.py` | Sidebar and stack with page headers |
| `ListManagerDialog` | `dialogs/list_manager.py` | Toolbar, list, empty state, selection-aware buttons |
| File helpers | `dialogs/files.py` | `choose_save_file`, `choose_open_file`, `choose_folder` |
| `FormRow`, `FormSection` | `widgets/forms.py` | Two-column aligned forms with help text and inline errors |
| `SecretEntry` | `widgets/secret_entry.py` | Reveal toggle, Caps Lock hint, generator, strength meter |
| `Chip`, `ChipFlow` | `widgets/chips.py` | Tags |
| `StatusDot` | `widgets/status_dot.py` | Vector status shape (● ◐ ✖ ○) with tooltip and accessible name |
| `IconButton`, `IconToggleButton`, `IconMenuButton`, `ActionBar` | `widgets/icon_button.py` | Icon-only controls; the tooltip doubles as the accessible name |
| `EmptyState` | `widgets/empty_state.py` | Icon, title, description and actions |
| `Toast`, `ToastOverlay`, `MessageHistoryButton` | `widgets/toast.py` | Views for `ToastController` |
| `build_menu`, `build_context_menu`, `popup_menu` | `widgets/menus.py` | Render `MenuSpec` |
| `tinted_icon_surface` | `widgets/icons.py` | Themed symbolic icon recolored at the device scale (HiDPI) |

## Usage rules

**Do**

- Take spacing from `Space` and colors from tokens or classes.
- Build dialogs on `BaseDialog`. Label the primary button with the action
  ("Delete Host", "Rename", "Connect"), and mark destructive actions
  `destructive=True`.
- Validate inline: give `FormRow` a validator and keep the dialog open with
  `set_response_validator`.
- Give every icon-only control a tooltip. `IconButton` also sets the
  accessible name.
- Show status with shape and text, not color alone (`StatusDot`, tree
  glyphs, tooltips).
- Use info toasts for hints. Use warning and error toasts only for problems;
  those are sticky.
- Wrap user-visible strings in `_()` from `gtkui/i18n.py`.

**Don't**

- Don't write `rgb(...)` or `#hex` in Python or ad-hoc CSS.
- Don't use `box-shadow` on inputs, buttons or inline panels.
- Don't call `Gtk.Dialog` or `Gtk.MessageDialog` directly, or label a
  non-delete confirmation "Delete".
- Don't bind global accelerators to keys terminals need (plain `Esc`,
  `Delete`, `Ctrl+<letter>`). Use `Ctrl+Shift` combinations, or handle the
  key in the focused widget.
- Don't draw icons pixel by pixel. Use themed symbolic icons.
