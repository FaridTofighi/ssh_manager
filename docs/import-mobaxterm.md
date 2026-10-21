# Importing sessions from MobaXterm

App menu → **Import → From MobaXterm (.mxtsessions)…**, or **Import from
MobaXterm…** in the empty sidebar. A five-step wizard reads a MobaXterm
sessions export and copies its folders and SSH sessions into the SSH Manager
inventory as groups and hosts. Nothing is written until you press **Import**. Nothing in
the file is executed, connected to or resolved (no DNS, no ping).

Code: `ssh_manager/importers/mobaxterm.py` (pure Python: parse, plan, map
credentials, commit), `gtkui/dialogs/mobaxterm_import.py` (the wizard) and
`gtkui/controllers/mobaxterm_import.py` (entry point).

## The `.mxtsessions` format

A plain-text, INI-like file. Real exports are ASCII with CRLF line endings
and no BOM. The importer also accepts a UTF-8 BOM, UTF-16 (with or without a
BOM), UTF-8, cp1252 and finally latin-1. It handles CRLF, LF and lone CR, and
strips NUL bytes. Files over 50 MB are refused.

```ini
[Bookmarks]
SubRep=Acme
ImgNum=41
bastion=#109#0%10.0.0.1%22%[alice]%%-1%-1%%%%%0%…

[Bookmarks_1]
SubRep=Acme\Team A\Servers
ImgNum=41
web 01=#109#0%10.0.0.10%22%[alice@corp.local]%%-1%-1%…
behind jump= #109#0%10.0.0.13%22%bob%%-1%-1%%10.0.0.254%22%alice%…
```

- **Folders:**
  - Each `[Bookmarks]` / `[Bookmarks_N]` section is one folder. `SubRep=` is
    its full path, with segments separated by `\`.
  - The hierarchy comes from the path only. The section number and order
    mean nothing.
  - Missing ancestors are created implicitly.
  - Sections without sessions are plain containers.
  - `ImgNum=` (an icon index) is ignored.
- **Sessions:**
  - Every other `name=value` line in a section is a session. The line is split
    at the first `=`, and spaces after it are allowed.
  - The value is `#<icon>#<type>%f0%f1%…`. The field count varies (49–64
    fields seen); missing fields read as empty.
  - **Types 0 (SSH), 6 (FTP) and 7 (SFTP) are imported.** Telnet, RDP, VNC,
    serial, Mosh, WSL and the other types are skipped and counted. FTP and
    SFTP can be left out with **Import FTP/SFTP sessions** (on by default).
- **FTP and SFTP fields:** only `f0` (host), `f1` (port; empty means 21 for
  FTP, 22 for SFTP) and `f2` (user) are read. Their other fields are not
  verified, so passive / active mode, FTPS, jump hosts and keys keep the
  defaults. An FTP session becomes a plain `ftp` host with the warning
  `ftps_unknown` ("check whether this server uses FTPS"). An FTP session
  with no user, or the user `anonymous`, is stored as anonymous
  (`metadata["transfer"]["anonymous"]`) and gets no credential.
- **SSH fields:**
  - `f0` is the host name. An IPv6 literal may be bracketed. A `host:port`
    form is split only when the port field is empty.
  - `f1` is the port (1–65535; anything else becomes 22, with a warning).
  - `f2` is the username (below).
  - `f7`, `f8` and `f9` are the jump host, its port and its user. When `f7` is
    empty, `f8` and `f9` are ignored.
  - A private-key path is used only when a field clearly is one (it ends in
    `.pem`, `.ppk` or `.key`, or sits under `.ssh`). A Windows path is kept,
    with a warning to select the key again.
  - A `%` zone id in a host name cannot occur, because `%` is the field
    separator.
- **Usernames (`f2`):**
  - `[label]` is a reference to a credential saved **inside MobaXterm**, and
    the label is not necessarily a login name.
  - `alice` is a literal username.
  - An empty field means no username.
  - The same person can appear as `[alice]`, `alice` and `[alice@corp.local]`.
- **Not imported:** passwords (never in an export), icons, colors, fonts,
  terminal settings, X11 forwarding and the other per-session options.
  MobaXterm escape tokens in names are left unchanged: none are decoded,
  because none could be verified.

## How folders and sessions are mapped

The import writes into the SSH Manager inventory (`docs/inventory-store.md`).
There, groups nest to any depth and a group can hold both hosts and
subgroups, so the MobaXterm tree is copied **1:1**.

| MobaXterm | SSH Manager |
|---|---|
| Folder `S1\S2\…\Sn` | Group path `S1 / S2 / … / Sn` (any depth) |
| Top-level folder `S1` | Top-level group `S1`; sessions directly in it are its hosts |
| A folder with sessions **and** subfolders | One group holding both |
| Empty container folders | Created as empty groups (**Skip empty folders** leaves them out) |
| Session | Host: alias = session name, `hostname`, `port`, `user` (see below), `proxyjump`, optionally `identityfile`; metadata `imported_from=mobaxterm` and `mobaxterm_folder=<original SubRep>` |

- **Under an existing group:** with **Import under an existing group…** (any
  group, chosen by its path), the whole MobaXterm tree is mounted beneath that
  group.
- **Names are kept verbatim:**
  - NFC normalisation.
  - Line breaks and tabs become a space, control and bidi-override characters
    are dropped, and the result is trimmed.
  - Spaces, `+`, parentheses, accents, Persian text and zero-width joiners
    stay as they are.
- **Only what the store cannot hold changes,** and every change is reported:
  - `/` becomes `∕` (U+2215), because `/` separates group paths;
  - names longer than 128 characters are cut at 128;
  - empty names become `Unnamed`, `Unnamed 2`, ….
- **Uniqueness, only where the store requires it:**
  - **Collisions:** sibling groups and the hosts of one group are unique
    case-insensitively. A collision gets ` (2)`, ` (3)` (file order), and is
    reported.
  - **Same names elsewhere:** sessions with the same name in different folders
    are not renamed.
  - **Existing groups:** a folder whose name matches an existing group at the
    same place is merged into it, which is what makes re-imports idempotent.
- **Hosts that cannot be imported:** an empty or invalid host name skips the
  session and reports it. The alias is free text now, so it cannot stand in
  for a host name.
- **Jump hosts:** a jump host becomes `ProxyJump [user@]host[:port]`, which
  `build_ssh_command` passes as `-J`. The port is left out when it is 22, and
  IPv6 hosts are bracketed. An invalid jump host is dropped, with a warning.
- **Safety:** host names, users and jump hosts that start with `-` are
  rejected, so they can never become `ssh` options. Folders nested deeper than
  32 levels are skipped.

## The Credentials step

1. **Usernames:** the wizard lists every distinct username in the *selected*
   SSH sessions. `[alice]` and `alice` are merged into one row, while
   `Alice` and `alice` stay separate rows with a ⚠ hint. Sessions without a
   username are listed as **(no username)**. Each row shows how many hosts
   use it, and its tooltip shows the raw forms.
2. **Default credential:** this first control pre-fills every row you have
   not set by hand; such rows say *default*.
3. **Per row:** choose an existing credential, **None**, or **Create new
   credential…**. That opens the credential editor pre-filled with the
   username; the password is typed there, never read from the file.
4. **Next:** stays disabled until every row has a decision. With no
   credentials at all, the step offers **Create New Credential…** and
   **Import without Credentials**.

On import, a host whose username maps to credential *C* gets
`User = C.username` and is bound to *C* (one batched save,
`CredentialStore.bind_many`). With **None**, a literal username is kept as
`User`, and a bracketed label is dropped. Jump-host users are written as they
are.

The credential store is unlocked at application start (master password), so
the wizard never needs a separate unlock. Credentials are re-checked right
before writing: one deleted meanwhile aborts the import with no changes.

## Conflicts and re-runs

An imported host **is the same host** as an existing one when it has the same
destination group, protocol, host name, port and user (host name and user
compared case-insensitively). An SFTP session therefore never matches the SSH
host on the same `host:22`. For such hosts:

- **Skip** (default) leaves the existing host untouched.
- **Import a copy named “… (2)”** adds a numbered copy.
- **Update the existing host** overwrites its options, user, metadata and
  credential binding **in place**. The host keeps its alias and its id, and
  the wizard asks for confirmation, showing the count. A database backup is
  made first. If an earlier copy has the same host, the one with the same
  alias is updated.

A different user means a different host: it is added, never merged. A
different host that already uses the alias gets ` (2)`.

Importing the same file twice with the defaults changes nothing; the tests
compare row counts and a content hash.

## Atomicity

The whole import is planned in memory. The commit is **one**
`InventoryStore.transaction()`, and nothing is written to disk outside the
database. Inside it:

- **Destination:** the destination group must still exist (another window may
  have deleted it).
- **Writes:** all groups are created (`create_group_path`), all hosts are
  inserted (`add_hosts_bulk`) and the overwritten ones updated.
- **Bindings:** the credential bindings are saved with `bind_many`, keyed by
  the **new host ids**.

Credentials are re-checked right before writing. The tree is refreshed once.

**Rollback is the transaction.** A database error, a failed binding save, a
problem found late, a deleted credential or destination, or a cancel all
leave the inventory exactly as it was: "Import failed, no changes were made".
While writing, the wizard cannot be closed. Cancel or Esc cancels the write,
the transaction rolls back, then the wizard closes.

## The report

The Review step shows a report you can copy. After the import, it is saved as
`~/.local/share/ssh-manager/imports/mobaxterm-<timestamp>.log`, with mode
0600 (under `$XDG_DATA_HOME` when set).

- **Contents:** counts, skipped types, issue codes with line numbers, the
  credential mapping (credential names only), and original → stored names.
- **Never included:** passwords or raw session lines.

The application log records only the outcome and counts.

## Troubleshooting

| Message | What to do |
|---|---|
| "This does not look like a MobaXterm sessions file." | The file has no `[Bookmarks]` section. Export the sessions again from MobaXterm. |
| "The file is empty." / "binary file" | Choose the `.mxtsessions` file itself, not an archive or a settings file. |
| "…larger than 50 MB…" | Not a sessions export; MobaXterm exports are a few MB at most. |
| `unsupported_type` in the report | Sessions of other types (RDP, VNC, …; FTP / SFTP when unticked) are counted, not imported. |
| `ftps_unknown` | An FTP session: the export does not say whether the server wants TLS. Edit the host and choose FTPS if it does. |
| `empty_hostname` / `invalid_hostname` | The session has no usable host name; fix it in MobaXterm or add the host by hand. |
| `windows_key_path` | The key path points to Windows; edit the host and choose the key again. |
| `slash_replaced` / `name_truncated` / `name_deduplicated` | A name had to change for the inventory; the report lists original → stored. |
| `destination_missing` | The group chosen under “Import under an existing group” was deleted; choose it again. |
| "The inventory is in use by another SSH Manager window" | Another instance is writing; try again in a moment. |
