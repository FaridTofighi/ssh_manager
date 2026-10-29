# UI changelog — GTK front-end redesign

Screenshots: `docs/ui-before/` (original UI), `docs/ui-after/` (current
"Relay" UI, plus the reference comparison) and `docs/ui-target/` (the
reference). Phase-1 screenshots are in the history of `docs/ui-after/`
(commit b7597c5). Design reference: `docs/ui-design-system.md`. Code map:
`docs/ui-architecture.md`.

## Large inventories: the group chooser

- New Host on a large group now opens a searchable group chooser; dialogs can
  no longer outgrow the screen. (New Host, or dropping hosts, on a top-level
  group with a thousand child groups used to open a prompt listing all of
  them, taller than the screen.) Type to filter, pick a group, or type a new
  name to create it. The Move / Copy group picker uses the same list when
  there are more than 30 groups.

## Broadcast colour

- Broadcast mode now uses a violet highlight instead of the red error colour:
  the strip (with the broadcast icon instead of a warning triangle, same
  text), its Stop button, the toolbar toggle, pane outlines, the Broadcast
  chip and the `BROADCAST` badge. Red is left for real problems (failed
  sessions, errors, destructive actions, critical statistics). Screenshots:
  `docs/ui-after/broadcast-violet-*.png`, `broadcast-strip-*.png`.

## Terminal macros

Details: `docs/macros.md`.

- Macros run on all broadcast-enabled sessions; sessions excluded from the
  broadcast never receive them. Runs on several sessions ask first (*Don't ask
  again* / Manage Macros → *Ask before running a macro on several sessions*),
  every session has Stop, the current one Stop All, and one message reports
  the result.

- **Record macro** (● in the session toolbar, *More session actions → Record
  Macro*): records what you type in the active terminal (text, pastes, named
  keys, pauses); passwords typed at prompts become masked secret steps. Stop
  asks for a name and an optional shortcut and shows the steps.
- **Run** a macro with its shortcut (only while a terminal has the focus) or
  from *Run Macro ▸*; it types into the current session (and, with broadcast,
  the included ones), with a banner and **Stop**.
- **Manage Macros…**: run, edit (name, shortcut, timing, steps), duplicate,
  delete.
- **Shortcuts:** a macro's shortcut is checked against every action and macro
  ("already assigned to “…”", Replace / Choose Another). New configurable
  actions: *Record macro / stop recording*, *Manage macros*, *Stop the running
  macro* (no default keys).
- **Security:** `macros.enc` is encrypted with the master password (PBKDF2-
  SHA256, 600 000 iterations, Fernet); without a master password macros cannot
  be saved, and the master password cannot be turned off while macros exist.

## Terminals: gnome-terminal mouse, configurable shortcuts, a local shell

Details: `docs/terminal.md`.

- **Mouse in every terminal** (SSH tabs, split panes, tiles, local tabs):
  - Shift+right-click opens a terminal menu, also in programs that use the mouse.
  - Right-click opens the same menu unless a program reads the mouse (setting:
    paste instead, or nothing).
  - Middle-click pastes the selection (setting: the clipboard, or nothing).
  - Selected text goes to the selection at once, and optionally to the clipboard.
- **Keyboard shortcuts can be changed:** Settings → Keyboard Shortcuts (or
  Customize… in the shortcuts window).
  - Change, add a second one, clear, reset, reset all.
  - Conflicts name the other action and offer Replace.
  - Changes apply at once, menus and tooltips included.
- **New terminal shortcuts:** Shift+Insert pastes the selection, and Ctrl+Insert
  copies. Select all, Clear scrollback, Reset terminal and Open terminal menu
  have no default key. In a text field these keys act on the field, never on a
  terminal behind it.
- **New tab = local shell:** `+` after the last tab, the empty state and
  Ctrl+Shift+T open your shell in a tab, like gnome-terminal. Quick Connect
  moved to the menu next to `+`.
- **Changed default:** Quick Connect is now **Ctrl+Alt+T** (it was
  Ctrl+Shift+T, which now opens a local terminal).

## Fixes

- Terminals: started from a terminal running tmux, SSH Manager passed `TERM=tmux-256color` on to `ssh`, and
  servers without that terminfo entry failed in `clear`, `less`, `top` ("'tmux-256color': unknown terminal
  type"). SSH tabs and local tabs now always get `TERM=xterm-256color` (what VTE emulates) and no `TMUX`.
- Transfers: an unexpected error in a file transfer now marks the job as failed instead of done.
- Toasts: after an error toast, clicks in a tall column above it were swallowed (menus, buttons and the
  terminal on the right did not react). Only the toasts themselves take clicks now.
- Toasts: errors disappear after 30 s and warnings after 20 s (they used to stay until dismissed); every
  message stays in the message history. Toast text is no longer selectable (copy it from the history).

## Remote Desktop, phase R4: import, export, Quick Connect, Wake-on-LAN

Details: `docs/rdp.md` ("Import, export, Quick Connect, Wake-on-LAN").

- **MobaXterm import:**
  - RDP sessions (type 4) become RDP hosts.
  - `CORP\alice` is split into domain and user. `[label]` users go through
    the Credentials step.
  - The new **Import RDP sessions** option is on by default.
  - The Credentials step tags each user with the protocols using it.
- **`.rdp` files:**
  - **Import:** Import → From Remote Desktop Files (.rdp)…. Several files
    go into a chosen group. Saved passwords (`password 51`) are reported,
    never imported. `authentication level:i:0` becomes "Ask the first
    time". Shared folders are checked.
  - **Export:** Export → Selected RDP Host as .rdp File…, also in the host
    menu. The file is UTF-16, for mstsc, and has no password.
- **Quick Connect:** accepts `rdp://[DOMAIN%5Cuser@]host[:port]`, and
  `host:3389` is treated as RDP.
- **Command line:** `--rdp HOST[:PORT] --user U --domain D`, or an
  `rdp://` argument, opens an unsaved session at start-up. There is no
  password option, and an invalid target exits with status 2.
- **Wake-on-LAN:**
  - **Settings:** MAC, broadcast address and wait, under RDP Connection.
  - **Wake and Connect:** in the host menu when a MAC is set.
  - **Targets:** packets go only to broadcast or private addresses.
- **Docs:** troubleshooting and a manual test checklist (Windows, xrdp) in
  `docs/rdp.md`.

## Remote Desktop, phase R3: shared folders, RD Gateway, SSH tunnel, presets

Details: `docs/rdp.md` ("Devices, gateway, tunnel, presets").

- **Shared folders:** real paths, symbolic links followed. Sensitive places
  (`/`, `/etc`, the home folder, `~/.ssh`, `~/.gnupg`, keyrings, the app's
  own folders) are shared only after a red confirmation, and are checked
  again at every launch. The toolbar's **Shared folders** menu opens them
  on this computer.
- **RD Gateway:** a gateway credential whose password goes only through
  FreeRDP's stdin pipe. The gateway certificate goes through the trust flow
  with its own pins. FreeRDP's trust on first use covers the server behind
  the gateway, which cannot be checked first. Test Connection checks the
  gateway.
- **SSH tunnel:** "Through" an SSH host of the inventory.
  - **Forwarding:** a loopback-only forward on a kernel-chosen port, one
    client at a time.
  - **Login:** same jump host, known_hosts and key / agent / password rules
    as SSH.
  - **Certificates:** the server is probed and pinned through the tunnel;
    FreeRDP uses `/server-name:` so names are checked against the real
    host.
  - **Lifetime:** the tunnel is closed with the session.
- **Presets:** "Automatic" picks LAN, Broadband, WAN or Slow link from the
  measured latency (documented table).
- **Advanced:** Hyper-V console (`/vmconnect`); turning on restricted admin
  mode asks first.

## Remote Desktop, phase R2: the remote screen in the tab, toolbar, reconnecting

Details: `docs/rdp.md` ("The remote screen in the tab").

- **Embedded engines:** FreeRDP in a `Gtk.Socket` on X11, or gtk-frdp's
  widget (members verified by introspection), shows the remote desktop
  inside the tab.
  - **Resizing:** the remote desktop follows the tab's size (debounced,
    HiDPI-aware, never a transient tiny size).
  - **Fallback:** if FreeRDP's window does not appear within 10 s, the tab
    switches to an external window and remembers that this engine fails on
    this session type.
- **Toolbar** above the remote screen (auto-hide, pin, F10): Send
  Ctrl+Alt+Del (where possible), full screen (Ctrl+Alt+Enter, focus mode),
  scale mode, resolution, keyboard, clipboard, screenshot, open the shared
  folder, connection info, reconnect and disconnect. Option changes apply
  to this session only, by reconnecting; unavailable actions say why.
- **Reconnecting:** a dropped network reconnects automatically, with
  backoff (2–60 s, a limited number of attempts, waiting while the network
  is down), and the card offers **Stop Reconnecting**. An idle timeout or a
  session taken over by someone else asks first; a sign-out never
  reconnects.
- **Keyboard and clipboard:** the grab indicator follows the focus, and
  the release key is shown in the tooltip. Clipboard direction, kinds and
  size limits are enforced, and contents are never logged.
- **Fix:** the launch check refused short passwords that happened to
  appear in the command line (for example `32` in `/bpp:32`, or a letter of
  the client's path); it now looks only at password positions.

## Remote Desktop, phase R1: sessions through FreeRDP (external window)

Details: `docs/rdp.md` ("Sessions").

- **Opening an RDP host:** a session tab, shown as a card while FreeRDP
  draws the desktop in its own window.
  - **The card:** server, engine, security, certificate, screen size, time
    connected, the connection log (with **Copy Log**), **Focus Window**,
    **Disconnect** and **Reconnect**.
  - **Status:** the tree's status dot, notifications and a banner with
    **Reconnect** / **Close Session** on failures.
- **Passwords:** stored password, or the dialog. Only a wrong password asks
  again (at most 3 times). Locked, disabled or expired accounts,
  certificates and network problems get their own messages.
  - **Save offer:** after a log-verified NLA sign-in, the app offers "Save
    this password for this host?".
  - **No exposure:** the password reaches FreeRDP only through its stdin
    pipe, never argv, the environment or the logs.
- **Certificates:** checked by the app before FreeRDP starts.
  - **First use:** asks once, offering Trust Once / Trust and Remember.
  - **Changed certificate:** blocked behind a warning whose default is
    Cancel.
  - **"Do not check":** confirmed on every connection and flagged in the
    tab.
- **Cleanup:** closing a connected tab asks first; quitting asks once for
  all remote desktops. FreeRDP's whole process group is stopped, and
  children left behind by a crash are stopped at the next start.
- **Fix:** the file browser's error banner now shows its text (the label
  was never made visible).

## Remote Desktop, phase R0: RDP hosts, settings, connection test, engines

Details: `docs/rdp.md`.

- **RDP hosts:**
  - The protocol is `rdp`, port 3389.
  - Each host gets a desktop icon in the tree, an `RDP` pill and the
    `proto:rdp` filter.
  - OpenSSH export leaves RDP hosts out.
- **Host dialog:** four pages, shown only for RDP hosts.
  - **Remote Desktop:** sign-in, security, certificate and **Test
    Connection**.
  - **Display & Input:** size, colors, scaling, performance preset and
    keyboard layout (Persian 0x429 included).
  - **Devices:** clipboard, shared folders, printers, sound, USB and smart
    cards.
  - **RDP Connection:** reconnect, RD Gateway, SSH tunnel, programs and
    advanced options.
  - **Gating:** options the engine cannot do are disabled, with the reason
    in the tooltip.
  - **Confirmations:** risky choices (no certificate check, standard RDP
    security, sharing the home folder, any extra option) ask first.
- **Test Connection:** shows reachability, latency, the security the server
  selects and its certificate, and whether that matches a pinned one. It
  never sends a password.
- **Settings → Remote Desktop:** engine preference, the FreeRDP client and
  the number of sessions at once. **Diagnostics…** lists every FreeRDP
  client, engine and capability, with **Copy** and install hints.
- **Shared trust store:** the FTPS certificate pins moved to
  `ssh_manager/trust.py` and are shared with RDP (RDP keys are scoped).
- **Opening an RDP host:** says that sessions come in the next phase, or
  how to install FreeRDP when none was found.

## File transfer, phase F4: integrations and diagnostics

Details: `docs/file-transfer.md` ("Preferences", "Diagnostics"),
`docs/protocols.md` ("Quick Connect URLs"), `docs/import-mobaxterm.md`.

- **MobaXterm import:** FTP (type 6) and SFTP (type 7) sessions become `ftp`
  and `sftp` hosts (host, port and user only). FTP gets the warning "check
  whether this server uses FTPS". Anonymous FTP sessions get no credential.
  The file step counts SSH and FTP/SFTP sessions separately; the options step
  has **Import FTP/SFTP sessions** (on), and the folder counts follow it.
  The protocol is now part of "the same host".
- **Quick Connect:** accepts `ssh://`, `sftp://`, `ftp://` and `ftps://`
  URLs (with an optional first folder). URLs with a password are refused.
  The connection is not saved; the toast offers **Save to Inventory…**
  (the host dialog, filled in).
- **Browse Files (SFTP):** in an SSH host's menu (tree and host bar `···`):
  an SFTP file browser with the host's address, user, key, jump host and
  saved password. Nothing new is stored.
- **Settings → File Transfer:** download folder, conflict policy, parallel
  transfers, bandwidth limit, hidden files, remembered folders, open with
  the default application, temporary-copy cleanup, the plain-FTP warning and
  the protocol log file (off by default).
- **Connection log:** an activity button in the file browser's header shows
  the tab's masked log with **Copy Log** and **Copy Diagnostics** (banner,
  features, TLS details, host key). SFTP now logs its connection steps.

## File transfer, phase F3: transfer queue and drag and drop

Details: `docs/file-transfer.md` ("Transfers", "Drag and drop").

- **Engine** (`transfer/engine.py`):
  - **Jobs:** upload, download, server-to-server copy (streaming relay),
    same-server move (rename) and local copy.
  - **Planner:** recursive, keeps empty folders, skips or follows symlinks,
    detects loops, caps depth at 64, and makes every local name safe.
  - **Queue:** per-endpoint connection pools (`max_connections`), a global
    cap, pause / resume (continues the `.part` file), cancel, and retry with
    backoff for dropped connections.
  - **Conflicts:** overwrite, overwrite if newer, skip, keep both, resume,
    or ask with "apply to all".
  - **Integrity:** size checks; `.part` + rename for uploads and downloads;
    partial files removed on cancel or failure.
  - **Speed and progress:** token-bucket speed limits per host, per tab and
    global; smoothed speed and ETA; progress at most 10 Hz.
- **Transfers panel:**
  - **Rows:** direction, name, progress, size, speed, ETA, status / error,
    with pause / resume / cancel / retry / reveal / remove.
  - **Toolbar:** pause all, resume all, clear finished, speed limit.
  - **Status bar:** "N transfers · speed".
  - **Notifications:** for big jobs (desktop notification when the window
    is not active).
- **Conflict dialog:** both sides' size and date, the choices, "Apply to
  all", and "Cancel the transfer".
- **Drag and drop:**
  - **Between panes:** upload / download, and move or copy (Ctrl) inside a
    pane or onto folder rows.
  - **Across tabs:** server to server.
  - **From the desktop:** files from the desktop's file manager are
    uploaded (`text/uri-list`).
  - **Feedback:** spring-loaded folders (800 ms), a drop highlight and a
    count badge.
  - **Validation:** payloads are validated on drop (only listed paths, live
    panes); read-only hosts, folder-into-itself and malformed data are
    refused.
- **Closing:** closing a tab or quitting with running transfers asks first
  (`SessionPage.confirm_close`).
- **`copy_file`:** `keep_partial` may be decided when the copy stops (pause
  vs cancel).
- **Tests:**
  - **Files:** `tests/test_transfer_engine.py` (names with malicious
    listings, planner, queue, conflicts, retries, pause / resume / cancel,
    connection limits, move and relay, throttled progress, limiter, DnD
    rules) and `tests/test_transfer_ui.py` (panel, conflict dialog, drops,
    rejections, relay, conflicts from worker threads, close confirmation,
    status summary, synthetic drag-data-received).
  - **Snapshots:** `docs/ui-after/file-browser-transfers-{light,dark}.png`.

## File transfer, phase F2: the file-browser tab

Details: `docs/file-transfer.md` ("The file browser").

- **Opening:** FTP, FTPS and SFTP hosts open a **file-browser tab** with a
  local and a remote pane.
  - **Pane controls:** a toolbar, breadcrumb path bar (Ctrl+L), live filter
    (Ctrl+F) and a sortable, filterable list (folders first, natural order,
    hidden files toggle, owner column for the server).
  - **Listings:** streamed in batches with a delayed spinner, cancelled by a
    newer navigation, and capped at 20,000 rendered rows.
  - **Footer:** counts and the selection size.
  - **Keyboard:** see the docs.
- **Operations:** new folder, inline rename, delete (confirmation), copy
  path or remote URL (no password), properties (folder size on demand,
  chmod editor).
  - **Open:** to a private temp folder, with upload-on-save after a
    confirmation.
  - **Bookmarks and history:** per-host bookmarks, recent folders, and
    remembered last folders.
  - **Simple transfers:** sequential file uploads and downloads, Ctrl+C /
    Ctrl+V across the panes.
- **Connecting:** the same password resolution, retry dialog and "Save this
  password?" offer as SSH.
  - **Plain FTP:** asks once per host and shows a "Not encrypted" badge.
  - **Trust:** "Trust this certificate?" with its details (pinned); a
    changed certificate defaults to Cancel; SFTP host keys are confirmed.
  - **Recovery:** the reconnect banner, plus keepalive with
    lost-connection detection.
- **Read-only hosts:** browse and download only.
- **Safety:**
  - **Names:** remote names are plain text, escaped in tooltips and bidi
    isolated.
  - **Downloads:** names are made safe and contained in the destination
    (`transfer/names.py`).
  - **Opened files:** temp files are 0600 in a 0700 folder and removed when
    the tab closes.
  - **Tab close:** every GLib source is removed.
- **Notebook:** a `SessionPage` base class (`gtkui/session_page.py`) and
  `TerminalNotebook.add_page()`. File tabs take no part in broadcast, and
  split, transcripts, stats and zoom are disabled for them. The tab menu
  hides Split and Save Transcript.
- **Preferences model:** `AppSettings.file_transfer` (download folder,
  conflict policy, concurrency, plain-FTP warning, open behaviour, temp
  cleanup, speed limit, hidden files, remember folders, protocol log). The
  page comes in F4.
- **New modules:** `transfer/browse.py` (sorting, filtering, paths,
  breadcrumbs, URLs, history), `transfer/local.py` (the local file system
  as a backend), `transfer/copy.py` (streaming copy with `.part` + rename),
  `transfer/names.py`, `transfer/state.py`.
- **Tests:**
  - **File:** `tests/test_file_browser.py`, covering the pure helpers, the
    pane and the tab with an in-memory backend (`tests/fake_backend.py`), a
    real FTP tab against `tests/fake_ftp.py`, and the window integration.
  - **Snapshots:** `docs/ui-after/file-browser-{light,dark}-{comfortable,compact}.png`.

## File transfer, phase F1: backends and host settings

Details: `docs/file-transfer.md`.

- **`ssh_manager/transfer`:** a `RemoteFileBackend` interface with
  `RemoteEntry`, cancellation and an error hierarchy (user message plus
  technical detail, never a password).
- **FTP / FTPS** (`ftplib` + `ssl`):
  - **TLS modes:** explicit `AUTH TLS` and implicit TLS.
  - **Data channel:** reuses the control connection's TLS session, so it
    works with servers that require it. The PASV address is ignored (no
    bounce); active mode has a peer check and a port range.
  - **Text handling:** UTF-8 negotiation, `surrogateescape` names, CR/LF
    command guard, `PASS` masked in the protocol log.
  - **Listings and transfers:** MLSD / LIST parsers, REST resume,
    MDTM/MFMT, `SITE CHMOD`.
  - **Robustness:** reconnect on `421` / dropped connection; tolerant of a
    missing `226`.
  - **Certificates:** system, custom CA, pinned (first-use trust, changed
    certificates refused) or insecure.
- **SFTP** (`paramiko`):
  - **Host keys:** SSH host-key rules (unknown keys confirmed and appended
    to `known_hosts`, changed keys refused).
  - **Authentication:** key with passphrase, agent, password and
    single-prompt keyboard-interactive.
  - **Operations:** streamed listings, `posix_rename`, chmod, symlinks,
    `utime`, resume.
- **`TransferSettings`** in `metadata["transfer"]` (versioned, unknown keys
  kept, invalid values fall back with a warning).
- **Host dialog:** a new **File transfer** page (security, connection,
  folders, transfers, advanced) shown for FTP / FTPS / SFTP.
  - **Inline validation:** for example, a custom CA without a file.
  - **Insecure TLS:** asks for confirmation every time it is turned on.
  - **Anonymous FTP:** disables the credential.
  - **Test connection:** runs in the background and saves nothing.
- **Tests:**
  - **Files:** `tests/test_transfer_backends.py` (pure parsers and settings,
    plus integration against real sockets) and `tests/test_transfer_dialog.py`.
  - **Fake servers:** `tests/fake_ftp.py` (FTP/FTPS, can require TLS
    session reuse) and `tests/fake_sftp.py`.
  - **Snapshots:** `docs/ui-after/dialog-host-transfer-{light,dark}.png`.
- **Next (F2):** the file-browser tab opens these hosts, with the Phase 1–3
  password retry / save flow at connect time.

## Connection protocols, phase F0: the foundation

Details: `docs/protocols.md`.

- **Registry** (`ssh_manager/protocols`): `ssh`, `ftp`, `ftps`, `sftp` with
  labels, badges, icons, default ports and capabilities. Openers are attached
  by the UI. `open_hosts()` dispatches by protocol; an unknown protocol, or
  one without an opener yet, is a message, not an exception.
- **Inventory schema v2:** `hosts.protocol` (default `ssh`, indexed), added
  after a backup. `HostEntry.protocol` is carried through the store, the
  manager, duplicate, move and imports. Non-SSH settings live in
  `metadata["transfer"]`.
- **No more SSH assumptions:**
  - **Export:** OpenSSH export skips non-SSH hosts and reports them.
  - **Commands:** `build_ssh_command` refuses them; Copy SSH command copies
    a `scheme://user@host:port` URL instead.
  - **Ports:** default ports follow the protocol.
  - **Logs and filters:** the audit log records `protocol=`; `proto:` /
    `protocol:` filter terms and search by protocol name.
- **UI:**
  - **Tree:** a new `sshm-transfer` icon and a protocol pill at the right
    edge of non-SSH host rows (SSH rows unchanged).
  - **Details and tooltip:** a "Protocol" fact and a tooltip line.
  - **Host dialog:** **Protocol** is now the first field. It swaps the SSH
    pages, updates the default port unless one was typed, and confirms
    before dropping settings. It is disabled for folder inventories.
- **Not yet:** FTP, FTPS and SFTP hosts can be created and are shown, but
  they open only from phase F2 (the file browser). Until then they show the
  "cannot open" message.
- **Tests:** `tests/test_protocols.py`.

## Sidebar starts collapsed

- **Every launch:** all groups, top-level and nested, start collapsed; only
  the top-level rows are visible. Nothing about expansion is saved, and keys
  left in an old `.ui_state.json` are ignored.
- **State model inverted:**
  - `SessionTreeView.expanded_keys` (groups opened this session) replaces
    `collapsed_keys`.
  - `populate()` no longer calls `expand_all()`.
  - Open groups survive refreshes, edits, imports, drag and drop, status
    updates and workspace changes. A renamed or moved group stays open;
    deleted groups are forgotten. Re-opening a parent re-opens the subgroups
    that were open inside it.
- **Selection:** restoring the selection after a rebuild never expands
  anything. Creating, moving or duplicating a host or group reveals it.
  Imports open only the first level of the imported top-level groups.
- **Search and filters:** they open the ancestors of the matches temporarily;
  clearing them brings back the previous view. Rows opened by drag hover or
  type-ahead search close again.
- **Expand / collapse all:** **Expand All** / **Collapse All** are in the
  filter popover and the tree's empty-space menu, with Ctrl+Shift+O /
  Ctrl+Shift+L (Ctrl+Shift+E and C are Split pane and Copy).
- **Faster:** the store is filled with the model detached and only the needed
  rows are expanded. For a synthetic 1,500-host / 800-group inventory (2,820
  rows) the first populate takes **~160 ms instead of ~520 ms**, with zero
  `expand_row` calls at startup.
- **Tests:** `tests/test_sidebar_collapsed.py`. Snapshot:
  `docs/ui-after/sidebar-collapsed-{light,dark}.png`. Tests and snapshot
  scenes that need visible hosts now open the groups explicitly
  (`tools/ui_snapshots.py` window helpers take `expanded=`).

## Passwords, phase 3: recovery and "Save this password for this host?"

Details: `docs/passwords.md` ("Recovery after "Permission denied"").

- **`PasswordFlow`** (`auth_flow.py`): a per-tab state machine for attempts
  (≤ 3 per connect), the typed password and a once-per-tab save offer.
  `PasswordRequest`, `SaveOffer`, `SaveContext`, `Decision` and
  `source_phrase` hold the texts. No GTK and no I/O.
- **Retry dialog** (`PasswordRequestDialog`, non-blocking):
  - **Text:** "‘web’ rejected the saved password (credential ‘Ops’). Enter
    the password for alice@10.0.0.10:22."
  - **Controls:** **Retry** / **Cancel** and "Attempt N of 3".
  - **Stopping:** Cancel, Escape or an empty entry stops the flow.
  - **First connect:** the same dialog asks for the first password.
- **Save confirmation** (`SavePasswordDialog`, non-blocking): shown after a
  verified login with a typed password.
  - **Save for this host** (default) creates or updates the host's private
    credential. **Don't save**, or Escape, keeps the password for the tab
    only.
  - **Hosts that cannot be edited:** they get **Use for this session**.
  - **Save errors:** they keep the dialog open with **Retry Save**.
  - **Never offered:** for a stored password that worked, a typed one equal
    to it, a vault entry (explained) or when `sshpass` is missing.
- **`CredentialController`:** `save_host_password` looks the host up again
  at save time (renamed, moved or deleted hosts are handled).
  `password_save_context` and `describe_password_source` are added. A
  password changed inside a session updates only a host's own password.
- **Tabs:**
  - **Dialogs:** they are per tab. A reconnect closes an open retry dialog,
    and closing the tab dismisses both dialogs and removes the GLib sources.
  - **Prompt:** the blocking first-password prompt is gone.
- **Paramiko:**
  - **Same rules:** the same resolution, retry text and 3-prompt limit.
  - **No saving in the connector:** `add_password_verified_callback` asks
    the same question instead.
  - **Removed:** the "Remember password" checkbox, the "Remember passwords
    by default" preference and `UIHostConfig.remember_password_default`.
- **Tests:** `tests/test_password_recovery.py`. Snapshots:
  `docs/ui-after/dialog-password-{retry,save}-{light,dark}.png`.

## Passwords, phase 2: a password specific to one host

Details: `docs/passwords.md` ("Passwords: credential vs host-specific").

- **Host dialog:** Credentials → **Password** offers "Use the credential's
  password (‘X’)" or "Use a password specific to this host".
  - **Controls:** a secret entry with show/hide, a status ("Saved" / "Not
    set — you will be asked when you connect"), **Change…** and **Clear**.
  - **Save/Cancel:** applied on Save; Cancel discards.
  - **Confirmations:** switching back, or picking another credential while a
    host password is saved, asks first.
  - **Locked store:** keeps the dialog open with the reason.
  - **Permissions:** disabled when the host's local permissions forbid
    editing.
- **Private credentials:** `Credential.private` / `based_on`, persisted in
  `credentials.json`; older files load unchanged.
  - **API:** `CredentialStore.create_private`, `update_private`,
    `private_for_host`, `is_private`, `shared_for_copy`, and
    `list(include_private=False)`, which hides them everywhere.
  - **No orphans:** a private credential and its secret are deleted when the
    host is deleted, re-bound, unbound, removed with its group or pruned. A
    failed save rolls everything back.
- **Copies:** duplicated hosts and groups bind the copy to the shared
  credential, never to the private one, and say so.
- **Display:**
  - **Host details:** a "Password" fact ("credential ‘X’" /
    "host-specific"), and Auth reads "Host-specific password".
  - **Tree tooltip:** a "Password: …" line.
- **Resolution:** unchanged order. A private credential resolves with source
  `host`. The terminal treats it like `credential` (retry prompt, never
  deleted).
- **Tests:** `tests/test_host_passwords.py`. Snapshots:
  `docs/ui-after/dialog-host-password-{saved,notset}-{light,dark}.png`
  (`tools/ui_snapshots.py`).

## Passwords, phase 1: correctness (no UI change)

Details: `docs/passwords.md`.

- **No re-seeding:** connecting no longer copies a credential's password into
  the per-host secret. The connection password is resolved read-only:
  vault → credential → legacy per-host secret → none (`PasswordResolution`).
- **No deletes on failure:** a rejected password no longer deletes the
  stored secret, in the terminal or in the Paramiko connector.
- **Verified success:** each `ssh` spawn adds a `LocalCommand` sentinel (a
  marker file in a private runtime directory) that proves authentication.
  Where `PermitLocalCommand no` forbids it, a heuristic decides. The log names
  the method.
- **No silent saves:** a typed password is kept for the tab's session only.
  The old "save after 2 s connected" path is removed.
- **`classify_exit`:** only a real authentication failure opens the password
  prompt. Host-key, network, public-key-only, OTP and "too many failures" do
  not.
- **Retry limit:** at most 3 attempts per connect, ≥ 1 s apart. Timers are
  removed when the tab closes.
- **Tests:** `tests/test_auth_flow.py`.

## MobaXterm import, amended for the inventory store

The import writes only into the app inventory: one transaction, no files.
Details: `docs/import-mobaxterm.md`.

- **Tree copied 1:1:** each MobaXterm folder becomes a group at any depth. A
  folder with sessions and subfolders is one group. Empty folders are created
  unless **Skip empty folders** is ticked. The flattening, `General` and
  `_hosts` are gone.
- **Names kept verbatim:**
  - Spaces, `+`, parentheses and Persian stay as written.
  - Only what the store rejects changes, and it is reported: `/` → `∕`, more
    than 128 characters, empty → `Unnamed`.
  - Collisions the store forbids get ` (2)`. The same name in different
    folders is fine.
  - The `display_name` metadata is no longer needed.
- **Matching:** existing hosts are matched by group path, host name, port and
  normalized user. Skip leaves them; Rename imports a ` (2)` copy; Overwrite
  updates the matched host in place, keeping its alias, id and bindings, after
  a backup.
- **Re-runs:** a second import with the defaults changes nothing.
- **Under an existing group:** mounts the whole tree beneath any group, picked
  by its path. A destination deleted meanwhile fails the import cleanly.
- **One transaction:** groups, hosts, updates and `bind_many` by the new host
  ids, then one refresh. Any error or a cancel rolls it back.
- **Removed:** `ConfigManager.import_groups` / `ImportTransaction` and the
  ASCII sanitizer. The wizard refuses a folder inventory.
- **Tests:**
  - **Names and structure:** verbatim names survive a round trip; an 11-level
    tree equals the source (path sets); folders with hosts and subfolders;
    slash replacement and numbering.
  - **Policies and re-runs:** an idempotent re-run (row counts and a content
    hash); Skip, Rename and Overwrite, where Overwrite keeps ids and picks the
    alias match among copies; the existing-group destination, and one deleted
    meanwhile.
  - **Bindings and rollback:** bindings by the new id survive a later rename
    and move; rollback on an injected failure, a failed binding save and a
    cancel; exactly one `BEGIN`/`COMMIT`.
  - **Performance:** 1,500 hosts in 800 folders take under 2 s.
  - **Wizard:** the wizard tests run on the store.

## The app-owned inventory (and the "Base path is a file" fix)

Details: `docs/inventory-store.md`. Screenshots:
`docs/ui-after/inventory-store-{dark,light}.png`.

### Part A — the bug

After every group was deleted, creating anything failed with "Base path
'~/.ssh/config' is a file". When `~/.ssh/conf.d` had no groups,
`ConfigManager.refresh()` fell back to `~/.ssh/config` and made that *file*
the base.

- **Fix:** the fallback is gone. An empty inventory stays empty and editable,
  and a missing folder is created with mode 0700.
- **A single file as inventory:** it is read-only. Every change asks
  "‘…’ is a file. SSH Manager keeps its groups in a folder. Use
  ‘~/.ssh/conf.d’ instead?" (Use Folder / Cancel); it never leaves a failing
  button.
- **Test:** regression tests with a temp HOME check that creates succeed and
  `~/.ssh/config` stays byte-identical.

### Part B — what changed

- **The store:** groups and hosts live in one SQLite database
  (`$XDG_DATA_HOME/ssh-manager/inventory.db`) that the app owns:
  - transactions, foreign keys, WAL, schema versions and rolling backups;
  - groups of any depth, which may hold hosts and subgroups; empty groups are
    real;
  - readable Unicode names (case-insensitive uniqueness that works for
    Unicode too);
  - stable host and group ids.
- **The facade:** `StoreConfigManager` keeps the `ConfigManager` API and the
  `manager.tree` shape. Every group is an entry, and top-level hosts sit under
  the child path `""`. The tree nests every group once, subgroups above
  hosts, with folder icons and counts that include subgroups.
- **Credential bindings:** keyed by host id. Renames and moves need nothing,
  duplicates copy their bindings, and bindings of deleted hosts are pruned
  (credentials are kept).
- **OpenSSH files are sources only:**
  - **Import → From OpenSSH Config…:** sources, preview, policy; idempotent;
    patterns and `Match` blocks skipped with notes.
  - **Export → To OpenSSH Config…:** writes a generated file, never
    `~/.ssh/config`. An optional `Include` line goes at the top, after a
    backup and with a diff shown first.
  - **Open config folder:** now opens the import.
- **First-run import:** offered once per source. Old names and old credential
  bindings carry over; nothing changes on "No".
- **Start-up problems:** each asks what to do — locked, damaged (restore a
  backup / start empty), newer schema (left untouched), unwritable
  (read-only). A read-only store explains itself, and changes made by other
  windows are picked up.
- **Preferences → Inventory:** shows the location, with Back Up Now, Reveal
  and Export.
- **MobaXterm import:** into the store, it keeps the original names and needs
  no `General` / `_hosts` workaround (one transaction, id-keyed bindings).
- **Command line:** `--inventory <db>` is new, and `--base-dir` now opens a
  folder inventory. The old "Base path" wording is gone.

### Decisions

- **Bindings stay in `credentials.json`** (keyed by host id) rather than in a
  database table. The credentials and their secrets live there already, and
  a database table would couple the two stores. Deleting hosts prunes their
  bindings when the tree next rebuilds.
- **`--base-dir <folder>` stays editable.** It is the layout the app itself
  created, the user chose it explicitly, and folder-based workflows and tests
  keep working. A single file is read-only.
- **"Open SSH Config Folder…" became an import** (for the store) rather than
  a read-only workspace. Folders are import sources.
- **The `Include` line goes at the top of `~/.ssh/config`,** not at the end
  as the brief said: at the end it would only apply to the last `Host`.
- **Locked database while running:** the write fails with "Try again" in the
  message, rather than offering a retry button.

### Verification

- **Store:** `tests/test_inventory_store.py`, 23 tests.
  - **Behaviour:** CRUD, Unicode and case-insensitive uniqueness, the name
    rules, groups that hold hosts and subgroups, move cycles, duplicate,
    cascade, depth 50, rollback on an injected failure.
  - **Lifecycle:** migration from v0, a newer schema refused untouched,
    corruption, an unwritable location, read-only, a second writer, data
    version, backup rotation and restore.
  - **Performance:** insert and load.
- **Facade:** `tests/test_inventory_manager.py`, 14 tests. The tree shape,
  Unicode names, rows without duplicates, every group operation, the host
  rules, external changes, lock messages, bulk helpers, bindings that follow
  ids, duplicate and prune, and a 5,000-host tree load.
- **OpenSSH:** `tests/test_inventory_openssh.py`, 10 tests.
  - **Import:** folder import with names, patterns, `Match`, BOM, CRLF, quotes
    and tabs; duplicate blocks; idempotent re-import and the policies; legacy
    bindings carried and reported; rollback when a binding fails; `Include`
    loops; the size cap; the first-run sources.
  - **Export:** a round trip; never over `~/.ssh/config`; an idempotent
    `Include` line with a backup.
- **Window and start-up:** `tests/test_gtkui_inventory_store.py`, 17 tests.
  - **Start-up:** each decision, including a too-new file that stays
    byte-identical.
  - **Names and validation:** Unicode groups and hosts through the UI (create,
    subgroup, rename, search, breadcrumb), inline duplicate validation,
    dialog validators.
  - **Behaviour:** bindings through moves and deletes, a read-only store,
    external changes, the first-run offer (imported once, or "No").
  - **Dialogs:** the import dialog, export with `Include`, Preferences →
    Inventory, and the MobaXterm wizard into the store.
- **Part A:** `tests/test_base_dir_regression.py`.
- **Full suite:** `python3 -m unittest discover` passes.

## Import from MobaXterm (`.mxtsessions`)

Details: `docs/import-mobaxterm.md`. Screenshots:
`docs/ui-after/import-wizard-{1-file,2-options,3-credentials,4-review,5-result}-{dark,light}.png`.

### What changed

- **Entry points:** app menu → **Import → From MobaXterm (.mxtsessions)…**
  (action `win.import-mobaxterm`). The empty sidebar's main button is now
  **Import from MobaXterm…**; *Add host* moved to the second button and
  *Open config folder…* to a flat link under them (new `EmptyState`
  `link_label`).
- **A five-step wizard** (`gtkui/dialogs/mobaxterm_import.py`):
  1. **File:** parsed in a worker thread; errors appear inline.
  2. **Destination and options:** new parents or an existing parent;
     Skip / Rename / Overwrite; empty folders; skip duplicates; a checkable
     folder tree with live counts.
  3. **Credentials:** every username in the selected SSH sessions mapped to a
     credential, None or a new one, with a default credential first.
  4. **Review:** a copyable report.
  5. **Result:** the write runs in a worker, is cancellable and rolls back.
     The report is saved (0600) under `<data dir>/imports/`.
- **Pure importer:** `ssh_manager/importers/mobaxterm.py` parses
  line by line (no `configparser`), plans the parent/child mapping (`General`
  and `_hosts` rules, sanitized unique names, original names kept in
  `# meta`), discovers usernames, applies the credential choices and the
  conflict policy, and commits.
- **Bulk storage APIs:**
  - `ConfigManager.import_groups` writes each file once, validates every path
    against the inventory folder, checks for concurrent changes, and rolls
    back on any failure (`ImportTransaction`).
  - `CredentialStore.bind_many` saves once and restores on error.
  - `render_group_file()` is split out of `_write_group_file`.
  - `choose_open_file` takes file filters.
- **Not imported:** RDP, FTP and other non-SSH sessions, MobaXterm icons,
  colors and fonts. Passwords are never in an export.

### Decisions and deviations

- **Group labels:** no display-name sidecar for groups. Every group rename,
  move, duplicate and delete would have to keep one consistent. Hosts keep
  their original name (`display_name`) and folder (`mobaxterm_folder`), and
  the report lists every folder stored under a different name.
- **Empty `SubRep=`:** MobaXterm writes this for sessions at the root of its
  tree. Such sessions are imported under a `MobaXterm` parent group, with an
  `empty_subrep` note, rather than dropped.
- **Sections without `SubRep`:** their sessions are skipped and reported
  (`orphan_session`).
- **Existing parent target:** chosen from a list of the parent groups,
  rather than through the move-host picker.
- **Unlock:** the credential store is unlocked at start-up (master
  password), so the wizard has no separate unlock step.
- **Extra issue codes:** `duplicate_host`, `existing_host_skipped`,
  `existing_host_renamed`, `existing_host_overwritten`, `empty_subrep`,
  `folder_too_deep` and `invalid_field`.
- **Rejected values:** host names, users and jump hosts starting with `-` are
  rejected, so they can never be read as `ssh` options.

### Verification

- **`tests/test_mobaxterm_import.py`** (55 tests, no GTK):
  - **Parser:** line endings, BOM, UTF-16, cp1252, NULs, truncated lines,
    names with `=`, unknown types, orphans, duplicate sections, odd
    `SubRep`s, depth 12 and over 32, Persian, RTL and zero-width names, long
    lines.
  - **File errors:** missing, folder, empty, binary, not MobaXterm,
    permission, size cap.
  - **Sanitizing and planning:** sanitizing and truncation, `General` /
    `_hosts`, empty folders, unique siblings and aliases, the existing-parent
    destination, selection, path traversal.
  - **Hosts and usernames:** host name, port and IPv6 edge cases, the
    jump-host formats and `build_ssh_command` compatibility, username
    discovery.
  - **Commit:** credential mapping (default, overrides, None with a literal or
    a label), the round trip through `load_config_tree`, Skip / Rename /
    Overwrite, an idempotent second run, clashes with existing files.
  - **Rollback:** an injected `OSError` on the third write, a failed binding
    save, a read-only target, a concurrent change, a deleted credential and a
    cancel all leave the inventory byte-identical.
  - **Bulk writes:** one refresh and one binding save with no per-host
    writes, `bind_many`, the private report with no secrets.
  - **Performance:** a synthetic 1,500-session, 800-folder, depth-12 export
    is parsed, planned and committed well under the bound.
- **`tests/test_gtkui_mobaxterm_import.py`** (10 tests): step gating, the
  empty credential store, unticking folders (counts and usernames), the full
  import and an idempotent re-run, creating a credential from a row, cancel
  at every step, closing during the write (rolled back, single start), parse
  errors, accessible names, entry points.
- **Test data:** synthetic only (`tests/mxt_synthetic.py`, which also
  generates large exports); no real export is in the repository.

## Phase 9b — one icon color: white on dark

Amends Phase 9: only the icon colors change. Target sheet regenerated with
the real renderer: `docs/ui-target/tree-icons-sheet.png`
(`tools/ui_snapshots.py docs/ui-target tree-icons-sheet`). The Phase 9 sheet
is in the history of that file.

### What changed

- **One token,** `icon_default`, draws every group and host icon:
  - folder, open folder, file-conf, server, and server + lock;
  - in every row state: normal, hover, selected, focused, drag-over and an
    unfocused window.
  - **Dark:** `#FFFFFF`, 15:1 or more on surface, hover and selected rows.
  - **Light:** `#18231F`, the light `text_primary`, 13:1 or more on the
    same three backgrounds. A white icon would be 1:1 on the light surfaces.
    Showing white on light would need a dark rounded tile behind the icon,
    which is deliberately not done.
  - The selected host no longer turns accent green; only its label, bar and
    background change.
- **Legacy names:** `icon_group`, `icon_file`, `icon_host` and
  `icon_host_selected` are now read-only aliases of `icon_default`, so a
  future change is a one-line edit. The amber, gray and accent icon values
  are gone. A test fails if any of those hex values appears in Python
  outside the theme token modules.
- **User colors** still win, on selected rows too. *Clear Color* returns to
  `icon_default`.
- **Unchanged:** the status badges keep their colors, and their ring is still
  cut out of the row's own background.
- **Same token elsewhere:** the host-bar tile icon, the group picker rows and
  empty-state icons now use `icon_default` too. Menu icons stay in the menu
  text color.

### Verification

- **`tests/test_tree_icons.py`:**
  - `icon_default` is `#FFFFFF` (dark) and `#18231F` (light), and the four
    legacy names resolve to it.
  - User color wins and clearing it restores the default.
  - The grep test for literal icon colors passes.
  - Contrast is ≥ 3:1 on all three row backgrounds, and ≥ 13:1 in practice.
- **`tests/test_gtkui_tree_icons.py`:**
  - In both themes, each of the five icon kinds was drawn in each of the six
    row states. The strongest outline pixel equals `icon_default` within 1
    per channel.
  - Badges keep their status colors, and a user-colored group keeps its
    color on selected rows. After *Clear Color* it is white.
  - In a child process, the real window switches dark → light → dark, and the
    tree re-tints without a restart.
- **Screenshots:** `docs/ui-after/` regenerated, including the four
  `tree-icons-*` scenarios.
- **Full suite:** `python3 -m unittest discover` passes.

## Phase 9 — bigger, distinguishable tree icons with the status on the host icon

Target: `docs/ui-target/tree-icons-sheet.png`. After:
`docs/ui-after/tree-icons-{dark,light}-{compact,comfortable}.png`
(`tools/ui_snapshots.py docs/ui-after tree-icons-dark-compact …`).

### What changed

- **One icon per role,** from the brief's drafts:

  | Row | Icon | Color |
  |---|---|---|
  | Group, collapsed | folder | amber `icon_group` |
  | Group, expanded | open folder | amber `icon_group` |
  | `*.conf` group | file | `icon_file` |
  | Host | server | `icon_host` |
  | Host on the selected row | server | accent `icon_host_selected` |

  - **Before:** nested groups had no icon at all, and hosts used a small
    monitor.
  - **User colors:** an item color the user picked still tints the icon.
  - **Read-only hosts:** shared hosts, or hosts without edit permission, get a
    lock overlay.
- **Sizes:**
  - **Compact:** 20 px icon, 30 px rows (hosts were 28), 14 px chevron, 8 px
    gap.
  - **Comfortable:** 24 px icon, 41 px rows, 16 px chevron, 10 px gap.
  - **Columns:** every level has one icon column (chevron, icon, label). Hosts
    keep the chevron slot empty, so the labels of a level line up.
- **Status on the host icon:** a badge at 42 % of the icon, bottom-right.
  - Connected is a green dot.
  - Connecting is an amber ring with a half fill. It pulses at 0.8 Hz while
    on screen, unless `gtk-enable-animations` is off.
  - Failed is a red dot, with a cut-out × at 24 px.
  - Unknown is a gray dot.
  - Each badge sits in a 2 px transparent cut-out, so the ring is the real
    row background on normal, hover and selected rows.
  - The right-edge status dot is gone; the right edge shows only group
    counts.
  - The row tooltip now updates on every status change. It used to keep the
    status from the last tree rebuild.
- **Rendering:** the icons are SVGs, and `widgets/icons.py` keeps rendering
  them with librsvg, tinted, at device pixels and cached.
  - **Strokes converted to fills:** the drafts' 1.75 strokes were converted to
  filled outlines with picosvg. The duotone is a second path at
  `opacity="0.2"`. With no strokes left, GTK's own symbolic loader draws them
  correctly too; a test renders them through it.
  - **Compositing:** `render_tree_icon()` composites the badge and lock
    (each a small SVG) with a `DEST_OUT` cut-out and caches by every input.
    The tree never parses SVG while drawing.
- **Tokens:** `icon_subgroup` became `icon_file`, and four tokens are new:
  `icon_host_selected`, `badge_connected` and `badge_unknown`, plus the
  brief's values for `icon_group`, `icon_file` and `icon_host`. Every icon
  and badge color is checked at ≥ 3:1 on `bg_surface`, `bg_hover` and
  `bg_selected` in both themes (`CONTRAST_PAIRS`).
- **Same family elsewhere:**
  - the group picker (used by Move and bulk move): folder icons on parents,
    file icons on child groups;
  - the host page: file icon for `*.conf` groups;
  - the host-bar tile: 24 px Compact, 28 px Comfortable (was 16 / 24);
  - the config-file icon: now `file-conf`;
  - the tree's drag icon (it is the rendered row);
  - context menus at 16 px.
- **Removed:** the unused `render_icon` / `render_status` / `render_count`
  helpers and `default_icon_color`, the per-kind tree offsets (`FOLDER`,
  `HOST_ICON`, `GROUP_ICON_X`, …, `DOT_RIGHT`, `DOT`) and the draft
  non-symbolic SVGs. The monitor icon stays: the status bar's CPU stat uses
  it.
- **Busy-loop fix found while testing:** `TerminalTab.focus_terminal()`
  retried with an idle callback for as long as the terminal was not shown.
  For a terminal that never gets shown (a closed tab, a hidden tile) that
  meant 100 % CPU forever. It now retries every 50 ms and gives up after 2 s.

### Deviations from the brief

- **Config-file group kind:** the brief's table calls it the "child-folder"
  kind. In the inventory, `RowKind.CHILD` is the `*.conf` file and
  `RowKind.CHILD_FOLDER` is a directory, so `CHILD` gets the file icon and
  directories get folders.
- **Light badge colors:** `success` and `text_muted` reach only 2.73:1 and
  2.71:1 on the light selected row, below the brief's own 3:1 target. The
  badge uses `badge_connected` `#2A9D6A` and `badge_unknown` `#848E88`, the
  nearest shades that reach 3:1. Dark keeps `success` and `text_muted`.
- **Icon height test:** the glyphs keep the sheet's 24-grid margins (the
  folder's ink is about 17/24 of its box). So the tests check that the
  rendered icon surface is exactly 20 / 24 px, and that the ink is 65–100 %
  of that height and centred on the row within 2 px, rather than "ink height
  = icon size ± 1".
- **Badge ring:** the ring is 2 px from 20 px up, not the 1.5 px first tried.
  At 1× a 1.5 px ring cannot hold a single clean pixel, and 2 px keeps the
  cut-out on whole pixels.
- **Read-only with a status:** a read-only host with an unknown status shows
  only the lock, bottom-right, as on the sheet. With a known status the badge
  takes the bottom-right and the lock moves to the top-right.
- **Not done:**
  - Group aggregate badges (optional in the brief).
  - Icons in the Host dialog's Group field: that field does not exist, and
    group choice happens in the group picker.
  - The breadcrumb stays text-only.
- **Reference comparison:** the tree region of
  `docs/ui-after/relay-report.md` now differs from the old Relay reference by
  3.0 % mean error (was 2.36 %). That is exactly at the 3 % target: the bigger
  icons are the intended change, and the old reference predates them.
- **Licensing:** the draft folder and folder-open outlines appear to be
  Lucide's (ISC). `assets/icons/LICENSE` says so; keep them with Lucide's
  notice, or redraw.

### Verification

- **`tests/test_tree_icons.py`** (pure): icon per row kind, expanded or
  empty groups, file-backed groups, hosts, selected host, lock; user tint;
  sizes per density; brief token values; ≥ 3:1 for every icon and badge color
  on surface, hover and selected in both themes; status → badge
  (shape and color, × only at ≥ 24 px, pulse); badge, lock and ring
  geometry; pulse ≤ 1 Hz and quantised. Also an SVG lint (no `stroke`, no
  `<style>`, `currentColor`, duotone path), the read-only rule in the row
  builder, and that the draft files are gone.
- **`tests/test_gtkui_tree_icons.py`** (rows rendered through the tree's
  painter onto cairo surfaces):
  - **Names:** icon per row (open folder, folder, file, server).
  - **Size and shape:** the icon surface is 20 / 24 px. The ink is centred,
    and its outline-strength ratio is between 8 % and 55 % (not empty, not a
    blob).
  - **Colors:** selected host = accent, selected group stays amber, the user
    tint is applied.
  - **Badges:** the badge colors match at 2×, and the ring pixel equals the
    row background on normal, hover and selected rows in both densities. The
    cut-out removes icon ink.
  - **Other rows:** lock alone, or with a badge; the right edge is empty on
    host rows and shows the count on groups.
  - **HiDPI:** at 2× the ink box doubles, and there is a ≥ 80 % contrast step
    between neighbouring pixels (crisp).
  - **Animation:** the pulse runs only with animations on, and the timer
    stops when nothing is connecting.
  - **Also checked:** a theme switch recolors without a restart, and the
    drag icon is drawn with the new painter.
  - **GTK's own loader:** the four icons come out at 20 and 24 px, neither a
    blob nor empty.
  - **Performance:** 2,040 rows paint from the cache with SVG parsing
    disabled. Fewer than 80 cached surfaces, under 2 ms per row.
  - **Real window:** tooltips follow status changes, and the group picker
    rows carry icons.
- **`tests/test_gtk_terminal_helpers.py`:** the focus retry is a timer and
  gives up.
- **Screenshots:** all of `docs/ui-after/` regenerated; the four
  `tree-icons-*` scenarios are new.
- **Full suite:** `python3 -m unittest discover` passes.

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
