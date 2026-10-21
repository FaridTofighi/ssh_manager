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

Status: **phase R0**. Hosts, settings, the probe, engine discovery,
Preferences and Diagnostics exist; opening an RDP host says that sessions
are not available yet (or, with no FreeRDP client, how to install one).

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
