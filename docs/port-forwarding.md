# Port forwarding (tunnels)

Ophion runs SSH port forwards for you: per host, started and stopped on
their own, with a status for each. Code: `ssh_manager/tunnels/model.py`
(forwards, validation), `ssh_manager/tunnels/engine.py` (the Paramiko engine,
no GTK), `gtkui/controllers/tunnels.py`, `gtkui/dialogs/tunnels.py`,
`gtkui/dialogs/forwards_page.py`.

## The three kinds

### Local (`ssh -L`): reach something the server can reach

A port on **your computer** leads to a service that only the server can reach.
The destination name is resolved **on the server**, so names of its network
work.

*Example: a database behind a jump server.* The database `db.internal:5432`
is only reachable from `bastion`. Add a **Local** forward to `bastion`:

| Field | Value |
|---|---|
| Listen on | `127.0.0.1` |
| Listening port | `15432` |
| Destination | `db.internal` |
| Destination port | `5432` |

Then point your database client at `127.0.0.1:15432`.

### Remote (`ssh -R`): let the server reach something on your side

A port on **the server** leads back to a service your computer can reach. The
destination is resolved **on your computer**.

*Example: show a local development web server to the remote side.* Your app
runs on `localhost:3000`. Add a **Remote** forward with listening port `9000`,
destination `localhost:3000`. On the server, `curl http://localhost:9000`
reaches your app.

The server decides whether it allows this. `AllowTcpForwarding no` refuses
it; listening on other addresses than its loopback needs `GatewayPorts`. A
refusal is shown as such.

### Dynamic (`ssh -D`): a SOCKS proxy

A **SOCKS5 proxy** on your computer: programs set to use it open every
connection from the server.

*Example: browse as if you were on the server's network.* Add a **Dynamic**
forward on port `1080`. In the browser's network settings choose a SOCKS v5
proxy at `127.0.0.1`, port `1080` (with "proxy DNS when using SOCKS v5", so
names are resolved on the server). **Copy Address** gives
`socks5://127.0.0.1:1080`.

**What the proxy supports:**
- **Yes:** SOCKS5 CONNECT to IPv4, IPv6 and domain-name addresses.
- **No:**
  - BIND and UDP ASSOCIATE (they get "command not supported");
  - SOCKS4;
  - authentication: it listens on loopback only.

## Managing forwards

- **Tunnels** (app menu → Tunnels…, the active-tunnels chip `⇄ N` in the top bar,
  or the `tunnels.open` shortcut action, no key by default):
  - **List:** every host with forwards, and its forwards: type, listening
    address, destination, status (an icon and a word: Active, Starting,
    Reconnecting (attempt n), Failed, Stopped), connections, traffic. The last
    error is in the tooltip.
  - **Toolbar:** Start / Stop, Edit…, Delete…, Copy Address, Open in Browser
    (local forwards on usual web ports), Add Forward… (pick the host in a
    searchable list), Stop All, and a filter.
- **Host dialog → Port Forwarding** (SSH hosts): the same forwards for one host,
  with the start options. The host's own `LocalForward` / `RemoteForward` /
  `DynamicForward` ssh options are listed as "From SSH options (not managed)",
  with **Import as Managed Forward**.
  - **The ssh option stays:** remove it on the SSH Options page if ssh should
    not open it too.
  - **Not understood:** options Ophion cannot read are left as they are.
- **The editor:** explains each type with one line and a small picture, checks
  as you type, and keeps **Save** disabled until the forward is valid. Its rules:
  - ports 1–65535 (a warning below 1024: those need administrator rights);
  - valid host names;
  - no second forward listening on the same address and port: local and
    dynamic forwards across the whole inventory, remote forwards on the same
    host.

## When forwards run

- **With a session:** opening a terminal for the host starts these forwards in
  the background, beside `ssh`, not as `-L` flags.
  - **Independent of the terminal:** they can be stopped without disconnecting,
    and do not depend on the terminal's ssh options.
  - **Stopping:** they stop when the host's last session closes ("Stop when the
    last session of this host closes" is on by default for them).
- **With the app:** after the main window is shown, one at a time.
  - **No dialogs:** failures are summed up in one notification and never open a
    dialog.
  - **Needs a password:** a forward that needs one shows *Failed — Needs
    password*. **Start** in Tunnels asks for it, for this session only.
- **By hand:** Start in Tunnels.

**Quitting with tunnels running** asks "N tunnels are running. Stop them and
quit?". Closing the window stops them all.

## How it works, and its limits

- **One connection per host:** all its forwards share one SSH connection. It uses
  the app's usual SSH connection: the same jump hosts, keys, agent, passwords,
  and known_hosts rules (`host_key_policy`).
  - **A changed host key:** the forward fails and shows the host-key dialog; it
    is never bypassed.
  - **An unknown key:** asks only when you started the forward yourself.
  - **Login failure:** no retry; you are asked.
- **Keepalive and reconnection:**
  - SSH keepalives every 30 s.
  - **A lost connection:** reconnects with a growing pause (2 s up to 60 s, back
    to 2 s after a minute without trouble), then requests every forward again.
  - **Shown as:** *Reconnecting (attempt n)*, with one notification when it
    drops and one when it is back (at most one each per host and minute).
- **Listening address:** `127.0.0.1` by default. Any other address needs **Bind
  to all interfaces**, with a warning: other computers can then use the
  forward, and it has no password. The engine refuses otherwise.
- **Busy ports:** "127.0.0.1:8080 is already used by another program". Only that
  forward fails; the host's other forwards keep running.
- **Limits:**
  - at most 64 simultaneous connections per forward (more are refused);
  - Unix-socket forwards (`LocalForward /path …`) are not managed.
- **Stored** in the host's metadata (`forwards`), included in Ophion's
  own export and import. The MobaXterm import does not read MobaXterm's
  tunnels.
