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

Status: **phase R1**. RDP hosts open in a session tab; the remote desktop
runs in FreeRDP's own window (the external-window engine) and the tab is a
session card. Embedding the desktop in the tab follows in R2.

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
2. **Password:** the stored one (vault → credential → host password), or
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

### Known limits (R1)

- **Embedding:** the remote desktop runs in its own window; embedding
  arrives in R2.
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
