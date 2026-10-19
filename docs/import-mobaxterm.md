# Importing sessions from MobaXterm

App menu → **Import → From MobaXterm (.mxtsessions)…**, or **Import from
MobaXterm…** in the empty sidebar. A five-step wizard reads a MobaXterm
sessions export and turns its folders and SSH sessions into SSH Manager
groups and hosts. Nothing is written until you press **Import**. Nothing in
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
  - **Only type 0 (SSH) is imported.** Telnet, RDP, VNC, FTP, SFTP, serial,
    Mosh, WSL and the other types are skipped and counted.
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

SSH Manager stores a parent group as a directory and a child group as a file
inside it (a child may be a nested path such as `a/b/c`). Hosts are `Host`
stanzas inside child-group files.

| MobaXterm | SSH Manager |
|---|---|
| Top-level folder `S1` | Parent group `S1` (one per top-level folder) |
| Folder `S1\S2\…\Sn` | Child group `S2/…/Sn` |
| Sessions directly in `S1` | Child group `General` (`_hosts` if a real subfolder is called General) |
| A folder with its own sessions **and** subfolders | Its sessions go to `…/Sn/_hosts`, because a file and a folder cannot share a name |
| Container folders (no SSH sessions beneath) | Not created, unless **Also create empty folders** is ticked |
| Session | Host, with `hostname`, `port`, `user` (below), `proxyjump` and optionally `identityfile` |
| Session name, folder path | `# meta display_name=…`, `# meta mobaxterm_folder=…`, `# meta imported_from=mobaxterm` |

- **Existing parent group:** with **Import under an existing parent group**,
  the top-level folder becomes the first segment of each child path.
- **Names on disk:**
  - Group names and aliases use only `A–Z a–z 0–9 . _ -`. Other characters
    become `-`, accents are transliterated, and leading or trailing `-` / `.`
    are stripped.
  - `.` and `..` are never used.
  - Names are capped at 64 characters, ending in a stable 4-character hash of
    the original.
  - Names that would be hidden by the loader (`.bak`, `.tmp`, …) are adjusted,
    and empty results become `unnamed-<n>`.
- **Uniqueness:** siblings and aliases in one group are kept unique
  case-insensitively (`-2`, `-3`, …). Naming is deterministic: the same file
  always gives the same names.
- **Original names:** each host keeps its original session name in
  `display_name`. Groups have no display-name storage. A sidecar file would
  have to follow every rename, move, duplicate and delete of groups, so the
  import report lists every folder that was stored under a different name
  instead, and each host records its original folder path in
  `mobaxterm_folder`.
- **Jump hosts:** a jump host becomes `ProxyJump [user@]host[:port]`, which
  the SSH command builder passes as `-J`. The port is left out when it is
  22, and IPv6 hosts are bracketed. An invalid jump host is dropped, with a
  warning.
- **Safety:**
  - Host names, users and jump hosts that start with `-` are rejected, so
    they can never become `ssh` options.
  - Every target path is checked to lie inside the inventory folder before
    anything is written. A file with `..\..\etc` or `C:\…` folders cannot
    write outside it.
  - Folders nested deeper than 32 levels are skipped.

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

## Conflicts, duplicates, re-runs

- **When a host already exists:**
  - **Skip** (default) keeps the existing host.
  - **Rename** imports the new one with a `-2` suffix.
  - **Overwrite** replaces it, after a confirmation that shows how many hosts
    are affected.
- **Skip duplicates:** with **Skip hosts whose host name, port and user
  already exist in the target group** (on by default), such hosts are not
  imported again, even under another alias.
- **Existing groups:** hosts are added to an existing group; its other hosts
  are kept.
- **Clashes on disk:** a group file where the import needs a folder gets a
  suffixed folder, and a folder where it needs a file puts the hosts in
  `_hosts`.
- **Re-runs:** importing the same file twice with the defaults adds nothing,
  and the wizard says so.

## Atomicity

The whole import is planned in memory first. Then
`ConfigManager.import_groups` writes each child-group file **once**,
atomically, and the credential bindings are saved once. The tree is
refreshed once.

- **Concurrent edits:** if any target file changed on disk since the import
  was prepared, nothing is written.
- **Failures:** a write error (disk full, permissions), a failed binding
  save, a deleted credential or a cancel restores overwritten files byte for
  byte and deletes the files and folders the import created. The result is
  "Import failed, no changes were made".
- **Busy wizard:** while writing, the wizard cannot be closed. Cancel or Esc
  cancels the write, rolls it back, then closes.

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
| `unsupported_type` in the report | Non-SSH sessions (RDP, FTP, …) are counted, not imported. |
| `empty_hostname` / `invalid_hostname` | The session has no usable host name; fix it in MobaXterm or add the host by hand. |
| `windows_key_path` | The key path points to Windows; edit the host and choose the key again. |
| `folder_collision_resolved` | A folder was stored under another name to avoid a clash; the report shows where. |
| "…changed since the import was prepared." | Another window or tool edited the inventory; run the import again. |
