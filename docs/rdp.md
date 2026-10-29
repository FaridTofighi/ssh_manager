# Remote Desktop (RDP)

RDP hosts use the protocol `rdp` (port 3389). The remote desktop itself is
drawn by **FreeRDP**, an optional system package: SSH Manager starts and
every other protocol works without it. Code: `ssh_manager/rdp/` (pure
Python) and `ssh_manager/gtkui/dialogs/rdp_page.py`,
`rdp_diagnostics.py`.

| Module | Role |
|---|---|
| `rdp/settings.py` | `RdpSettings` (`host.metadata["rdp"]`), user names (`split_rdp_user`), extra-option allow-list |
| `rdp/probe.py` | "Test connection": the X.224 negotiation probe and the server certificate |
| `rdp/engines.py` | Which FreeRDP clients exist, their versions and options, the engines and their capabilities |
| `trust.py` | Trusted certificates shared with FTPS (`TrustStore`; RDP keys are scoped `rdp:`) |
| `rdp/rdpfile.py` | `.rdp` files: import (strict parser) and export |
| `rdp/wol.py` | Wake-on-LAN magic packets |
| `gtkui/controllers/rdp_files.py` | The `.rdp` import / export actions and **Wake and Connect** |

Status: **phase R4** (complete). With gtk-frdp, or FreeRDP on X11, the remote desktop
is shown inside the tab under a toolbar; elsewhere, or if embedding fails,
it runs in FreeRDP's own window and the tab is a session card.

| Module | Role (R1) |
|---|---|
| `rdp/command.py` | The version-aware argv builder and the secret payload |
| `rdp/process.py` | Spawning, output, stopping, the child registry and the stale-child reaper |
| `rdp/outcome.py` | What an exit means (exit codes, `ERRCONNECT_*`, `ERRINFO_*`, certificate lines) |
| `rdp/trust_flow.py` | Certificate trust decided before FreeRDP starts |
| `gtkui/rdp/tab.py`, `gtkui/rdp/runtime.py` | The session card and the window's FreeRDP runtime |

## Engines

| Engine | What | Needs |
|---|---|---|
| Embedded (gtk-frdp) | A GTK widget wrapping FreeRDP (GNOME Connections) | The `Frdp` GObject-Introspection typelib (X11 or Wayland) |
| Embedded (FreeRDP in the tab) | `xfreerdp` / `xfreerdp3` inside a `Gtk.Socket` (`/parent-window:<XID>`) | GTK on X11 (or XWayland) and an xfreerdp client |
| External window | Any FreeRDP client in its own window; the tab shows a session card | Any of `xfreerdp3`, `xfreerdp`, `sdl-freerdp3`, `sdl-freerdp`, `wlfreerdp3`, `wlfreerdp` |

They are tried in that order. **Preferences → Remote Desktop → Show
sessions** chooses Automatic, Embedded or External window; a host can pick
its own engine (host dialog → RDP Connection → Advanced). When the wish
cannot be met the next available engine is used and the host dialog says
why. With no engine at all, RDP hosts still save, and opening one shows the
install command for the distribution.

### Install

| Distribution | Command |
|---|---|
| Debian, Ubuntu | `sudo apt install freerdp3-x11` (`freerdp2-x11` on older releases) |
| Fedora, RHEL | `sudo dnf install freerdp` |
| Arch | `sudo pacman -S freerdp` |
| openSUSE | `sudo zypper install freerdp` |

gtk-frdp is packaged by few distributions (it ships with GNOME
Connections). A **Flatpak** build cannot see clients installed on the host.

### Discovery

`discover()` runs off the GTK thread at start-up and after the preference
changes; dialogs read the cached report and never wait for it.

- **Where:** the client chosen in Preferences, then `PATH` in the order above.
- **How:** each client runs with `/version` (then `--version`) and `/help`.
  It gets no credentials, no stdin, a 3-second timeout and a minimal
  environment (`PATH`, `HOME`, `LD_LIBRARY_PATH`, `LANG=C`).
- **Version:** from "This is FreeRDP version X.Y.Z". Only 2.x and 3.x are
  supported.
- **Options:** the option names listed by `/help` (both the 2.x layout
  `/name`, `+name`, `-name` and the 3.x `[-|/]name`). They decide what the
  argv builder may use and which capabilities a build has; a build without
  USB support has no `/usb`, for instance.
- **Password channel:** 3.x reads its whole command line from a pipe
  (`/args-from:fd:<n>` or `stdin`: "can not be combined with any other"
  argument, one argument per line, an empty line ends it). 2.x has only
  `/from-stdin`, which reads the password from stdin. A client with neither
  never gets a password from the app (see "Secrets").
- **Diagnostics:** **Preferences → Remote Desktop → Diagnostics…** shows
  the report: the session type, the GTK backend, gtk-frdp, every client
  with its version and password channel, every engine with its capabilities
  or the reason it is unavailable, the chosen engine and install hints.
  **Copy** puts the report (no credentials, no hosts) on the clipboard.

Verified against FreeRDP **3.15.0** (Debian `freerdp3-x11`: its `/help`
output is `tests/fixtures/freerdp/help-3.15.txt`) and **2.11.7** (the option
table of its source, `tests/fixtures/freerdp/help-2.11.txt`).

### Capability matrix

The host dialog disables options the chosen engine cannot do and names the
reason in the tooltip; the launcher (R1) drops them with a log note.

| Capability | gtk-frdp | FreeRDP in the tab | External window | From |
|---|---|---|---|---|
| Remote screen in the tab | yes | yes | no (session card) | engine |
| Follows the window size | yes | `/dynamic-resolution` | `/dynamic-resolution` | `/help` |
| Smart sizing | yes | `/smart-sizing` | `/smart-sizing` | `/help` |
| Full screen | yes | `/f` | `/f` | `/help` |
| Clipboard | yes | `/clipboard` | `/clipboard` | `/help` |
| Shared folders, printers, sound, microphone | no | `/drive`, `/printer`, `/sound`, `/microphone` | same | `/help` |
| USB, smart cards | no | `/usb`, `/smartcard` | same | `/help` (build-dependent) |
| RD Gateway | no | `/gateway` (3.x) or `/g` (2.x) | same | `/help` |
| Multiple monitors | no | `/multimon` | `/multimon` (not `wlfreerdp`) | `/help` |
| RemoteApp | no | `/app` | `/app` | `/help` |
| Keyboard grab | yes | `/grab-keyboard` | `/grab-keyboard` (not `wlfreerdp`) | `/help` |
| Screenshot, Send Ctrl+Alt+Del | R2 | R2 | no | — |

The gtk-frdp column is conservative until R2 inspects the installed typelib.

## Host settings (`metadata["rdp"]`)

`RdpSettings` is stored as JSON with `"v": 1`. Unknown keys are kept and
written back. An invalid value falls back to its default with a warning in
the host dialog, and `from_json` never raises.

| Group | Settings |
|---|---|
| Sign-in | domain, use the DOMAIN\ prefix of the user name, ask for the password every time |
| Security | security (automatic, NLA, NLA-ext, TLS, standard RDP), TLS fallback, TLS level, certificate policy and pins, console session, restricted admin, Remote Credential Guard |
| Display | size (fit the tab, fixed 200×200–8192×8192, full screen), colors, scaling, desktop scale, smart sizing, monitors |
| Performance | preset (automatic, LAN, broadband, WAN, slow link, custom), graphics codec, compression, wallpaper, themes, font smoothing, animations, window drag, composition |
| Input | keyboard layout (incl. Persian 0x429 and "same as local"), Unicode input, keyboard grab, Alt/Windows swap, mouse motion, touch |
| Devices | clipboard (both, to remote, from remote, off), shared folders, home folder, printers, sound, microphone, USB selectors, smart cards |
| Connection | timeout (15 s), auto-reconnect (on, 5 attempts), network detection, RD Gateway, SSH tunnel host, start program, working folder, RemoteApp, load-balance info, engine, extra options |

Defaults: NLA when offered, certificate "ask", clipboard on, every other
redirection **off**. Sharing the home folder, standard RDP security,
"do not check the certificate" and "allow any option" each ask first.

### User names

`split_rdp_user()`:

| Typed | Sent |
|---|---|
| `CORP\alice` | user `alice`, domain `CORP` |
| `.\admin` | user `admin`, domain `.` (local account) |
| `alice@corp.example` | user `alice@corp.example`, **no** domain (UPN) |
| `alice` | user `alice` |
| `a\b\c`, control characters, > 256 characters | refused with a message |

The host's **Domain** field wins over a `DOMAIN\` prefix; without "Use the
domain from a DOMAIN\user name" the prefix is ignored. A UPN is never sent
with a domain.

### Extra options

Advanced FreeRDP flags are checked against an allow-list (for example
`+async-update`, `/gdi:hw`, `/tls:seclevel:0`). "Allow any option"
accepts other flags after a confirmation. Some options are refused even
then:

- **Credentials and the target:** `/p`, `/u`, `/d`, `/v`.
- **Secret channels:** `/args-from`, `/from-stdin`.
- **Gateway credentials and certificate overrides.**
- **Anything that loads code or files or reaches the network:** add-ins,
  proxies, scripts, `/rdp2tcp`.

Error messages name the option only, never its value, because a value
typed there by mistake may be a password.

## Test connection

The **Remote Desktop** page's **Test Connection** runs `probe()` in a
worker thread:

1. **Connect:** TCP with the host's timeout. The latency is the connect time.
2. **Negotiate:** send a TPKT + X.224 Connection Request with an
   `RDP_NEG_REQ` for TLS | NLA | NLA-ext ([MS-RDPBCGR] 2.2.1.1). The reply
   is either an `RDP_NEG_RSP` (the selected protocol) or an
   `RDP_NEG_FAILURE`.
   - `SSL_NOT_ALLOWED_BY_SERVER`: a second request asks for standard RDP
     security.
   - A server that hangs up gets one more try without NLA-ext.
   - A Connection Confirm without negotiation data means an old server
     (standard RDP security only).
3. **Certificate:** for TLS and NLA, a TLS handshake **without
   verification** reads the certificate only to show it: subject, issuer,
   validity, SHA-256, and whether it matches a pinned one. CredSSP is never
   started; **no credential is ever sent**.
4. **Hints:** for example "the server asks for NLA, but this host forces
   standard RDP".

Every read has a timeout and a byte limit (a TPKT longer than 4 KiB is
refused). Only the documented fields are read, and all server data is shown
as plain text, never as markup. The tests cover every failure code and a
range of bad replies: truncated, oversized, garbage, wrong lengths, unknown
types and timeouts.

## Secrets (the rules for every phase)

- The password never appears in argv, `/proc/<pid>/cmdline`, the
  environment, logs, temporary files, the clipboard or exports.
- FreeRDP 3.x gets its whole command line (`/p:` included) through a pipe
  with `/args-from:fd:<n>`; the pipe is closed right after.
- FreeRDP 2.x gets `/from-stdin` and the password on stdin. Its own
  certificate prompt is answered "no" in that mode, so the app settles
  certificate trust before launching (`/cert:fingerprint:sha256:…`).
- A client with neither channel is never given a password: the user types
  it in FreeRDP's own prompt, and the app says so.

## Sessions (R1)

### One connection

1. **Engine:** the host's engine, else the preference, else the first one
   available. In R1 an embedded engine falls back to the external window
   (the connection log says so).
2. **Password:** the stored one (credential → host password), or
   the password dialog when nothing is stored or "Ask for the password every
   time" is on.
3. **Probe:** reachability, the security the server selects and its
   certificate (as in "Test connection"). An unreachable server or a
   refused security request ends here, before FreeRDP starts.
4. **Trust:** see the table below.
5. **Launch:** `build_launch()` makes the plan and `RdpProcess` starts it.
   - **The password:** 3.x gets everything, `/p:` included, through
     `/args-from:stdin`, and the pipe is closed at once. 2.x gets
     `/from-stdin:force` and only the password on stdin. A test client
     records its argv, environment, stdin and `/proc/self/cmdline` to prove
     the password is only ever on stdin.
   - **Logging:** `/log-level:WARN` plus
     `/log-filters:com.freerdp.gdi:INFO,com.freerdp.core:INFO`, so the two
     lines below are printed without the noise of everything else.
6. **Connected:** decided by FreeRDP's log or by a fallback (next section).
7. **End:** `classify_rdp_exit()` decides what happened; see "Outcomes".

### Verified success

`PostConnect` runs only after the connection is active (both versions'
`rdp_client_connect` wait for activation), and every client calls `gdi_init`
there, which logs **"Local framebuffer format"** at INFO on `com.freerdp.gdi`.
That line marks the session "Connected (verified)". Without it (a client
that logs differently), a client alive for 10 s without an error line counts
as "Probably connected" (the log says which).

"Save this password for this host?" is offered only after a **log-verified
NLA** sign-in with a password typed in this tab: with TLS or standard RDP
security the sign-in happens on the remote screen, so a running session
does not prove the password was right.

### Certificate trust

FreeRDP would ask about an unknown certificate on its own console, which
nobody sees. So the app decides first, then starts FreeRDP with
`/cert:deny,fingerprint:sha256:aa:bb:…` (the colon form both 2.11 and 3.15
accept). If the server shows another certificate at launch, FreeRDP stops
("certificate not trusted, aborting") and the tab reports it.

| Policy | No pin yet | Pin matches | Pin differs |
|---|---|---|---|
| Ask the first time (default) | Trust once / Trust and remember / Cancel | connect | CHANGED warning, Cancel is the default |
| Trust the first certificate | remember, connect | connect | CHANGED warning |
| Only the pinned certificate | asks to pin | connect | CHANGED warning |
| Refuse unknown certificates | refuse | connect | refuse |
| Do not check | asked on every connection, `/cert:ignore`, "Certificate not verified" in the tab | | |

Pins are in the shared `TrustStore` under `rdp:<host id>|<host>:<port>`. A
changed certificate is never accepted silently. Both outcomes were checked
with the real 3.15 client against the test server in `tests/fake_rdp.py`:
the right pin is accepted and the connection fails later at NLA; a wrong pin
is refused.

### Outcomes

From FreeRDP's sources (`xfreerdp.h` exit codes, `errconnect.c`,
`errinfo.c`, `tls.c`):

| Outcome | Examples | Password dialog? |
|---|---|---|
| Disconnected | closed, signed out, idle or time limit, someone else connected, an administrator ended it | no |
| Sign-in failed | `ERRCONNECT_LOGON_FAILURE`, `WRONG_PASSWORD`, `AUTHENTICATION_FAILED`, exit 132 / 134 / 154 | **yes**: at most 3 attempts, at least 1 s apart |
| Account problem | locked out, disabled, expired, password expired or must change, no right to sign in remotely, access denied | no; each has its own message |
| Certificate rejected | "certificate not trusted, aborting", "REMOTE HOST IDENTIFICATION HAS CHANGED" | no |
| Network / DNS / TLS | `CONNECT_TRANSPORT_FAILED`, `DNS_NAME_NOT_FOUND`, `TLS_CONNECT_FAILED`, `KDC_UNREACHABLE` | no |
| Security mismatch | `SECURITY_NEGO_CONNECT_FAILED`, exit 133 | no; suggests another security mode |
| Engine problem | exit 128 (options refused), pre- or post-connect failures | no |

FreeRDP 3.15 logs "Command line parsing failed at … [0]" even for options
it accepted; such lines are ignored.

### Processes

- **Own group:** every client runs in its own process group (session), with
  `close_fds` and never through a shell.
- **Output:** stdout and stderr are read on threads. Lines are capped at
  4 KiB, ANSI sequences are removed, every secret of the launch is replaced
  by `***`, and the last 400 lines are kept for the connection log.
- **Stopping:** **Disconnect**, closing the tab (which asks while
  connected) and quitting (which asks once for all sessions) send SIGTERM
  to the group, then SIGKILL after 2 s.
- **Child registry:** `$XDG_RUNTIME_DIR/ssh-manager/rdp-children.json`
  (0600) records each child. If the app was killed, the next start stops a
  recorded group only if that pid still runs the same client with the same
  start time, and only if the app instance that started it is gone.
- **Limit:** at most "Sessions at once" (Preferences → Remote Desktop, 8 by
  default) run together.
- **Worker threads:** discovery, probes, the stale-child check and stopping
  run on short threads wrapped in `gc_guard.worker`. That pauses Python's
  cycle collector while they run.
  - **Why:** otherwise the collector could finalize GTK widgets on a worker.
    The suite showed this as a deadlock between GObject's signal lock and
    the GIL, and as rare crashes.
  - **Exceptions:** long-lived threads (output readers, the tunnel) are not
    wrapped.

### Known limits (R1)

- **Focus Window:** needs `xdotool` or `wmctrl` on X11. Wayland lets no
  program raise another program's window; the button then says why.
- **FreeRDP 2.x:** cannot receive a separate gateway password safely (use
  "same credentials" or FreeRDP 3), and cannot limit the clipboard to one
  direction (the clipboard is turned off instead).
- **Shared folders:** FreeRDP's command line cannot share a folder
  read-only, so a folder marked read-only is **not shared** (with a note);
  it is never shared read-write.
- **Gateway passwords:** a separate gateway password containing a comma or
  a quote cannot be passed to FreeRDP 3; FreeRDP then asks for it itself.

## The remote screen in the tab (R2)

### FreeRDP in the tab (X11)

- **No XEmbed:** FreeRDP's `/parent-window:<XID>` creates its window and
  `XReparentWindow`s it into the given window (`xf_window.c`). It does not
  speak XEmbed, so `Gtk.Socket`'s `plug-added` never fires.
- **Finding the window:** FreeRDP creates its window in `PostConnect`,
  after the sign-in. So once the session is connected, the tab looks for a
  child of its socket with `XQueryTree` (ctypes over libX11, no new
  dependency), every 200 ms for up to 10 s.
- **Resizing:** size changes are debounced (250 ms) and passed to FreeRDP's
  window with `XResizeWindow`, in device pixels (HiDPI) and rounded to even
  sizes. FreeRDP then asks the server for the new size
  (`/dynamic-resolution`). Nothing is resized before the tab has its real
  size, so the server never sees a tiny transient size.
- **Full screen:** `-toggle-fullscreen` keeps FreeRDP from switching its own
  window to full screen; Ctrl+Alt+Enter and the toolbar make the tab full
  screen instead.
- **Fallback:** if no FreeRDP window appears within 10 s of connecting (a
  window manager or compositor that interferes), the tab notes it, switches
  to an external window and remembers the failure for this engine and
  session type (`$XDG_STATE_HOME/ssh-manager/rdp-embed.json`).

### gtk-frdp

- **API:** the adapter uses gtk-frdp 0.2's `Frdp.Display`: the `username`,
  `password`, `domain` and `scaling` properties, `open_host`, `close`,
  `is_open`, `set_scaling`, and the `rdp-connected`, `rdp-disconnected`,
  `rdp-error`, `rdp-auth-failure` and
  `rdp-needs-certificate-verification` signals.
- **Checked at discovery:** the installed typelib must have every one of
  those members; an older or different build is listed as unavailable,
  naming what it lacks.
- **Password:** it is a property set just before `open_host` and cleared
  right after; it never appears on a command line.
- **Certificates:** they are answered from the app's own decision. The
  certificate approved before connecting is accepted "for this session
  only" (`2`) and any other is refused (`0`); FreeRDP's own known-hosts
  file is never used.
- **What it cannot do:** gtk-frdp connects with its own FreeRDP settings
  (no command line), so shared folders, devices, the gateway and RemoteApp
  are unavailable with it (the host dialog says so). It is not packaged by
  Debian 13, so it was checked against its source and a stand-in class with
  its members, not against a real build.

### Toolbar

A thin bar above the remote screen. It hides after 3 s, comes back on the
handle (or F10 while the remote does not have the keyboard), and can be
pinned. It sits above the screen rather than over it, because a native X
window always covers what GTK draws.

| Button | What it does |
|---|---|
| Send Ctrl+Alt+Del | Disabled for FreeRDP in the tab (no outside input); Ctrl+Alt+End inside the session does the same |
| Full screen | The window full screen and in focus mode; Ctrl+Alt+Enter |
| Scale mode | Fit the window / actual size / smart sizing: reconnects with the new options |
| Resolution | Fit the window or a fixed size: reconnects |
| Keyboard | Shows whether keys go to the remote and how to get them back; toggles grabbing (reconnects) |
| Clipboard | Both ways, one way or off: reconnects |
| Screenshot | Saves a PNG (a file dialog that asks before overwriting) |
| Open the shared folder | The first shared folder in the file manager |
| Connection info | Server, engine, security, certificate, size, colors, how the connection was verified, shared devices |
| Reconnect / Disconnect / Pin | — |

FreeRDP cannot change these options while it runs. Toolbar changes apply
to this session only, by reconnecting; the host's settings are unchanged.

### Keyboard

- **Grab:** while the remote screen has the focus, keys go to the remote
  computer. With FreeRDP the client grabs them itself, and Right Ctrl gives
  the keyboard back (FreeRDP's key, not configurable). gtk-frdp grabs on its
  own as well, and the toolbar shows its state.
- **State tracking:** `rdp/keyboard.py` models it as a state machine: focus
  in grabs, focus out or a tab switch releases and lets go of every modifier
  still down, and the configured release shortcut
  (`<Control><Alt>Control_R`) always works. App shortcuts (switch tab, Quick
  Connect, full screen) stay reserved only while nothing is grabbed.
- **Stuck keys:** FreeRDP's X11 client releases pressed keys on FocusOut
  itself.

### Clipboard

`rdp/clipboard.py` decides per transfer: the direction allowed by the host
(both, to the remote, from the remote, off), the kind (text, image; files
only where the engine can copy them, otherwise use a shared folder) and a
size limit. The log may say "clipboard text, 1234 bytes", never the
contents. FreeRDP 3 enforces the direction with
`/clipboard:direction-to:…`; FreeRDP 2 cannot, so a one-way setting turns
the clipboard off there.

### Reconnecting

`rdp/reconnect.py`:

| End of a session | Reconnect |
|---|---|
| Network dropped after a working session (auto-reconnect on) | automatic: 2, 4, 8 … s, at most 60 s, `reconnect_max` times (0 = up to 20) |
| Idle timeout, time limit, someone else connected, an administrator disconnected | **asks**: Reconnect in the banner |
| You disconnected, signed out | never |
| Sign-in, account, certificate, security problems | never automatic |

While reconnecting, the card shows "Reconnecting… (attempt n of m)" with
**Stop Reconnecting**. If the network is down (`Gio.NetworkMonitor`),
attempts wait for it and are not counted. A certificate trusted "once"
stays trusted for the tab's later attempts.

### Known limits (R2)

- **Floating toolbar:** not possible over a native X window, so it is a
  collapsible bar.
- **Thumbnails in the tile view:** not shown; the session card stands in.
- **Lower frame rate for hidden tabs:** FreeRDP has no runtime control
  for it.
- **gtk-frdp:** not testable here without a packaged build (see above).

## Devices, gateway, tunnel, presets (R3)

### Shared folders

`rdp/drives.py` checks every folder in the host dialog and again right before each launch:

- **Resolving:** the path is resolved to its real location (`~` expanded,
  symbolic links followed). It must be an existing folder, and its path
  must not contain a comma (FreeRDP's separator).
- **Sensitive places:** the file system root, `/etc`, `/root`, `/boot`,
  `/proc`, `/sys`, `/dev`, `/var/lib`, `/usr` itself, the home folder,
  `~/.ssh`, `~/.gnupg`, `~/.password-store`, the keyrings, `~/.aws`,
  `~/.kube`, `~/.docker` and SSH Manager's own configuration and data
  folders. Any folder that *is*, *is inside* or *contains* one of them
  counts, and so does a symbolic link that leads into one. Ordinary folders
  in the home folder and `/usr/share` are fine.
- **Confirmation:** a sensitive folder is shared only after a red
  confirmation for that folder (stored as `allow_sensitive`). Without it the
  launch leaves the folder out, with a note.
- **Read-only:** a folder marked read-only is not shared, because FreeRDP
  cannot share read-only (see R1).
- **Opening:** the toolbar's **Shared folders** menu opens any of them on
  this computer.

### Printers, sound, microphone, USB, smart cards

These options are passed only when the client's `/help` lists them;
otherwise the dialog disables them and says why. They are off by default.
USB and smart-card redirection hand the device to the remote computer while
connected (the tooltips say so).

### RD Gateway

- **Credentials:** "Use the same credentials as the server" sends only
  `g:host:port`; FreeRDP 3 then sets `GatewayUseSameCredentials`. Otherwise
  the gateway user, domain and the password of a chosen saved credential go
  into `/gateway:…,u:…,d:…,p:…`. That password travels only through the
  stdin pipe, like the server's.
- **Transport:** automatic, HTTP or RPC.
- **Certificates:** the gateway's certificate is probed (a TLS handshake
  only) and goes through the same trust flow, with its own pins (scope
  `rdp-gw`).
- **The server behind the gateway:** it usually cannot be reached directly,
  so it is not probed. With "Ask the first time", "Trust the first
  certificate" or "Only the pinned certificate" and no pin yet, FreeRDP's
  own trust-on-first-use is used for it (`/cert:tofu`): it refuses a
  changed certificate later. The card says "server: trusted on first use by
  FreeRDP". "Refuse unknown certificates" needs a pinned server
  certificate.
- **Test Connection:** checks the gateway (reachable, certificate).
- **Limits:** an RD Gateway and an SSH tunnel cannot be combined.

### Through an SSH host (tunnel)

"Through" on the RDP Connection page names an SSH host of the inventory:

1. **Login:** the app logs in to it with the SFTP code's connection steps
   (`SftpBackend.open_transport`): the same jump host (`ProxyJump` /
   `ProxyCommand`), known_hosts rules, and key / agent / saved-password
   order as everywhere else. An unknown host key asks with the usual
   dialog; a rejected login says so ("did not accept the login").
2. **Listener:** `rdp/tunnel.py` listens on **127.0.0.1** only, on a port
   the kernel chooses (no `SO_REUSEADDR`). It accepts only loopback clients
   and **one connection at a time**, and forwards each over a
   `direct-tcpip` channel to the server.
3. **Probe:** the server is probed **through** the tunnel, and its
   certificate is pinned for the real host name.
4. **Launch:** FreeRDP connects to `/v:127.0.0.1:<port>` with
   `/server-name:<real host>`, so TLS and Kerberos names are checked against
   the real server. Verification is not weakened.
5. **Lifetime:** the tunnel lives as long as the session. It closes with
   the tab, on Disconnect, and before every new attempt (a reconnect opens a
   new one). If it fails while connecting, the message says it was the
   tunnel.

The tests run it against a paramiko SSH server that forwards to a test RDP
server.

### Performance presets

| Preset | `/network:` | Wallpaper | Themes | Font smoothing | Menu animations | Window drag | Composition |
|---|---|---|---|---|---|---|---|
| LAN | `lan` | on | on | on | on | on | on |
| Broadband | `broadband-high` | on | on | on | off | on | off |
| WAN | `wan` | off | on | on | off | off | off |
| Slow link | `modem` | off | off | off | off | off | off |
| Automatic | from the measured latency, else `auto` | | | | | | |
| Custom | — (the toggles themselves) | | | | | | |

"Automatic" uses the probe's connect time:

| Measured latency | Preset |
|---|---|
| under 5 ms | LAN |
| under 40 ms | Broadband |
| under 150 ms | WAN |
| slower | Slow link |
| unknown (through a tunnel or a gateway) | FreeRDP's own detection (`/network:auto`) |

The note in the connection log says which preset was chosen.

### Programs and advanced options

- **Programs:** start program and working folder (`/shell:`,
  `/shell-dir:`), and RemoteApp (`/app:program:…,name:…,cmd:…` in 3.x;
  `/app:` `/app-name:` `/app-cmd:` in 2.x).
- **Sessions:** console (admin) session (`/admin`), and restricted admin
  mode (`/restricted-admin`, asks first: the session then acts as the
  computer, not as you, on the network).
- **Load-balance information:** checked and limited to 256 characters.
- **Hyper-V console:** the VM's GUID (`/vmconnect:<id>`; set the port to
  2179).

### Known limits (R3)

- **SSH tunnel login:** the SSH host's password comes from its saved
  credential; the tunnel does not open a password prompt of its own (keys
  and the agent work as usual).
- **Test Connection with a tunnel:** it checks the server directly; through
  the tunnel it is checked when connecting.
- **Behind an RD Gateway:** the server cannot be checked before FreeRDP
  starts (see above).
- **Audio devices:** no device picker; the engine's default device is
  used.

## Import, export, Quick Connect, Wake-on-LAN (R4)

### MobaXterm sessions

Type 4 sessions of a `.mxtsessions` export become `rdp` hosts (see
`docs/import-mobaxterm.md`). **Import RDP sessions** in the wizard's options
(on by default) leaves them out.

- **Fields read:** `f0` host, `f1` port (empty means 3389) and `f2` user.
- **User names:**
  - A literal `CORP\alice` is split into the domain `CORP` (stored in
    `metadata["rdp"]["domain"]`) and the user `alice`.
  - A bracketed label `[alice]` is a MobaXterm credential reference. It goes
    through the Credentials step like SSH users.
  - Rows in the Credentials step show which protocols use each name.
- **Not imported:** display and redirection options. Their positions in the
  export are not documented, so the defaults are used and the report says
  so.

### `.rdp` files

**Import → From Remote Desktop Files (.rdp)…** reads one or more files
(200 at most) into a group you choose. Each file becomes one host named
after the file; a name already used gets a number.

- **Encodings:** UTF-16 LE with a BOM (what mstsc writes), UTF-8, or
  Windows-1252.
- **Size limits:** 256 KiB per file and 4096 characters per line. Lines
  with control characters are refused, and a file without a valid
  `full address` is reported and skipped.
- **Passwords:** `password 51:b:` blobs, gateway passwords and access
  tokens are **never** imported. They are DPAPI data usable only by the
  Windows account that wrote them. The report names the key, never its
  value.
- **Mapped keys:**
  - address and port;
  - `DOMAIN\user`;
  - screen mode, size and monitors;
  - color depth;
  - clipboard, printers, smart cards, sound and microphone;
  - certificate policy;
  - gateway;
  - start program and RemoteApp;
  - load-balance information;
  - the performance preset (`connection type`);
  - automatic reconnection.
- **Certificates:** `authentication level:i:0` ("connect and don't warn")
  **never** turns certificate checking off. It becomes "Ask the first
  time", with a note.
- **Shared folders:** `drivestoredirect` entries are checked like shared
  folders. Sensitive or missing folders, Windows drive letters and `*` are
  reported and left out.
- **Other keys:** keys with no counterpart are listed in the report.
  Harmless ones are kept in `metadata["rdp"]["extra"]["rdpfile"]` and
  written back on export.

**Export → Selected RDP Host as .rdp File…** (also in the host's context
menu) writes a UTF-16 file for mstsc and other clients. It contains the
address, user and domain, display, devices, gateway and programs, and
**never a password**. `prompt for credentials` follows "Ask for the
password every time".

### Quick Connect and the command line

- **Quick Connect:**
  - `rdp://[DOMAIN%5Cuser@]host[:port]` opens an unsaved RDP tab. The user
    may be percent-encoded, and a path is ignored.
  - `host:3389` (with or without `user@`) is also treated as RDP.
  - A password in the address is refused, as for the other schemes.
- **Command line:**

  ```bash
  python3 -m ssh_manager --rdp win-01.acme.test:3390 --user alice --domain CORP
  python3 -m ssh_manager rdp://CORP%5Calice@10.0.0.50
  ```

  - There is no password option. The password is asked for, or comes from
    the host's credential once it is saved.
  - An invalid target exits with status 2 before any window opens.

### Wake-on-LAN

RDP Connection → **Wake-on-LAN** stores a MAC address, a broadcast address
(default `255.255.255.255`) and a wait (0–600 s, default 30). The settings
are off while the MAC is empty.

**Wake and Connect** (the host's context menu, shown when a MAC is set)
sends one magic packet over UDP port 9 and then connects after the wait.

- **Allowed targets:** packets go only to a broadcast or private / local
  address.
- **Refused addresses:** a MAC of all zeros or all `FF` is refused.

## Troubleshooting

| Symptom | What to do |
|---|---|
| "FreeRDP is not installed" | Install `freerdp3-x11` (or `freerdp2-x11`); Settings → Remote Desktop → Diagnostics shows what was found and the exact package name. |
| The remote screen opens in a separate window | Embedding needs X11 or gtk-frdp. On Wayland without gtk-frdp, FreeRDP has its own window; **Focus Window** needs `xdotool` or `wmctrl`. |
| "The certificate changed" | The server presents a different certificate than the pinned one. Check with the administrator before trusting it; **Trust the New Certificate** replaces the pin. The host dialog's Security section shows how many certificates are pinned for the host. |
| Authentication fails with NLA | Check the domain (`CORP\alice` or `alice@corp.example`). If the server has NLA off, choose Security → TLS. Never turn certificate checking off to "fix" a login. |
| The password is never filled in | The FreeRDP version supports no secret channel (`/args-from:stdin` on 3.x, `/from-stdin` on 2.x); the program asks FreeRDP to prompt instead, never `/p:` on the command line. |
| Black screen or very slow | Choose a slower preset (WAN or Slow link), or lower the color depth. |
| Gateway: "access denied" | Gateway credentials differ from the server's: untick "same credentials" and choose the gateway credential. |
| SSH tunnel fails | The SSH host must allow TCP forwarding (`AllowTcpForwarding`); its password must come from a saved credential. |
| Clipboard does not work | Clipboard redirection is per host (Devices → Clipboard); FreeRDP 2.x cannot limit its direction. |
| Wake and Connect does nothing | The machine must have Wake-on-LAN on in its firmware and network card; across subnets, use the directed broadcast of the target's network (for example `10.0.0.255`). |
| A `.rdp` file is refused | The report says why: no `full address`, a file over 256 KiB, or an invalid host name. |

## Manual test checklist

To be run before a release against a Windows server (2019 or later, NLA on)
and an xrdp server (Debian). Use test accounts only.

1. **Engines:** Settings → Remote Desktop → Diagnostics lists the FreeRDP
   version and engine. Remove FreeRDP: the app starts, and opening an RDP
   host explains how to install it.
2. **Certificates:**
   - Windows, first connection: the trust dialog shows the fingerprint, and
     **Trust** pins it.
   - Second connection: no prompt.
   - Replace the certificate: the change warning appears, and the session
     does not connect without a decision.
3. **Credentials:**
   - Connect with a saved credential; check that the password is in no
     `ps` output, log, or the audit log.
   - "Ask every time" prompts.
   - A wrong password reports an authentication failure.
4. **Display:**
   - Embedded on X11: resize the window, and the remote desktop follows (or
     scales).
   - Full screen and back.
   - Send Ctrl+Alt+Del.
5. **Clipboard:** copy text both ways; with Clipboard off, nothing passes.
6. **Shared folder:** share a folder; it appears under "This PC" on Windows
   and in xrdp's `thinclient_drives`. A sensitive folder asks first.
7. **Reconnect:** disconnect the network for 20 s; the tab reconnects, and
   closing the tab kills FreeRDP (`pgrep freerdp` shows nothing).
8. **Gateway and tunnel:**
   - Through an RD Gateway, if one is available.
   - Through an SSH host with xrdp only on 127.0.0.1.
9. **Import:**
   - A MobaXterm export with type 4 sessions (`CORP\alice` and `[alice]`).
   - A `.rdp` file saved by mstsc with a saved password: it is reported, not
     imported.
   - Export, then import into mstsc.
10. **Quick Connect / CLI:**
    - `rdp://CORP%5Calice@host`;
    - `host:3389`;
    - `--rdp host --user alice --domain CORP`.
11. **Wake-on-LAN:** set the MAC, shut the machine down, then use **Wake and
    Connect**.
12. **Accessibility:** every RDP dialog field is reachable with Tab and has a
    mnemonic; Orca reads the toolbar buttons.
