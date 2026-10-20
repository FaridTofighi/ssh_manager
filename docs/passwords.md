# Passwords

How the terminal finds the password for a connection, when it asks for one,
and how it knows a login worked. Code: `ssh_manager/auth_flow.py` (no GTK),
`CredentialController.resolve_password` and `TerminalTab` in
`ssh_manager/gtk_terminal.py`.

## Where the password comes from

Each connection resolves its password once per attempt, **read-only**, in this
order (`PasswordResolution.source`):

1. **vault:** the host's bound vault entry, when the vault is unlocked;
2. **credential:** the password of the credential bound to the host;
3. **legacy:** the per-host secret stored by older versions under
   `(host name, port, user)`;
4. **none:** nothing is stored.

Resolving never writes. Older versions copied the credential's password into
the legacy per-host secret on every connect; that is gone, so a shared
credential stays the single source of its password.

A locked or broken secret store (`SecureStoreError`) is treated as "no
password". It shows "Credential '…' is unavailable." and logs the error type
only; the terminal never blocks on it.

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
