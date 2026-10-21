# Connection protocols

SSH Manager is a connection manager: a host is an SSH server, or an FTP,
FTPS or SFTP server for file transfer. Remote desktop (RDP, VNC) can follow
without touching the core. Code: `ssh_manager/protocols/__init__.py` (pure
Python, no GTK).

## The registry

A `ConnectionKind` describes one protocol:

| Field | Meaning |
|---|---|
| `id` | Stored in `hosts.protocol`: `ssh`, `ftp`, `ftps`, `sftp` (later `rdp`, `vnc` …) |
| `label`, `badge`, `icon` | Host dialog label, tree / tab pill text, bundled icon (`sshm-server`, `sshm-transfer`) |
| `default_port` | 22 (SSH, SFTP), 21 (FTP, explicit FTPS; implicit FTPS is a setting, 990) |
| `supports_credentials`, `supports_keys`, `supports_jump_host` | What the host dialog offers |
| `terminal` | Opens a terminal. Broadcast, split input, remote stats and password-change tracking apply only to these |
| `encrypted` | False for plain FTP (warnings) |
| `family` | `shell` or `transfer` (icon and UI grouping) |
| `settings_defaults()`, `validate(host)` | Protocol settings and the problems that keep a host from connecting |

The GTK layer attaches an **opener** per protocol:

```python
protocols.set_opener("ssh", lambda win, host: win.sessions.open_ssh_host(host))
```

- **Dispatch:** `SessionController.open_hosts()` opens every host with its
  protocol's opener: a terminal tab for SSH, a file browser for the FTP
  family (phase F2), and so on.
- **No opener:** a protocol this version does not know, or one without an
  opener yet, gives "“web” uses a protocol this version cannot open (…)",
  never an exception.

### Adding a protocol

1. Write a module that calls `protocols.register(ConnectionKind(...))`.
2. In its UI code, call `protocols.set_opener(id, opener)`. The opener
   returns the alias when it opened a tab.
3. If it has settings, keep them under its own key in `host.metadata`.

Nothing else in the core changes. The host dialog lists every registered
kind; the tree, the tooltips, the `proto:` filter and the details page read
the registry.

## Where SSH was assumed

| Place | With other protocols |
|---|---|
| OpenSSH export / `~/.ssh` Include | Non-SSH hosts are left out, and the toast says how many |
| `build_ssh_command()` | Raises `ValueError` for a non-SSH host |
| Copy SSH command | Copies `ftp://user@host:port` (never a password); Copy SCP command says "not an SSH host" |
| Default port (`host_connection_params`, details, tooltip) | The protocol's default |
| Latency | A plain TCP connect (it never ran `ssh`) |
| Audit log | Every line has `protocol=…` |
| Search and filters | `proto:ftp` / `protocol:sftp`; typing `sftp` finds SFTP hosts |
| Tree | File-transfer icon and a protocol pill (`FTP`, `FTPS`, `SFTP`) at the row's right edge; SSH rows are unmarked |
| Details page | A "Protocol" fact, the protocol's icon, and the URL as copy text |
| Host dialog | **Protocol** is the first field; it swaps pages, updates the default port unless you typed one, and asks before dropping settings on an existing host |
| Folder inventories | OpenSSH config files: SSH only (the Protocol field is disabled) |
| Quick Connect | `[user@]host[:port]` stays SSH (`host:3389` is RDP); `ssh://`, `sftp://`, `ftp://`, `ftps://` and `rdp://` URLs open the matching tab (`protocols/urls.py`) |
| MobaXterm import | Types 6 / 7 / 4 become `ftp` / `sftp` / `rdp` hosts; the protocol is part of "the same host" |
| SSH host menu | **Browse Files (SFTP)** opens an SFTP view of the SSH host (same id, credential, key and jump host; nothing stored) |
| RDP | `rdp` (port 3389, `sshm-desktop` icon, `RDP` pill, `proto:rdp`): Remote Desktop pages in the host dialog, settings in `metadata["rdp"]`; see `docs/rdp.md` |

## Quick Connect URLs

`parse_connection_url()` accepts `scheme://[user@]host[:port][/folder]` for
`ssh`, `sftp`, `ftp`, `ftps` and `rdp`. An `rdp://` user may be
`DOMAIN%5Cuser`; its path is ignored. The user and the folder may be
percent-encoded; IPv6 hosts are bracketed. The folder becomes the
connection's first remote folder (`ssh://` ignores it), and `ftps://…:990`
selects implicit TLS. A URL with a password (`ftp://user:secret@host`) is
**refused** with "Do not put a password in the address": it would end up in
the tab title and the history, so the program asks for the password instead.
Queries and fragments are refused too.

The connection is temporary: nothing is written to the inventory. The toast
after connecting offers **Save to Inventory…**, which opens the host dialog
filled in (alias = host name, protocol, user, port, first folder) and then
asks for a group.
