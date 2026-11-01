# The inventory store

Ophion keeps its groups and hosts in **one SQLite database that it
owns**. OpenSSH config files are import sources and export targets only. They
are never the live store, and `~/.ssh` is never written implicitly.

| | |
|---|---|
| Location | `$XDG_DATA_HOME/ophion/inventory.db` (default `~/.local/share/ophion/inventory.db`); `--inventory <file>` uses another one |
| Permissions | database 0600, folder 0700 |
| Backups | `backups/inventory-<stamp>-<reason>.db` (newest 5) and `inventory.db.bak`; made on schema migration, by **Back Up Now** (Preferences → Inventory), before deleting a group that contains hosts, and before an Overwrite import |
| Code | `ssh_manager/inventory/store.py` (`InventoryStore`), `manager.py` (`StoreConfigManager`), `openssh.py` (import/export), `startup.py` (opening with decisions) |

## Why

Groups used to be files and folders under `~/.ssh/conf.d`, which caused
several problems:

- names were limited to `[A-Za-z0-9._-]`;
- a folder could not hold both hosts and subfolders (the `_hosts`
  workaround);
- empty groups were not real;
- renames were file operations that could half-fail;
- every change rewrote a file and reloaded everything;
- credential bindings were keyed by `parent::child::alias`, so every rename
  had to re-key them.

It also allowed the bug where the app adopted `~/.ssh/config` (a file) as its
base, after which every create failed with "Base path … is a file". At
connect time the app never read those files anyway: `build_ssh_command()`
builds the `ssh` command from the host's options.

## Model

```
groups(id, parent_id → groups ON DELETE CASCADE, name, name_key, position, note, created_at, updated_at)
       UNIQUE(name_key) for top-level groups, UNIQUE(parent_id, name_key) below them
hosts (id, group_id → groups ON DELETE CASCADE, alias, alias_key, hostname, port, user,
       options_json, metadata_json, position, created_at, updated_at, UNIQUE(group_id, alias_key),
       protocol DEFAULT 'ssh'  -- schema v2, indexed)
meta  (key, value)            schema version in PRAGMA user_version (2)
```

- **Protocols (schema v2):** `hosts.protocol` is `ssh`, `ftp`, `ftps` or
  `sftp` (see `docs/protocols.md`).
  - **Migration:** opening a v1 database adds the column (existing hosts
    become `ssh`) after a `schema-v1` backup.
  - **Unknown ids:** ids written by a newer version are kept.
  - **Settings:** non-SSH settings live in `metadata["transfer"]`, never in
    the options (those become `ssh -o` arguments).

- **Groups:**
  - Groups form a tree of any depth, and a group can hold hosts and
    subgroups. Empty groups are real.
  - Names are any readable text: trimmed, 1–128 characters, no `/`, no
    control, line-break or bidi-override characters (Persian ZWNJ is fine).
- **Uniqueness:** names are unique per parent, and aliases per group, without
  regard to case. The comparison uses a stored `casefold()` key, which also
  works for non-ASCII letters (SQLite's `NOCASE` only folds ASCII).
- **Hosts:**
  - A host alias is a free display name.
  - Without a host name, the alias is the `ssh` target, so it must then be a
    valid host name or IP address.
  - Host names, users and jump hosts may not start with `-`, so they can
    never be read as `ssh` options.
  - Other SSH options and the metadata (`tags`, `permissions`, …) are kept as
    JSON, with the same meaning as before.
- **Transactions:** writers use `BEGIN IMMEDIATE`; bulk operations are one
  transaction; foreign keys are on; the journal is WAL. `PRAGMA data_version`
  shows when another window or instance changed the database, and the tree
  reloads (checked every 2 s).

## The UI facade

`StoreConfigManager` keeps the `ConfigManager` API that the GTK layer has
always used:

- the parent and child group methods (`create`, `rename`, `duplicate`,
  `move`, `delete`), plus `add_host`, `update_host`, `delete_host`,
  `move_hosts` and `refresh`;
- results are `(ok, message)` and never exceptions;
- one refresh per change.

`manager.tree` keeps its shape, `{top-level group: {child path: GroupConfig}}`:

- **Child paths:** the child path is the `/`-joined names below the top-level
  group. **Every group is an entry**, including containers, empty groups, and
  groups that hold hosts and subgroups.
- **Top-level hosts:** hosts directly in a top-level group use the child path
  `""`.
- **Ids:** `GroupConfig.id` and `HostEntry.id` are stable.
  `GroupConfig.path` and `HostEntry.source_file` are legacy, and empty for
  the store.
- **Bulk helpers:** `transaction()`, `create_group_path(parent, "a/b/c")` and
  `add_hosts_bulk(...)`. `CredentialStore.bind_many()` saves once.

The tree shows subgroups above hosts. Counts include subgroups, and store
groups use folder icons (`.conf` file icons are only for folder inventories).
Name prompts follow the backend (`gtkui.validation.validators_for`); for the
store they report a duplicate sibling inline, as you type.

## Credential bindings

Bindings are kept in `credentials.json`, keyed by the **host id** for store
hosts:

- **Renames and moves:** these need no re-keying.
- **Duplicates:** a duplicated group's hosts get new ids, and their bindings
  are copied to them.
- **Deletes:** when hosts are deleted, their bindings are pruned after the
  tree rebuilds. Credentials are never deleted.
- **Folder inventories:** the old `parent::child::alias` keys keep working for
  folder inventories, and the store leaves them alone.

## Importing and exporting

- **Import → From OpenSSH Config…:**
  - **Sources:** tick `~/.ssh/config`, `~/.ssh/conf.d`, or add other files and
    folders.
  - **Preview:** shows the number of hosts and groups, plus notes.
  - **Policy:** choose Skip, Import with a suffix, or Overwrite.
  - **Folder mapping:** in a folder, each folder and each file becomes a
    group, with exactly the same names that older versions used.
  - **Skipped:** `Host *` and other patterns are skipped with a note, and
    `Match` blocks with a warning.
  - **Parsing details:** duplicate `Host` blocks merge (the first value wins,
    as in OpenSSH), and quotes are stripped. `Include` is followed, with loops
    cut. Tabs, `=`, CRLF and a UTF-8 BOM are handled. Files over 20 MB are
    refused.
  - **Re-imports:** importing the same hosts again adds nothing (same group,
    host name, port and user).
  - **Untouched sources:** the source files are only read.
- **Export → Ophion Export (.sshm)…** and **Import → From Ophion
  Export (.sshm)…:** Ophion's own lossless format (groups at any depth,
  every protocol and its settings, colours, notes, credential references, no
  passwords; optionally encrypted). The import commits in one transaction,
  like the MobaXterm import. See `docs/export-import.md`.
- **Export → To OpenSSH Config…:**
  - Writes a generated file, `~/.config/ophion/ssh_config` by default,
    with mode 0600 and a "Generated by Ophion" header.
  - Aliases are made unique and safe, and app-only fields (tags, notes,
    permissions, …) are left out.
  - It never writes over `~/.ssh/config`.
  - Afterwards it offers to add one line to `~/.ssh/config`:
    `Include ~/.config/ophion/ssh_config  # added by Ophion`.
    Before you confirm, the dialog shows the diff. The line goes **at the
    top**, because OpenSSH applies an `Include` placed after a `Host` line to
    that host only. A timestamped backup (`config.sshm-backup-<stamp>`) is
    made first, and adding the line again does nothing.
- **Open config folder / Import from OpenSSH config:** for the store, these
  open the import dialog. A folder is an import source, not a workspace.

## First run

When the database does not exist yet, Ophion starts empty and editable.
If `~/.ssh/conf.d` or `~/.ssh/config` contains hosts, it asks once:
"Found existing SSH hosts in … Import them?"

- **Import:** imports with the old names, and translates every old
  `parent::child::alias` credential binding to the new host id. Bindings
  without a matching host are reported, never silently dropped.
- **No / close:** changes nothing, and that source is never offered again
  (stored in `meta`). Import is still available from the menu.

The old `conf.d` files are never changed.

## Command line

| Option | Meaning |
|---|---|
| *(none)* | the store at the default location |
| `--inventory <file>` | another database (tests, portable use) |
| `--base-dir <folder>` | the legacy folder inventory, as older versions used it, **editable in place** |

**Decision:** a folder passed with `--base-dir` stays editable. It is an
explicit choice, it is the layout the app itself created, and the existing
folder-based tests and workflows keep working. A single *file* there is
read-only: changing anything offers to switch to `~/.ssh/conf.d`. The default
inventory is always the store.

## When something goes wrong

At start-up, every problem asks what to do (`inventory/startup.py`):

| Problem | Choices |
|---|---|
| Another instance is writing (locked) | Retry · Open read-only · Quit |
| The database is damaged | Restore latest backup · Start empty (the damaged file is kept next to it as `inventory.db.damaged-<stamp>`) · Quit |
| Written by a newer Ophion | Open empty and read-only · Quit — the file is not touched (the schema version is read before anything writes) |
| Folder missing, read-only, or the disk is full | Open read-only (the existing file, else empty) · Quit |

- **Read-only inventory:** changing anything explains why instead of failing.
- **Locked while running:** a write while another instance holds the lock
  comes back as "in use by another Ophion window. Try again."
- **Other problems:** a full disk and similar problems are plain messages.

## Performance

These are tested in `tests/test_inventory_store.py` and
`tests/test_inventory_manager.py` with synthetic data:

- inserting 2,000 hosts in one transaction takes about 65 ms (target < 1 s);
- loading the tree for 5,000 hosts takes well under the 300 ms target;
- there are no per-host refreshes.
