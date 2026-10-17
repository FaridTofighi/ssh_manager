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
    theme/              tokens, stylesheet generator, css_dark/css_light, loader, terminal_schemes
    widgets/            chips, status_dot, toast, empty_state, secret_entry, icon_button, menus, forms, icons
    dialogs/            base, confirm, prompt, files, secrets, settings_layout, list_manager,
                        host_dialog, preferences, credentials, vault, group_picker,
                        config_viewer, password, bulk_tag
    views/              header, sidebar, session_tree, details, workspace, statusbar
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
      credentials_controller.py  credential/vault resolution, permissions, password persistence
      host_actions.py       HostActions: create/edit/duplicate/move/tag/delete hosts
      group_actions.py      GroupActions: parent/child group CRUD
      inventory.py          reloads, shared inventory polling, Git watch
      app_dialogs.py        settings, credentials, vault, config file, about, shortcuts
      layout.py             sidebar visibility, workspace maximize, compact header
      colors.py             per-item tree colors
```

## Responsibilities

- **Views** build widgets and expose callbacks or attributes. They hold no
  inventory logic. Examples: `SidebarView.on_search_changed`,
  `SessionTreeView.populate(rows)`, `DetailsView.set_content(content)`.
- **Controllers** contain the UI logic and never construct widgets, apart
  from opening dialogs. Each one gets the window (`self.win`) to reach shared
  services (`manager`, stores, settings) and its sibling controllers
  (`win.tree`, `win.sessions`, …).
- **GTK-free modules** hold logic worth testing without a display: tree
  rows, filters, menus, toast rules, UI state, validation and presenters.
- **`SSHManagerWindow`** creates services and controllers, builds the views,
  connects view callbacks to controller methods, registers actions, and
  loads and saves `UiState`.

`TerminalNotebook` composes the workspace views. Its toolbar is bound to
whichever session is active, and it reports output to the window through the
`log(message, level)` callback.

## Adding things

### A new dialog

1. Subclass `BaseDialog` in `gtkui/dialogs/`. Pick a `DialogSize`, add
   content with `add_body()`, then `add_cancel()` and
   `add_primary(_("_Action"), destructive=…)`.
2. Use `FormSection` and `FormRow` with validators from `validation.py`, and
   call `set_response_validator(lambda r: not is_accept(r) or section.validate())`.
3. Provide an `ask()` that runs, reads the values, destroys the dialog and
   returns a result. Call it from a controller.

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
self.win._notify(_("Upload failed."), Level.ERROR,      # sticky, stays in history
                 action_label=_("_Retry"), action=retry)
```

Use `Level.INFO` for hints. Every toast is also recorded in the message
history popover in the header bar.

## Lifetimes and cleanup

- `ToastController.shutdown()` (overlay destroy and window destroy) cancels
  the auto-dismiss timers.
- `InventoryController.shutdown()` removes the shared-inventory poll and
  stops the Git watcher thread.
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
| Validators, menu specs, toast rules, dialogs and widgets | `test_gtkui_components.py`, `test_gtkui_dialogs.py` |
| Tree rows, filtering, empty states | `test_gtkui_tree.py` |
| Workspace, broadcast, spawn path | `test_gtkui_workspace.py` |
| Accessibility, accelerators, shortcuts window | `test_gtkui_accessibility.py` |
| End-to-end controller flows on the real window | `test_gtkui_window_flows.py` (fixture: `tests/gtk_window_fixture.py`) |

GTK tests skip when `Gtk.init_check()` fails. Windows are never mapped.
`tools/ui_snapshots.py <dir>` renders the screenshots in `docs/ui-after/`
offscreen.
