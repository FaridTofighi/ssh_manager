# Known hosts and SSH host keys

How SSH Manager shows and handles SSH host keys: the terminal banner, the
Known Hosts manager, and the file browser / tunnel dialog. Code:
`ssh_manager/known_hosts.py` (no GTK), `ssh_manager/host_key_output.py`,
`gtkui/dialogs/known_hosts.py`, `gtkui/dialogs/host_key.py`.

## What a host key is

Every SSH server has a key pair of its own, its **host key**. When you connect,
the server proves it holds the private half; your computer compares the public
half with the one it stored the first time (`~/.ssh/known_hosts`). A matching
key means you reached the same server as before, not an impostor in between.

The first time you connect, nothing is stored yet. ssh shows the key's
**fingerprint** (`SHA256:…`) and asks whether to trust it. Compare it with the
fingerprint your administrator or hosting provider publishes. After *yes*, the
key is saved and checked silently on every later connection.

## "REMOTE HOST IDENTIFICATION HAS CHANGED"

The server presented a key different from the stored one. ssh refuses to
connect. There are two kinds of reason:

- **Harmless:** the server was reinstalled, its keys were regenerated, or its
  address now points to another machine.
- **An attack:** someone is intercepting the connection (man in the middle).

From the client alone you cannot tell which. Find out before you accept the
new key: ask the server's owner, compare the new fingerprint with a trusted
source, or check from another network.

## In the terminal

- **Changed key:** when ssh stops with this warning, the tab shows a warning
  bar: "The server's host key has changed…".
  - **Details…** shows:
    - the stored key (type and fingerprint, read from the `known_hosts` line ssh
      named, after checking that this line belongs to the host);
    - the new fingerprint, the file's date, how to verify, and **Open Known
      Hosts…**.
  - **Remove Old Key and Reconnect…** asks first (*Cancel* is the default):
    - **Removes exactly that one line**, after a backup. If the line is
      something else now, the host's entries are searched again. With more than
      one candidate nothing is removed, and the manager is the place to decide.
    - **Then reconnects.** ssh asks again whether to trust the new key, **in the
      terminal**. SSH Manager never answers that question for you.
- **Unknown key (first connection):** an information bar shows the fingerprint
  with **Copy Fingerprint**. You answer ssh in the terminal as before. There is
  no "Trust" button.
- **One tab only:** the bar belongs to that tab. Its buttons never act on other
  sessions, even with broadcast on. After the failed ssh exits, the tab is
  ended as usual (`r` reconnects, `q` closes).

**How the output is recognised:** the exact warning block, then `Host key
verification failed.` as the last output, after ssh has exited without logging
in. For the first connection, ssh's question must be the last output while it
waits. Text shown inside a session (`cat` of a saved warning) never triggers
it.

## File browser, SSH tunnels, Test Connection, remote stats

- **File browser and RDP SSH tunnel:** the same dialog — **Details…**,
  **Remove Old Key and Retry…**, **Close**.
- **Test Connection:** shows both fingerprints in its result.
- **Remote stats:** a background check never opens a dialog. The status bar shows
  "Host key changed", and the log gets one warning per host per session.

**Settings → SSH Security → Auto-add new hosts:**
- **For:** the file browser, tunnels and the Paramiko backend.
- **Unknown keys:** accepted for that connection only, and nothing is saved.
- **Changed keys:** still refused.
- **Terminals:** follow ssh's own configuration (`StrictHostKeyChecking`).

## The Known Hosts manager

App menu → **Known Hosts…**, a host's menu → **Forget Host Key…** (opens on that
host's keys), or the `known-hosts` shortcut action (no key by default; set one
in Settings → Keyboard Shortcuts).

- **Views:**
  - **All entries** (malformed lines greyed, never rewritten);
  - **Inventory hosts without a known key** (never connected; information only);
  - **Several keys per host**;
  - **Unmatched hashed entries**.
- **Columns:** the inventory alias (when an entry belongs to a host), the
  address or pattern, key type, SHA256 fingerprint (MD5 and size in the
  tooltip), line, marker (`@cert-authority`, `@revoked`), comment.
- **Delete Selected…** lists what goes, keeps a backup, and offers **Undo** for
  30 seconds.
- **Import…** merges another known_hosts file. It shows how many entries are
  new and how many are duplicates first.
- **Export Selected…**, **Copy Fingerprint**, **Backup Folder**.
- **Verify Host Key Now…** (a selected inventory host):
  - **How:** a key exchange without logging in. The presented key is compared
    with the stored ones, and nothing is written.
  - **Results:** *matches*, *different* (both fingerprints shown), or *not
    stored*.
  - **Limit:** it connects directly; jump hosts are not used for this check.

**What it cannot do:** say *why* a key changed, or trust a key for you. Trusting
stays ssh's question, or the file browser's own first-connection dialog.

### Hashed entries

With `HashKnownHosts yes`, ssh stores `|1|salt|hash` instead of the host name.
That is HMAC-SHA1 of the name, so a stolen file does not list your servers. The
name cannot be read back. To show an alias, the manager hashes every inventory
host name, with each entry's salt.

- **Speed:** with many hosts and many hashed entries this takes a moment. It runs
  in the background with progress shown, and the result is cached until the file
  changes.
- **Leftovers:** hashed entries that no inventory host matches are listed
  separately.

### How edits are made

- **Safely:**
  - a lock on the file (`flock`);
  - a check that it did not change since it was read (otherwise "Reload and try
    again", nothing written);
  - a timestamped backup next to it (`known_hosts.bak-YYYYmmdd-HHMMSS`, the 5
    newest kept);
  - an atomic replace (temporary file, `fsync`, rename, mode 0600).
- **Untouched lines:** comments, blank lines, markers, CRLF and odd spacing keep
  their exact bytes.
