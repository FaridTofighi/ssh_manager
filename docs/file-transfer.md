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
  - **Passwords:** the same rules as SSH (vault → credential → host-specific
    → saved). A rejected password opens the same retry dialog (at most 3).
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

## Known limits (so far)

- **Transfers in this phase:** sequential, files only. Folders, the transfer
  queue, conflicts and drag and drop come in phase F3.
- **Not yet:** column widths and visibility are not remembered per host yet.
  A recursive delete runs in the background without a progress dialog.
- **Skipped (optional in the brief):** "Open in terminal" for hosts that also
  have an SSH entry.

- **Integration test servers:** they are small fakes in `tests/`
  (`fake_ftp.py`, `fake_sftp.py`). `pyftpdlib` is not needed. Setting
  `SSHM_SKIP_INTEGRATION=1` skips the socket tests.
- **SFTP jump hosts:** use `ProxyJump` / `ProxyCommand` through an `ssh -W`
  command, like the Paramiko SSH backend.
