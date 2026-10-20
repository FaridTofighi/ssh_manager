# Passwords

How the terminal finds the password for a connection, when it asks for one,
and how it knows a login worked. Code: `ssh_manager/auth_flow.py` (no GTK),
`CredentialController.resolve_password` and `TerminalTab` in
`ssh_manager/gtk_terminal.py`.

## Where the password comes from

Each connection resolves its password once per attempt, **read-only**, in this
order (`PasswordResolution.source`):

1. **vault:** the host's bound vault entry, when the vault is unlocked;
2. **credential** or **host:** the password of the credential bound to the
   host. It is `host` when that credential is the host's own (see below);
3. **legacy:** the per-host secret stored by older versions under
   `(host name, port, user)`;
4. **none:** nothing is stored.

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

- **Limits:** at most **3** attempts per connect, at least 1 s apart. Connect
  and Reconnect start a fresh count. Closing the tab removes every pending
  timer.
- **Nothing is saved:** a rejected password never deletes or changes a stored
  secret. A typed password that worked is kept **in memory for that tab** and
  reused by its Reconnect. It is never written to disk.

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
