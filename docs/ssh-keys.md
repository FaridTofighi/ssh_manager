# SSH keys

Creating, protecting and using SSH key pairs, installing them on servers, and
the ssh-agent. Code: `ophion/ssh_keys.py` (no GTK),
`gtkui/controllers/ssh_keys_controller.py`, `gtkui/dialogs/ssh_keys.py`,
`gtkui/dialogs/ssh_keys_deploy.py`, `gtkui/dialogs/ssh_agent_section.py`.

## Opening

App menu → **SSH Keys…**, the **SSH Keys…** button in Credentials, or the
**SSH Keys…** button beside *Private key* in the host dialog. The window
action is `ssh-keys.open` (no shortcut by default; set one in Settings →
Shortcuts).

## The list

Every private key in `~/.ssh`, plus keys added by path from elsewhere (their
paths are listed in `ssh_keys.json` beside the settings; nothing secret is
stored there). For each key: type and size, SHA256 fingerprint, comment,
whether it has a passphrase (and whether it is saved), whether a `.pub` file
exists, and how many hosts use it (`IdentityFile`).

Listing never asks for a passphrase. The public half is read from the `.pub`
file or from the unencrypted part of the private key file. For an old-style
encrypted PEM key it shows as "unknown (encrypted)" until **Derive Public Key…**
is given the passphrase (it also writes the missing `.pub`).

A host whose `IdentityFile` points to a file that no longer exists shows in the
list with a warning icon. The host dialog warns too ("This file does not
exist.").

## Generating

**Generate…** asks for the type (Ed25519, recommended; RSA 3072 / 4096), a file
name (never an existing one: `id_ed25519_2` … is suggested), a comment and a
passphrase. Without a passphrase it warns that anyone who gets the file can
use the key. The key is made in the background; Cancel drops it.

- The private key is written in OpenSSH format. With a passphrase it is
  encrypted with bcrypt (the same format as `ssh-keygen`). If the `bcrypt`
  module is missing, RSA keys fall back to the traditional encrypted PEM
  format, and an Ed25519 key with a passphrase is refused with that reason.
- Files are created with `os.open(…, O_EXCL, 0600)`: never over an existing
  file, and never readable by others even for a moment. The `.pub` file is
  0644 and `~/.ssh` is 0700.
- The new key is loaded back before success is reported.

## Passphrases

**Remember the passphrase in Ophion (encrypted)** saves it in the app's password store, the same
place as host passwords, so the master password protects it (see
`docs/security.md`). It is filed under the key's real path and never written
to settings, the inventory, logs or messages. **Change Passphrase…** rewrites
the key atomically (temporary file, then rename); an empty new passphrase
removes it.

Where a saved passphrase is used:

- **SSH tabs** (system ssh): when ssh prints `Enter passphrase for key '…':`
  for the host's key, the saved passphrase is typed once at that prompt. If
  it is wrong, ssh asks again and you type it yourself. It is never passed to
  ssh in arguments or the environment.
- **Paramiko connections**: the file browser, tunnels, Run Command on Hosts,
  RDP over SSH and the Paramiko terminal backend.

## Other actions

**Import…** copies a key into `~/.ssh` (0600, with its `.pub`) or uses it
where it is. **Copy Public Key** and **Show Public Key** give the
`authorized_keys` line. **Rename…** keeps the saved passphrase, and **Delete**
asks first and names the hosts that still use the key. **Show in Folder**
opens the key's folder.

## Installing a key on hosts

**Deploy to Hosts…** in the keys window, or **Install SSH Key…** in a host's
or group's menu. This is what `ssh-copy-id` does:

- **Login:** each host's saved credentials (password, key or agent), with
  the app's usual host-key checking. A changed or unknown host key is
  reported, never accepted.
- **Install:** creates `~/.ssh` (0700) and `authorized_keys` (0600) if
  needed, and appends the key only if it is not there yet. The key is
  compared, not the comment or options, and nothing else in the file
  changes. The check and the append run under `flock`, so two inventory
  entries for one server add it once.
- **Verify:** a second connection with only that key (no password, no agent)
  checks that it works.
- **Results:** a table with Installed / Already present / Auth failed /
  Host key changed / Unreachable … per host. Copy Report and Retry Failed are
  there.
- **Set IdentityFile** (off by default): points the successful hosts at the
  key in the inventory, with Undo.

Password login on the servers is left as it is; Ophion never offers to
turn it off.

## The ssh-agent

Settings → Security → **SSH agent** shows whether an agent answers at
`SSH_AUTH_SOCK` and how many keys it holds.

**Add Key to Agent…** runs `ssh-add <file>` on a private pseudo-terminal with
`SSH_ASKPASS` removed (`SSH_ASKPASS_REQUIRE=never`). The saved passphrase, or
one you type, is written at ssh-add's prompt. If ssh-add asks again (a wrong
passphrase), it is stopped. The passphrase is never in its arguments or
environment, and is masked out of any message.

**Forward the SSH agent (ForwardAgent)** is a checkbox on the host dialog's
Connection page. It is off by default, applies to that host only, and is never
set globally. Ticking it shows the warning: anyone with root on that server
can use your keys while you are connected. When ticked, the host gets
`ForwardAgent yes`; when unticked, the option is removed.

## Limits

- Hardware (FIDO `-sk`) keys are listed when found, but can only be generated
  with `ssh-keygen`. OpenSSH certificates (`-cert.pub`) are not handled.
- ECDSA keys can be imported and used but not generated here.
- The agent row uses the agent of the environment Ophion was started
  from. Starting an agent is up to the desktop session.
