# Terminals: mouse, clipboard, shortcuts and the local shell

Every terminal in SSH Manager (SSH tabs, both panes of a split, tiles and the
local shell tab) behaves like gnome-terminal. Code: `ssh_manager/terminal_input.py`
(mouse rules), `gtk_terminal.py` (`TerminalTab`), `gtkui/controllers/shortcut_registry.py`
(shortcuts), `local_shell.py` and `gtkui/local_terminal.py` (the local shell).

## Mouse

| Click | What it does |
|---|---|
| Shift + right-click | Opens the terminal menu, always, also in programs that use the mouse (vim, tmux, mc, htop) |
| Right-click | The menu (setting: paste the clipboard, paste the selection, or nothing). Goes to the program when it reads the mouse |
| Middle-click (wheel press) | Pastes the selection (PRIMARY), like every X11 terminal (setting: the clipboard, or nothing). Goes to the program when it reads the mouse |
| Shift + middle-click | The middle-click setting, also while the program reads the mouse |
| Left button, wheel | Unchanged: select text, scroll the scrollback |

Settings → Terminal → *Mouse and selection* (`settings.json`: `terminal_input`).

**Mouse reporting.** When a program turns on mouse reporting (`\e[?1000h` and
friends), unshifted clicks go to it. Right-click is handled *after* VTE's own
handler, as gnome-terminal does: VTE consumes every press it reports, so the
menu appears only when nothing in the terminal reads the mouse. VTE has no
public getter for this. A non-default middle-click setting looks at VTE's pointer
instead: VTE shows an arrow instead of its text pointer while a program reads
the mouse. With the default (paste the selection), middle-click is left to VTE
entirely.

**The menu** acts on the pane under the pointer: Copy, Copy as Plain Text, Paste,
Paste Selection, Select All · Clear Scrollback, Reset Terminal · Zoom In / Out /
Normal Size · Split Pane / Close Split, Reconnect (when disconnected; Restart in
a local tab), Save Transcript… · Preferences… (opens Settings → Terminal). Each
item shows its current shortcut.

## Selection

* Selecting text with the mouse puts it into the PRIMARY selection (VTE does
  this itself); *Select All* does too. It can be pasted at once with a
  middle-click or Shift+Insert.
* *Selecting text also copies it to the clipboard* (off by default) copies every
  non-empty selection to the clipboard too, as plain text. An empty selection
  never replaces the clipboard.
* Where there is no PRIMARY selection (some Wayland compositors), these
  actions do nothing; they never fail.

## Keyboard shortcuts

Every window action can be changed in Settings → *Keyboard Shortcuts* (or
*Customize…* in the shortcuts window, Ctrl+?). Click a row, or use its buttons:

* **Change…** — press the new shortcut (Esc cancels, Backspace removes it).
* **Add…** — an additional shortcut for the same action.
* **Clear** — no shortcut (the action stays in its menus).
* **Reset** / **Reset All** — back to the defaults (Reset All asks first).

A shortcut that another action uses is reported with the other action's name;
*Replace* moves it. A plain key, or Ctrl+letter without Shift or Alt, asks
first: programs in the terminal would no longer receive it. Return, Delete,
Alt+1…9 and Shift+F10 are handled by the focused widget and are shown as fixed.
Changes apply at once (menus, tooltips and the shortcuts window included) and
are stored in `settings.json` (`shortcuts`, only what differs from the
defaults; `[]` = disabled). An unknown action or an invalid key in the file is
ignored with a warning.

Terminal defaults:

| Action | Default |
|---|---|
| Copy | Ctrl+Shift+C, Ctrl+Insert |
| Paste | Ctrl+Shift+V |
| Paste selection | Shift+Insert |
| Open terminal context menu, Select all, Clear scrollback, Reset terminal | none (Ctrl+Shift+A is "Show all sessions") |
| New local terminal | Ctrl+Shift+T |
| Quick connect | Ctrl+Alt+T (was Ctrl+Shift+T) |
| Connect to the selected host | Ctrl+T |

The terminal actions act only on the focused terminal. In a text field (the
search box, a dialog) they do the field's own copy / paste / select all —
Shift+Insert in the search box pastes there, never into a terminal behind it —
and anywhere else nothing.

## The local shell ("New tab")

`+` after the last tab, Ctrl+Shift+T or *New Local Terminal* (empty state,
sidebar Add-host menu) opens the user's shell in a tab. The chevron next to
`+` (or a right-click on `+`) also offers Quick Connect.

* **Not a host:** no tree entry, history, credentials, password prompts,
  sshpass or remote statistics. Nothing typed into it is watched for passwords.
* **Title:** the shell's title (`user@host: ~/dir` with most shells), else
  *Local*.
* **Split, zoom, copy / paste, search, transcripts, appearance:** as in SSH tabs.
  Broadcast leaves local tabs out unless you include them.
* **Duplicate and split:** another shell in the same folder.
* **When the shell exits** (Ctrl+D, `exit`): the tab closes. With *Keep the tab
  open* it stays, with "The shell exited with status N" and **Restart** /
  **Close**.
* **Closing** a local tab, or quitting, asks only while a program other than the
  shell runs in the foreground (compared with `tcgetpgrp` on the terminal).
  Closing the tab hangs up the shell (SIGHUP), as in gnome-terminal.

Settings → Terminal → *Local terminal* (`settings.json`: `local_shell`):

| Setting | Values | Default |
|---|---|---|
| Shell (`command`) | a path or a name on PATH; empty = automatic | automatic |
| Login shell (`login_shell`) | starts it with `-l` | off |
| Start in (`start_in`) | `current` (the current local tab's folder), `home` | `current` |
| When the shell exits (`on_exit`) | `close`, `hold`, `hold_on_error` | `close` |

**Which shell:** the setting, else `$SHELL`, else the account's shell
(`/etc/passwd`), else `/bin/bash`, else `/bin/sh` — always an absolute path to
an executable file. If it cannot be started, the tab says why and offers
*Choose Another Shell…*.

**Its environment** is the app's, minus `SSHPASS` and every `SSH_MANAGER_*` /
`SSHM_*` variable and the outer `TMUX`, `TMUX_PANE`, `STY` and `WINDOW`. `TERM`
is `xterm-256color` and `COLORTERM=truecolor`, whatever terminal the app was
started from (SSH tabs too; see *TERM* below). `HOME`,
`USER`, `LANG`, `LC_*`, `DISPLAY`, `WAYLAND_DISPLAY`, `SSH_AUTH_SOCK` and
`DBUS_SESSION_BUS_ADDRESS` are kept. The shell gets exactly this list: VTE's
`SPAWN_NO_PARENT_ENVV` stops VTE from adding the app's environment back.

**AppImage:** when SSH Manager runs from its AppImage (`SSH_MANAGER_APPDIR` is
set), the bundled folders are removed from `PATH`, `LD_LIBRARY_PATH`,
`XDG_DATA_DIRS`, `GI_TYPELIB_PATH`, `GSETTINGS_SCHEMA_DIR`, `GTK_PATH`,
`GIO_MODULE_DIR`, `PYTHONHOME`, `PYTHONPATH` and the launcher's other paths, and
its own variables are dropped (`APPIMAGE`, `APPDIR`, `PYTHONNOUSERSITE`, the GTK
module caches …). So `git`, `python`, `ssh` and every other program started
from the shell are the host's, not the bundled ones.

## TERM

Every terminal child (the local shell and `ssh` in SSH tabs) gets
`TERM=xterm-256color` and `COLORTERM=truecolor`, like gnome-terminal: that is
what VTE emulates. The `TERM` of the terminal the app was started from
(`tmux-256color` inside tmux, `xterm-kitty` …) is not passed on, nor are tmux's
and screen's own variables. `ssh` sends `TERM` to the server, and a server
without that terminfo entry fails with "'tmux-256color': unknown terminal type"
in `clear`, `less`, `top` …. A profile's own `TERM` (profile environment
variables) still wins. Code: `local_shell.terminal_env`.
