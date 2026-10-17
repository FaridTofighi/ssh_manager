# SSH Manager

SSH Manager is a local desktop and terminal UI for browsing grouped OpenSSH
config files, launching SSH sessions, managing credentials, and editing host
metadata.

## Launching

GTK desktop app:

```bash
python3 main.py
python3 ssh_manager.py
python3 -m ssh_manager
```

Textual terminal app:

```bash
python3 -m ssh_manager.main
```

Diagnostic config listing:

```bash
python3 farid.py --base-dir ~/.ssh/conf.d
```

All entry points accept `--base-dir`. The GTK entry points default to
`~/.ssh/conf.d`; the Textual app also accepts `--terminal`.

## Config Layout

The grouped inventory mode expects a directory such as:

```text
~/.ssh/conf.d/
  prod/
    web.conf
    db/main.conf
  staging/
    hosts.conf
```

Files directly inside the base directory are also loaded as root-level child
groups. Pointing `--base-dir` at a single OpenSSH config file is supported, and
that direct file mode follows `Include` directives.

## Using the GTK App

- **Sidebar**: search hosts by alias, host name, user, tags or jump host. The
  filter button next to the search field takes `key:value` terms joined with
  `AND` (for example `tag:prod AND owner:alice`) and saves filters as smart
  groups. Right-click a row, or press Shift+F10, for its actions.
- **Details drawer** (F9): facts about the selected host or group, plus
  Connect, Edit, Duplicate and Copy SSH/SCP actions. Select several hosts to
  connect, move or tag them together.
- **Sessions**: double-click a host, press Enter, or use Quick Connect
  (Ctrl+Shift+T) for an ad-hoc `user@host:port`. The session toolbar acts on
  the active tab: split pane, save transcript, copy as plain text, show all
  sessions, broadcast and remote stats.
- **Messages** appear as toasts; the message-history button in the header keeps
  every message.
- **Keyboard shortcuts**: press Ctrl+? or F1 for the full list. The most
  common are Ctrl+N (new host), Ctrl+T (connect), Ctrl+W (close session),
  Ctrl+Tab (next session), Alt+1…9 (go to session), Ctrl+B (sidebar),
  Ctrl+Shift+B (broadcast) and F11 (maximize workspace). Terminal copy and
  paste are Ctrl+Shift+C / Ctrl+Shift+V.

## Broadcast Input

Open two or more sessions and turn on Broadcast (toolbar or Ctrl+Shift+B).
The workspace switches to tile view, a red strip shows how many sessions
receive your typing, and those sessions get a red border. Uncheck
`Broadcast` on a tile to leave that session out. Stop with the strip's
button, Ctrl+Shift+B, or Esc when focus is outside a terminal.

## Theming

Settings → Appearance offers *Follow system*, *Light* and *Dark*. Changes
apply immediately. The application font size scales the whole interface.
All colors, spacing and type sizes come from design tokens in
`ssh_manager/gtkui/theme/`; see `docs/ui-design-system.md`. Terminal colors
are configured separately under Settings → Terminal.

## Expired Passwords

When a remote host forces an interactive password change, the GTK embedded
terminal watches for standard `current password`, `new password`, and `retype`
prompts. After the user enters and confirms the new password, the app updates
the saved secret for that host. If the host is bound to a reusable credential or
an unlocked vault entry, that credential source is updated as well.

## Password Storage

Saved passwords go to the system keyring (via the `keyring` package) when one is
available. Otherwise they are stored in `~/.config/ssh-manager/passwords.json`
(or under `$XDG_CONFIG_HOME`), encrypted with a key derived from the master
password. An existing plain-text file is encrypted automatically on the next
unlock, and the file is re-encrypted when the master password changes. If the
master password is disabled, the file is kept as private (0600) plain text.

## Paramiko Mode

With "Paramiko + VTE" enabled in Settings, connections honor the host's
`IdentityFile`, `ProxyCommand`, and `ProxyJump` options. Jump hops are reached
through the system `ssh` client in batch mode, so they need key or agent
authentication.

## Documentation

- `docs/ui-design-system.md` — tokens, CSS classes, components, usage rules
- `docs/ui-architecture.md` — module map and how to add dialogs, actions and toasts
- `CHANGELOG-UI.md` — UI changes, moved features, shortcuts
- `docs/ui-before/`, `docs/ui-after/` — screenshots; regenerate the latter with
  `python3 tools/ui_snapshots.py docs/ui-after` (renders offscreen in a sandbox)

## Verification

This repository currently uses the standard library unittest runner:

```bash
python3 -m unittest discover
python3 -m compileall ssh_manager tests farid.py main.py ssh_manager.py
```

The GTK and VTE bindings come from the distribution, so a virtualenv needs
access to system packages:

```bash
python3 -m venv --system-site-packages .venv
.venv/bin/python -m unittest discover
```

The app depends on Python plus GTK 3 / VTE GI bindings for the desktop UI.
Runtime modules used by optional features include Paramiko, Textual, pexpect,
cryptography, and keyring (for the system keyring; without it, passwords use
the encrypted file store).
