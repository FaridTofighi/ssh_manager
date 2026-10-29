# File transfer: FTP, FTPS and SFTP

Hosts can use the protocols `ftp`, `ftps` and `sftp` (see
`docs/protocols.md`). Code: `ssh_manager/transfer/` (pure Python, no GTK).

| Module | Role |
|---|---|
| `backend.py` | `RemoteFileBackend` interface, `RemoteEntry`, `CancelToken`, error classes |
| `ftp.py` | FTP and FTPS (explicit `AUTH TLS`, implicit TLS) on `ftplib` + `ssl` |
| `sftp.py` | SFTP on `paramiko` (already used by the SSH connector) |
| `listing.py` | `MLSD` and `LIST` parsers (Unix, DOS / IIS, NetWare, VMS) |
| `settings.py` | `TransferSettings`, stored in `host.metadata["transfer"]` |
| `pins.py` | Trusted certificate fingerprints |
| `factory.py` | The right backend for a host |
| `protolog.py` | The per-tab connection log and the optional rotating protocol log file |

No new runtime dependency: `ftplib`, `ssl` and the existing `paramiko`.

## Settings (host dialog → File transfer)

Stored as `metadata["transfer"]` (`"v": 1`), never as SSH options.
- **Invalid values** fall back to the default, and the page shows a warning.
- **Unknown keys** (from a newer version) are kept.

| Setting | Default | Notes |
|---|---|---|
| Protocol / port | FTP 21, FTPS 21 (implicit 990), SFTP 22 | The port placeholder follows the protocol and TLS mode |
| TLS (FTPS) | explicit | `explicit` (`AUTH TLS`) or `implicit` (TLS from the first byte) |
| Certificate | verify with the system CAs | `custom_ca` (choose a file), `pinned`, `insecure` (asks every time it is turned on) |
| Passive mode | on | Off = active mode, optionally within a local port range |
| Encoding | automatic (UTF-8, `OPTS UTF8 ON` when offered) | `latin-1`, `cp1252`, `cp1256`, `cp1251`, `shift_jis` or any codec name |
| Timeouts | connect 15 s, read 60 s | |
| Keepalive | 30 s (`NOOP` / SSH ignore packet) | 0 = off |
| Parallel connections | 3 | 1–8; FTP needs one connection per transfer |
| Remote / local folder | login folder / — | |
| Mode | binary | "Text files in ASCII mode" with an extension list |
| Upload to `.part`, then rename | on | Never a half-written file under the real name |
| Keep modification times | on | `MFMT` (FTP, when offered), `utime` (SFTP) |
| Speed limit | unlimited | KB/s |
| Anonymous login | off | User `anonymous`, password `anonymous@` (a convention, not a secret); no credential |
| Trust the PASV address | off | See "PASV" below |
| Listing | automatic | `MLSD` when offered, else `LIST`; or force `MLSD`, `LIST`, `LIST -a` |
| Server time offset | 0 min | For `LIST` times of servers in another time zone |
| Show hidden files, follow symlinks | off | |

**Test connection** connects in the background with the values as entered.
- **Reports:** the banner, TLS version and cipher, certificate fingerprint,
  features and the time taken.
- **Never saves anything:** not a certificate, not a host key, not a password.

## Security

- **Certificates (FTPS):** verified against the system CAs (or a custom CA
  file) by default, TLS 1.2 minimum.
  - **Untrusted certificate:** subject, issuer, validity and the SHA-256
    fingerprint are shown. Trusting it pins the fingerprint in
    `$XDG_DATA_HOME/ssh-manager/tls-pins.json` (0600), keyed by host id and
    `hostname:port`.
  - **Changed certificate:** a pinned certificate that changes is refused
    ("certificate has CHANGED").
- **TLS session reuse:** vsftpd (`require_ssl_reuse`), FileZilla Server and
  ProFTPD refuse a data connection that does not resume the control
  connection's TLS session. Python's `ftplib` does not do that; the backend
  passes `session=` when it wraps the data socket. The tests run against a
  server that enforces it, and show that stock `ftplib` is refused there.
- **PASV:** the IP address in a PASV reply is ignored, and the control
  connection's peer is used. This stops FTP-bounce and redirect tricks. Only
  "Trust the server-provided PASV address" (for some NAT setups) changes it.
- **Active mode:** a data connection from any address other than the server's
  is closed.
- **Commands:** a path with CR, LF or NUL is refused before anything is sent.
  `PASS` is masked in the protocol log.
- **Names:** decoded with `surrogateescape`, so a name in an unexpected
  encoding round-trips unchanged. Server text (names, banners) is shown as
  plain text, never markup.
- **SFTP host keys:** the same rules as SSH (`host_key_policy`, the system
  `known_hosts`).
  - **Unknown key:** shown with its fingerprint. Accepting it appends a line
    to `~/.ssh/known_hosts`, as `ssh` does.
  - **Changed key:** refused.
- **SFTP authentication:** the host's private key (with passphrase), the SSH
  agent, a password, or keyboard-interactive with one password prompt.
  One-time-code prompts are reported as unsupported.

## Listings

- **MLSD:** RFC 3659 facts (type, size, modify, perm, unix.mode, owner).
- **Unix `LIST`:** with or without the group column; symlinks
  (`name -> target`), names with spaces and device files. `HH:MM` dates get
  the current year, or the previous one if that would be in the future.
- **DOS / IIS `LIST`:** `01-02-24 03:04PM <DIR> name`, two-digit years.
- **NetWare, VMS:** best effort.
- **Unrecognized lines:** kept as entries of kind `other`, never dropped.
- **Skipped lines:** `total N`, `.` and `..`.
- **Empty folders:** a `550` / `450` answer for an empty folder is an empty
  listing.

## Errors

Every failure is a `BackendError` with a message for the user and a
technical detail for the expander. Neither contains a password.

| Error | When |
|---|---|
| `AuthError` | `530`, SSH authentication failure |
| `NotFound` / `PermissionDenied` | `550` (by its text), `ENOENT` / `EACCES` |
| `DiskFull` | `452`, `552` |
| `ProtocolError` | `553` (bad name), unexpected replies, CR/LF in a path |
| `Unsupported` | `500`/`502`/`504`, a missing feature (`REST`, `MFMT`, `SITE CHMOD`) |
| `Disconnected` | `421`, `425`/`426`, a reset or closed connection |
| `Timeout` | No answer in time |
| `UntrustedCertificate` / `CertificateChanged` / `TlsError` | TLS |
| `UnknownHostKey` / `HostKeyChanged` | SFTP host keys |

- **Reconnect:** after `421` or a dropped connection, FTP reconnects up to
  3 times with backoff (0.5, 1, 2 s) and returns to the current folder.
- **Missing `226`:** a server that closes the data connection without one
  does not hang the transfer.

## The file browser (FTP, FTPS, SFTP tabs)

Double-click an FTP, FTPS or SFTP host to open a **file-browser tab**:
- `gtkui/file_browser/tab.py`, a `SessionPage` like the terminal;
- it sits in the same strip, tile grid and status bar.
- **SSH-only features stay off for these tabs:** split, broadcast,
  transcripts, remote stats, zoom and copy-as-plain-text. The toolbar and
  tab menu hide or disable them.
- **Layout:** the local pane (this computer) and the remote pane (the
  server) side by side. "Show only the server's files" hides the local side.
  - **Toolbar:** Back, Forward, Up, Home, Refresh, New folder, Rename,
    Delete, Show hidden.
  - **Path bar:** breadcrumbs; Ctrl+L types a location (completing loaded
    folder names). An invalid location shows an error and keeps the old
    folder.
  - **Also in each pane:** a live filter (Ctrl+F, `*` / `?` wildcards) and a
    footer ("N items · M selected · size").
- **The list:**
  - **Columns:** Name, Size, Modified (full time in the tooltip),
    Permissions (`rwxr-xr-x (755)`), Owner (remote).
  - **Sorting:** click a header; folders first, natural, case-insensitive
    order.
  - **Selection:** multi-select with rubber band, plus type-ahead.
  - **Large folders:** listings stream in batches; a spinner appears after
    300 ms; the rendered rows are capped (20,000) with "refine the filter".
    A newer navigation cancels an older one.
- **Keyboard:**

  | Keys | Action |
  |---|---|
  | Enter | Open |
  | Backspace / Alt+Up | Up |
  | Alt+Left / Alt+Right | Back / Forward |
  | F2 | Rename (inline) |
  | Delete | Delete (confirmation lists the items) |
  | F5 | Refresh |
  | F7 | New folder |
  | Ctrl+A | Select all |
  | Ctrl+C, then Ctrl+V on the other side | Copy across |

- **Operations** (context menu): Open, Download / Upload, New folder,
  Rename, Copy path, Properties, Copy remote URL (never with a password),
  Delete.
  - **Properties:** a folder's size on demand, and permissions with an octal
    entry plus check boxes when `chmod` is supported.
  - **Open (remote file):** downloads into a private temporary folder (0700,
    file 0600) and opens it with the default application. When the file is
    saved, the browser offers to upload it back (to `.part`, then rename).
    The folder is removed when the tab closes.
- **Bookmarks and history:**
  - **Bookmarks:** per host, in the bookmarks menu, with the recent server
    folders.
  - **Last folders:** the last local and remote folders are remembered per
    host in `$XDG_DATA_HOME/ssh-manager/file-browser.json` (0600). Turn this
    off in Preferences.
- **Read-only hosts** (local permission "Allow editing" off): browse and
  download only. Upload, new folder, rename and delete are disabled, and say
  why.
- **Connecting:**
  - **Passwords:** the same rules as SSH (credential → host-specific → saved). A rejected password opens the same retry dialog (at most 3).
    A typed password that worked is offered once: "Save this password for
    this host?".
  - **Plain FTP with a password:** asks once per host and keeps a "Not
    encrypted" badge.
  - **Certificates and host keys:** an untrusted FTPS certificate or an
    unknown SFTP host key is shown for confirmation. A changed certificate
    defaults to Cancel; a changed host key is refused.
  - **Lost connection:** shows the red banner with **Reconnect**.
- **Closing a tab:** cancels listings and transfers, removes its timers,
  closes the connection in the background and deletes its temporary files.
- **Remote names are untrusted:**
  - **Display:** shown as plain text (escaped in tooltips) and wrapped in
    Unicode isolates, so right-to-left names never reorder the row.
  - **Downloads:** names are made safe (`transfer/names.py`). A name like
    `../../x` lands as `.._.._x` inside the destination folder, never
    outside it.

## Transfers

Every upload, download and copy is a **job** in the tab's **Transfers**
panel (`transfer/engine.py`, `gtkui/file_browser/transfers.py`).

- **Planning:** a job is planned in the background ("Scanning… N files").
  - **Folders:** copied recursively, and empty folders are kept.
  - **Symbolic links:** skipped (reported) unless "Follow symbolic links" is
    on. Loops are caught (device + inode locally, visited paths remotely),
    and the depth is capped at 64.
- **Running:**
  - **Limits:** at most the host's "Parallel connections" at a time (FTP
    opens one connection per transfer), within the global cap from
    Preferences.
  - **States:** queued → running → paused / done / failed / cancelled /
    skipped.
  - **Progress:** bytes, a smoothed speed and an ETA, updated at most 10
    times a second.
  - **Row actions:** pause, resume, cancel, retry the failed files, reveal
    the destination, remove.
  - **Panel actions:** pause all, resume all, clear finished, and a speed
    limit in KB/s.
- **Retries:** a dropped connection or timeout is retried twice (1 s, then
  3 s). Other errors fail the file with its reason. "Retry" re-queues only
  the failed files.
- **Conflicts:** when a name exists, the dialog shows both sides' size and
  date. The choices:
  - **Overwrite**;
  - **Overwrite if newer**;
  - **Skip**;
  - **Keep both** (`name (1).ext`);
  - **Resume** (when the existing file is shorter and the server can
    resume).

  "Apply to all" answers the rest of the job. The default policy is in
  Preferences ("Ask" by default).
- **Integrity:**
  - **Uploads:** go to `name.part` and are renamed when complete (setting
    "Upload to a .part file").
  - **Downloads:** always go to `name.part` in the destination, then
    `os.replace`.
  - **Checks:** the size is checked after every file, and the modification
    time is set when "Keep modification times" is on.
  - **Cancel and failure:** a cancel or failure removes the partial file.
  - **Pause:** keeps the `.part` file, and **resume** continues it where it
    stopped (when the server supports resuming).
- **Untrusted names:** every name from the server is made safe before it
  touches the disk (`transfer/names.py`).
  - **Neutralized:** `..`, absolute paths, drive letters, path separators,
    NUL / control characters, bidi tricks, and trailing dots and spaces.
  - **Long names:** shortened with a hash.
  - **Windows reserved names:** reported.
  - **Containment:** the resulting path is checked to stay inside the
    destination, symlinks included. `../../.bashrc` becomes `.._.._.bashrc`
    in the chosen folder.
- **Speed:** token buckets per host ("Speed limit" in the host settings),
  per tab (panel) and globally (Preferences). Data is streamed in 256 KiB
  chunks in constant memory.
- **Closing:**
  - **A tab:** closing it with transfers running asks "N transfers are in
    progress. Cancel them and close?".
  - **The window:** quitting asks too.
- **Notifications:** a big job (≥ 20 files or ≥ 100 MB) finishing shows a
  message, and a desktop notification when the window is not active.
  Failures always show one.
- **Status bar:** shows "2 transfers · 14.2 MB/s"; the header's Transfers
  button opens the panel.

## Drag and drop

| From | To | Result |
|---|---|---|
| Local pane (or the desktop's file manager) | Remote pane or a remote folder row | Upload |
| Remote pane | Local pane or a local folder row | Download |
| A pane | A folder row in the same pane | Move (hold **Ctrl** to copy) |
| Remote pane of tab A | Remote pane of tab B (another server) | Server-to-server copy, streamed through this computer (no temporary file) |
| Remote pane | Remote pane of the same connection | Move by rename (Ctrl: copy through a second connection) |
| Remote pane | The desktop / another application | Not supported: use **Download** |

- **Drop feedback:**
  - **Folder rows:** highlighted; hovering one for 800 ms opens it
    (spring-loaded).
  - **Scrolling:** the list scrolls near its edges.
  - **Drag icon:** shows the item count.
  - **Esc:** cancels the drag.
- **Rejected drops:**
  - **Forbidden targets:** a read-only host, or a folder into itself or a
    descendant.
  - **Payload checks:** paths not listed in the source pane (the
    `application/x-sshmanager-entries` payload is validated on drop, never
    trusted).
  - **External sources:** URIs that are not existing local files.
- **Keyboard alternative:** **Ctrl+C** in one pane and **Ctrl+V** in the
  other, or the context menu's **Upload** / **Download**.

## Preferences (Settings → File Transfer)

Stored as `file_transfer` in the settings file.

| Setting | Default | Effect |
|---|---|---|
| Download folder | `~` | Where the local side of a new browser starts (a host's own local folder or a remembered one wins) |
| When a file exists | Ask | The default conflict policy of new transfers |
| Show hidden files | off | Both panes (a host can also turn it on) |
| Remember the last folders | on | Per host, in `file-browser.json` |
| Open remote files with the default application | on | Off: "Open" downloads only |
| Delete temporary copies when closing | on | Removes the tab's temporary folder |
| Parallel transfers | 3 | Global cap (1–16); each host also has `max_connections` |
| Bandwidth limit | 0 (unlimited) | KiB/s for all transfers of a tab |
| Warn before unencrypted FTP | on | The "Send the password unencrypted?" question |
| Keep a protocol log file | **off** | See below |

## Diagnostics

- **Connection log:** the activity button in a file-browser tab's header
  shows the tab's log: FTP commands and replies (`>` / `<`), SFTP steps
  (server version, host key, the authentication methods tried, the home
  folder) and connection failures with their technical detail. It keeps the
  last 2000 lines in memory. **Copy Log** copies it.
- **Copy Diagnostics** copies the protocol, server address, user, status,
  FTP mode / listing command / encoding, FTPS mode and verification, and the
  server's banner, system type, features, TLS version and cipher,
  certificate fingerprint or host key.
- **Masking:** `PASS …` is masked by the FTP backend, and the log masks the
  password of the current attempt again in every line. Control and bidi
  characters from the server are shown as `?`. Nothing in the log, the
  clipboard copies or the file contains a password.
- **Protocol log file** (off by default): the same lines, with the date and
  the host alias, appended to `$XDG_STATE_HOME/ssh-manager/protocol.log`
  (`~/.local/state/…`), mode 0600, rotated at 1 MiB with three old copies.
  It applies to tabs opened after it is turned on.

## Known limits

- **Remote to desktop:** dragging from the remote pane to another
  application is not supported (it would need XDS). Use **Download**.
- **Window-level summary:** the transfer summary is per tab (its status
  bar); there is no window-wide indicator yet.
- **Columns:** widths and visibility are not remembered per host yet.
- **Recursive delete:** runs in the background without a progress dialog.
- **Skipped (optional in the brief):** "Open in terminal" for hosts that also
  have an SSH entry. The other direction exists: **Browse Files (SFTP)** on
  an SSH host.
- **Files view status:** the SFTP view of an SSH host does not change the
  host's status dot in the tree (that belongs to its terminal sessions).
- **Protocol log file:** turning it on or off affects new tabs only.
- **SFTP checksums:** after a transfer, sizes are checked; there are no
  checksums.
- **Integration test servers:** small fakes in `tests/`. `pyftpdlib` is not
  needed, and `SSHM_SKIP_INTEGRATION=1` skips the socket tests.
