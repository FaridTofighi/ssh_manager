# Security

How SSH Manager protects saved passwords and macros, what it does not protect
against, and how data of older versions is moved. Code:
`ssh_manager/keybox.py`, `ssh_manager/aead.py`, `ssh_manager/secure_store.py`
(`EncryptedFileKeyring`), `ssh_manager/macros.py`, `ssh_manager/unlock.py`,
`ssh_manager/migration.py`; the dialogs are in `gtkui/unlock_flow.py`,
`gtkui/migration_flow.py` and `gtkui/dialogs/security_page.py`.

## One key, protected by the master password

A random 32-byte **data key** encrypts everything secret. It is never stored
in clear: `keybox.json` holds it **wrapped** (encrypted) with a key derived
from the master password, and optionally a second time with a key derived from
the recovery key. Each store uses its own subkey,
`HKDF-SHA256(data key, salt = keybox id, info = "ssh-manager/<store>/v1")`,
for `secrets` (`passwords.json`), `macros` (`macros.enc`) and `export`.

- **Changing the master password** re-wraps the data key in one atomic
  rewrite of `keybox.json`; the stores are not touched. A failed write leaves
  the old password valid.
- **A wrong password** fails the AES-GCM tag of the wrapped key. That is the
  check: no password hash is stored.
- **In memory:** the master password is not kept after unlocking (an
  interrupted migration keeps it only until the move has run). The data key
  lives in a `bytearray` that is zeroed when the app closes (best effort:
  Python may hold copies).

### Key derivation (measured on the development machine)

| | |
|---|---|
| Algorithm | scrypt, `n = 2^17`, `r = 8`, `p = 1` (128 MiB) |
| Time | about 250 ms to create, 250 ms to unlock |
| Argon2id | used for new key files when the installed `cryptography` has it (time 3, 64 MiB, 4 lanes); it did not on the development machine (cryptography 43) |

Both algorithms are read everywhere. A key file made with Argon2id on a
system without it gives a clear "not supported here" message. Parameters below
the minimums (scrypt `n < 2^15`, PBKDF2 under 600 000 iterations, Argon2
under 32 MiB) are refused as "damaged or unsafe". The unlock runs in a worker
thread with a spinner. After five wrong passwords, each next try waits 1, 2,
4 … at most 30 seconds; there is no permanent lock-out.

### Files

| File (in `~/.config/ssh-manager/` unless noted) | Holds | Protection |
|---|---|---|
| `keybox.json` | the wrapped data key, the KDF parameters, `migrated` counts | AES-256-GCM, 0600 |
| `passwords.json` (version 3) | every saved password (`credential:<id>`, `host:port:user`) | AES-256-GCM with the `secrets` subkey, 0600 |
| `macros.enc` (version 2) | macros, their shortcuts and secret steps | AES-256-GCM with the `macros` subkey, 0600 |
| `credentials.json` | credential names, usernames and host bindings | not secret, not encrypted |
| `settings.json`, `ui_state.json`, `smart_groups.json` | preferences and window state | not secret, not encrypted |
| `~/.local/share/ssh-manager/inventory.db` | hosts, users, groups, notes | **not encrypted** |
| `~/.local/share/ssh-manager/tls-pins.json` | accepted FTPS certificate fingerprints | not secret, not encrypted |

Each encrypted file records the `keybox_id` in its authenticated header: a
file of another key is refused ("This file belongs to a different key") and
never overwritten. A damaged, truncated or changed file is kept as it is and
reported. All writes are atomic: a 0600 temporary file in the same 0700
folder, `fsync`, rename, `fsync` of the folder. The previous version is kept
as `<name>.bak`, encrypted like the file.

The OS keyring (GNOME Keyring, KWallet …) is **no longer a password store**.
It is used only in "Remember on this computer" mode (below), and older
versions' copies are read once by the migration.

## Threat model

**Protects against**

- someone who copies the data files, or reads them offline;
- backups or sync tools that copy the configuration folder;
- a lost or stolen disk when the account is not unlocked;
- edits to `settings.json`: no setting turns protection off. The old
  `master_password_hash` / `master_password_disabled` keys are ignored once
  `keybox.json` exists and are no longer written.

All of this assumes a strong master password: scrypt (or Argon2id) makes each
guess expensive, not impossible.

**Does not protect against**

- malware running as the same user: it can log keystrokes or read the
  app's memory while it is unlocked;
- anyone at an unlocked session;
- in **Remember on this computer** mode, anyone who can open your OS keyring
  (usually: anyone who can sign in to your account);
- copies the file system keeps elsewhere: removing old files overwrites them
  with zeros, then `fsync` and unlink, but SSDs, journaling and snapshots
  may still hold old blocks.

**Forgetting both** the master password and the recovery key means the
saved passwords and macros cannot be recovered. `inventory.db` (hosts,
groups, users) does not depend on the key and is unaffected.

Idle auto-lock is not implemented (a possible later addition).

## At start-up

`ssh_manager/unlock.py` looks at the folder (never at secrets) and decides:

| Situation | What happens |
|---|---|
| no key file and nothing to protect | **Set a master password**, or **Don't Save Passwords** (asks for passwords every time; Preferences → Security can turn saving on later). Cancel quits. |
| a key file | the unlock dialog, or the data key from the OS keyring in remember mode (any failure falls back to the dialog silently) |
| data of an older version | the migration (below) |
| an unfinished migration | unlock, then the migration resumes |
| encrypted stores but **no** `keybox.json` | "The key file keybox.json is missing": quit without changes (the default), or start without them; the old files are kept as `*.orphaned-<time>`. A new key is never created silently. |

### Unlock modes (Preferences → Security → When SSH Manager Starts)

- **Ask for the master password** (default).
- **Remember on this computer (no prompt):** after confirming the master
  password, the data key goes to the OS keyring (service `ssh-manager`, key
  `keybox:<keybox id>`). Anyone who can sign in to this computer account can
  open SSH Manager; your saved passwords stay encrypted on disk. Choosing
  *Ask* again deletes the entry. Without a usable keyring the option is
  disabled and says why. The mode is only "is that keyring entry there":
  there is no flag in any JSON file.

### Recovery key

Offered when the master password is set, and in Preferences → Security. It
is 160 random bits shown **once** as 8 groups of 4 Base32 characters; you
type the last two groups to confirm, or skip (then none is kept). It wraps the
same data key with its own unlocker. **Forgot master password?** on the
unlock dialog takes the recovery key and sets a new master password; the
recovery key stays valid until you create a new one, which invalidates it.
Neither the recovery key nor the master password is stored.

### Don't save passwords

With saving off nothing is written: every connection asks for its password,
and macros cannot be saved (they need the key). Turning it on in Preferences
→ Security sets a master password. Turning it off deletes `keybox.json` and
`passwords.json` after a confirmation that names how many passwords will be
deleted, and is refused while macros exist.

## Migration from older versions

Older versions kept passwords in the OS keyring, or in `passwords.json`
(version 1: plain JSON; version 2: encrypted with a key derived from the
master password), macros in `macros.enc` version 1, and a master-password
hash in `settings.json`. At the first start one dialog explains the move,
then:

1. **A master password for the key file.**
   - It was set → type it (checked against the old hash); it stays your
     master password.
   - It was set with "don't ask on startup" → choose one again. With a
     usable OS keyring it is remembered on this computer, so start-up stays
     prompt-free. Without one it must be typed at every start (a key file
     next to the data would protect nothing).
   - None was set → create one. **Don't Save Passwords** is offered only
     when nothing secret was saved; otherwise the choice is *Not Now*.
2. **The key file** is created (and the recovery key offered). Just before
   it, the marker `migration.pre-keybox` is written, so a crash from here on
   is detected and resumed.
3. **Collect** the old secrets, into memory only: `passwords.json` v1/v2 read
   without writing; the OS keyring by every key the app could have used
   (`credential:<id>` of each credential, `host:port:user` of each inventory
   host with each credential's username); whatever an interrupted run already
   wrote (the newest value wins). Missing keyring entries are counted, not
   errors.
4. **Write and verify** `passwords.json.keybox-new` (version 3), comparing
   every value. Copy the old file to `passwords.json.pre-keybox` and verify
   the copy. Swap the new file in, `fsync`, and verify again.
5. **Macros:** `macros.enc` v1 is opened with the old password and written as
   v2. It is verified by reading it back; the v1 file is kept as
   `macros.enc.pre-keybox`.
6. **Record** the counts in `keybox.json` (informational only) and remove the
   marker.
7. **Clean up**, only after checking the new stores again: the
   `*.pre-keybox` copies and the old backups are overwritten with zeros and
   removed. "Moved N saved passwords. Remove the old copies from the system
   keyring?" (default *Remove*) deletes only copies whose secret is in the
   new store. Kept copies can be removed later from Preferences → Security.

Nothing old is deleted or overwritten before its new copy was written,
synced and verified. The legacy reader never writes: the old file class
would have encrypted a plain file in place.

**If it fails or is cancelled** (wrong password, unreadable file, keyring
locked, disk full, *Not Now*), nothing is deleted. The app starts in **safe
mode**: the old data untouched, saved passwords unavailable, and the banner
"Saved passwords could not be moved. Nothing was lost. Try again from
Preferences → Security → Finish migration." Every step can run again: a
rerun finds what is already done and continues.

**If it is interrupted** (crash, power loss), the next start sees the marker
or the `*.pre-keybox` copies. It asks for the master password, runs the
steps again and cleans up.
