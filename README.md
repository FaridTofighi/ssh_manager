# SSH Manager

SSH Manager is a local desktop and terminal UI for organising SSH hosts in
groups, launching SSH sessions, managing credentials, and editing host
metadata. The desktop app keeps its own inventory; OpenSSH config files can be
imported and exported.

## How to install and run

These steps start from a fresh Debian 13 or Ubuntu 24.04 (or later) desktop.
The app runs on the system's Python packages, so it needs no virtualenv and no
`pip`.

### 1. Install the packages

```bash
sudo apt update
sudo apt install -y \
  git python3 \
  python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-vte-2.91 \
  python3-paramiko python3-cryptography python3-pexpect \
  openssh-client sshpass \
  freerdp3-x11 \
  python3-keyring xdotool
```

| Packages | What they are for |
|---|---|
| `python3-gi`, `python3-gi-cairo`, `gir1.2-gtk-3.0`, `gir1.2-vte-2.91` | The GTK window and the built-in terminals (required) |
| `python3-paramiko` | SFTP, the file browser, and Remote Desktop through an SSH host |
| `python3-cryptography` | The encrypted password and macro files (required to save passwords) |
| `python3-pexpect` | Running SSH sessions |
| `openssh-client`, `sshpass` | `ssh` itself, and filling in saved passwords |
| `freerdp3-x11` | Remote Desktop (`freerdp2-x11` on older releases). Optional: without it, everything except RDP works |
| `python3-keyring` | Optional: *Remember on this computer* (no master-password prompt) and moving passwords saved by older versions |
| `xdotool` | Optional: makes **Focus Window** work when FreeRDP runs in its own window |

The Textual terminal app also needs `python3-textual python3-rich`. The GTK app
does not.

### 2. Get the code

```bash
git clone <repository> ssh-manager
cd ssh-manager
```

### 3. Run it

```bash
python3 -m ssh_manager
```

On the first run:

- **Master password:** the app asks you to create one (and offers a recovery
  key). It encrypts the passwords and macros saved on this computer. **Don't
  Save Passwords** starts without one: every connection asks for its
  password. Settings → Security changes this later.
- **Updating from an older version:** passwords saved in the desktop keyring
  or the old `passwords.json`, and the macros, are moved to the new encrypted
  files automatically. Nothing is deleted until the move is verified. See
  `docs/security.md`.
- **Inventory:** created at `~/.local/share/ssh-manager/inventory.db`.
- **Importing:** if `~/.ssh/config` or `~/.ssh/conf.d` has hosts, the app
  offers once to import them. MobaXterm exports, OpenSSH config files and
  `.rdp` files can be imported later from the app menu → Import.

Some other ways to start it:

```bash
python3 -m ssh_manager --inventory ~/other.db                          # another inventory database
python3 -m ssh_manager --rdp 10.0.0.50 --user alice --domain CORP     # open one Remote Desktop session
```

### 4. Check the installation

- **Remote Desktop:** Settings → Remote Desktop → Diagnostics shows the
  FreeRDP version found and the engine that will be used.
- **Tests (optional):** run the test suite from a desktop session; the GTK
  tests need a display.

  ```bash
  python3 -m unittest discover -s tests -t .
  ```

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

The GTK app uses its own inventory database by default (see below);
`--inventory <file>` picks another database, and `--base-dir <folder>` opens a
folder of grouped config files instead (the layout of older versions, edited in
place). The Textual app and `farid.py` work on folders (`--base-dir`, default
`~/.ssh/conf.d`); the Textual app also accepts `--terminal`.

## Inventory

The desktop app keeps groups and hosts in
`~/.local/share/ssh-manager/inventory.db` (SQLite, mode 0600, rolling backups).
Groups can be nested to any depth, a group can hold hosts and subgroups, and
names can be any readable text — spaces, accents, Persian, … Your `~/.ssh`
files are never changed implicitly:

- **First run**: if `~/.ssh/conf.d` or `~/.ssh/config` has hosts, the app
  offers once to import them (with their names and saved credential
  bindings); "No" changes nothing.
- **Import → From OpenSSH Config…** (files or folders, with a preview) and
  **Import → From MobaXterm…**.
- **Export → To OpenSSH Config…** writes `~/.config/ssh-manager/ssh_config`
  and can add one `Include` line to `~/.ssh/config` (after a backup).
- **Settings → Inventory** shows the location, with Back Up Now, Reveal and
  Export.

Details: `docs/inventory-store.md`.

## Folder Layout (`--base-dir`)

A folder inventory expects a directory such as:

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
- **Host bar**: shows the host of the session in front (or the tree selection
  when no session is open), with Connect/Reconnect and a `···` menu. The ⓘ
  button (Ctrl+I, F9) opens a panel on the right edge of the workspace with
  the host's details (Edit, Duplicate, Copy SSH command) and its activity;
  Esc or a click elsewhere closes it, and it never resizes the terminal.
  Select several hosts to connect, move or tag them together.
- **Sessions**: double-click a host, press Enter, or use Quick Connect
  (Ctrl+Alt+T, or the menu next to the `+` after the last tab) for an ad-hoc
  `user@host:port` or an `sftp://`, `ftps://`, `ftp://` or `ssh://` URL (a
  temporary connection; the toast offers **Save to Inventory…**).
- **Host keys**: when ssh reports a changed or unknown host key, the tab
  shows a bar with the fingerprints. **Remove Old Key and Reconnect…** asks
  first and keeps a backup; ssh then asks again in the terminal. App menu →
  **Known Hosts…** lists `~/.ssh/known_hosts` with the inventory hosts
  (hashed entries included): delete with Undo, import, export, verify a key
  now. See `docs/known-hosts.md`.
- **Run Command on Hosts**: one non-interactive command or script on many hosts
  at once, with no tabs. Results show in a table: status, exit code, output,
  grouping of identical outputs and a diff against the majority. You can
  export them (CSV/JSON/text) and re-run failed hosts. It asks first on more
  than 5 hosts or for disruptive commands. Saved commands are encrypted. See
  `docs/multi-run.md`.
- **Port forwarding**: Local (`-L`), Remote (`-R`) and Dynamic (SOCKS5)
  forwards per host, started by hand, with a session or with the app, beside
  the terminal (they stop without disconnecting). App menu → **Tunnels…** shows
  them with their status; the `⇄ N` chip counts the active ones. See
  `docs/port-forwarding.md`.
- **Local terminal**: `+` after the last tab, or Ctrl+Shift+T, opens your own
  shell in a tab, like gnome-terminal (split, zoom, copy/paste and transcripts
  as in SSH tabs). See `docs/terminal.md`.
- **Macros**: the ● button in the session toolbar records what you type in a
  terminal; Stop asks for a name and an optional shortcut. Run a macro with its
  shortcut (in a focused terminal) or from *More session actions → Run Macro*;
  *Manage Macros…* edits and deletes them. Macros (passwords typed at prompts
  included) are stored encrypted with the key the master password protects. See `docs/macros.md`.
- **Terminal mouse**: Shift+right-click opens the terminal menu (also in vim,
  tmux, htop …), right-click opens it too unless a program uses the mouse,
  middle-click and Shift+Insert paste the selection, and selected text is
  copied to the selection at once. Settings → Terminal changes the clicks.
  Every session has its own tab, labelled with the host name, or with its IP
  address, or both (Settings → Appearance → Tab label); duplicates read
  `web-01 (2)`, and F2 renames a tab. Drag tabs to reorder them; the tooltip
  shows `user@host:port` and the status. The session toolbar acts on the
  active tab: split pane, save transcript, copy as plain text, show all
  sessions, broadcast and remote stats.
- **File transfer**: hosts can be FTP, FTPS or SFTP (host dialog →
  Protocol). They open in a two-pane file browser with a transfer queue,
  drag and drop and a connection log; SSH hosts have **Browse Files (SFTP)**
  in their menu. Settings → File Transfer holds the defaults. See
  `docs/file-transfer.md` and `docs/protocols.md`.
- **Remote Desktop (RDP)**: hosts can be RDP (host dialog → Protocol) with
  their own sign-in, security, certificate, display, device and gateway
  settings and a **Test Connection** that shows the server's security and
  certificate. Sessions use FreeRDP, installed separately; Settings →
  Remote Desktop → Diagnostics shows what was found.
  - **Import and export:** `.rdp` files can be imported (Import → From
    Remote Desktop Files) and exported. Passwords are never read from or
    written to them.
  - **Quick Connect:** accepts `rdp://` and `host:3389`.
  - **Command line:** `--rdp HOST[:PORT] [--user U] [--domain D]` opens a
    session at start-up.
  - **Wake-on-LAN:** a host with a MAC address has **Wake and Connect**.

  See `docs/rdp.md`.
- **Import from MobaXterm**: app menu → Import → From MobaXterm
  (.mxtsessions)…, or the empty sidebar's main button. SSH, FTP, SFTP and RDP
  sessions and their folders become groups and hosts; you choose which SSH
  Manager credential replaces each username found in the file. Other
  sessions, icons and colors are not imported, and passwords are never in an export. Any failure
  rolls back completely; re-running the same import adds nothing. See
  `docs/import-mobaxterm.md`.
- **SSH Manager export (.sshm)**: app menu → Export → SSH Manager Export, or
  Export Group… / Export Selected Hosts… in the sidebar, writes groups,
  subgroups, hosts of every protocol, colours and credential references (never
  passwords; optionally encrypted with a passphrase). Import → From SSH Manager
  Export brings them back with a wizard (credentials per user, colours, the
  same conflict rules as the MobaXterm import). See `docs/export-import.md`.
- **Messages** appear as toasts; the activity button in the header keeps every
  message and can filter to the current host.
- **Keyboard shortcuts**: press Ctrl+? or F1 for the full list. The most
  common are Ctrl+N (new host), Ctrl+T (connect), Ctrl+W (close session),
  Ctrl+Tab / Ctrl+Shift+Tab (next / previous session), Alt+1…9 (go to
  session), F2 (rename tab), Ctrl+Shift+D (duplicate session), Ctrl+I (host
  details; in a terminal Ctrl+I is still Tab, use F9 there), Ctrl+B (sidebar),
  Ctrl+Shift+B (broadcast) and F11 (focus mode). Terminal copy and paste are
  Ctrl+Shift+C / Ctrl+Shift+V (also Ctrl+Insert / Shift+Insert, which pastes
  the selection); Ctrl+Shift+T opens a local terminal and Ctrl+Alt+T Quick
  Connect. Every shortcut can be changed in Settings → Keyboard Shortcuts.
  The sidebar starts with every group collapsed;
  Ctrl+Shift+O expands all groups and Ctrl+Shift+L collapses them again (also
  in the filter popover and the tree's empty-space menu).

## Broadcast Input

Open two or more sessions and turn on Broadcast (toolbar or Ctrl+Shift+B).
The workspace switches to tile view, a red strip shows how many sessions
receive your typing, and those sessions get a red border. Uncheck
`Broadcast` on a tile to leave that session out. Stop with the strip's
button, Ctrl+Shift+B, or Esc when focus is outside a terminal.

When broadcast sessions have ended (for example after Ctrl+D), `q` (close)
and `r` (reconnect) in one of them apply to every ended session in the
broadcast. The banner's Close / Reconnect and the toolbar's Reconnect do the
same. Sessions still connected and sessions left out of the broadcast are
not touched. The ended sessions reconnect 150 ms apart. The tab menu's
Close and Reconnect act on that tab only.

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
the saved secret for that host. If the host is bound to a reusable credential,
that credential is updated as well.

## Password Storage

Saved passwords are stored in `~/.config/ssh-manager/passwords.json` (or under
`$XDG_CONFIG_HOME`), encrypted with AES-256-GCM. The key is in `keybox.json`,
itself protected by the master password (scrypt; Argon2id where available)
and optionally by a recovery key. The desktop keyring is no longer used to
store passwords, and there is no plain-text mode. **Settings → Security**
covers the rest:

- change the master password;
- *Ask for the master password* or *Remember on this computer*;
- the recovery key and how many passwords are saved;
- *Don't save passwords*;
- removing the copies older versions left in the keyring.

The threat model (what this protects against and what it does not) is in
`docs/security.md`.

Credentials → **Export…** / **Import…** move the shared credentials and their
passwords between computers in a passphrase-encrypted `.sshcred` file (see
`docs/passwords.md`).

## Paramiko Mode

With "Paramiko + VTE" enabled in Settings, connections honor the host's
`IdentityFile`, `ProxyCommand`, and `ProxyJump` options. Jump hops are reached
through the system `ssh` client in batch mode, so they need key or agent
authentication.

## Documentation

- `docs/ui-design-system.md` — tokens, CSS classes, components, usage rules
- `docs/ui-architecture.md` — module map and how to add dialogs, actions and toasts
- `docs/import-mobaxterm.md` — the MobaXterm import: format, mapping, credentials, troubleshooting
- `docs/export-import.md` — SSH Manager's own export / import (.sshm): format, security, wizard, limits
- `docs/inventory-store.md` — the inventory database, OpenSSH import/export, first-run import, recovery
- `docs/protocols.md` — connection protocols, the registry, Quick Connect URLs
- `docs/file-transfer.md` — FTP / FTPS / SFTP: settings, security, the file browser, transfers, diagnostics
- `docs/terminal.md` — terminal mouse and clipboard, configurable shortcuts, the local shell
- `docs/security.md` — the master password, the key file, unlock modes, the recovery key, the migration, the threat model
- `docs/known-hosts.md` — SSH host keys: the terminal banner, the Known Hosts manager, hashed entries
- `docs/port-forwarding.md` — tunnels: Local / Remote / SOCKS forwards, when they run, limits
- `docs/multi-run.md` — Run Command on Hosts: sending the script, results, safety checks, saved commands, limits
- `docs/macros.md` — terminal macros: recording, secrets, playback, shortcuts, the encrypted file
- `docs/rdp.md` — Remote Desktop: engines, install, settings, secrets, sessions, import / export, troubleshooting, the manual test checklist
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
cryptography (the encrypted password and macro files), and keyring (optional:
"Remember on this computer" and reading passwords older versions saved there).
