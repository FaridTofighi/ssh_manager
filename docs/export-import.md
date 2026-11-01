# Ophion export and import (`*.sshm`)

Ophion's own file format: the inventory's groups and subgroups (any
depth), hosts of every protocol with their settings, colours, notes, tags and
credential *references*, in one file that Ophion on another computer (or
after a reinstall) imports again without loss. It never contains a password.

Code: `ophion/native_format.py` (format, integrity, encryption),
`ophion/exporters/native.py` (export), `ophion/importers/native.py`
(import), `gtkui/dialogs/native_export.py`, `gtkui/dialogs/native_import.py`,
`gtkui/controllers/native_transfer.py`.

## Exporting

* App menu → **Export → Ophion Export (.sshm)…** (the whole inventory).
* A group's context menu → **Export Group…** (the group and its subgroups).
* A host's context menu → **Export Selected Hosts…** (the hosts selected in the
  sidebar, with the groups they are in).
* The action *Export as Ophion file (.sshm)* can get a shortcut
  (Settings → Keyboard Shortcuts).

The dialog asks what to export and what to include: **Colours** (on),
**Notes and tags** (on), **Credential references — names and usernames only**
(on), **Smart groups** (off), and **Protect with a passphrase** (off; recommended).
The passphrase is typed twice, gets a strength hint and is never stored.
Without one, the dialog says: *This file lists your servers, users and
settings. It contains no passwords.*

The file (default name `ophion-YYYYMMDD-HHMM.sshm`) is written atomically
with mode 0600; the file chooser asks before replacing a file; an unwritable
folder shows the system's error. Read-only inventories can be exported. A
toast reports the result; **Details** opens the export report (counts per
protocol, colours, credentials referenced, fields removed, warnings).

**Never exported:** passwords (credentials, host-specific passwords), private
key files (only their paths), TLS
certificate pins, macros, the master password, application settings,
history and activity, window state.

### Exporting a group or some hosts

An exported group becomes a **top-level** group in the file: its own name is
kept, the path above it is not (import it under any group you like). With
selected hosts, the file holds the groups that contain them (with their
names, notes and colours) and only those hosts.

## The secret audit

Besides passwords (which are never read), the exporter removes everything in a
host's own data that could be a secret, and counts each removal in the report
(names of fields and hosts only, never values):

| Where | What is removed |
|---|---|
| SSH options | any key containing `password`, `passwd`, `pwd`, `secret`, `token`, `passphrase` (any case) |
| `ProxyCommand`, `LocalCommand` (and any value mentioning `sshpass` or `--password`) | the value after `sshpass … -p`, `--password` / `--password=`, `SSHPASS=` is replaced by `<removed>`; the command stays and is listed in the report (ssh's own `-p` port flag is left alone) |
| `identityfile` | kept (a path is not a secret) and listed in the report: the key itself is not exported |
| links left by removed features | removed |
| `metadata.transfer` (FTP/FTPS/SFTP) | `pinned_sha256` (certificate pin) and any password-like key; `anonymous_password` is kept (the `anonymous@` convention, not a secret) |
| `metadata.rdp` | `pinned_sha256`, the gateway's local `credential_id`, any password-like key; `via_ssh` becomes `via_ssh_ref` |
| any other metadata | password-like keys, at any depth |

## The format (version 1)

One UTF-8 JSON document (no BOM, LF line ends, one-space indent, keys in a
stable order: two exports of the same inventory differ only in `exported_at`).
Media type `application/x-ophion+json` (files from SSH Manager, typed `application/x-ssh-manager+json`, still
open), extension `.sshm`. The format ids below kept their old names on purpose: renaming them would make
existing files unreadable (`docs/rename-audit.md`).

```json
{
 "format": "ssh-manager-export",
 "version": 1,
 "app_version": "1dce51f",
 "exported_at": "2026-10-30T12:00:00Z",
 "scope": "all",
 "counts": {"groups": 3, "hosts": 2,
            "by_protocol": {"ssh": 1, "rdp": 1, "ftp": 0, "ftps": 0, "sftp": 0}},
 "options": {"colors": true, "notes": true, "credential_refs": true, "smart_groups": false},
 "groups": [
  {"name": "Prod", "note": "production", "color": "#AA0000", "position": 0,
   "hosts": [
    {"ref": "h1", "alias": "web-01", "protocol": "ssh", "hostname": "web1.example.org",
     "port": 22, "user": "deploy",
     "options": {"proxyjump": "jump@bastion", "identityfile": "~/.ssh/id_ed25519"},
     "metadata": {"tags": ["web"], "notes": "main site", "environment": "production"},
     "color": "", "position": 0,
     "credential": {"ref": "c1", "mode": "shared"}}],
   "children": [
    {"name": "Windows", "note": "", "color": "", "position": 0, "children": [],
     "hosts": [
      {"ref": "h2", "alias": "dc-01", "protocol": "rdp", "hostname": "dc1.corp",
       "port": 3389, "user": "administrator",
       "metadata": {"rdp": {"v": 1, "domain": "CORP", "via_ssh_ref": "h1"}},
       "color": "#123456", "position": 0,
       "credential": {"mode": "host-specific"}}]}]}],
 "credentials": [{"ref": "c1", "name": "Ops account", "username": "deploy"}],
 "smart_groups": [],
 "integrity": {"sha256": "…"}
}
```

* **A tree:** a group holds hosts **and** subgroups (`children`), any depth;
  empty groups are exported. `position` keeps the sidebar order.
* **Names verbatim**, as stored.
* **`ref`** values are sequential in tree order (`h1…`, `c1…`); database ids
  never appear in the file.
* **Hosts:** every field except `alias` is optional. `protocol` is the
  registry id (`ssh`, `rdp`, `ftp`, `ftps`, `sftp`); other ids are kept as they
  are. `options` are the SSH options; `metadata` carries tags, environment,
  owner, service, notes, permissions, the default session profile, and the
  per-protocol settings `transfer` and `rdp` in full (after the secret audit).
* **Credentials:** a host using a shared credential points to an entry of
  `credentials` (name and username only: used to suggest a match when
  importing). `{"mode": "host-specific"}` marks a host-specific password.
* **Colours:** `#RRGGBB` or `#RRGGBBAA`, `""` for none. Exported from the tree's
  colours: groups by `"<top>"` / `"<top>/<child>/<…>"`, hosts by
  `"<top>::<child path>::<alias>"`.
* **Integrity:** `integrity.sha256` is the SHA-256 of the canonical JSON
  (sorted keys, separators `,` and `:`, UTF-8) of every top-level key except
  `exported_at` and `integrity`. A mismatch means the file was edited: the
  wizard warns and asks before importing (hand-edited files are fine). A
  missing `integrity` is accepted.
* **Versions:** `version` is an integer; Ophion imports versions 1 to its
  own and refuses a newer one ("created by a newer Ophion"). Unknown keys
  are ignored; unknown protocols and metadata keys are kept.

### With a passphrase

```json
{"format": "ssh-manager-export-encrypted", "version": 1,
 "kdf": {"name": "pbkdf2-sha256", "iterations": 600000, "salt": "<base64, 16 bytes>"},
 "cipher": "aes-256-gcm", "nonce": "<base64, 12 bytes>", "data": "<base64>"}
```

`data` is the plain document encrypted with AES-256-GCM; the key is
PBKDF2-HMAC-SHA256 of the passphrase with the stored salt and iteration count
(at least 600 000). The header (`format`, `version`, `kdf`, `cipher`, `nonce`)
is authenticated: changing it makes decryption fail.

## Importing

App menu → **Import → From Ophion Export (.sshm)…**, the empty sidebar's
**Import an Ophion export…**, or the action *Import an Ophion export
(.sshm)*. The file is recognised by its content, not its name. The wizard
follows the MobaXterm one (`docs/import-mobaxterm.md`):

1. **File** — the summary: who exported it and when, groups, hosts per
   protocol, colours, credential references, the integrity check. An encrypted
   file asks for the passphrase (a wrong one: an inline error, retry after a
   short pause).
2. **Destination and options** — at the top level or **under an existing
   group** (a searchable list); *Import colours* with the policy for groups and
   hosts that already have one (**only where there is none** — the default —,
   **replace**, **don't change**); *Import notes and tags*; SSH / RDP /
   FTP-FTPS-SFTP; *Skip empty groups*; *Import smart groups* (when the file has
   some).
3. **Groups to import** — the file's groups with check boxes and host counts;
   unticking a group leaves out its branch; a filter box.
4. **Credentials** — every user found in the selected hosts (the referenced
   credential's username, else the host's user; case-only variants marked ⚠;
   `(no user)`; the protocols as tags; where each came from in the tooltip).
   For each: an existing credential, **None (keep the user as written)** or
   **Create new credential…** (the password is typed there — it is never in
   the file). A **default credential** fills the rows not set by hand. A
   credential with the same name and username as the file's reference — or,
   failing that, the only credential with that username — is chosen and marked
   **matched**. *Next* waits until every row is decided; with no credentials at
   all: *Create New Credential…* or *Import without Credentials*. Hosts with a
   host-specific password ask for it on first connect.
5. **Hosts that already exist** — the same host is the same group, protocol,
   host name, port and user (host name and user in any case); a different user
   is a different host. **Skip** (default), **Import a copy “… (2)”** or
   **Update the existing host** (asks with the count; a database backup first;
   the alias and id stay; options, user, metadata, colour and credential
   binding are updated). Existing groups with the same path are merged, so
   importing the same file again with *Skip* changes nothing.
6. **Review** — the summary and the copyable report; **Import**.
7. **Result** — written in one database transaction; *Cancel* / *Esc* before
   the end undo it; a failure leaves the inventory exactly as it was ("Import
   failed, no changes were made"). The report is saved next to the MobaXterm
   ones (`<data dir>/imports/sshm-import-*.log`, mode 0600).

What the import does:

* A host mapped to credential *C* gets `User = C.username` and is bound to *C*;
  with None the user stays as written; jump-host users stay as written.
* A host-specific password becomes a host-specific credential **without** a
  password (asked on first connect).
* `via_ssh_ref` becomes the new id of that host; when that host is not
  imported, the tunnel is left out (warning).
* Colours are saved to the UI state **after** the database commit, under the
  final names (renamed or numbered groups included, under the destination);
  if that save fails, the import stays and a warning says so.
* Key file paths are kept as they are; the report lists hosts whose key file
  does not exist on this computer.
* Imported hosts get no "imported from" marker: export → import → export gives
  the same file.

## Limits and checks

* File ≤ 50 MB, nesting ≤ 32 levels, ≤ 200 000 hosts, text fields ≤ 4 096
  characters (notes ≤ 64 KB): what is over is left out and reported.
* Names follow the MobaXterm import's rules (`/` becomes `∕`, at most 128
  characters, empty names numbered, duplicates numbered "(2)"), all reported.
* Host names, users and jump hosts starting with `-` are refused; ports
  1–65535 (else the protocol's default); colours `#RRGGBB(AA)`; control and
  bidi-override characters are removed.
* JSON errors give the line and column; a truncated or non-UTF-8 file gets a
  plain-language message.
* Nothing in the file is executed, resolved or connected to.

Speed on the development machine: 1 200 hosts — read 0.02 s, plan 0.02 s,
commit 0.06 s, export 0.02 s; 10 000 hosts — read 0.2 s, plan 0.16 s, commit
0.5 s, export 0.12 s.
