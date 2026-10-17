# SSH Manager

SSH Manager is a local desktop and terminal UI for browsing grouped OpenSSH
config files, launching SSH sessions, managing credentials, and editing host
metadata.

## Launching

GTK desktop app:

```bash
python3 main.py
python3 ssh_manager.py
python3 -m ssh_manager
```

Textual terminal app:

```bash
python3 -m ssh_manager.main
```

Diagnostic config listing:

```bash
python3 farid.py --base-dir ~/.ssh/conf.d
```

All entry points accept `--base-dir`. The GTK entry points default to
`~/.ssh/conf.d`; the Textual app also accepts `--terminal`.

## Config Layout

The grouped inventory mode expects a directory such as:

```text
~/.ssh/conf.d/
  prod/
    web.conf
    db/main.conf
  staging/
    hosts.conf
```

Files directly inside the base directory are also loaded as root-level child
groups. Pointing `--base-dir` at a single OpenSSH config file is supported, and
that direct file mode follows `Include` directives.

## Broadcast Input

Open multiple sessions, then use `Show All Sessions` to tile every active
terminal together. Enable `Broadcast Input` to mirror typed input from the
focused session to all included connected sessions. In the tiled view, uncheck
`Broadcast` on any session that should be excluded from command fan-out.

## Expired Passwords

When a remote host forces an interactive password change, the GTK embedded
terminal watches for standard `current password`, `new password`, and `retype`
prompts. After the user enters and confirms the new password, the app updates
the saved secret for that host. If the host is bound to a reusable credential or
an unlocked vault entry, that credential source is updated as well.

## Password Storage

Saved passwords go to the system keyring (via the `keyring` package) when one is
available. Otherwise they are stored in `~/.config/ssh-manager/passwords.json`
(or under `$XDG_CONFIG_HOME`), encrypted with a key derived from the master
password. An existing plain-text file is encrypted automatically on the next
unlock, and the file is re-encrypted when the master password changes. If the
master password is disabled, the file is kept as private (0600) plain text.

## Paramiko Mode

With "Paramiko + VTE" enabled in Settings, connections honor the host's
`IdentityFile`, `ProxyCommand`, and `ProxyJump` options. Jump hops are reached
through the system `ssh` client in batch mode, so they need key or agent
authentication.

## Verification

This repository currently uses the standard library unittest runner:

```bash
python3 -m unittest discover
python3 -m compileall ssh_manager tests farid.py main.py ssh_manager.py
```

The GTK and VTE bindings come from the distribution, so a virtualenv needs
access to system packages:

```bash
python3 -m venv --system-site-packages .venv
.venv/bin/python -m unittest discover
```

The app depends on Python plus GTK 3 / VTE GI bindings for the desktop UI.
Runtime modules used by optional features include Paramiko, Textual, pexpect,
cryptography, and keyring (for the system keyring; without it, passwords use
the encrypted file store).
