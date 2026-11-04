# Known hosts and SSH host keys

How Ophion shows and handles SSH host keys: the terminal banner, the
Known Hosts manager, and the file browser / tunnel dialog. Code:
`ophion/known_hosts.py` (no GTK), `ophion/host_key_output.py`,
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
      terminal**. Ophion never answers that question for you.
- **Unknown key (first connection), no saved password:** an information bar
  shows the fingerprint with **Copy Fingerprint**. You answer ssh in the
  terminal as before. There is no "Trust" button. With a saved password, see
  the next section.
- **One tab only:** the bar belongs to that tab. Its buttons never act on other
  sessions, even with broadcast on. After the failed ssh exits, the tab is
  ended as usual (`r` reconnects, `q` closes).

**How the output is recognised:** the exact warning block, then `Host key
verification failed.` as the last output, after ssh has exited without logging
in. For the first connection, ssh's question must be the last output while it
waits. Text shown inside a session (`cat` of a saved warning) never triggers
it.

## Sessions with a saved password (pre-flight)

**Why:** a saved password is typed by `sshpass -e ssh`. sshpass cannot answer
ssh's first-connection question: on a key that is not in `known_hosts` it
stops and exits with **status 6**, with no output. On a computer whose
`known_hosts` is still empty (a fresh account, for example on Ubuntu) every
password host failed that way.

**The rule:** before a password session starts, Ophion checks the host key the
way ssh will, off the main window's thread:

- `ssh -G` with the session's own options gives the effective host name,
  port, `HostKeyAlias`, `known_hosts` files and `HashKnownHosts`.
- **Known** (plain, `[host]:port`, wildcard or hashed line): the session
  starts as before. A **changed** key is still refused by ssh, and the
  changed-key bar above appears; there is no trust offer.
- **Unknown:** the server's key is read with a key exchange only (no login,
  nothing sent, at most 5 seconds), and the first-connection dialog shows its
  type and SHA-256 fingerprint.
  - **Connect** adds it to the first `known_hosts` file ssh reads (hashed when
    `HashKnownHosts` is on; file 0600, folder 0700), then the password is
    sent.
  - **Cancel** ends in "Connection cancelled". Nothing is written or sent.
- **Unreachable:** "Could not read the server's key…"; nothing is sent.
- **Left to ssh, unchanged:** your own `StrictHostKeyChecking` (`yes`, `no`,
  `accept-new`) and hosts behind `ProxyJump` / `ProxyCommand`.

If sshpass still exits 6, the tab says "Host key for *host* is not trusted
yet. Click to review it.", and **Review Host Key…** runs the check again.

**Background commands never accept a key.** Remote stats, Run Command on
Hosts and port forwards never ask about an unknown key on their own and
never trust one. They say "Host key not trusted yet — open the session once
to accept it." (Run Command's **Details…** and starting a tunnel yourself
still offer the same dialog.)

## File browser, SSH tunnels, Test Connection, remote stats

- **File browser and RDP SSH tunnel:** the same dialog — **Details…**,
  **Remove Old Key and Retry…**, **Close**.
- **Run Command on Hosts:** the result names the host name and port that
  were checked, the key presented and the stored fingerprints. **Details…**
  opens the same dialog, then offers **Re-run this host**.
- **Test Connection:** shows both fingerprints in its result.
- **Remote stats:** a background check never opens a dialog. The status bar shows
  "Host key changed" (or "Host key not trusted" for a key not in `known_hosts`
  yet), and the log gets one warning per host per session.

**Settings → SSH Security → Auto-add new hosts:**
- **For:** the file browser, tunnels and the Paramiko backend.
- **Unknown keys:** accepted for that connection only, and nothing is saved.
- **Changed keys:** still refused.
- **Terminals:** follow ssh's own configuration (`StrictHostKeyChecking`).

**How a key is matched (as OpenSSH does).** The connection offers the key
types stored for the host first, so a server with several keys presents one
you already trust. Plain, `[host]:port`, wildcard and hashed lines all count.
A key is accepted when **any** matching line of its type holds it; stale or
duplicate lines do not matter. It counts as **changed** only when lines of
its type exist and none matches. A host known **only with other key types**
(the server no longer offers the trusted type) gets the first-connection
question, naming the type trusted before. If accepted, the new key is added
beside the old one. Strict checking still refuses it without asking.
`@cert-authority` lines are ignored, and a key on an `@revoked` line is
always refused.

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
