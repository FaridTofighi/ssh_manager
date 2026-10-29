# Passwords

How the terminal finds the password for a connection, when it asks for one,
and how it knows a login worked. Code: `ssh_manager/auth_flow.py` (no GTK),
`CredentialController.resolve_password` and `TerminalTab` in
`ssh_manager/gtk_terminal.py`.

## Where the password comes from

Each connection resolves its password once per attempt, **read-only**, in this
order (`PasswordResolution.source`):

1. **credential** or **host:** the password of the credential bound to the
   host. It is `host` when that credential is the host's own (see below);
2. **legacy:** the per-host secret stored by older versions under
   `(host name, port, user)`;
3. **none:** nothing is stored.

<!-- legacy-migration-note -->
**Moved from the Credential Vault.** The vault (its own password and file,
`vault.json`) was removed. At the first start after the update its entries
are moved into Credentials: each becomes a shared credential (an existing one
with the same username and password is reused), the hosts that used it are
bound to it, everything is verified, and only then does SSH Manager offer to
delete the old file. *Later* keeps everything and asks again at the next
start; meanwhile the Credentials manager has **Import old vault…**. Code:
`ssh_manager/legacy_vault.py` (migration only).
<!-- /legacy-migration-note -->

Resolving never writes. Older versions copied the credential's password into
the legacy per-host secret on every connect; that is gone, so a shared
credential stays the single source of its password.

A locked or broken secret store (`SecureStoreError`) is treated as "no
password". It shows "Credential '…' is unavailable." and logs the error type
only; the terminal never blocks on it.

## Passwords: credential vs host-specific

In the host dialog, **Credentials → Password** chooses where the host's
password comes from:

- **Use the credential's password** (default): the password of the
  credential chosen above. Every host bound to that credential shares it. The
  option is unavailable until a credential is chosen.
- **Use a password specific to this host:** the host gets its own password.
  The shared credential is not changed.
  - **Status:** "Saved" or "Not set — you will be asked when you connect".
  - **Change… / Clear:** replace or remove the saved password. An empty entry
    keeps the saved one.
  - **Save and Cancel:** nothing is stored before **Save**; **Cancel**
    discards every change.

A host-specific password is a **private credential**: an ordinary
`Credential` with `private: true`, bound 1:1 to the host through the usual
bindings (`credentials.json`, keyed by the host id; by
`parent::child::alias` in folder inventories). Its secret is stored like any
credential's (`credential:<id>` in the keyring or encrypted file).

- **Fields:**
  - `based_on` remembers the shared credential it replaced.
  - Its username is the shared credential's (or the host's User when none),
    updated on every Save.
- **Hidden:** `CredentialStore.list()` leaves it out. The credential manager,
  the host dialog's credential list and the MobaXterm import never show it.
  `list(include_private=True)` and `get()` still return it.
- **API:**
  - `create_private(host_key, username, password)` binds the host in one
    save. When the host already has one, it is updated instead.
  - `update_private`, `private_for_host`, `is_private`, `shared_for_copy`.
  - **Phase 3** saves a password for a host with
    `CredentialController.apply_host_credentials(host, shared_id,
    PasswordChoice(host_specific=True, password=…))`.
- **Lifecycle (no orphans):** whenever a change of bindings leaves a private
  credential bound to no host, it and its secret are deleted in the same
  save. That covers:
  - host deleted;
  - re-bound to another credential, or unbound;
  - "Use the credential's password" chosen again (after a confirmation);
  - another credential chosen while in host-specific mode (after a
    confirmation);
  - group deleted;
  - bindings of deleted hosts pruned;
  - another binding renamed onto the host.

  Renames and moves keep it: ids are stable, and folder keys are re-keyed.
- **Copies:** duplicating a host or a group binds the copy to the **shared**
  credential the original's password replaced (none if it was deleted). The
  notification says the host-specific password was not copied.
- **Display:** host details show "Password: credential ‘X’" or
  "Password: host-specific", and Auth reads "Host-specific password". The tree
  tooltip shows the same line. The value is never shown, logged, exported or
  written to host options or metadata.
- **Locked store:** with a locked password file or a failing keyring, Save
  keeps the dialog open with the reason and changes nothing. If saving still
  fails afterwards, "Could not save the password for ‘…’. Nothing was
  changed." is shown and every store is rolled back.
- **Every connection uses it:** terminal tabs, Reconnect, Duplicate Session,
  remote stats and the Paramiko connector all resolve through
  `resolve_password`.
  - **Split panes:** they open a plain `ssh` and ask for the password in the
    pane, as before.
  - **Not affected:** there is no SFTP browser, and broadcast only sends
    keystrokes.

## Moving credentials to another computer

**Credentials → Export…** writes the shared credentials with their passwords
to a `.sshcred` file, always encrypted with an export passphrase you type
twice (scrypt n=32768, r=8, p=1 and AES-256-GCM, the header authenticated; it
is not the master password and is never stored). Host-specific passwords stay
on this computer. **Import…** asks for the passphrase, lists the credentials
with a check box each and, when a name is already used, skips it, numbers it
"(2)" or keeps the existing one if its username and password are the same.
A wrong passphrase, a changed file or a newer format writes nothing. Code:
`ssh_manager/credentials_export.py`, `ssh_manager/aead.py`.

## What triggers the password dialog

- The **first attempt** has no stored password (and `sshpass` is installed).
- An attempt **failed authentication** (`AuthOutcome.AUTH_FAILED`: `sshpass`
  exit 5, "Permission denied, please try again", "Permission denied
  (…password…)", "Authentication failed").

Nothing else asks for a password. `classify_exit` keeps these apart:

| Outcome | Examples | Prompt? |
|---|---|---|
| `AUTH_FAILED` | sshpass exit 5; "Permission denied, please try again" | yes |
| `HOSTKEY` | sshpass exit 6; "Host key verification failed" | no |
| `NETWORK` | connection refused / timed out, unknown host name | no |
| `NO_PASSWORD_METHOD` | "Permission denied (publickey)" | no, says the server does not accept password login |
| `OTP_REQUIRED` | "Verification code:" | no |
| `OTHER` | "Too many authentication failures", other exits | no |

- **Limits:** at most **3** password prompts per connect, at least 1 s
  apart. Connect and Reconnect start a fresh count. Closing the tab removes
  every pending timer.
- **Nothing is deleted:** a rejected password never deletes or changes a stored
  secret.
- **Nothing is saved silently:** a typed password that worked is kept **in
  memory for that tab** and reused by its Reconnect. It is written only when
  you answer **Save for this host** (below).

## Recovery after "Permission denied"

The flow is a per-tab state machine (`PasswordFlow` in
`ssh_manager/auth_flow.py`). The GTK side only shows its texts.

1. **First attempt:** the resolved password (credential, host-specific or
   legacy). With none stored, the password dialog opens first.
2. **On `AUTH_FAILED`,** after ≥ 1 s, a non-blocking dialog asks again.
   - **Title:** "Password for ‘web’".
   - **Text:** "‘web’ rejected the saved password (credential ‘Ops’). Enter
     the password for `alice@10.0.0.10:22`." The phrase names the source: a
     credential, a host-specific password, a saved password, or the password
     you entered.
   - **Buttons:** **Retry** / **Cancel**, and "Attempt N of 3".
   - **Cancel, Escape or an empty entry:** stops the flow. The red banner
     with **Reconnect** stays.
3. **Retry:** the typed password goes to the next `sshpass` spawn (through
   `SSHPASS`, never argv) and is held in memory only.
4. **After a verified login** (LocalCommand sentinel, or the heuristic) with
   a typed password, the tab asks **once**: "Save this password for ‘web’?"
   - **Text:** "Login succeeded with the password you entered. Saving it
     makes this host use its own password from now on. The credential ‘Ops’
     and other hosts are not changed." With the heuristic it says "Login
     appears to have succeeded…".
   - **Save for this host** (default): stores the password as the host's
     private credential (`CredentialController.save_host_password`).
     - **Host looked up again:** by id, or by path in folder inventories, in
       case it was renamed, moved or deleted while the dialog was open.
     - **Phase 2 API:** it creates the private credential or updates it. The
       shared credential is untouched.
     - **Result:** the tree and details refresh to "Password:
       host-specific", and "Password saved for ‘web’." is shown.
   - **Don't save** (also Escape or closing the window): the password stays
     in this tab only. This tab does not ask again; a later connect starts
     from the normal resolution order and may ask again.

**When the save dialog does not appear**

- the password came from a store and worked;
- the typed password equals the stored one (nothing is said);
- it was already asked in this tab;
- `sshpass` is missing (success cannot be verified; the existing warning
  stays);
- auxiliary sessions (split panes, Paramiko stats);
- host-key, network, publickey-only, OTP and "too many authentication
  failures" outcomes (no password dialog at all).

**Edge cases**

- **Hosts that cannot be edited:** `can_edit=false`, a read-only inventory,
  or a secrets policy that forbids unencrypted passwords. They show **Use for
  this session** instead of **Save for this host**; nothing is written.
- **Save errors** (locked store, master password, keyring error):
  - the dialog stays open with the reason and **Retry Save**;
  - the session stays connected;
  - only the error type is logged.
- **Host deleted while the dialog was open:** "‘web’ no longer exists; the
  password was not saved."
- **Tab closed or window destroyed with a dialog open:** the dialog is
  dismissed, nothing is done, and every GLib source is removed.
- **Reconnect with a retry dialog open:** the dialog is closed.
- **Several tabs failing at once:** each shows its own dialog titled with
  its alias. The state is per tab, with no shared or global state (a queue
  is out of scope).
- **Password changed inside a session** (`passwd`): for a host with its own
  password, only that password is updated. Other hosts keep the previous
  behaviour.

**Paramiko backend**

- **Same resolution and dialogs:** the same resolution, the same retry text
  and attempt count, and at most 3 prompts.
- **Nothing saved by the connector:** after a login with a typed password,
  `Connector.add_password_verified_callback` hands it to the tab, which asks
  the same question.
- **Removed:** the "Remember password for this host" checkbox, the
  "Remember passwords by default" preference and
  `UIHostConfig.remember_password_default`. The `remember_password_default`
  setting is still read and written for old settings files, but nothing uses
  it.

## Why the app uses LocalCommand

A started `ssh` process proves nothing: it is still alive while the server
asks for the password again. To know that authentication **succeeded**, each
spawn adds

```
-o PermitLocalCommand=yes -o LocalCommand=touch -- '<marker>'
```

OpenSSH runs `LocalCommand` on this machine right after a successful login,
so the marker file appearing means "authenticated".

- **The marker:** `$XDG_RUNTIME_DIR/ssh-manager/<uuid>.ok`, in a 0700
  directory. It is deleted when the attempt ends. The path is shell-quoted,
  with `%` doubled, because `LocalCommand` expands `%` tokens.
- **A host's own `LocalCommand`** still runs, after the marker
  (`touch …; <your command>`).
- **Fallback:** a host with `PermitLocalCommand no` gets no sentinel. The app
  then uses a heuristic instead: alive for 3 s, new output after the
  password, no failure text and no trailing `password:` prompt.
- **The log** says which method confirmed the login ("verified" or
  "probable").

The password itself goes to `sshpass` via the `SSHPASS` environment variable.
It is never on the command line, in logs, notifications or tooltips.
